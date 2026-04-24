"""CalDAV calendar adapter -- SyncProvider subclass for CalDAV servers.

Translates between the tree-based sync framework (SyncNode/SyncItem) and the
CalDAV protocol (WebDAV + iCalendar).  Uses httpx for HTTP and vobject for
iCalendar parsing/generation.
"""
from __future__ import annotations

import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape as xml_escape

import httpx
import vobject  # type: ignore[import-untyped]

from groupware_sync.models import (
    ChangeSet,
    ItemType,
    NodeType,
    SyncItem,
    SyncNode,
    compute_identity_key,
)
from groupware_sync.provider import (
    NotificationCapability,
    NotificationPolicy,
    SyncProvider,
)
from groupware_sync_calendar import tz
from groupware_sync_calendar.uid_normalize import normalize_outlook_goid

log = logging.getLogger(__name__)

DAV_NS = "DAV:"
CAL_NS = "urn:ietf:params:xml:ns:caldav"

TIMEOUT = 30.0

# CLASS -> privacy mapping
_CLASS_TO_PRIVACY: dict[str, str] = {
    "PUBLIC": "public",
    "PRIVATE": "private",
    "CONFIDENTIAL": "secret",
}
_PRIVACY_TO_CLASS: dict[str, str] = {v: k for k, v in _CLASS_TO_PRIVACY.items()}

# TRANSP -> free/busy mapping
_TRANSP_TO_FREEBUSY: dict[str, str] = {
    "TRANSPARENT": "free",
    "OPAQUE": "busy",
}
_FREEBUSY_TO_TRANSP: dict[str, str] = {v: k for k, v in _TRANSP_TO_FREEBUSY.items()}

# PARTSTAT mapping
_PARTSTAT_MAP: dict[str, str] = {
    "NEEDS-ACTION": "needs-action",
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "TENTATIVE": "tentative",
    "DELEGATED": "delegated",
}
_PARTSTAT_REVERSE: dict[str, str] = {v: k for k, v in _PARTSTAT_MAP.items()}


class CalDavCalendarAdapter(SyncProvider):
    """SyncProvider implementation backed by a CalDAV server."""

    notification_policy = NotificationPolicy(
        create_item=NotificationCapability.UNSUPPORTED,
        update_item=NotificationCapability.UNSUPPORTED,
        delete_item=NotificationCapability.UNSUPPORTED,
        delete_container=NotificationCapability.UNSUPPORTED,
    )

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        calendar_filter: Optional[str] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            auth=httpx.BasicAuth(username, password),
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        self._calendar_home: Optional[str] = None
        self._calendar_filter = calendar_filter

    # -- SyncProvider interface ------------------------------------------------

    @property
    def name(self) -> str:
        return "caldav"

    def build_tree(
        self,
        item_type: ItemType,
        known_states: Optional[dict[str, tuple[str, str]]] = None,
    ) -> SyncNode:
        """Build a container/leaf tree for all calendars and events.

        Only fetches IDs and fingerprints (ETags), not full event data.
        If known_states provides a stored sync-token for a calendar that
        matches the current server sync-token, that calendar's children are
        skipped entirely.
        """
        if known_states is None:
            known_states = {}

        calendars = self._discover()

        root = SyncNode(
            node_id="root",
            name="root",
            node_type=NodeType.CONTAINER,
        )

        for cal_href, cal_name in calendars:
            # Apply calendar filter if set
            if self._calendar_filter and cal_name != self._calendar_filter:
                continue

            stored = known_states.get(cal_href)

            if stored is not None:
                stored_cursor, stored_merkle = stored
                # Check if the sync-token is unchanged
                current_token = self._get_sync_token(cal_href)
                if current_token and current_token == stored_cursor:
                    log.debug(
                        "skipping calendar %s (sync-token unchanged: %s)",
                        cal_name,
                        current_token,
                    )
                    cal_node = SyncNode(
                        node_id=cal_href,
                        name=cal_name,
                        node_type=NodeType.CONTAINER,
                        merkle_hash=stored_merkle,
                        state_cursor=current_token,
                        skipped=True,
                    )
                    root.children.append(cal_node)
                    continue

            # Fetch children for this calendar
            cal_node = SyncNode(
                node_id=cal_href,
                name=cal_name,
                node_type=NodeType.CONTAINER,
            )

            # Store the current sync-token for next sync
            current_token = self._get_sync_token(cal_href)
            if current_token:
                cal_node.state_cursor = current_token

            # PROPFIND Depth:1 to list events with ETags
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:propfind xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
                "<d:prop>"
                "<d:getetag/>"
                "<d:resourcetype/>"
                "</d:prop>"
                "</d:propfind>"
            )
            url = self._abs_url(cal_href)
            resp = self._client.request(
                "PROPFIND",
                url,
                headers={"Depth": "1", "Content-Type": "application/xml"},
                content=body.encode(),
            )
            resp.raise_for_status()

            multistatus = ET.fromstring(resp.text)
            for response_el in multistatus.findall(f"{{{DAV_NS}}}response"):
                href = _text(response_el, f"{{{DAV_NS}}}href")
                if not href:
                    continue

                # Skip collection entries (the calendar itself)
                propstat = response_el.find(f"{{{DAV_NS}}}propstat")
                if propstat is None:
                    continue
                prop = propstat.find(f"{{{DAV_NS}}}prop")
                if prop is None:
                    continue
                rt = prop.find(f"{{{DAV_NS}}}resourcetype")
                if rt is not None and rt.find(f"{{{DAV_NS}}}collection") is not None:
                    continue

                etag = _text(prop, f"{{{DAV_NS}}}getetag")
                # Derive identity_key from the href filename per the
                # widespread CalDAV convention "<UID>.ics" (matches what
                # our own create_item path writes). Lets CalDAV↔CalDAV
                # pairs match by RFC 5545 UID like JMAP↔Graph do.
                basename = href.rsplit("/", 1)[-1]
                uid = basename[:-4] if basename.endswith(".ics") else None
                idk = (
                    compute_identity_key(
                        {"uid": normalize_outlook_goid(uid)}, ["uid"]
                    )
                    if uid else None
                )
                leaf = SyncNode(
                    node_id=href,
                    name=href,
                    node_type=NodeType.LEAF,
                    fingerprint=etag or "",
                    item_type=ItemType.CALENDAR_EVENT,
                    identity_key=idk,
                )
                cal_node.children.append(leaf)

            root.children.append(cal_node)

        root.compute_merkle()
        return root

    def get_items(self, container_id: str, ids: list[str]) -> list[SyncItem]:
        """Batch-fetch full event data for the given IDs using calendar-multiget."""
        if not ids:
            return []

        href_elements = "".join(
            f"<d:href>{xml_escape(href)}</d:href>" for href in ids
        )
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<cal:calendar-multiget xmlns:d="DAV:" '
            'xmlns:cal="urn:ietf:params:xml:ns:caldav">'
            "<d:prop>"
            "<d:getetag/>"
            "<cal:calendar-data/>"
            "</d:prop>"
            f"{href_elements}"
            "</cal:calendar-multiget>"
        )
        url = self._abs_url(container_id)
        resp = self._client.request(
            "REPORT",
            url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()

        items: list[SyncItem] = []
        multistatus = ET.fromstring(resp.text)
        for response_el in multistatus.findall(f"{{{DAV_NS}}}response"):
            href = _text(response_el, f"{{{DAV_NS}}}href")
            if not href:
                continue

            propstat = response_el.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue

            etag = _text(prop, f"{{{DAV_NS}}}getetag")
            ical_text = _text(prop, f"{{{CAL_NS}}}calendar-data")
            if not ical_text:
                continue

            item = _ical_to_sync_item(ical_text, href, etag or "")
            items.append(item)

        return items

    def get_changes(
        self, container_id: str, cursor: str
    ) -> Optional[ChangeSet]:
        """Incremental changes since cursor via WebDAV sync-collection REPORT."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:sync-collection xmlns:d="DAV:">'
            f"<d:sync-token>{xml_escape(cursor)}</d:sync-token>"
            "<d:sync-level>1</d:sync-level>"
            "<d:prop>"
            "<d:getetag/>"
            "</d:prop>"
            "</d:sync-collection>"
        )
        url = self._abs_url(container_id)
        resp = self._client.request(
            "REPORT",
            url,
            headers={"Content-Type": "application/xml"},
            content=body.encode(),
        )

        # Server rejects the sync-token (stale or unsupported)
        if resp.status_code in (403, 409, 412):
            log.info(
                "sync-token rejected (status %d), falling back to full fetch",
                resp.status_code,
            )
            return None
        resp.raise_for_status()

        multistatus = ET.fromstring(resp.text)
        created_or_updated: list[str] = []
        destroyed: list[str] = []

        for response_el in multistatus.findall(f"{{{DAV_NS}}}response"):
            href = _text(response_el, f"{{{DAV_NS}}}href")
            if not href:
                continue
            status = _text(response_el, f"{{{DAV_NS}}}status")
            propstat = response_el.find(f"{{{DAV_NS}}}propstat")

            if status and "404" in status:
                destroyed.append(href)
            elif propstat is not None:
                ps_status = _text(propstat, f"{{{DAV_NS}}}status")
                if ps_status and "404" in ps_status:
                    destroyed.append(href)
                else:
                    created_or_updated.append(href)
            else:
                created_or_updated.append(href)

        new_token_el = multistatus.find(f"{{{DAV_NS}}}sync-token")
        new_token = (
            new_token_el.text
            if new_token_el is not None and new_token_el.text
            else cursor
        )

        return ChangeSet(
            created=created_or_updated,
            updated=[],
            destroyed=destroyed,
            new_cursor=new_token,
        )

    def create_container(
        self, name: str, parent_id: Optional[str] = None
    ) -> str:
        """Create a calendar collection via MKCALENDAR."""
        self._ensure_calendar_home()
        if self._calendar_home is None:
            raise RuntimeError("calendar home discovery failed")

        col_href = f"{self._calendar_home.rstrip('/')}/{name}/"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<cal:mkcalendar xmlns:d="DAV:" '
            'xmlns:cal="urn:ietf:params:xml:ns:caldav">'
            "<d:set>"
            "<d:prop>"
            f"<d:displayname>{xml_escape(name)}</d:displayname>"
            "</d:prop>"
            "</d:set>"
            "</cal:mkcalendar>"
        )
        url = self._abs_url(col_href)
        resp = self._client.request(
            "MKCALENDAR",
            url,
            headers={"Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()
        return col_href

    def delete_container(self, container_id: str) -> None:
        """Delete a calendar collection."""
        url = self._abs_url(container_id)
        resp = self._client.request("DELETE", url)
        resp.raise_for_status()

    def create_item(
        self, container_id: str, item: SyncItem
    ) -> tuple[str, str]:
        """Create a calendar event via PUT. Returns (href, etag).

        Preserves the source-side UID when available so the resulting
        filename ('<UID>.ics') and the body's UID match across providers.
        Identity-based pairing reads UID from the filename, so generating
        a fresh UUID here would leave the new resource looking unpaired
        on the very next sync.
        """
        uid = item.fields.get("uid")
        if not uid:
            src_href = item.provider_id or ""
            basename = src_href.rstrip("/").rsplit("/", 1)[-1]
            if basename.endswith(".ics"):
                uid = basename[:-4]
        if not uid:
            uid = str(uuid.uuid4())
        ical_text = _sync_item_to_ical(item, uid=uid)
        href = f"{container_id.rstrip('/')}/{uid}.ics"
        url = self._abs_url(href)
        resp = self._client.put(
            url,
            headers={
                "Content-Type": "text/calendar",
                "If-None-Match": "*",
            },
            content=ical_text.encode("utf-8"),
        )
        resp.raise_for_status()
        etag = resp.headers.get("ETag", "")
        return href, etag

    def update_item(self, container_id: str, item: SyncItem) -> str:
        """Update an existing calendar event via PUT. Returns the new ETag."""
        # Always derive the UID from the resource filename so the iCal body
        # matches the existing resource (Radicale enforces UID consistency).
        href = item.provider_id or ""
        basename = href.rstrip("/").rsplit("/", 1)[-1]
        file_uid = basename[:-4] if basename.endswith(".ics") else None
        ical_text = _sync_item_to_ical(item, uid=file_uid)
        url = self._abs_url(item.provider_id)
        headers: dict[str, str] = {"Content-Type": "text/calendar"}
        if item.fingerprint:
            headers["If-Match"] = item.fingerprint
        resp = self._client.put(
            url,
            headers=headers,
            content=ical_text.encode("utf-8"),
        )
        resp.raise_for_status()
        return resp.headers.get("ETag", "")

    def delete_item(self, container_id: str, item_id: str) -> None:
        """Delete a calendar event via DELETE."""
        url = self._abs_url(item_id)
        resp = self._client.request("DELETE", url)
        resp.raise_for_status()

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()

    # -- CalDAV internals ------------------------------------------------------

    def _abs_url(self, path: str) -> str:
        """Resolve a path (possibly relative) to an absolute URL."""
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            base_parsed = urlparse(self._base_url)
            if parsed.hostname != base_parsed.hostname:
                raise ValueError(
                    f"URL host mismatch: {parsed.hostname} != {base_parsed.hostname}"
                )
            return path
        return urljoin(self._base_url + "/", path.lstrip("/"))

    def _ensure_calendar_home(self) -> None:
        """Discover the calendar-home-set if not yet cached."""
        if self._calendar_home is not None:
            return
        self._discover()

    def _discover(self) -> list[tuple[str, str]]:
        """Discover calendars via CalDAV well-known + PROPFIND sequence.

        Returns a list of (href, display_name) for each calendar.
        Also caches ``_calendar_home``.
        """
        # Step 1: Find principal URL via well-known redirect
        principal_url = self._discover_principal()

        # Step 2: Get calendar-home-set from principal
        cal_home = self._discover_calendar_home(principal_url)
        self._calendar_home = cal_home

        # Step 3: List calendars under the home
        return self._list_calendars(cal_home)

    def _discover_principal(self) -> str:
        """PROPFIND /.well-known/caldav to find the principal URL."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:">'
            "<d:prop>"
            "<d:current-user-principal/>"
            "</d:prop>"
            "</d:propfind>"
        )
        url = f"{self._base_url}/.well-known/caldav"
        resp = self._client.request(
            "PROPFIND",
            url,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()

        tree = ET.fromstring(resp.text)
        # Look for current-user-principal/href
        for response_el in tree.findall(f"{{{DAV_NS}}}response"):
            propstat = response_el.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue
            cup = prop.find(f"{{{DAV_NS}}}current-user-principal")
            if cup is not None:
                href = _text(cup, f"{{{DAV_NS}}}href")
                if href:
                    return href

        # Fallback: use the final URL path after redirects
        return urlparse(str(resp.url)).path

    def _discover_calendar_home(self, principal_url: str) -> str:
        """PROPFIND on principal to get calendar-home-set."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
            "<d:prop>"
            "<cal:calendar-home-set/>"
            "</d:prop>"
            "</d:propfind>"
        )
        url = self._abs_url(principal_url)
        resp = self._client.request(
            "PROPFIND",
            url,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()

        tree = ET.fromstring(resp.text)
        for response_el in tree.findall(f"{{{DAV_NS}}}response"):
            propstat = response_el.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue
            home_set = prop.find(f"{{{CAL_NS}}}calendar-home-set")
            if home_set is not None:
                href = _text(home_set, f"{{{DAV_NS}}}href")
                if href:
                    return href

        raise ValueError("CalDAV server did not provide calendar-home-set")

    def _list_calendars(self, home_url: str) -> list[tuple[str, str]]:
        """PROPFIND Depth:1 on calendar home to list calendars."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
            "<d:prop>"
            "<d:displayname/>"
            "<d:resourcetype/>"
            "<d:getetag/>"
            "</d:prop>"
            "</d:propfind>"
        )
        url = self._abs_url(home_url)
        resp = self._client.request(
            "PROPFIND",
            url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()

        calendars: list[tuple[str, str]] = []
        tree = ET.fromstring(resp.text)
        for response_el in tree.findall(f"{{{DAV_NS}}}response"):
            href = _text(response_el, f"{{{DAV_NS}}}href")
            if not href:
                continue

            propstat = response_el.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue

            # Check resourcetype contains <cal:calendar/>
            rt = prop.find(f"{{{DAV_NS}}}resourcetype")
            if rt is None:
                continue
            if rt.find(f"{{{CAL_NS}}}calendar") is None:
                continue

            display_name = _text(prop, f"{{{DAV_NS}}}displayname") or href
            calendars.append((href, display_name))

        return calendars

    def _get_sync_token(self, calendar_href: str) -> Optional[str]:
        """PROPFIND Depth:0 to get the current sync-token for a calendar."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:">'
            "<d:prop>"
            "<d:sync-token/>"
            "</d:prop>"
            "</d:propfind>"
        )
        url = self._abs_url(calendar_href)
        resp = self._client.request(
            "PROPFIND",
            url,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            content=body.encode(),
        )
        if resp.status_code >= 400:
            return None

        tree = ET.fromstring(resp.text)
        for response_el in tree.findall(f"{{{DAV_NS}}}response"):
            propstat = response_el.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue
            token = _text(prop, f"{{{DAV_NS}}}sync-token")
            if token:
                return token
        return None


# -- iCalendar <-> SyncItem translation ----------------------------------------


def _text(parent: ET.Element, tag: str) -> Optional[str]:
    """Extract text from a child element, or None if missing."""
    el = parent.find(tag)
    if el is not None and el.text:
        return el.text.strip()
    return None


def _dt_to_utc_str(dt_val: datetime | date) -> str:
    """Convert a datetime/date to a UTC ISO string."""
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is not None:
            utc_dt = dt_val.astimezone(timezone.utc)
            return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Naive datetime -- assume UTC
        return dt_val.strftime("%Y-%m-%dT%H:%M:%SZ")
    # date object (all-day)
    return dt_val.isoformat()


def _extract_tz_name(dt_prop: Any) -> str:
    """Extract IANA timezone name from a vobject datetime property."""
    # Check for TZID parameter
    params = getattr(dt_prop, "params", {})
    tzid = params.get("TZID", [])
    if tzid:
        name = tzid[0] if isinstance(tzid, list) else tzid
        return tz.windows_to_iana(name)

    # Check for tzinfo on the value
    dt_val = dt_prop.value
    if isinstance(dt_val, datetime) and dt_val.tzinfo is not None:
        tz_name = getattr(dt_val.tzinfo, "key", None)
        if tz_name:
            return tz_name
        # Check if it's UTC
        if dt_val.utcoffset() == timedelta(0):
            return "Etc/UTC"

    return ""


def _parse_attendee(att: Any) -> dict[str, str]:
    """Parse a vobject ATTENDEE property into a dict."""
    result: dict[str, str] = {}
    value = att.value
    if value and value.lower().startswith("mailto:"):
        result["email"] = value[7:]
    elif value:
        result["email"] = value

    params = getattr(att, "params", {})

    cn = params.get("CN", [])
    if cn:
        result["name"] = cn[0] if isinstance(cn, list) else cn

    role = params.get("ROLE", [])
    if role:
        val = role[0] if isinstance(role, list) else role
        result["role"] = val.lower()

    partstat = params.get("PARTSTAT", [])
    if partstat:
        val = partstat[0] if isinstance(partstat, list) else partstat
        result["status"] = _PARTSTAT_MAP.get(val.upper(), val.lower())

    rsvp = params.get("RSVP", [])
    if rsvp:
        val = rsvp[0] if isinstance(rsvp, list) else rsvp
        result["rsvp"] = val.upper() == "TRUE"  # type: ignore[assignment]

    return result


def _ical_to_sync_item(ical_text: str, href: str, etag: str) -> SyncItem:
    """Parse an iCalendar string into a SyncItem."""
    cal = vobject.readOne(ical_text)
    fields: dict[str, Any] = {}

    # Find the first VEVENT
    events = list(cal.contents.get("vevent", []))
    if not events:
        return SyncItem(
            provider_id=href,
            item_type=ItemType.CALENDAR_EVENT,
            fields=fields,
            fingerprint=etag,
        )
    event = events[0]

    # UID
    if hasattr(event, "uid"):
        fields["uid"] = event.uid.value

    # Summary
    if hasattr(event, "summary"):
        fields["summary"] = event.summary.value

    # Description
    if hasattr(event, "description"):
        fields["description"] = event.description.value

    # DTSTART
    if hasattr(event, "dtstart"):
        dt_val = event.dtstart.value
        if isinstance(dt_val, datetime):
            fields["all_day"] = False
            tz_name = _extract_tz_name(event.dtstart)
            if tz_name and tz_name != "Etc/UTC":
                fields["dtstart_tz"] = tz_name
                # Convert to UTC using tz module if naive
                if dt_val.tzinfo is None:
                    fields["dtstart_utc"] = tz.to_utc(
                        dt_val.isoformat(), tz_name
                    )
                else:
                    fields["dtstart_utc"] = _dt_to_utc_str(dt_val)
            else:
                fields["dtstart_tz"] = ""
                fields["dtstart_utc"] = _dt_to_utc_str(dt_val)
        elif isinstance(dt_val, date):
            fields["all_day"] = True
            fields["dtstart_utc"] = dt_val.isoformat()
            fields["dtstart_tz"] = ""

    # DTEND / DURATION
    if hasattr(event, "dtend"):
        dt_val = event.dtend.value
        if isinstance(dt_val, datetime):
            tz_name = _extract_tz_name(event.dtend)
            if tz_name and tz_name != "Etc/UTC":
                fields["dtend_tz"] = tz_name
                if dt_val.tzinfo is None:
                    fields["dtend_utc"] = tz.to_utc(
                        dt_val.isoformat(), tz_name
                    )
                else:
                    fields["dtend_utc"] = _dt_to_utc_str(dt_val)
            else:
                fields["dtend_tz"] = ""
                fields["dtend_utc"] = _dt_to_utc_str(dt_val)
        elif isinstance(dt_val, date):
            fields["dtend_utc"] = dt_val.isoformat()
            fields["dtend_tz"] = ""
    elif hasattr(event, "duration") and hasattr(event, "dtstart"):
        # Compute dtend from dtstart + duration
        dtstart_val = event.dtstart.value
        dur = event.duration.value
        dtend_val = dtstart_val + dur
        if isinstance(dtend_val, datetime):
            fields["dtend_utc"] = _dt_to_utc_str(dtend_val)
            fields["dtend_tz"] = fields.get("dtstart_tz", "")
        elif isinstance(dtend_val, date):
            fields["dtend_utc"] = dtend_val.isoformat()
            fields["dtend_tz"] = ""

    # Location
    if hasattr(event, "location"):
        fields["location"] = event.location.value

    # Status
    if hasattr(event, "status"):
        fields["status"] = event.status.value.lower()

    # Priority
    for prio_prop in event.contents.get("priority", []):
        try:
            fields["priority"] = int(prio_prop.value)
        except (ValueError, TypeError):
            pass

    # CLASS -> privacy
    for class_prop in event.contents.get("class", []):
        val = str(class_prop.value).upper()
        fields["privacy"] = _CLASS_TO_PRIVACY.get(val, "public")

    # TRANSP -> free_busy
    for transp_prop in event.contents.get("transp", []):
        val = str(transp_prop.value).upper()
        fields["free_busy"] = _TRANSP_TO_FREEBUSY.get(val, "busy")

    # Categories
    categories: list[str] = []
    for cat_prop in event.contents.get("categories", []):
        cat_val = cat_prop.value
        if isinstance(cat_val, list):
            categories.extend(cat_val)
        else:
            categories.append(str(cat_val))
    if categories:
        fields["categories"] = categories

    # Sequence
    for seq_prop in event.contents.get("sequence", []):
        try:
            fields["sequence"] = int(seq_prop.value)
        except (ValueError, TypeError):
            pass

    # Created
    if hasattr(event, "created"):
        fields["created"] = _dt_to_utc_str(event.created.value)

    # Last-Modified -> updated
    if hasattr(event, "last_modified"):
        fields["updated"] = _dt_to_utc_str(event.last_modified.value)

    # RRULE
    for rrule_prop in event.contents.get("rrule", []):
        # vobject stores RRULE as an object; serialize back to text
        fields["rrule"] = rrule_prop.value

    # EXDATE
    exdates: list[str] = []
    for exdate_prop in event.contents.get("exdate", []):
        ex_val = exdate_prop.value
        if isinstance(ex_val, list):
            for dt_item in ex_val:
                exdates.append(_dt_to_utc_str(dt_item))
        else:
            exdates.append(_dt_to_utc_str(ex_val))
    if exdates:
        fields["exdates"] = exdates

    # Organizer
    if hasattr(event, "organizer"):
        org = event.organizer
        org_dict: dict[str, str] = {}
        org_value = org.value
        if org_value and org_value.lower().startswith("mailto:"):
            org_dict["email"] = org_value[7:]
        elif org_value:
            org_dict["email"] = org_value
        params = getattr(org, "params", {})
        cn = params.get("CN", [])
        if cn:
            org_dict["name"] = cn[0] if isinstance(cn, list) else cn
        if org_dict:
            fields["organizer"] = org_dict

    # Attendees
    attendees: list[dict[str, str]] = []
    for att_prop in event.contents.get("attendee", []):
        att_dict = _parse_attendee(att_prop)
        if att_dict:
            attendees.append(att_dict)
    if attendees:
        fields["attendees"] = attendees

    # VALARM -> reminder_minutes
    alarms = getattr(event, "valarm_list", []) or event.contents.get(
        "valarm", []
    )
    for alarm in alarms:
        if hasattr(alarm, "trigger"):
            trigger = alarm.trigger.value
            if isinstance(trigger, timedelta):
                # Negative timedelta means before event
                total_seconds = abs(int(trigger.total_seconds()))
                fields["reminder_minutes"] = total_seconds // 60
                # Check for action
                if hasattr(alarm, "action"):
                    fields["reminder_action"] = alarm.action.value.lower()
                break

    # URL
    for url_prop in event.contents.get("url", []):
        fields["url"] = url_prop.value

    # GEO
    for geo_prop in event.contents.get("geo", []):
        geo_val = geo_prop.value
        if hasattr(geo_val, "latitude") and hasattr(geo_val, "longitude"):
            fields["geo"] = {
                "lat": float(geo_val.latitude),
                "lon": float(geo_val.longitude),
            }
        elif isinstance(geo_val, str) and ";" in geo_val:
            parts = geo_val.split(";", 1)
            try:
                fields["geo"] = {
                    "lat": float(parts[0]),
                    "lon": float(parts[1]),
                }
            except ValueError:
                pass

    # Color (X-APPLE-CALENDAR-COLOR or COLOR from RFC 7986)
    for color_prop in event.contents.get("x-apple-calendar-color", []):
        fields["color"] = color_prop.value
    if "color" not in fields:
        for color_prop in event.contents.get("color", []):
            fields["color"] = color_prop.value

    # Conference (RFC 7986)
    for conf_prop in event.contents.get("conference", []):
        fields["conference"] = conf_prop.value

    return SyncItem(
        provider_id=href,
        item_type=ItemType.CALENDAR_EVENT,
        fields=fields,
        fingerprint=etag,
    )


def _sync_item_to_ical(item: SyncItem, uid: Optional[str] = None) -> str:
    """Build an iCalendar string from a SyncItem."""
    cal = vobject.iCalendar()
    event = cal.add("vevent")
    fields = item.fields

    # UID -- reuse existing or generate
    if uid is None:
        # Try to extract UID from fields or provider_id
        uid = fields.get("uid")
        if not uid:
            href = item.provider_id or ""
            basename = href.rstrip("/").rsplit("/", 1)[-1]
            if basename.endswith(".ics"):
                uid = basename[:-4]
            else:
                uid = str(uuid.uuid4())
    event.add("uid").value = uid

    # Summary
    if fields.get("summary") is not None:
        event.add("summary").value = fields["summary"]

    # Description
    if fields.get("description") is not None:
        event.add("description").value = fields["description"]

    # DTSTART
    is_all_day = fields.get("all_day", False)
    dtstart_utc = fields.get("dtstart_utc")
    dtstart_tz = fields.get("dtstart_tz", "")

    if dtstart_utc:
        dtstart_prop = event.add("dtstart")
        if is_all_day:
            dtstart_prop.value = date.fromisoformat(dtstart_utc)
        elif dtstart_tz:
            # Convert from UTC to local time
            local_str = tz.from_utc(dtstart_utc, dtstart_tz)
            from zoneinfo import ZoneInfo

            local_dt = datetime.fromisoformat(local_str).replace(
                tzinfo=ZoneInfo(dtstart_tz)
            )
            dtstart_prop.value = local_dt
        else:
            # UTC — use vobject's utc sentinel so it serialises as DTSTART;TZID=UTC
            utc_str = dtstart_utc.replace("Z", "+00:00")
            dtstart_prop.value = datetime.fromisoformat(utc_str).replace(
                tzinfo=vobject.icalendar.utc
            )

    # DTEND
    dtend_utc = fields.get("dtend_utc")
    dtend_tz = fields.get("dtend_tz", "")

    if dtend_utc:
        dtend_prop = event.add("dtend")
        if is_all_day:
            dtend_prop.value = date.fromisoformat(dtend_utc)
        elif dtend_tz:
            local_str = tz.from_utc(dtend_utc, dtend_tz)
            from zoneinfo import ZoneInfo

            local_dt = datetime.fromisoformat(local_str).replace(
                tzinfo=ZoneInfo(dtend_tz)
            )
            dtend_prop.value = local_dt
        else:
            utc_str = dtend_utc.replace("Z", "+00:00")
            dtend_prop.value = datetime.fromisoformat(utc_str).replace(
                tzinfo=vobject.icalendar.utc
            )

    # Location
    if fields.get("location") is not None:
        event.add("location").value = fields["location"]

    # Status
    if fields.get("status") is not None:
        event.add("status").value = fields["status"].upper()

    # Priority
    if fields.get("priority") is not None:
        event.add("priority").value = str(fields["priority"])

    # CLASS (privacy)
    if fields.get("privacy") is not None:
        class_val = _PRIVACY_TO_CLASS.get(fields["privacy"], "PUBLIC")
        event.add("class").value = class_val

    # TRANSP (free_busy)
    if fields.get("free_busy") is not None:
        transp_val = _FREEBUSY_TO_TRANSP.get(fields["free_busy"], "OPAQUE")
        event.add("transp").value = transp_val

    # Categories
    if fields.get("categories"):
        cat_prop = event.add("categories")
        cat_prop.value = fields["categories"]

    # Sequence
    if fields.get("sequence") is not None:
        event.add("sequence").value = str(fields["sequence"])

    # RRULE
    if fields.get("rrule"):
        rrule_prop = event.add("rrule")
        rrule_prop.value = fields["rrule"]

    # EXDATE
    if fields.get("exdates"):
        for exdate_str in fields["exdates"]:
            exdate_prop = event.add("exdate")
            if "T" in exdate_str:
                utc_str = exdate_str.replace("Z", "+00:00")
                exdate_prop.value = [
                    datetime.fromisoformat(utc_str).replace(
                        tzinfo=timezone.utc
                    )
                ]
            else:
                exdate_prop.value = [date.fromisoformat(exdate_str)]

    # Organizer
    if fields.get("organizer"):
        org = fields["organizer"]
        org_prop = event.add("organizer")
        email = org.get("email", "")
        org_prop.value = f"mailto:{email}"
        if org.get("name"):
            org_prop.params["CN"] = [org["name"]]

    # Attendees
    for att_entry in fields.get("attendees", []):
        att_prop = event.add("attendee")
        email = att_entry.get("email", "")
        att_prop.value = f"mailto:{email}"
        if att_entry.get("name"):
            att_prop.params["CN"] = [att_entry["name"]]
        if att_entry.get("role"):
            att_prop.params["ROLE"] = [att_entry["role"].upper()]
        if att_entry.get("status"):
            partstat = _PARTSTAT_REVERSE.get(
                att_entry["status"], att_entry["status"].upper()
            )
            att_prop.params["PARTSTAT"] = [partstat]
        if att_entry.get("rsvp") is not None:
            att_prop.params["RSVP"] = [
                "TRUE" if att_entry["rsvp"] else "FALSE"
            ]

    # VALARM (reminder)
    if fields.get("reminder_minutes") is not None:
        alarm = event.add("valarm")
        alarm.add("action").value = fields.get("reminder_action", "display").upper()
        trigger = alarm.add("trigger")
        trigger.value = timedelta(minutes=-fields["reminder_minutes"])

    # URL
    if fields.get("url") is not None:
        event.add("url").value = fields["url"]

    # GEO
    if fields.get("geo"):
        geo = fields["geo"]
        geo_prop = event.add("geo")
        geo_prop.value = f"{geo['lat']};{geo['lon']}"

    # Color
    if fields.get("color") is not None:
        event.add("x-apple-calendar-color").value = fields["color"]

    # Conference
    if fields.get("conference") is not None:
        event.add("conference").value = fields["conference"]

    return cal.serialize()
