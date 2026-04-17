"""Verify GraphCalendarAdapter declares its notification policy and sends
the Prefer: outlook.send-notifications=false header on every write."""
from __future__ import annotations

import httpx

from groupware_sync.models import ItemType, SyncItem
from groupware_sync.provider import NotificationCapability
from groupware_sync_calendar.adapters.graph_adapter import (
    GRAPH_BASE,
    GraphCalendarAdapter,
)

PREFER_HEADER = "outlook.send-notifications=false"


def _adapter_with_transport(transport: httpx.MockTransport) -> GraphCalendarAdapter:
    adapter = GraphCalendarAdapter("token")
    adapter._client = httpx.Client(
        base_url=GRAPH_BASE,
        headers={"Authorization": "Bearer token"},
        transport=transport,
        timeout=5.0,
    )
    return adapter


def test_policy_is_best_effort_on_all_ops():
    p = GraphCalendarAdapter.notification_policy
    assert p.create_item is NotificationCapability.BEST_EFFORT
    assert p.update_item is NotificationCapability.BEST_EFFORT
    assert p.delete_item is NotificationCapability.BEST_EFFORT
    assert p.delete_container is NotificationCapability.BEST_EFFORT


def test_create_item_sends_prefer_header():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json={"id": "new-id", "lastModifiedDateTime": "2026-04-17T00:00:00Z"})

    adapter = _adapter_with_transport(httpx.MockTransport(handler))
    item = SyncItem(
        provider_id="",
        item_type=ItemType.CALENDAR_EVENT,
        fields={"summary": "x", "uid": "u1", "dtstart_utc": "2026-06-01T10:00:00Z", "dtend_utc": "2026-06-01T11:00:00Z", "dtstart_tz": "UTC", "dtend_tz": "UTC", "all_day": False},
    )
    adapter.create_item("calid", item)
    assert len(captured) == 1
    prefer = captured[0].headers.get("Prefer", "")
    assert PREFER_HEADER in prefer


def test_update_item_sends_prefer_header():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"lastModifiedDateTime": "2026-04-17T00:00:00Z"})

    adapter = _adapter_with_transport(httpx.MockTransport(handler))
    item = SyncItem(
        provider_id="ev1",
        item_type=ItemType.CALENDAR_EVENT,
        fields={"summary": "y", "uid": "u2", "dtstart_utc": "2026-06-01T10:00:00Z", "dtend_utc": "2026-06-01T11:00:00Z", "dtstart_tz": "UTC", "dtend_tz": "UTC", "all_day": False},
    )
    adapter.update_item("calid", item)
    assert len(captured) == 1
    prefer = captured[0].headers.get("Prefer", "")
    assert PREFER_HEADER in prefer


def test_delete_item_sends_prefer_header():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    adapter = _adapter_with_transport(httpx.MockTransport(handler))
    adapter.delete_item("calid", "ev1")
    assert len(captured) == 1
    prefer = captured[0].headers.get("Prefer", "")
    assert PREFER_HEADER in prefer


def test_read_requests_do_not_send_prefer_header():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"value": []})

    adapter = _adapter_with_transport(httpx.MockTransport(handler))
    adapter._request("GET", "/me/calendars")
    assert len(captured) == 1
    prefer = captured[0].headers.get("Prefer", "")
    assert PREFER_HEADER not in prefer
