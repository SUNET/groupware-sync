"""Cleanup of malformed Stalwart Alert objects.

Steady-state sync converged without rewriting events whose Alert wrapper
was missing ``@type="Alert"`` or whose VALARM-import trigger was empty,
so the repair command has to walk every event and patch them in place.
"""
from __future__ import annotations

from unittest.mock import patch

from groupware_sync_calendar.adapters.jmap_adapter import JmapCalendarAdapter


def _make_adapter() -> JmapCalendarAdapter:
    adapter = JmapCalendarAdapter("https://example.invalid/jmap", "token")
    adapter._api_url = "https://example.invalid/jmap/api"
    adapter._account_id = "u0"
    adapter._max_objects_in_get = 100
    return adapter


def _scripted_call(events: list[dict], updates_log: list[dict]):
    """Stub _call: serve query, get, set in that order from a fixed event list."""
    state = {"served_query": False}

    def fake_call(self, methods):  # noqa: ANN001
        out = []
        for m in methods:
            name, args, cid = m[0], m[1], m[2]
            if name == "CalendarEvent/query":
                if state["served_query"]:
                    out.append([name, {"ids": []}, cid])
                else:
                    state["served_query"] = True
                    out.append([name, {"ids": [e["id"] for e in events]}, cid])
            elif name == "CalendarEvent/get":
                wanted = set(args.get("ids", []))
                out.append([name, {"list": [e for e in events if e["id"] in wanted]}, cid])
            elif name == "CalendarEvent/set":
                upd = args.get("update") or {}
                updates_log.append(upd)
                out.append([name, {"updated": {eid: None for eid in upd}}, cid])
        return out
    return fake_call


def test_repair_rewrites_alert_missing_type_preserving_offset():
    """Alert wrapper without @type='Alert' but with a usable OffsetTrigger
    must be rewritten as a clean Alert with the same offset."""
    events = [
        {
            "id": "ev1",
            "alerts": {
                "a0": {
                    # Missing @type="Alert" — the bug we fixed in the emitter.
                    "trigger": {"@type": "OffsetTrigger", "offset": "-PT15M", "relativeTo": "start"},
                    "action": "display",
                },
            },
        },
    ]
    updates_log: list[dict] = []
    adapter = _make_adapter()
    with patch.object(JmapCalendarAdapter, "_call", _scripted_call(events, updates_log)):
        with patch.object(JmapCalendarAdapter, "_ensure_session", lambda self: None):
            counts = adapter.repair_malformed_alerts()
    assert counts == {"scanned": 1, "malformed": 1, "repaired": 1, "cleared": 0, "errors": 0}
    assert len(updates_log) == 1
    patch_alerts = updates_log[0]["ev1"]["alerts"]
    assert isinstance(patch_alerts, dict)
    alert = next(iter(patch_alerts.values()))
    assert alert["@type"] == "Alert"
    assert alert["trigger"]["@type"] == "OffsetTrigger"
    assert alert["trigger"]["offset"] == "-PT15M"


def test_repair_clears_alert_with_no_trigger():
    """Imported VALARM that landed without a trigger has no usable offset
    to preserve — clear the field so the UI stops crashing."""
    events = [
        {
            "id": "ev2",
            "alerts": {
                "a0": {"action": "display"},  # no trigger at all
            },
        },
    ]
    updates_log: list[dict] = []
    adapter = _make_adapter()
    with patch.object(JmapCalendarAdapter, "_call", _scripted_call(events, updates_log)):
        with patch.object(JmapCalendarAdapter, "_ensure_session", lambda self: None):
            counts = adapter.repair_malformed_alerts()
    assert counts["malformed"] == 1
    assert counts["cleared"] == 1
    assert counts["repaired"] == 0
    assert updates_log[0]["ev2"]["alerts"] is None


def test_repair_skips_well_formed_alerts():
    """Already-correct Alert objects must not be touched."""
    events = [
        {
            "id": "ev3",
            "alerts": {
                "a0": {
                    "@type": "Alert",
                    "trigger": {"@type": "OffsetTrigger", "offset": "-PT10M", "relativeTo": "start"},
                    "action": "display",
                },
            },
        },
        {"id": "ev4", "alerts": None},
        {"id": "ev5"},  # field absent
    ]
    updates_log: list[dict] = []
    adapter = _make_adapter()
    with patch.object(JmapCalendarAdapter, "_call", _scripted_call(events, updates_log)):
        with patch.object(JmapCalendarAdapter, "_ensure_session", lambda self: None):
            counts = adapter.repair_malformed_alerts()
    assert counts == {"scanned": 3, "malformed": 0, "repaired": 0, "cleared": 0, "errors": 0}
    assert updates_log == []


def test_repair_dry_run_makes_no_writes():
    events = [
        {
            "id": "ev6",
            "alerts": {
                "a0": {
                    "trigger": {"@type": "OffsetTrigger", "offset": "-PT5M"},
                    "action": "display",
                },
            },
        },
    ]
    updates_log: list[dict] = []
    adapter = _make_adapter()
    with patch.object(JmapCalendarAdapter, "_call", _scripted_call(events, updates_log)):
        with patch.object(JmapCalendarAdapter, "_ensure_session", lambda self: None):
            counts = adapter.repair_malformed_alerts(dry_run=True)
    assert counts == {"scanned": 1, "malformed": 1, "repaired": 1, "cleared": 0, "errors": 0}
    assert updates_log == []
