"""Verify JmapCalendarAdapter declares its notification policy and sends
sendSchedulingMessages: false on every CalendarEvent/set call."""
from __future__ import annotations

from unittest.mock import patch

from groupware_sync.models import ItemType, SyncItem
from groupware_sync.provider import NotificationCapability
from groupware_sync_calendar.adapters.jmap_adapter import JmapCalendarAdapter


def _make_adapter() -> JmapCalendarAdapter:
    # URL and token are irrelevant — we stub _call.
    adapter = JmapCalendarAdapter("https://example.invalid/jmap", "token")
    adapter._api_url = "https://example.invalid/jmap/api"
    adapter._account_id = "u0"
    return adapter


def test_policy_is_best_effort_on_all_ops():
    p = JmapCalendarAdapter.notification_policy
    assert p.create_item is NotificationCapability.BEST_EFFORT
    assert p.update_item is NotificationCapability.BEST_EFFORT
    assert p.delete_item is NotificationCapability.BEST_EFFORT
    assert p.delete_container is NotificationCapability.BEST_EFFORT


def _capture_call(calls: list[list]) -> callable:
    """Replace _call so tests can inspect request envelopes."""
    def fake_call(self, methods):  # noqa: ANN001
        calls.append(methods)
        out = []
        for m in methods:
            name = m[0]
            args = m[1]
            cid = m[2]
            if "create" in args:
                out.append([name, {"created": {k: {"id": f"srv-{k}", "updated": "2026"} for k in args["create"]}}, cid])
            elif "update" in args:
                out.append([name, {"updated": {k: None for k in args["update"]}}, cid])
            elif "destroy" in args:
                out.append([name, {"destroyed": list(args["destroy"])}, cid])
        return out
    return fake_call


def test_create_item_sends_send_scheduling_messages_false():
    adapter = _make_adapter()
    calls: list[list] = []
    item = SyncItem(provider_id="", item_type=ItemType.CALENDAR_EVENT, fields={"summary": "x", "uid": "u1"})
    with patch.object(JmapCalendarAdapter, "_call", _capture_call(calls)):
        adapter.create_item("cal1", item)
    assert len(calls) == 1
    method = calls[0][0]
    assert method[0] == "CalendarEvent/set"
    assert method[1].get("sendSchedulingMessages") is False


def test_update_item_sends_send_scheduling_messages_false():
    adapter = _make_adapter()
    calls: list[list] = []
    item = SyncItem(provider_id="ev1", item_type=ItemType.CALENDAR_EVENT, fields={"summary": "y", "uid": "u2"})
    with patch.object(JmapCalendarAdapter, "_call", _capture_call(calls)):
        with patch.object(JmapCalendarAdapter, "_get_item_fingerprint", return_value="fp"):
            adapter.update_item("cal1", item)
    set_call = calls[0][0]
    assert set_call[0] == "CalendarEvent/set"
    assert set_call[1].get("sendSchedulingMessages") is False


def test_delete_item_sends_send_scheduling_messages_false():
    adapter = _make_adapter()
    calls: list[list] = []
    with patch.object(JmapCalendarAdapter, "_call", _capture_call(calls)):
        adapter.delete_item("cal1", "ev1")
    assert len(calls) == 1
    method = calls[0][0]
    assert method[0] == "CalendarEvent/set"
    assert method[1].get("sendSchedulingMessages") is False
