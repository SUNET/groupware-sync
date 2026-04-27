"""On a Graph 4xx during create or update, the adapter must:
  * extract Graph's error.code + error.message into the ValueError
  * log the redacted request payload at DEBUG so the offending
    property is visible without exposing free-text content

Mirrors the diagnostic logging the JMAP adapter already has."""
from __future__ import annotations

import logging

import httpx
import pytest

from groupware_sync.models import ItemType, SyncItem
from groupware_sync_calendar.adapters.graph_adapter import (
    GRAPH_BASE,
    GraphCalendarAdapter,
    _format_graph_error,
    _redact_graph_payload,
)

# -- Redaction helper --------------------------------------------------------


def test_redact_graph_payload_is_pure():
    body = {
        "subject": "Lunch with Alice",
        "body": {"contentType": "text", "content": "private notes"},
        "location": {"displayName": "Bistro Rue"},
        "locations": [{"displayName": "Café"}],
        "attendees": [
            {"emailAddress": {"address": "a@x", "name": "A"}, "type": "required"},
        ],
        "organizer": {"emailAddress": {"address": "me@x", "name": "Me"}},
        "start": {"dateTime": "2030-01-01T09:00:00", "timeZone": "UTC"},
    }
    out = _redact_graph_payload(body)
    assert out["subject"] == "<redacted>"
    assert out["body"]["content"] == "<redacted>"
    assert out["location"]["displayName"] == "<redacted>"
    assert out["locations"][0]["displayName"] == "<redacted>"
    assert out["attendees"][0]["emailAddress"]["address"] == "<redacted>"
    assert out["attendees"][0]["emailAddress"]["name"] == "<redacted>"
    assert out["organizer"]["emailAddress"]["address"] == "<redacted>"
    # Structural fields preserved.
    assert out["start"] == body["start"]
    assert out["body"]["contentType"] == "text"
    assert out["attendees"][0]["type"] == "required"
    # Input not mutated.
    assert body["subject"] == "Lunch with Alice"


def test_redact_graph_payload_handles_missing_fields():
    out = _redact_graph_payload({"start": {"dateTime": "x", "timeZone": "UTC"}})
    assert out == {"start": {"dateTime": "x", "timeZone": "UTC"}}


# -- Graph error formatter ---------------------------------------------------


def _resp(status: int, body: dict | str) -> httpx.Response:
    if isinstance(body, dict):
        return httpx.Response(status, json=body, request=httpx.Request("POST", "https://x"))
    return httpx.Response(status, text=body, request=httpx.Request("POST", "https://x"))


def test_format_graph_error_with_typed_error_body():
    r = _resp(400, {"error": {"code": "ErrorInvalidPropertyRequest", "message": "Foo bar."}})
    assert _format_graph_error("X", r) == "X: HTTP 400 ErrorInvalidPropertyRequest — Foo bar."


def test_format_graph_error_without_error_object():
    r = _resp(400, {"value": []})
    assert "HTTP 400" in _format_graph_error("X", r)


def test_format_graph_error_non_json_body():
    r = _resp(400, "Bad request, not JSON")
    assert _format_graph_error("X", r) == "X: HTTP 400 (non-JSON body)"


# -- Adapter integration: create + update raise rich ValueError --------------


def _adapter_with_response(resp: httpx.Response) -> GraphCalendarAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return resp
    a = GraphCalendarAdapter("token")
    a._client = httpx.Client(
        base_url=GRAPH_BASE,
        headers={"Authorization": "Bearer token"},
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return a


def _item() -> SyncItem:
    return SyncItem(
        provider_id="graph-id-x",
        item_type=ItemType.CALENDAR_EVENT,
        fields={
            "uid": "ev-1",
            "summary": "Lunch with Alice",
            "description": "Private notes",
            "dtstart_utc": "2030-01-01T09:00:00Z",
            "dtstart_tz": "Europe/Stockholm",
            "dtend_utc": "2030-01-01T09:30:00Z",
            "dtend_tz": "Europe/Stockholm",
        },
        fingerprint="",
    )


def test_create_item_raises_value_error_on_400():
    err_body = {
        "error": {"code": "ErrorInvalidPropertyRequest", "message": "Bad attendees."},
    }
    adapter = _adapter_with_response(_resp(400, err_body))
    with pytest.raises(ValueError) as exc_info:
        adapter.create_item("cal-1", _item())
    msg = str(exc_info.value)
    assert "HTTP 400" in msg
    assert "ErrorInvalidPropertyRequest" in msg
    assert "Bad attendees." in msg


def test_update_item_raises_value_error_on_400():
    err_body = {
        "error": {"code": "ErrorRequestBodyTooLarge", "message": "Too big."},
    }
    adapter = _adapter_with_response(_resp(400, err_body))
    with pytest.raises(ValueError) as exc_info:
        adapter.update_item("cal-1", _item())
    msg = str(exc_info.value)
    assert "graph-id-x" in msg
    assert "ErrorRequestBodyTooLarge" in msg
    assert "Too big." in msg


def test_create_item_emits_debug_log_with_redacted_payload(caplog):
    err_body = {"error": {"code": "Bad", "message": "Bad."}}
    adapter = _adapter_with_response(_resp(400, err_body))
    caplog.set_level(logging.DEBUG, logger="groupware_sync_calendar.adapters.graph_adapter")
    with pytest.raises(ValueError):
        adapter.create_item("cal-1", _item())
    debug = [r for r in caplog.records
             if r.levelno == logging.DEBUG and "request payload" in r.getMessage()]
    assert len(debug) == 1
    msg = debug[0].getMessage()
    assert "Lunch with Alice" not in msg
    assert "Private notes" not in msg
    assert "<redacted>" in msg
    # Structural keys still present so the operator can diagnose.
    assert "start" in msg or "timeZone" in msg
