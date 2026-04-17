"""Microsoft Graph calendar adapter -- SyncProvider subclass.

Translates between the tree-based sync framework (SyncNode/SyncItem) and the
Microsoft Graph REST API v1.0 for calendar events.

Uses ``tz`` for Windows↔IANA timezone mapping and ``rrule`` for Graph
recurrence↔RRULE translation.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

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
from groupware_sync_calendar.rrule import graph_recurrence_to_rrule, rrule_to_graph_recurrence
from groupware_sync_calendar.tz import from_utc, iana_to_windows, to_utc, windows_to_iana

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_HOST = "graph.microsoft.com"
TIMEOUT = 30.0
MAX_429_RETRIES = 3

# -- Mapping tables ----------------------------------------------------------

_IMPORTANCE_TO_PRIORITY: dict[str, int] = {
    "low": 6,
    "normal": 5,
    "high": 1,
}

_SENSITIVITY_TO_PRIVACY: dict[str, str] = {
    "normal": "public",
    "private": "private",
    "personal": "private",
    "confidential": "secret",
}

_SHOW_AS_MAP: dict[str, str] = {
    "free": "free",
    "busy": "busy",
    "tentative": "tentative",
    "oof": "oof",
    "workingElsewhere": "working_elsewhere",
    "unknown": "busy",
}

_ATTENDEE_TYPE_TO_ROLE: dict[str, str] = {
    "required": "attendee",
    "optional": "optional",
    "resource": "attendee",
}

_RESPONSE_STATUS_MAP: dict[str, str] = {
    "none": "needs-action",
    "notResponded": "needs-action",
    "accepted": "accepted",
    "declined": "declined",
    "tentativelyAccepted": "tentative",
    "organizer": "accepted",
}

# Reverse maps for writing
_PRIORITY_TO_IMPORTANCE: dict[str, str] = {}  # built dynamically below
_PRIVACY_TO_SENSITIVITY: dict[str, str] = {
    "public": "normal",
    "private": "private",
    "secret": "confidential",
}

_ROLE_TO_ATTENDEE_TYPE: dict[str, str] = {
    "attendee": "required",
    "optional": "optional",
}

_PARTSTAT_TO_RESPONSE: dict[str, str] = {
    "needs-action": "notResponded",
    "accepted": "accepted",
    "declined": "declined",
    "tentative": "tentativelyAccepted",
}


def _priority_to_importance(priority: int) -> str:
    """Map iCalendar priority (0-9) to Graph importance."""
    if priority == 0:
        return "normal"
    if 1 <= priority <= 4:
        return "high"
    if priority == 5:
        return "normal"
    # 6-9
    return "low"


class GraphCalendarAdapter(SyncProvider):
    """SyncProvider implementation for Microsoft Graph calendar events."""

    notification_policy = NotificationPolicy(
        create_item=NotificationCapability.BEST_EFFORT,
        update_item=NotificationCapability.BEST_EFFORT,
        delete_item=NotificationCapability.BEST_EFFORT,
        delete_container=NotificationCapability.BEST_EFFORT,
    )

    def __init__(
        self, access_token: str, calendar_filter: Optional[str] = None
    ) -> None:
        self._client = httpx.Client(
            base_url=GRAPH_BASE,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT,
        )
        self._calendar_filter = calendar_filter

    # -- Internal helpers ------------------------------------------------------

    @staticmethod
    def _validate_url(url: str) -> bool:
        """Check that an absolute URL points to graph.microsoft.com."""
        parsed = urlparse(url)
        if parsed.hostname != GRAPH_HOST:
            log.warning("rejecting URL with unexpected host: %s", parsed.hostname)
            return False
        return True

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """HTTP request with 429 rate-limit retry and Retry-After backoff."""
        for attempt in range(MAX_429_RETRIES + 1):
            resp = self._client.request(method, url, **kwargs)
            if resp.status_code != 429 or attempt == MAX_429_RETRIES:
                return resp
            retry_after = int(resp.headers.get("Retry-After", "5"))
            log.warning(
                "graph 429 rate-limited, retrying in %ds (attempt %d/%d)",
                retry_after,
                attempt + 1,
                MAX_429_RETRIES,
            )
            time.sleep(retry_after)
        return resp  # unreachable, but keeps mypy happy

    def _write_request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """Variant of _request that adds the notification-suppression Prefer
        header. Use for POST/PATCH/DELETE on events and event-bearing
        endpoints. Read paths stay on _request to keep the header off GETs.
        """
        headers = dict(kwargs.pop("headers", None) or {})
        existing_prefer = headers.get("Prefer", "")
        suppress = "outlook.send-notifications=false"
        headers["Prefer"] = (
            f"{existing_prefer}, {suppress}" if existing_prefer else suppress
        )
        return self._request(method, url, headers=headers, **kwargs)

    # -- SyncProvider interface ------------------------------------------------

    @property
    def name(self) -> str:
        return "m365"

    def build_tree(
        self,
        item_type: ItemType,
        known_states: Optional[dict[str, tuple[str, str]]] = None,
    ) -> SyncNode:
        """Build a container/leaf tree for all calendars and events.

        Only fetches IDs and lastModifiedDateTime for fingerprinting, not full
        event data.
        """
        root = SyncNode(
            node_id="root",
            name="root",
            node_type=NodeType.CONTAINER,
        )

        # 1. Fetch the default calendar (may not appear in /me/calendars)
        calendars: list[dict[str, str]] = []
        try:
            r = self._request("GET", "/me/calendar")
            r.raise_for_status()
            default = r.json()
            calendars.append({
                "id": default["id"],
                "name": default.get("name", "Calendar"),
            })
        except Exception:
            log.warning("could not fetch default calendar, trying /me/calendars only")

        # 2. List all calendars (paginated)
        url: Optional[str] = "/me/calendars"
        while url:
            r = self._request("GET", url)
            r.raise_for_status()
            data = r.json()
            for item in data.get("value", []):
                if any(c["id"] == item["id"] for c in calendars):
                    continue
                calendars.append({
                    "id": item["id"],
                    "name": item.get("name", "Calendar"),
                })
            next_link = data.get("@odata.nextLink")
            url = next_link if next_link and self._validate_url(next_link) else None

        # 3. Apply calendar_filter if set
        if self._calendar_filter:
            calendars = [
                c for c in calendars
                if c["name"].lower() == self._calendar_filter.lower()
            ]
            if not calendars:
                log.warning("calendar filter %r matched nothing", self._calendar_filter)
            else:
                calendars[0]["name"] = "__synced__"

        # 4. For each calendar, fetch event IDs + timestamps (lightweight)
        for cal in calendars:
            cal_node = SyncNode(
                node_id=cal["id"],
                name=cal["name"],
                node_type=NodeType.CONTAINER,
            )

            events_url: Optional[str] = (
                f"/me/calendars/{cal['id']}/events"
                f"?$select=id,lastModifiedDateTime,iCalUId&$top=100"
            )
            while events_url:
                r = self._request("GET", events_url)
                r.raise_for_status()
                data = r.json()
                for event in data.get("value", []):
                    idk = compute_identity_key(
                        {"uid": event.get("iCalUId")}, ["uid"]
                    )
                    leaf = SyncNode(
                        node_id=event["id"],
                        name=event["id"],
                        node_type=NodeType.LEAF,
                        fingerprint=event.get("lastModifiedDateTime", ""),
                        item_type=ItemType.CALENDAR_EVENT,
                        identity_key=idk,
                    )
                    cal_node.children.append(leaf)
                next_link = data.get("@odata.nextLink")
                events_url = (
                    next_link if next_link and self._validate_url(next_link) else None
                )

            root.children.append(cal_node)

        root.compute_merkle()
        return root

    def get_items(self, container_id: str, ids: list[str]) -> list[SyncItem]:
        """Fetch full event data for the given IDs."""
        if not ids:
            return []

        items: list[SyncItem] = []
        for eid in ids:
            try:
                r = self._request("GET", f"/me/events/{eid}")
                if r.status_code == 404:
                    log.warning("graph event %s not found, skipping", eid)
                    continue
                r.raise_for_status()
                items.append(_graph_to_sync_item(r.json()))
            except httpx.HTTPStatusError as e:
                log.error("graph get event %s failed: %s", eid, e)
        return items

    def get_changes(
        self, container_id: str, cursor: str
    ) -> Optional[ChangeSet]:
        """Incremental changes via Graph delta link.

        The cursor IS the deltaLink from a previous sync. Returns None if
        the delta link has expired (410 Gone) or on error.
        """
        updated: list[str] = []
        destroyed: list[str] = []
        url: Optional[str] = cursor
        new_cursor = cursor

        if url and url.startswith("http") and not self._validate_url(url):
            log.warning("delta link has unexpected host, falling back to full fetch")
            return None

        try:
            while url:
                r = self._request("GET", url)
                if r.status_code == 410:
                    log.info("graph delta link expired, falling back to full fetch")
                    return None
                r.raise_for_status()
                data = r.json()
                for item in data.get("value", []):
                    eid = item["id"]
                    if item.get("@removed"):
                        destroyed.append(eid)
                    else:
                        updated.append(eid)
                next_link = data.get("@odata.nextLink")
                if next_link and self._validate_url(next_link):
                    url = next_link
                else:
                    url = None
                    delta_link = data.get("@odata.deltaLink")
                    if delta_link and self._validate_url(delta_link):
                        new_cursor = delta_link
                    else:
                        new_cursor = cursor
        except httpx.HTTPStatusError:
            log.warning("graph delta query failed, falling back to full fetch")
            return None

        return ChangeSet(
            created=[],
            updated=updated,
            destroyed=destroyed,
            new_cursor=new_cursor,
        )

    def create_container(
        self, name: str, parent_id: Optional[str] = None
    ) -> str:
        """Create a calendar, return its provider ID."""
        r = self._write_request("POST", "/me/calendars", json={"name": name})
        r.raise_for_status()
        return r.json()["id"]

    def delete_container(self, container_id: str) -> None:
        """Delete a calendar."""
        r = self._write_request("DELETE", f"/me/calendars/{container_id}")
        r.raise_for_status()

    def create_item(self, container_id: str, item: SyncItem) -> tuple[str, str]:
        """Create an event. Returns (new_id, server_fingerprint)."""
        body = _sync_item_to_graph(item)
        r = self._write_request(
            "POST", f"/me/calendars/{container_id}/events", json=body
        )
        r.raise_for_status()
        data = r.json()
        return data["id"], data.get("lastModifiedDateTime", "")

    def update_item(self, container_id: str, item: SyncItem) -> str:
        """Update an existing event. Returns server-assigned fingerprint."""
        body = _sync_item_to_graph(item)
        r = self._write_request("PATCH", f"/me/events/{item.provider_id}", json=body)
        r.raise_for_status()
        return r.json().get("lastModifiedDateTime", "")

    def delete_item(self, container_id: str, item_id: str) -> None:
        """Delete an event. Silently ignores 404 (already gone)."""
        r = self._write_request("DELETE", f"/me/events/{item_id}")
        if r.status_code == 404:
            log.info("graph event %s already deleted", item_id)
            return
        r.raise_for_status()

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()


# -- Graph JSON <-> SyncItem translation ---------------------------------------


def _graph_to_sync_item(event: dict[str, Any]) -> SyncItem:
    """Translate a Graph event JSON dict into a SyncItem."""
    fields: dict[str, Any] = {}

    # UID (iCalendar UID, not Graph's internal id)
    if event.get("iCalUId"):
        fields["uid"] = event["iCalUId"]

    # Subject / summary
    if event.get("subject") is not None:
        fields["summary"] = event["subject"]

    # Body / description
    body = event.get("body")
    if body and body.get("content"):
        fields["description"] = body["content"]

    # Start time
    start = event.get("start")
    if start and start.get("dateTime") and start.get("timeZone"):
        iana_tz = windows_to_iana(start["timeZone"])
        fields["dtstart_utc"] = to_utc(start["dateTime"], iana_tz)
        fields["dtstart_tz"] = iana_tz

    # End time
    end = event.get("end")
    if end and end.get("dateTime") and end.get("timeZone"):
        iana_tz = windows_to_iana(end["timeZone"])
        fields["dtend_utc"] = to_utc(end["dateTime"], iana_tz)
        fields["dtend_tz"] = iana_tz

    # All-day
    if event.get("isAllDay") is not None:
        fields["all_day"] = event["isAllDay"]

    # Location
    location = event.get("location")
    if location:
        if location.get("displayName"):
            fields["location"] = location["displayName"]
        coords = location.get("coordinates")
        if coords and (coords.get("latitude") is not None or coords.get("longitude") is not None):
            fields["geo"] = {
                "latitude": coords.get("latitude"),
                "longitude": coords.get("longitude"),
            }

    # Status
    if event.get("isCancelled"):
        fields["status"] = "cancelled"
    else:
        fields["status"] = "confirmed"

    # Priority / importance
    importance = event.get("importance")
    if importance and importance in _IMPORTANCE_TO_PRIORITY:
        fields["priority"] = _IMPORTANCE_TO_PRIORITY[importance]

    # Privacy / sensitivity
    sensitivity = event.get("sensitivity")
    if sensitivity and sensitivity in _SENSITIVITY_TO_PRIVACY:
        fields["privacy"] = _SENSITIVITY_TO_PRIVACY[sensitivity]

    # Free/busy status
    show_as = event.get("showAs")
    if show_as and show_as in _SHOW_AS_MAP:
        fields["free_busy"] = _SHOW_AS_MAP[show_as]

    # Categories
    categories = event.get("categories")
    if categories:
        fields["categories"] = list(categories)

    # Timestamps
    if event.get("lastModifiedDateTime"):
        fields["updated"] = event["lastModifiedDateTime"]
    if event.get("createdDateTime"):
        fields["created"] = event["createdDateTime"]

    # Recurrence
    recurrence = event.get("recurrence")
    if recurrence is not None:
        try:
            fields["rrule"] = graph_recurrence_to_rrule(recurrence)
        except (ValueError, KeyError) as e:
            log.warning("failed to parse recurrence for event %s: %s", event.get("id"), e)

    # Organizer
    organizer = event.get("organizer")
    if organizer and organizer.get("emailAddress"):
        ea = organizer["emailAddress"]
        fields["organizer"] = {
            "email": ea.get("address", ""),
            "name": ea.get("name", ""),
        }

    # Attendees
    attendees = event.get("attendees")
    if attendees:
        att_list: list[dict[str, str]] = []
        for att in attendees:
            ea = att.get("emailAddress", {})
            att_type = att.get("type", "required")
            status = att.get("status", {})
            response = status.get("response", "none")
            att_list.append({
                "email": ea.get("address", ""),
                "name": ea.get("name", ""),
                "role": _ATTENDEE_TYPE_TO_ROLE.get(att_type, "attendee"),
                "partstat": _RESPONSE_STATUS_MAP.get(response, "needs-action"),
            })
        if att_list:
            fields["attendees"] = att_list

    # Reminder
    if event.get("isReminderOn") and event.get("reminderMinutesBeforeStart") is not None:
        fields["reminder_minutes"] = event["reminderMinutesBeforeStart"]

    # Conference / online meeting
    conference = event.get("onlineMeetingUrl")
    if not conference:
        online_meeting = event.get("onlineMeeting")
        if online_meeting:
            conference = online_meeting.get("joinUrl")
    if conference:
        fields["conference"] = conference

    # Timestamps for SyncItem
    updated_at: Optional[datetime] = None
    fingerprint = event.get("lastModifiedDateTime", "")
    if fingerprint:
        try:
            updated_at = datetime.fromisoformat(
                fingerprint.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            pass

    return SyncItem(
        provider_id=event["id"],
        item_type=ItemType.CALENDAR_EVENT,
        fields=fields,
        updated_at=updated_at,
        fingerprint=fingerprint,
    )


def _sync_item_to_graph(item: SyncItem) -> dict[str, Any]:
    """Translate a SyncItem into Graph event JSON for create/update."""
    body: dict[str, Any] = {}
    fields = item.fields

    # Subject
    if fields.get("summary") is not None:
        body["subject"] = fields["summary"]

    # Body / description
    if fields.get("description") is not None:
        body["body"] = {
            "contentType": "text",
            "content": fields["description"],
        }

    # Start time
    if fields.get("dtstart_utc") and fields.get("dtstart_tz"):
        iana_tz = fields["dtstart_tz"]
        local_dt = from_utc(fields["dtstart_utc"], iana_tz)
        body["start"] = {
            "dateTime": local_dt,
            "timeZone": iana_to_windows(iana_tz),
        }

    # End time
    if fields.get("dtend_utc") and fields.get("dtend_tz"):
        iana_tz = fields["dtend_tz"]
        local_dt = from_utc(fields["dtend_utc"], iana_tz)
        body["end"] = {
            "dateTime": local_dt,
            "timeZone": iana_to_windows(iana_tz),
        }

    # All-day
    if fields.get("all_day") is not None:
        body["isAllDay"] = fields["all_day"]

    # Location
    if fields.get("location") is not None or fields.get("geo") is not None:
        loc: dict[str, Any] = {}
        if fields.get("location") is not None:
            loc["displayName"] = fields["location"]
        geo = fields.get("geo")
        if geo and (geo.get("latitude") is not None or geo.get("longitude") is not None):
            loc["coordinates"] = {
                "latitude": geo.get("latitude"),
                "longitude": geo.get("longitude"),
            }
        body["location"] = loc

    # Importance / priority
    if fields.get("priority") is not None:
        body["importance"] = _priority_to_importance(fields["priority"])

    # Sensitivity / privacy
    if fields.get("privacy") is not None:
        body["sensitivity"] = _PRIVACY_TO_SENSITIVITY.get(
            fields["privacy"], "normal"
        )

    # Free/busy → showAs (reverse map: values become keys)
    if fields.get("free_busy") is not None:
        reverse_show_as = {v: k for k, v in _SHOW_AS_MAP.items()}
        body["showAs"] = reverse_show_as.get(fields["free_busy"], "busy")

    # Categories
    if fields.get("categories") is not None:
        body["categories"] = list(fields["categories"])

    # Recurrence
    if fields.get("rrule") is not None:
        try:
            body["recurrence"] = rrule_to_graph_recurrence(fields["rrule"])
        except (ValueError, KeyError) as e:
            log.warning("failed to convert rrule to graph recurrence: %s", e)

    # Organizer
    if fields.get("organizer") is not None:
        org = fields["organizer"]
        body["organizer"] = {
            "emailAddress": {
                "address": org.get("email", ""),
                "name": org.get("name", ""),
            }
        }

    # Attendees
    if fields.get("attendees") is not None:
        att_list: list[dict[str, Any]] = []
        for att in fields["attendees"]:
            role = att.get("role", "attendee")
            partstat = att.get("partstat", "needs-action")
            att_list.append({
                "emailAddress": {
                    "address": att.get("email", ""),
                    "name": att.get("name", ""),
                },
                "type": _ROLE_TO_ATTENDEE_TYPE.get(role, "required"),
                "status": {
                    "response": _PARTSTAT_TO_RESPONSE.get(partstat, "notResponded"),
                },
            })
        body["attendees"] = att_list

    # Reminder
    if fields.get("reminder_minutes") is not None:
        body["isReminderOn"] = True
        body["reminderMinutesBeforeStart"] = fields["reminder_minutes"]

    # Conference — onlineMeeting is read-only for non-Teams meetings, skip

    return body
