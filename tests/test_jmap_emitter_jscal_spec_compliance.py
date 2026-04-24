"""JMAP adapter emitter conformance to JSCalendar RFC 8984.

Regression coverage for two bugs Stalwart exposed as
`invalidProperties — Invalid property.` in the wild:

1. `recurrenceRules[].until` emitted as a bare date (`YYYY-MM-DD`)
   violates RFC 8984 §1.4.6 — `until` is a `LocalDateTime`.
2. `timeZone` emitted on all-day events (`showWithoutTime = true`)
   violates RFC 8984 §4.4.1 — `timeZone MUST NOT be set` in that case.
"""
from __future__ import annotations

from groupware_sync.models import ItemType, SyncItem
from groupware_sync_calendar.adapters.jmap_adapter import (
    _sync_item_to_jmap,
    _text_to_jscal_rrule,
)

# -- Bug A: recurrence UNTIL must be LocalDateTime ----------------------------

def test_rrule_until_date_only_is_coerced_to_local_datetime():
    """RFC 5545 allows UNTIL=YYYYMMDD; JSCalendar requires a full
    LocalDateTime. Our emitter must coerce the date form to end-of-day
    so the recurrence set still includes the source's last day."""
    rule = _text_to_jscal_rrule("FREQ=WEEKLY;BYDAY=MO;UNTIL=20260629")
    assert rule["until"] == "2026-06-29T23:59:59"


def test_rrule_until_datetime_is_preserved_without_Z_suffix():
    """UNTIL=YYYYMMDDTHHMMSSZ -> YYYY-MM-DDTHH:MM:SS (no zone suffix).
    JSCalendar LocalDateTime is zoneless; the event's timeZone governs
    interpretation."""
    rule = _text_to_jscal_rrule(
        "FREQ=WEEKLY;BYDAY=MO;UNTIL=20261221T235959Z"
    )
    assert rule["until"] == "2026-12-21T23:59:59"


def test_rrule_until_datetime_without_Z_also_preserved():
    rule = _text_to_jscal_rrule(
        "FREQ=WEEKLY;BYDAY=MO;UNTIL=20261221T235959"
    )
    assert rule["until"] == "2026-12-21T23:59:59"


# -- Bug B: all-day events must not carry timeZone ----------------------------

def _item(**fields: object) -> SyncItem:
    return SyncItem(
        provider_id="probe",
        item_type=ItemType.CALENDAR_EVENT,
        fields=dict(fields),
        fingerprint="",
    )


def test_all_day_event_omits_timezone():
    """showWithoutTime=true MUST NOT be accompanied by timeZone."""
    item = _item(
        uid="ev-1",
        summary="Semester",
        dtstart_utc="2021-09-16T00:00:00Z",
        dtstart_tz="Etc/UTC",
        all_day=True,
    )
    body = _sync_item_to_jmap(item)
    assert body.get("showWithoutTime") is True
    assert "timeZone" not in body, (
        f"timeZone must be absent for all-day events; got {body.get('timeZone')!r}"
    )
    # start is still required, in LocalDateTime form
    assert body.get("start") == "2021-09-16T00:00:00"


def test_timed_event_still_carries_timezone():
    item = _item(
        uid="ev-2",
        summary="Meeting",
        dtstart_utc="2026-05-01T10:00:00Z",
        dtstart_tz="Europe/Stockholm",
        all_day=False,
    )
    body = _sync_item_to_jmap(item)
    assert body.get("timeZone") == "Europe/Stockholm"
    assert body.get("showWithoutTime") is False


def test_all_day_event_without_explicit_flag_defaults_to_timezone_present():
    """When all_day isn't set in fields, we treat as timed (backwards
    compatible with older SyncItems that lack the all_day flag)."""
    item = _item(
        uid="ev-3",
        summary="Meeting",
        dtstart_utc="2026-05-01T10:00:00Z",
        dtstart_tz="Europe/Stockholm",
    )
    body = _sync_item_to_jmap(item)
    assert body.get("timeZone") == "Europe/Stockholm"
    assert "showWithoutTime" not in body
