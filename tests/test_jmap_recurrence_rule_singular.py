"""Stalwart's JSCalendar implementation (calcard) maps only the singular
property name `recurrenceRule` — despite RFC 8984 §4.3.3 defining the
property on Event as `recurrenceRules` (plural, array). Sending the
plural spec-compliant form gets rejected with
`invalidProperties [recurrenceRules]`.

The JMAP adapter emits the singular form on write so Stalwart accepts
it, and reads both names on parse so it survives whichever shape the
server returns. These tests lock in both directions."""
from __future__ import annotations

from groupware_sync.models import ItemType, SyncItem
from groupware_sync_calendar.adapters.jmap_adapter import (
    _jmap_to_sync_item,
    _sync_item_to_jmap,
)

# -- Write path: _sync_item_to_jmap emits singular ---------------------------

def _item_with_rrule(rrule: str) -> SyncItem:
    return SyncItem(
        provider_id="probe",
        item_type=ItemType.CALENDAR_EVENT,
        fields={
            "uid": "ev-1",
            "summary": "weekly standup",
            "dtstart_utc": "2030-01-01T09:00:00Z",
            "dtstart_tz": "Etc/UTC",
            "dtend_utc": "2030-01-01T09:30:00Z",
            "rrule": rrule,
        },
        fingerprint="",
    )


def test_emit_recurrence_uses_singular_key():
    body = _sync_item_to_jmap(_item_with_rrule("FREQ=WEEKLY;BYDAY=MO"))
    # The singular name is what Stalwart accepts.
    assert "recurrenceRule" in body
    # The spec plural name must not be present (Stalwart rejects it).
    assert "recurrenceRules" not in body


def test_emit_recurrence_is_scalar_not_array():
    """Stalwart's probe accepted both object and array, but the default
    shape — and what the @type marker implies — is a single RecurrenceRule
    object. Use the scalar form for minimal surprise."""
    body = _sync_item_to_jmap(_item_with_rrule("FREQ=WEEKLY;BYDAY=MO"))
    rule = body["recurrenceRule"]
    assert isinstance(rule, dict)
    assert rule.get("@type") == "RecurrenceRule"
    assert rule.get("frequency") == "weekly"


def test_emit_omits_recurrence_when_no_rrule_field():
    item = SyncItem(
        provider_id="probe",
        item_type=ItemType.CALENDAR_EVENT,
        fields={
            "uid": "ev-2",
            "summary": "one-shot",
            "dtstart_utc": "2030-01-01T09:00:00Z",
            "dtstart_tz": "Etc/UTC",
            "dtend_utc": "2030-01-01T09:30:00Z",
        },
        fingerprint="",
    )
    body = _sync_item_to_jmap(item)
    assert "recurrenceRule" not in body
    assert "recurrenceRules" not in body


# -- Read path: _jmap_to_sync_item accepts both names ------------------------

def _event_payload(recurrence_key: str, value) -> dict:
    return {
        "id": "srv-1",
        "uid": "uid-1",
        "title": "weekly",
        "start": "2030-01-01T09:00:00",
        "timeZone": "Etc/UTC",
        "duration": "PT30M",
        recurrence_key: value,
    }


def test_parse_recurrence_from_singular_object():
    rule = {"@type": "RecurrenceRule", "frequency": "weekly"}
    item = _jmap_to_sync_item(_event_payload("recurrenceRule", rule))
    assert item.fields.get("rrule") == "FREQ=WEEKLY"


def test_parse_recurrence_from_singular_array():
    """Stalwart's probe accepted an array under the singular key too;
    if a server responds in that shape, parse succeeds."""
    rules = [{"@type": "RecurrenceRule", "frequency": "daily"}]
    item = _jmap_to_sync_item(_event_payload("recurrenceRule", rules))
    assert item.fields.get("rrule") == "FREQ=DAILY"


def test_parse_recurrence_from_plural_array():
    """Forward-compat with a spec-correct JMAP server that returns the
    RFC-8984 plural name after Stalwart fixes the bug (or against any
    other server)."""
    rules = [{"@type": "RecurrenceRule", "frequency": "monthly"}]
    item = _jmap_to_sync_item(_event_payload("recurrenceRules", rules))
    assert item.fields.get("rrule") == "FREQ=MONTHLY"


def test_parse_no_rrule_when_neither_key_present():
    item = _jmap_to_sync_item({
        "id": "srv-1",
        "uid": "uid-1",
        "title": "one-shot",
        "start": "2030-01-01T09:00:00",
        "timeZone": "Etc/UTC",
    })
    assert "rrule" not in item.fields
