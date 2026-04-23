"""JMAP calendar adapter: identity_key on leaves from uid."""
from __future__ import annotations

from unittest.mock import patch

from groupware_sync.models import ItemType, NodeType, compute_identity_key
from groupware_sync_calendar.adapters.jmap_adapter import JmapCalendarAdapter


def _make_adapter() -> JmapCalendarAdapter:
    a = JmapCalendarAdapter("https://example.invalid/jmap", "token")
    a._api_url = "https://example.invalid/jmap/api"
    a._account_id = "u0"
    return a


def test_build_tree_requests_uid_property():
    """The CalendarEvent/get call in build_tree must include 'uid' in properties."""
    captured: list = []

    def fake_call(self, methods):  # noqa: ANN001
        captured.append(methods)
        out = []
        for m in methods:
            name = m[0]
            cid = m[2]
            if name == "Calendar/get":
                out.append([name, {"list": [{"id": "cal1", "name": "Kalender"}]}, cid])
            elif name == "CalendarEvent/query":
                out.append([name, {"ids": ["e1", "e2"]}, cid])
            elif name == "CalendarEvent/get":
                out.append([name, {"list": [
                    {"id": "e1", "updated": "2026-04-17T00:00:00Z",
                     "uid": "uid-one", "calendarIds": {"cal1": True}},
                    {"id": "e2", "updated": "2026-04-17T00:00:00Z",
                     "uid": "uid-two", "calendarIds": {"cal1": True}},
                ]}, cid])
        return out

    adapter = _make_adapter()
    with patch.object(JmapCalendarAdapter, "_call", fake_call):
        with patch.object(JmapCalendarAdapter, "_get_events_state", return_value=None):
            root = adapter.build_tree(ItemType.CALENDAR_EVENT)

    # Assert: the CalendarEvent/get invocation requested 'uid' + 'calendarIds'
    get_calls = [m for call in captured for m in call if m[0] == "CalendarEvent/get"]
    assert get_calls, "no CalendarEvent/get calls made"
    props = get_calls[0][1].get("properties", [])
    assert "uid" in props, f"expected 'uid' in properties, got {props}"
    assert "calendarIds" in props, f"expected 'calendarIds' in properties, got {props}"

    # Assert: leaves carry identity_key derived from uid
    cal = root.children[0]
    assert cal.node_type == NodeType.CONTAINER
    leaf_by_id = {c.node_id: c for c in cal.children}
    assert leaf_by_id["e1"].identity_key == compute_identity_key(
        {"uid": "uid-one"}, ["uid"]
    )
    assert leaf_by_id["e2"].identity_key == compute_identity_key(
        {"uid": "uid-two"}, ["uid"]
    )


def test_events_bucketed_by_calendar_ids_no_inCalendars_filter():
    """Stalwart rejects inCalendars filters. The adapter must query once
    account-wide and bucket events into the right calendar container
    using the event's calendarIds map.
    """
    captured: list = []

    def fake_call(self, methods):  # noqa: ANN001
        captured.append(methods)
        out = []
        for m in methods:
            name = m[0]
            cid = m[2]
            if name == "Calendar/get":
                out.append([name, {"list": [
                    {"id": "cal_a", "name": "A"},
                    {"id": "cal_b", "name": "B"},
                ]}, cid])
            elif name == "CalendarEvent/query":
                out.append([name, {"ids": ["e1", "e2", "e3"]}, cid])
            elif name == "CalendarEvent/get":
                out.append([name, {"list": [
                    {"id": "e1", "updated": "2026-04-17T00:00:00Z",
                     "uid": "u1", "calendarIds": {"cal_a": True}},
                    {"id": "e2", "updated": "2026-04-17T00:00:00Z",
                     "uid": "u2", "calendarIds": {"cal_b": True}},
                    {"id": "e3", "updated": "2026-04-17T00:00:00Z",
                     "uid": "u3", "calendarIds": {"cal_a": True}},
                ]}, cid])
        return out

    adapter = _make_adapter()
    with patch.object(JmapCalendarAdapter, "_call", fake_call):
        with patch.object(JmapCalendarAdapter, "_get_events_state", return_value=None):
            root = adapter.build_tree(ItemType.CALENDAR_EVENT)

    # No inCalendars / calendarIds / inCalendar filter sent to Stalwart.
    query_calls = [m for call in captured for m in call if m[0] == "CalendarEvent/query"]
    for qc in query_calls:
        assert "filter" not in qc[1], (
            f"CalendarEvent/query must not send a filter: {qc[1]}"
        )

    # Events bucketed to the correct container
    cal_by_id = {c.node_id: c for c in root.children}
    assert {c.node_id for c in cal_by_id["cal_a"].children} == {"e1", "e3"}
    assert {c.node_id for c in cal_by_id["cal_b"].children} == {"e2"}


def test_events_paginated_and_batched_by_max_objects_in_get():
    """Both CalendarEvent/query (pagination) and CalendarEvent/get
    (batching) respect maxObjectsInGet. The query mock honours
    position+limit so we can verify the adapter advances the cursor
    and stops on a short page rather than assuming a single response.
    """
    all_ids = [f"e{i}" for i in range(250)]
    query_windows: list[tuple[int, int]] = []
    get_call_sizes: list[int] = []

    def fake_call(self, methods):  # noqa: ANN001
        out = []
        for m in methods:
            name = m[0]
            args = m[1]
            cid = m[2]
            if name == "Calendar/get":
                out.append([name, {"list": [{"id": "cal1", "name": "K"}]}, cid])
            elif name == "CalendarEvent/query":
                position = args.get("position", 0)
                limit = args.get("limit", len(all_ids))
                page = all_ids[position:position + limit]
                query_windows.append((position, len(page)))
                out.append([name, {"ids": page}, cid])
            elif name == "CalendarEvent/get":
                ids = args.get("ids", [])
                get_call_sizes.append(len(ids))
                out.append([name, {"list": [
                    {"id": eid, "updated": "2026-04-17T00:00:00Z",
                     "uid": f"u-{eid}", "calendarIds": {"cal1": True}}
                    for eid in ids
                ]}, cid])
        return out

    adapter = _make_adapter()
    adapter._max_objects_in_get = 100
    with patch.object(JmapCalendarAdapter, "_call", fake_call):
        with patch.object(JmapCalendarAdapter, "_get_events_state", return_value=None):
            root = adapter.build_tree(ItemType.CALENDAR_EVENT)

    assert query_windows == [(0, 100), (100, 100), (200, 50)], (
        f"expected 3 paginated query windows, got {query_windows}"
    )
    assert get_call_sizes == [100, 100, 50], (
        f"expected 3 batched gets of 100, 100, 50, got {get_call_sizes}"
    )
    cal = root.children[0]
    assert len(cal.children) == 250


def test_leaf_has_no_identity_key_when_uid_missing():
    def fake_call(self, methods):  # noqa: ANN001
        out = []
        for m in methods:
            name = m[0]
            cid = m[2]
            if name == "Calendar/get":
                out.append([name, {"list": [{"id": "cal1", "name": "Kalender"}]}, cid])
            elif name == "CalendarEvent/query":
                out.append([name, {"ids": ["e1"]}, cid])
            elif name == "CalendarEvent/get":
                out.append([name, {"list": [
                    {"id": "e1", "updated": "2026-04-17T00:00:00Z",
                     "calendarIds": {"cal1": True}},
                ]}, cid])
        return out

    adapter = _make_adapter()
    with patch.object(JmapCalendarAdapter, "_call", fake_call):
        with patch.object(JmapCalendarAdapter, "_get_events_state", return_value=None):
            root = adapter.build_tree(ItemType.CALENDAR_EVENT)

    leaf = root.children[0].children[0]
    assert leaf.identity_key is None
