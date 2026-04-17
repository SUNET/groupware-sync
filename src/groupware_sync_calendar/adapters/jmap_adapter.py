"""JMAP calendar adapter — SyncProvider subclass for Stalwart JMAP.

Translates between the tree-based sync framework (SyncNode/SyncItem) and the
JMAP protocol (RFC 8620) with JSCalendar (RFC 8984).

Mirrors the JMAP protocol patterns (session discovery, ``_call()``,
``_ensure_session()``, 429 retry, ``follow_redirects=True``) from the existing
contacts JMAP adapter.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

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
from groupware_sync_calendar import tz

log = logging.getLogger(__name__)

TIMEOUT = 30.0
USING = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:calendars"]

# JSCalendar status -> normalised lowercase
_STATUS_MAP: dict[str, str] = {
    "confirmed": "confirmed",
    "tentative": "tentative",
    "cancelled": "cancelled",
}

# JSCalendar privacy -> normalised
_PRIVACY_MAP: dict[str, str] = {
    "public": "public",
    "private": "private",
    "secret": "secret",
}
_PRIVACY_REVERSE: dict[str, str] = {v: k for k, v in _PRIVACY_MAP.items()}

# JSCalendar freeBusyStatus -> normalised
_FREEBUSY_MAP: dict[str, str] = {
    "busy": "busy",
    "free": "free",
}
_FREEBUSY_REVERSE: dict[str, str] = {v: k for k, v in _FREEBUSY_MAP.items()}

# JSCalendar day abbreviations -> RRULE day abbreviations
_JSCAL_DAY_TO_RRULE: dict[str, str] = {
    "mo": "MO",
    "tu": "TU",
    "we": "WE",
    "th": "TH",
    "fr": "FR",
    "sa": "SA",
    "su": "SU",
}
_RRULE_DAY_TO_JSCAL: dict[str, str] = {v: k for k, v in _JSCAL_DAY_TO_RRULE.items()}

# RRULE FREQ values that are valid
_VALID_FREQ = {"YEARLY", "MONTHLY", "WEEKLY", "DAILY", "HOURLY", "MINUTELY", "SECONDLY"}


class JmapCalendarAdapter(SyncProvider):
    """SyncProvider implementation backed by a Stalwart JMAP server."""

    notification_policy = NotificationPolicy(
        create_item=NotificationCapability.BEST_EFFORT,
        update_item=NotificationCapability.BEST_EFFORT,
        delete_item=NotificationCapability.BEST_EFFORT,
        delete_container=NotificationCapability.BEST_EFFORT,
    )

    def __init__(
        self,
        jmap_url: str,
        access_token: str,
        calendar_filter: Optional[str] = None,
    ) -> None:
        self._base_url = jmap_url.rstrip("/")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        self._api_url: Optional[str] = None
        self._account_id: Optional[str] = None
        self._calendar_filter = calendar_filter  # filter by name if set

    # -- SyncProvider interface ------------------------------------------------

    @property
    def name(self) -> str:
        return "stalwart"

    def build_tree(
        self,
        item_type: ItemType,
        known_states: Optional[dict[str, tuple[str, str]]] = None,
    ) -> SyncNode:
        """Build a container/leaf tree for all calendars and events.

        Only fetches IDs and fingerprints (the ``updated`` timestamp), not full
        event data. If known_states provides a stored JMAP state for a
        calendar that matches the current server state, that calendar's
        children are skipped entirely.
        """
        self._ensure_session()
        if known_states is None:
            known_states = {}

        root = SyncNode(
            node_id="root",
            name="root",
            node_type=NodeType.CONTAINER,
        )

        # 1. List calendars
        results = self._call([
            ["Calendar/get", {"accountId": self._account_id}, "cal0"],
        ])
        calendars: list[dict[str, str]] = []
        for result in results:
            if result[0] == "Calendar/get":
                for item in result[1].get("list", []):
                    calendars.append({
                        "id": item["id"],
                        "name": item.get("name", "Default"),
                    })

        # Filter to specified calendar if configured
        if self._calendar_filter:
            calendars = [
                cal for cal in calendars
                if cal["name"].lower() == self._calendar_filter.lower()
            ]
            if not calendars:
                log.warning("calendar filter %r matched nothing", self._calendar_filter)
            else:
                # Normalize name so both sides match when paired
                calendars[0]["name"] = "__synced__"

        # 2. For each calendar, check if we can skip it
        for cal in calendars:
            cal_id = cal["id"]
            stored = known_states.get(cal_id)

            if stored is not None:
                stored_cursor, stored_merkle = stored
                # Ask JMAP if state changed since stored_cursor
                current_state = self._get_events_state()
                if current_state and current_state == stored_cursor:
                    log.debug(
                        "skipping calendar %s (state unchanged: %s)",
                        cal["name"],
                        current_state,
                    )
                    cal_node = SyncNode(
                        node_id=cal_id,
                        name=cal["name"],
                        node_type=NodeType.CONTAINER,
                        merkle_hash=stored_merkle,
                        state_cursor=current_state,
                        skipped=True,
                    )
                    root.children.append(cal_node)
                    continue

            # State changed or no stored state — fetch children
            cal_node = SyncNode(
                node_id=cal_id,
                name=cal["name"],
                node_type=NodeType.CONTAINER,
            )

            # Get current JMAP state for this type (to store for next sync)
            current_state = self._get_events_state()
            if current_state:
                cal_node.state_cursor = current_state

            # Query event IDs in this calendar
            query_results = self._call([
                [
                    "CalendarEvent/query",
                    {
                        "accountId": self._account_id,
                        "filter": {"inCalendars": [cal_id]},
                    },
                    "q0",
                ],
            ])
            event_ids: list[str] = []
            for result in query_results:
                if result[0] == "CalendarEvent/query":
                    event_ids = result[1].get("ids", [])

            if event_ids:
                # Fetch only id + updated for fingerprinting
                get_results = self._call([
                    [
                        "CalendarEvent/get",
                        {
                            "accountId": self._account_id,
                            "ids": event_ids,
                            "properties": ["id", "updated", "uid"],
                        },
                        "g0",
                    ],
                ])
                for result in get_results:
                    if result[0] == "CalendarEvent/get":
                        for event in result[1].get("list", []):
                            idk = compute_identity_key(
                                {"uid": event.get("uid")}, ["uid"]
                            )
                            leaf = SyncNode(
                                node_id=event["id"],
                                name=event["id"],
                                node_type=NodeType.LEAF,
                                fingerprint=event.get("updated", ""),
                                item_type=ItemType.CALENDAR_EVENT,
                                identity_key=idk,
                            )
                            cal_node.children.append(leaf)

            root.children.append(cal_node)

        root.compute_merkle()
        return root

    def _get_events_state(self) -> Optional[str]:
        """Get the current JMAP state for CalendarEvent."""
        results = self._call([
            [
                "CalendarEvent/get",
                {"accountId": self._account_id, "ids": []},
                "s0",
            ],
        ])
        for result in results:
            if result[0] == "CalendarEvent/get":
                return result[1].get("state")
        return None

    def get_items(self, container_id: str, ids: list[str]) -> list[SyncItem]:
        """Fetch full event data for the given IDs."""
        self._ensure_session()
        if not ids:
            return []

        results = self._call([
            [
                "CalendarEvent/get",
                {"accountId": self._account_id, "ids": ids},
                "g0",
            ],
        ])
        items: list[SyncItem] = []
        for result in results:
            if result[0] == "CalendarEvent/get":
                for event in result[1].get("list", []):
                    items.append(_jmap_to_sync_item(event))
        return items

    def get_changes(
        self, container_id: str, cursor: str
    ) -> Optional[ChangeSet]:
        """Incremental changes since *cursor* via CalendarEvent/changes."""
        self._ensure_session()
        try:
            results = self._call([
                [
                    "CalendarEvent/changes",
                    {
                        "accountId": self._account_id,
                        "sinceState": cursor,
                    },
                    "ch0",
                ],
            ])
        except httpx.HTTPStatusError:
            log.warning("JMAP changes request failed, falling back to full fetch")
            return None

        for result in results:
            if result[0] == "CalendarEvent/changes":
                data = result[1]
                if data.get("type") == "cannotCalculateChanges":
                    log.info("JMAP cannotCalculateChanges, falling back to full fetch")
                    return None
                return ChangeSet(
                    created=data.get("created", []),
                    updated=data.get("updated", []),
                    destroyed=data.get("destroyed", []),
                    new_cursor=data.get("newState", cursor),
                )
            if result[0] == "error":
                err_type = result[1].get("type", "")
                if err_type == "cannotCalculateChanges":
                    log.info("JMAP cannotCalculateChanges, falling back to full fetch")
                    return None
                log.warning("JMAP changes error: %s", result[1])
                return None
        return None

    def create_container(
        self, name: str, parent_id: Optional[str] = None
    ) -> str:
        """Create a calendar, return its provider ID."""
        self._ensure_session()
        results = self._call([
            [
                "Calendar/set",
                {
                    "accountId": self._account_id,
                    "create": {"new1": {"name": name}},
                },
                "cal1",
            ],
        ])
        for result in results:
            if result[0] == "Calendar/set":
                created = result[1].get("created", {})
                item = created.get("new1", {})
                return item["id"]
        raise ValueError(f"failed to create calendar {name!r}")

    def delete_container(self, container_id: str) -> None:
        """Delete a calendar."""
        self._ensure_session()
        results = self._call([
            [
                "Calendar/set",
                {
                    "accountId": self._account_id,
                    "destroy": [container_id],
                },
                "cald0",
            ],
        ])
        for result in results:
            if result[0] == "Calendar/set":
                not_destroyed = result[1].get("notDestroyed", {})
                if container_id in not_destroyed:
                    log.warning(
                        "JMAP delete calendar %s failed: %s",
                        container_id,
                        not_destroyed[container_id],
                    )

    def create_item(self, container_id: str, item: SyncItem) -> tuple[str, str]:
        """Create a calendar event. Returns (new_id, server_fingerprint)."""
        self._ensure_session()
        event = _sync_item_to_jmap(item)
        event["calendarIds"] = {container_id: True}
        results = self._call([
            [
                "CalendarEvent/set",
                {
                    "accountId": self._account_id,
                    "sendSchedulingMessages": False,
                    "create": {"new1": event},
                },
                "c0",
            ],
        ])
        for result in results:
            if result[0] == "CalendarEvent/set":
                not_created = result[1].get("notCreated", {})
                if "new1" in not_created:
                    err = not_created["new1"]
                    raise ValueError(
                        f"JMAP create calendar event failed: "
                        f"{err.get('type', 'unknown')} — {err.get('description', '')}"
                    )
                created = result[1].get("created", {})
                new_item = created.get("new1", {})
                new_id = new_item["id"]
                fingerprint = new_item.get("updated", "")
                return new_id, fingerprint
        raise ValueError("JMAP create calendar event failed: no CalendarEvent/set in response")

    def update_item(self, container_id: str, item: SyncItem) -> str:
        """Update an existing calendar event. Returns server-assigned fingerprint."""
        self._ensure_session()
        event = _sync_item_to_jmap(item)
        results = self._call([
            [
                "CalendarEvent/set",
                {
                    "accountId": self._account_id,
                    "sendSchedulingMessages": False,
                    "update": {item.provider_id: event},
                },
                "u0",
            ],
        ])
        for result in results:
            if result[0] == "CalendarEvent/set":
                not_updated = result[1].get("notUpdated", {})
                if item.provider_id in not_updated:
                    log.error(
                        "JMAP update event %s failed: %s",
                        item.provider_id,
                        not_updated[item.provider_id],
                    )
        # Fetch the new fingerprint
        fp = self._get_item_fingerprint(item.provider_id)
        return fp

    def _get_item_fingerprint(self, item_id: str) -> str:
        """Fetch just the updated timestamp for a single item."""
        results = self._call([
            [
                "CalendarEvent/get",
                {
                    "accountId": self._account_id,
                    "ids": [item_id],
                    "properties": ["id", "updated"],
                },
                "fp0",
            ],
        ])
        for result in results:
            if result[0] == "CalendarEvent/get":
                for event in result[1].get("list", []):
                    if event["id"] == item_id:
                        return event.get("updated", "")
        return ""

    def delete_item(self, container_id: str, item_id: str) -> None:
        """Delete a calendar event."""
        self._ensure_session()
        results = self._call([
            [
                "CalendarEvent/set",
                {
                    "accountId": self._account_id,
                    "sendSchedulingMessages": False,
                    "destroy": [item_id],
                },
                "d0",
            ],
        ])
        for result in results:
            if result[0] == "CalendarEvent/set":
                not_destroyed = result[1].get("notDestroyed", {})
                if item_id in not_destroyed:
                    log.warning(
                        "JMAP delete event %s failed: %s",
                        item_id,
                        not_destroyed[item_id],
                    )

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()

    # -- JMAP internals --------------------------------------------------------

    def _ensure_session(self) -> None:
        """Discover JMAP session (apiUrl + accountId) if not cached."""
        if self._api_url is not None:
            return
        r = self._client.get(f"{self._base_url}/.well-known/jmap")
        r.raise_for_status()
        session = r.json()
        self._api_url = session["apiUrl"]

        # Find the account with calendars capability
        for acct_id, acct in session.get("accounts", {}).items():
            caps = acct.get("accountCapabilities", {})
            if "urn:ietf:params:jmap:calendars" in caps:
                self._account_id = acct_id
                break

        if self._account_id is None:
            # Fall back to primaryAccounts
            primary = session.get("primaryAccounts", {})
            self._account_id = primary.get(
                "urn:ietf:params:jmap:calendars",
                next(iter(session.get("accounts", {})), None),
            )

        if self._account_id is None:
            raise ValueError("JMAP session has no account with calendars capability")

        log.debug(
            "JMAP session: apiUrl=%s accountId=%s",
            self._api_url,
            self._account_id,
        )

    def _call(self, method_calls: list[list[Any]]) -> list[list[Any]]:
        """POST a JMAP request and return methodResponses.

        Retries on 429 Too Many Requests with exponential backoff.
        """
        import time as _time

        self._ensure_session()
        body = {
            "using": USING,
            "methodCalls": method_calls,
        }
        for attempt in range(8):
            r = self._client.post(self._api_url, json=body)  # type: ignore[arg-type]
            if r.status_code == 429:
                default_wait = min(5 * (2 ** attempt), 120)
                retry_after = int(r.headers.get("retry-after", default_wait))
                log.warning(
                    "JMAP 429 — retrying in %ds (attempt %d/8)",
                    retry_after,
                    attempt + 1,
                )
                _time.sleep(retry_after)
                continue
            r.raise_for_status()
            return r.json()["methodResponses"]
        r.raise_for_status()  # raise on final attempt
        return []  # unreachable


# -- ISO 8601 Duration helpers ------------------------------------------------


_DURATION_RE = re.compile(
    r"^(?P<sign>-?)P"
    r"(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?"
    r")?$"
)


def _parse_iso_duration(dur: str) -> timedelta:
    """Parse an ISO 8601 duration string into a timedelta.

    Handles: P1D, PT1H, PT30M, PT1H30M, P1W, -PT15M, etc.
    """
    m = _DURATION_RE.match(dur)
    if not m:
        raise ValueError(f"cannot parse ISO 8601 duration: {dur!r}")
    sign = -1 if m.group("sign") == "-" else 1
    weeks = int(m.group("weeks") or 0)
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    return sign * timedelta(
        weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds
    )


def _format_iso_duration(td: timedelta) -> str:
    """Format a timedelta as an ISO 8601 duration string."""
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        prefix = "-"
        total_seconds = -total_seconds
    else:
        prefix = ""

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = [f"{prefix}P"]
    if days:
        parts.append(f"{days}D")
    time_parts: list[str] = []
    if hours:
        time_parts.append(f"{hours}H")
    if minutes:
        time_parts.append(f"{minutes}M")
    if seconds:
        time_parts.append(f"{seconds}S")
    if time_parts:
        parts.append("T")
        parts.extend(time_parts)

    result = "".join(parts)
    # Edge case: zero duration
    if result == "P" or result == "-P":
        return "PT0S"
    return result


# -- Recurrence rule translation ------------------------------------------------


def _jscal_rrule_to_text(rule: dict[str, Any]) -> str:
    """Translate a JSCalendar recurrenceRule object to RRULE text.

    Example input: {"frequency": "weekly", "interval": 2, "byDay": [{"day": "mo"}]}
    Example output: "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"
    """
    parts: list[str] = []

    freq = rule.get("frequency", "").upper()
    if freq in _VALID_FREQ:
        parts.append(f"FREQ={freq}")
    else:
        return ""

    interval = rule.get("interval")
    if interval is not None and interval != 1:
        parts.append(f"INTERVAL={interval}")

    # BYDAY — each entry has "day" and optional "nthOfPeriod"
    by_day = rule.get("byDay")
    if by_day:
        day_strs: list[str] = []
        for entry in by_day:
            day_abbr = _JSCAL_DAY_TO_RRULE.get(entry.get("day", "").lower(), "")
            if not day_abbr:
                continue
            nth = entry.get("nthOfPeriod")
            if nth is not None:
                day_strs.append(f"{nth}{day_abbr}")
            else:
                day_strs.append(day_abbr)
        if day_strs:
            parts.append(f"BYDAY={','.join(day_strs)}")

    by_month = rule.get("byMonth")
    if by_month:
        parts.append(f"BYMONTH={','.join(str(m) for m in by_month)}")

    by_month_day = rule.get("byMonthDay")
    if by_month_day:
        parts.append(f"BYMONTHDAY={','.join(str(d) for d in by_month_day)}")

    count = rule.get("count")
    if count is not None:
        parts.append(f"COUNT={count}")

    until = rule.get("until")
    if until is not None:
        # JSCalendar until is a date or datetime string
        # Remove any separators for RRULE format
        until_str = until.replace("-", "").replace(":", "").replace("T", "T")
        parts.append(f"UNTIL={until_str}")

    return ";".join(parts)


def _text_to_jscal_rrule(text: str) -> dict[str, Any]:
    """Translate RRULE text to a JSCalendar recurrenceRule object.

    Example input: "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE"
    Example output: {"@type": "RecurrenceRule", "frequency": "weekly", "interval": 2,
                     "byDay": [{"@type": "NDay", "day": "mo"}, {"@type": "NDay", "day": "we"}]}
    """
    rule: dict[str, Any] = {"@type": "RecurrenceRule"}
    # Strip RRULE: prefix if present
    if text.upper().startswith("RRULE:"):
        text = text[6:]

    for part in text.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.upper().strip()

        if key == "FREQ":
            rule["frequency"] = value.lower()
        elif key == "INTERVAL":
            rule["interval"] = int(value)
        elif key == "COUNT":
            rule["count"] = int(value)
        elif key == "UNTIL":
            # Reformat to ISO 8601 if needed
            v = value.strip()
            if len(v) == 8:
                # YYYYMMDD -> YYYY-MM-DD
                rule["until"] = f"{v[:4]}-{v[4:6]}-{v[6:8]}"
            elif "T" in v and len(v) >= 15:
                # YYYYMMDDTHHMMSS -> YYYY-MM-DDTHH:MM:SS
                d_part = v[:8]
                t_part = v[9:15] if len(v) >= 15 else v[9:]
                formatted = f"{d_part[:4]}-{d_part[4:6]}-{d_part[6:8]}T{t_part[:2]}:{t_part[2:4]}:{t_part[4:6]}"
                if v.endswith("Z"):
                    formatted += "Z"
                rule["until"] = formatted
            else:
                rule["until"] = v
        elif key == "BYDAY":
            days: list[dict[str, Any]] = []
            for day_str in value.split(","):
                day_str = day_str.strip()
                # May have numeric prefix like "2MO" or "-1FR"
                m = re.match(r"^(-?\d+)?([A-Z]{2})$", day_str.upper())
                if m:
                    day_entry: dict[str, Any] = {"@type": "NDay"}
                    day_lower = _RRULE_DAY_TO_JSCAL.get(m.group(2), "")
                    if day_lower:
                        day_entry["day"] = day_lower
                        if m.group(1) is not None:
                            day_entry["nthOfPeriod"] = int(m.group(1))
                        days.append(day_entry)
            if days:
                rule["byDay"] = days
        elif key == "BYMONTH":
            rule["byMonth"] = [m.strip() for m in value.split(",")]
        elif key == "BYMONTHDAY":
            rule["byMonthDay"] = [int(d.strip()) for d in value.split(",")]

    return rule


# -- JSCalendar <-> SyncItem translation ----------------------------------------


def _jmap_to_sync_item(event: dict[str, Any]) -> SyncItem:
    """Translate a JSCalendar event dict into a SyncItem."""
    fields: dict[str, Any] = {}

    # UID
    if event.get("uid"):
        fields["uid"] = event["uid"]

    # Summary / title
    if event.get("title"):
        fields["summary"] = event["title"]

    # Description
    if event.get("description"):
        fields["description"] = event["description"]

    # Start time + timezone
    start_str = event.get("start")
    tz_name = event.get("timeZone")

    if start_str and tz_name:
        fields["dtstart_utc"] = tz.to_utc(start_str, tz_name)
        fields["dtstart_tz"] = tz_name

        # Duration -> dtend
        duration_str = event.get("duration")
        if duration_str:
            try:
                delta = _parse_iso_duration(duration_str)
                start_dt = datetime.fromisoformat(start_str)
                end_dt = start_dt + delta
                fields["dtend_utc"] = tz.to_utc(end_dt.isoformat(), tz_name)
                fields["dtend_tz"] = tz_name
            except (ValueError, TypeError):
                log.warning("failed to parse duration %r for event %s", duration_str, event.get("id"))
    elif start_str:
        # No timezone — treat as UTC
        fields["dtstart_utc"] = start_str if start_str.endswith("Z") else start_str + "Z"
        if event.get("duration"):
            try:
                delta = _parse_iso_duration(event["duration"])
                start_dt = datetime.fromisoformat(start_str)
                end_dt = start_dt + delta
                end_iso = end_dt.isoformat()
                fields["dtend_utc"] = end_iso if end_iso.endswith("Z") else end_iso + "Z"
            except (ValueError, TypeError):
                pass

    # All-day
    if event.get("showWithoutTime") is not None:
        fields["all_day"] = bool(event["showWithoutTime"])

    # Location — first entry
    locations = event.get("locations")
    if locations and isinstance(locations, dict):
        for _key, loc in locations.items():
            if loc.get("name"):
                fields["location"] = loc["name"]
            if loc.get("coordinates"):
                fields["geo"] = loc["coordinates"]
            break  # take first

    # Status
    status = event.get("status")
    if status:
        normalised = _STATUS_MAP.get(status.lower())
        if normalised:
            fields["status"] = normalised

    # Priority
    if event.get("priority") is not None:
        fields["priority"] = int(event["priority"])

    # Privacy
    privacy = event.get("privacy")
    if privacy:
        normalised = _PRIVACY_MAP.get(privacy.lower())
        if normalised:
            fields["privacy"] = normalised

    # Free/busy
    freebusy = event.get("freeBusyStatus")
    if freebusy:
        normalised = _FREEBUSY_MAP.get(freebusy.lower())
        if normalised:
            fields["free_busy"] = normalised

    # Categories (keywords)
    keywords = event.get("keywords")
    if keywords and isinstance(keywords, dict):
        fields["categories"] = sorted(keywords.keys())

    # Sequence
    if event.get("sequence") is not None:
        fields["sequence"] = int(event["sequence"])

    # Created
    if event.get("created"):
        fields["created"] = event["created"]

    # Updated
    if event.get("updated"):
        fields["updated"] = event["updated"]

    # Recurrence rules
    rrules = event.get("recurrenceRules")
    if rrules and isinstance(rrules, list) and len(rrules) > 0:
        rrule_text = _jscal_rrule_to_text(rrules[0])
        if rrule_text:
            fields["rrule"] = rrule_text

    # Excluded recurrence rules — warn and skip
    if event.get("excludedRecurrenceRules"):
        log.warning(
            "event %s has excludedRecurrenceRules — not translatable, skipping",
            event.get("id"),
        )

    # Recurrence overrides -> exdates (exclusions)
    overrides = event.get("recurrenceOverrides")
    if overrides and isinstance(overrides, dict):
        exdates: list[str] = []
        for dt_key, override_val in overrides.items():
            # An exclusion is an empty dict or has "excluded": true
            if not override_val or (isinstance(override_val, dict) and override_val.get("excluded")):
                exdates.append(dt_key)
        if exdates:
            fields["exdates"] = sorted(exdates)

    # Participants — split into organizer and attendees
    participants = event.get("participants")
    if participants and isinstance(participants, dict):
        attendees: list[dict[str, Any]] = []
        for _pid, p in participants.items():
            roles = p.get("roles", {})
            send_to = p.get("sendTo", {})
            email = send_to.get("imip", "")
            if email.lower().startswith("mailto:"):
                email = email[7:]

            entry: dict[str, Any] = {}
            if email:
                entry["email"] = email
            if p.get("name"):
                entry["name"] = p["name"]
            if p.get("participationStatus"):
                entry["status"] = p["participationStatus"]
            if p.get("expectReply") is not None:
                entry["rsvp"] = bool(p["expectReply"])

            if roles.get("owner"):
                entry["role"] = "organizer"
                fields["organizer"] = entry
            else:
                # Determine role
                if roles.get("attendee"):
                    entry["role"] = "attendee"
                elif roles.get("chair"):
                    entry["role"] = "chair"
                elif roles.get("optional"):
                    entry["role"] = "optional"
                else:
                    entry["role"] = "attendee"
                attendees.append(entry)

        if attendees:
            fields["attendees"] = attendees

    # Alerts -> reminder_minutes (first offset trigger)
    alerts = event.get("alerts")
    if alerts and isinstance(alerts, dict):
        for _aid, alert in alerts.items():
            trigger = alert.get("trigger", {})
            if trigger.get("@type") == "OffsetTrigger" or trigger.get("type") == "offset":
                offset = trigger.get("offset", "")
                if offset:
                    try:
                        td = _parse_iso_duration(offset)
                        fields["reminder_minutes"] = abs(int(td.total_seconds())) // 60
                    except ValueError:
                        log.warning("cannot parse alert offset %r", offset)
                break  # take first

    # Links -> url (first with no specific rel)
    links = event.get("links")
    if links and isinstance(links, dict):
        for _lid, link in links.items():
            href = link.get("href", "")
            if href:
                fields["url"] = href
                break  # take first

    # Color
    if event.get("color"):
        fields["color"] = event["color"]

    # Virtual locations -> conference
    virtual_locs = event.get("virtualLocations")
    if virtual_locs and isinstance(virtual_locs, dict):
        for _vlid, vloc in virtual_locs.items():
            uri = vloc.get("uri", "")
            if uri:
                fields["conference"] = uri
                break  # take first

    # Updated timestamp for SyncItem
    updated_at: Optional[datetime] = None
    if ts := event.get("updated"):
        try:
            updated_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    return SyncItem(
        provider_id=event.get("id", ""),
        item_type=ItemType.CALENDAR_EVENT,
        fields=fields,
        updated_at=updated_at,
        fingerprint=event.get("updated", ""),
    )


def _sync_item_to_jmap(item: SyncItem) -> dict[str, Any]:
    """Translate a SyncItem into a JSCalendar event dict for JMAP."""
    event: dict[str, Any] = {"@type": "Event"}
    fields = item.fields

    # UID
    if fields.get("uid"):
        event["uid"] = fields["uid"]

    # Title / summary
    if fields.get("summary"):
        event["title"] = fields["summary"]

    # Description
    if fields.get("description"):
        event["description"] = fields["description"]

    # Start + duration
    dtstart_utc = fields.get("dtstart_utc")
    dtstart_tz = fields.get("dtstart_tz", "Etc/UTC")
    if dtstart_utc:
        local_start = tz.from_utc(dtstart_utc, dtstart_tz)
        event["start"] = local_start
        event["timeZone"] = dtstart_tz

        # Compute duration from dtend
        dtend_utc = fields.get("dtend_utc")
        if dtend_utc:
            try:
                start_dt = datetime.fromisoformat(
                    dtstart_utc.replace("Z", "+00:00")
                )
                end_dt = datetime.fromisoformat(
                    dtend_utc.replace("Z", "+00:00")
                )
                delta = end_dt - start_dt
                event["duration"] = _format_iso_duration(delta)
            except (ValueError, TypeError):
                log.warning("failed to compute duration from dtstart/dtend")

    # All-day
    if fields.get("all_day") is not None:
        event["showWithoutTime"] = bool(fields["all_day"])

    # Location
    if fields.get("location") or fields.get("geo"):
        loc: dict[str, Any] = {"@type": "Location"}
        if fields.get("location"):
            loc["name"] = fields["location"]
        if fields.get("geo"):
            loc["coordinates"] = fields["geo"]
        event["locations"] = {"loc0": loc}

    # Status
    if fields.get("status"):
        event["status"] = fields["status"]

    # Priority
    if fields.get("priority") is not None:
        event["priority"] = int(fields["priority"])

    # Privacy
    if fields.get("privacy"):
        jscal_privacy = _PRIVACY_REVERSE.get(fields["privacy"])
        if jscal_privacy:
            event["privacy"] = jscal_privacy

    # Free/busy
    if fields.get("free_busy"):
        jscal_fb = _FREEBUSY_REVERSE.get(fields["free_busy"])
        if jscal_fb:
            event["freeBusyStatus"] = jscal_fb

    # Categories -> keywords
    categories = fields.get("categories")
    if categories and isinstance(categories, list):
        event["keywords"] = {cat: True for cat in categories}

    # Sequence
    if fields.get("sequence") is not None:
        event["sequence"] = int(fields["sequence"])

    # Recurrence rules
    rrule_text = fields.get("rrule")
    if rrule_text:
        rule = _text_to_jscal_rrule(rrule_text)
        if rule.get("frequency"):
            event["recurrenceRules"] = [rule]

    # Exdates -> recurrenceOverrides (exclusions)
    exdates = fields.get("exdates")
    if exdates and isinstance(exdates, list):
        overrides: dict[str, dict[str, Any]] = {}
        for exdate in exdates:
            overrides[exdate] = {"excluded": True}
        event["recurrenceOverrides"] = overrides

    # Participants — organizer + attendees
    organizer = fields.get("organizer")
    attendees = fields.get("attendees")
    if organizer or attendees:
        participants: dict[str, dict[str, Any]] = {}
        idx = 0

        if organizer:
            p: dict[str, Any] = {"@type": "Participant"}
            p["roles"] = {"owner": True, "attendee": True}
            if organizer.get("email"):
                p["sendTo"] = {"imip": f"mailto:{organizer['email']}"}
            if organizer.get("name"):
                p["name"] = organizer["name"]
            if organizer.get("status"):
                p["participationStatus"] = organizer["status"]
            if organizer.get("rsvp") is not None:
                p["expectReply"] = bool(organizer["rsvp"])
            participants[f"p{idx}"] = p
            idx += 1

        if attendees:
            for att in attendees:
                p = {"@type": "Participant"}
                role = att.get("role", "attendee")
                if role == "chair":
                    p["roles"] = {"chair": True, "attendee": True}
                elif role == "optional":
                    p["roles"] = {"optional": True}
                else:
                    p["roles"] = {"attendee": True}
                if att.get("email"):
                    p["sendTo"] = {"imip": f"mailto:{att['email']}"}
                if att.get("name"):
                    p["name"] = att["name"]
                if att.get("status"):
                    p["participationStatus"] = att["status"]
                if att.get("rsvp") is not None:
                    p["expectReply"] = bool(att["rsvp"])
                participants[f"p{idx}"] = p
                idx += 1

        if participants:
            event["participants"] = participants

    # Alerts (reminder)
    reminder = fields.get("reminder_minutes")
    if reminder is not None:
        offset = _format_iso_duration(timedelta(minutes=-int(reminder)))
        event["alerts"] = {
            "a0": {
                "trigger": {
                    "@type": "OffsetTrigger",
                    "offset": offset,
                    "relativeTo": "start",
                },
                "action": "display",
            },
        }

    # URL -> links
    if fields.get("url"):
        event["links"] = {
            "l0": {"@type": "Link", "href": fields["url"]},
        }

    # Color
    if fields.get("color"):
        event["color"] = fields["color"]

    # Conference -> virtual locations
    if fields.get("conference"):
        event["virtualLocations"] = {
            "vl0": {
                "@type": "VirtualLocation",
                "uri": fields["conference"],
            },
        }

    return event
