"""The JMAP /set error formatter must include the `properties` list from
the server response. Stalwart uses that list to name the offending field
on `invalidProperties` rejections; without surfacing it, operators have
to probe the API to figure out which field was rejected (as this project
did, repeatedly, for `recurrenceRules`)."""
from __future__ import annotations

import httpx
import pytest

from groupware_sync.models import ItemType, SyncItem
from groupware_sync_calendar.adapters.jmap_adapter import (
    JmapCalendarAdapter,
    _format_set_error,
)

# -- Helper unit tests --------------------------------------------------------

def test_format_set_error_without_properties():
    msg = _format_set_error("X", {"type": "invalidProperties", "description": "Invalid property."})
    assert msg == "X: invalidProperties — Invalid property."


def test_format_set_error_with_properties():
    msg = _format_set_error("X", {
        "type": "invalidProperties",
        "description": "Invalid property.",
        "properties": ["recurrenceRules"],
    })
    assert msg == "X: invalidProperties — Invalid property. (properties: ['recurrenceRules'])"


def test_format_set_error_multi_property():
    msg = _format_set_error("X", {
        "type": "invalidProperties",
        "description": "Invalid property.",
        "properties": ["recurrenceRules", "alerts"],
    })
    assert "['recurrenceRules', 'alerts']" in msg


def test_format_set_error_falsy_properties_is_ignored():
    msg = _format_set_error("X", {"type": "invalidProperties", "description": "Invalid property.", "properties": []})
    assert "properties" not in msg


def test_format_set_error_missing_type_and_description():
    msg = _format_set_error("X", {})
    assert msg == "X: unknown — "


# -- Integration: create_item surfaces properties in the ValueError ----------

JMAP_SESSION_PAYLOAD = {
    "apiUrl": "https://jmap.example.com/api",
    "accounts": {
        "acc1": {"accountCapabilities": {"urn:ietf:params:jmap:calendars": {}}},
    },
    "primaryAccounts": {"urn:ietf:params:jmap:calendars": "acc1"},
    "capabilities": {"urn:ietf:params:jmap:core": {"maxObjectsInGet": 100}},
}


def _adapter_with_create_failure(properties: list[str] | None) -> JmapCalendarAdapter:
    err: dict = {"type": "invalidProperties", "description": "Invalid property."}
    if properties is not None:
        err["properties"] = properties
    responses = [
        httpx.Response(200, json=JMAP_SESSION_PAYLOAD),
        httpx.Response(200, json={
            "methodResponses": [[
                "CalendarEvent/set",
                {"notCreated": {"new1": err}},
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


def _item() -> SyncItem:
    return SyncItem(
        provider_id="probe",
        item_type=ItemType.CALENDAR_EVENT,
        fields={
            "uid": "ev-1",
            "summary": "X",
            "dtstart_utc": "2030-01-01T09:00:00Z",
            "dtstart_tz": "Etc/UTC",
            "dtend_utc": "2030-01-01T09:30:00Z",
        },
        fingerprint="",
    )


def test_create_item_error_message_includes_properties_list():
    adapter = _adapter_with_create_failure(["recurrenceRules"])
    with pytest.raises(ValueError) as exc_info:
        adapter.create_item("cal-1", _item())
    assert "properties: ['recurrenceRules']" in str(exc_info.value)


def test_create_item_error_message_without_properties():
    adapter = _adapter_with_create_failure(None)
    with pytest.raises(ValueError) as exc_info:
        adapter.create_item("cal-1", _item())
    msg = str(exc_info.value)
    assert "invalidProperties" in msg
    assert "properties" not in msg  # no "(properties: [...])" segment
