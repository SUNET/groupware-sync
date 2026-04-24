"""Tests for JMAP invalid-property diagnostic logging (issue #5).

Covers two concerns:
  1. The ``_redact_event_payload`` helper rewrites free-text fields while
     preserving structural keys.
  2. ``create_item`` / ``update_item`` emit a DEBUG log record with the
     redacted payload when Stalwart returns ``invalidProperties``.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any

import httpx
import pytest

from groupware_sync.models import ItemType, SyncItem
from groupware_sync_calendar.adapters.jmap_adapter import (
    JmapCalendarAdapter,
    _redact_event_payload,
)


# -- Redaction helper unit tests ------------------------------------------------

def _sample_event() -> dict[str, Any]:
    return {
        "@type": "Event",
        "uid": "ev-1",
        "title": "Lunch with Alice",
        "description": "Private strategy notes",
        "start": "2026-05-01T12:00:00",
        "timeZone": "Europe/Stockholm",
        "duration": "PT1H",
        "status": "confirmed",
        "privacy": "private",
        "freeBusyStatus": "busy",
        "showWithoutTime": False,
        "keywords": {"lunch": True},
        "priority": 5,
        "recurrenceRules": [{"@type": "RecurrenceRule", "frequency": "weekly"}],
        "participants": {
            "p0": {
                "@type": "Participant",
                "roles": {"owner": True, "attendee": True},
                "name": "Alice Smith",
                "sendTo": {"imip": "mailto:alice@example.com"},
                "participationStatus": "accepted",
            },
            "p1": {
                "@type": "Participant",
                "roles": {"attendee": True},
                "name": "Bob Jones",
                "sendTo": {"imip": "mailto:bob@example.com"},
            },
        },
        "locations": {
            "loc0": {"@type": "Location", "name": "Bistro Rue"},
        },
    }


def test_redacts_title_and_description():
    out = _redact_event_payload(_sample_event())
    assert out["title"] == "<redacted>"
    assert out["description"] == "<redacted>"


def test_redacts_participant_name_and_email():
    out = _redact_event_payload(_sample_event())
    assert out["participants"]["p0"]["name"] == "<redacted>"
    assert out["participants"]["p0"]["sendTo"]["imip"] == "mailto:<redacted>"
    assert out["participants"]["p1"]["name"] == "<redacted>"
    assert out["participants"]["p1"]["sendTo"]["imip"] == "mailto:<redacted>"


def test_redacts_location_name():
    out = _redact_event_payload(_sample_event())
    assert out["locations"]["loc0"]["name"] == "<redacted>"


def test_preserves_structural_and_enum_keys():
    ev = _sample_event()
    out = _redact_event_payload(ev)
    # Every structural/enum key must round-trip unchanged.
    for key in (
        "@type", "uid", "start", "timeZone", "duration",
        "status", "privacy", "freeBusyStatus", "showWithoutTime",
        "keywords", "priority", "recurrenceRules",
    ):
        assert out[key] == ev[key], f"key {key!r} was modified"
    # Participant roles / participationStatus must survive.
    assert out["participants"]["p0"]["roles"] == ev["participants"]["p0"]["roles"]
    assert (
        out["participants"]["p0"]["participationStatus"]
        == ev["participants"]["p0"]["participationStatus"]
    )


def test_does_not_mutate_input():
    ev = _sample_event()
    snapshot = copy.deepcopy(ev)
    _redact_event_payload(ev)
    assert ev == snapshot


def test_handles_missing_optional_sections():
    ev = {"@type": "Event", "uid": "ev-2", "start": "2026-05-01T12:00:00"}
    out = _redact_event_payload(ev)
    # Should return without raising; no title/participants/locations to touch.
    assert out["uid"] == "ev-2"
    assert "title" not in out
    assert "participants" not in out
    assert "locations" not in out


def test_redaction_output_is_json_serialisable():
    """The whole point is to log this via json.dumps — confirm it never
    contains non-serialisable values."""
    out = _redact_event_payload(_sample_event())
    json.dumps(out)  # must not raise


# -- Logging integration tests --------------------------------------------------

JMAP_SESSION_PAYLOAD = {
    "apiUrl": "https://jmap.example.com/api",
    "accounts": {
        "acc1": {
            "accountCapabilities": {"urn:ietf:params:jmap:calendars": {}},
        },
    },
    "primaryAccounts": {"urn:ietf:params:jmap:calendars": "acc1"},
    "capabilities": {"urn:ietf:params:jmap:core": {"maxObjectsInGet": 100}},
}


def _adapter_with_create_failure() -> JmapCalendarAdapter:
    """Build an adapter whose _call() returns a notCreated invalidProperties."""
    responses = [
        # session discovery
        httpx.Response(200, json=JMAP_SESSION_PAYLOAD),
        # CalendarEvent/set (create) returns notCreated.new1
        httpx.Response(200, json={
            "methodResponses": [[
                "CalendarEvent/set",
                {
                    "notCreated": {
                        "new1": {
                            "type": "invalidProperties",
                            "description": "Invalid property.",
                        },
                    },
                },
                "c0",
            ]],
        }),
    ]
    idx = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        resp = responses[idx["n"]]
        idx["n"] += 1
        return resp

    a = JmapCalendarAdapter("https://jmap.example.com", "tok")
    a._client = httpx.Client(
        headers={"Authorization": "Bearer tok"},
        transport=httpx.MockTransport(handler),
        timeout=5.0,
        follow_redirects=True,
    )
    return a


def _adapter_with_update_failure() -> JmapCalendarAdapter:
    responses = [
        httpx.Response(200, json=JMAP_SESSION_PAYLOAD),
        httpx.Response(200, json={
            "methodResponses": [[
                "CalendarEvent/set",
                {
                    "notUpdated": {
                        "srv-id-1": {
                            "type": "invalidProperties",
                            "description": "Invalid property.",
                        },
                    },
                },
                "u0",
            ]],
        }),
        # _get_item_fingerprint does one more /get call.
        httpx.Response(200, json={
            "methodResponses": [[
                "CalendarEvent/get",
                {"list": [{"id": "srv-id-1", "updated": "2026-04-23T00:00:00Z"}]},
                "fp0",
            ]],
        }),
    ]
    idx = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        resp = responses[idx["n"]]
        idx["n"] += 1
        return resp

    a = JmapCalendarAdapter("https://jmap.example.com", "tok")
    a._client = httpx.Client(
        headers={"Authorization": "Bearer tok"},
        transport=httpx.MockTransport(handler),
        timeout=5.0,
        follow_redirects=True,
    )
    return a


def _sync_item() -> SyncItem:
    return SyncItem(
        provider_id="srv-id-1",
        item_type=ItemType.CALENDAR_EVENT,
        fields={
            "uid": "ev-1",
            "summary": "Lunch with Alice",
            "description": "Private strategy notes",
            "dtstart_utc": "2026-05-01T10:00:00Z",
            "dtstart_tz": "Europe/Stockholm",
            "dtend_utc": "2026-05-01T11:00:00Z",
        },
        updated_at=None,
        fingerprint="",
    )


def test_create_item_emits_debug_log_with_redacted_payload(caplog):
    adapter = _adapter_with_create_failure()
    caplog.set_level(logging.DEBUG, logger="groupware_sync_calendar.adapters.jmap_adapter")
    with pytest.raises(ValueError, match="invalidProperties"):
        adapter.create_item("cal-1", _sync_item())

    debug_records = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "request payload" in r.getMessage()
    ]
    assert len(debug_records) == 1, "expected exactly one DEBUG payload log"
    msg = debug_records[0].getMessage()
    # Free text must be redacted.
    assert "Lunch with Alice" not in msg
    assert "Private strategy notes" not in msg
    assert "<redacted>" in msg
    # Structural keys must still be present — they're what the operator
    # needs in order to diagnose the invalid property.
    assert "timeZone" in msg or "duration" in msg


def test_update_item_emits_debug_log_with_redacted_payload(caplog):
    adapter = _adapter_with_update_failure()
    caplog.set_level(logging.DEBUG, logger="groupware_sync_calendar.adapters.jmap_adapter")
    # update_item does not raise on notUpdated — it logs + continues.
    adapter.update_item("cal-1", _sync_item())

    debug_records = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "request payload" in r.getMessage()
    ]
    assert len(debug_records) == 1
    msg = debug_records[0].getMessage()
    assert "Lunch with Alice" not in msg
    assert "<redacted>" in msg
