"""Graph calendar adapter: identity_key on leaves from iCalUId."""
from __future__ import annotations

import httpx

from groupware_sync.models import ItemType, compute_identity_key
from groupware_sync_calendar.adapters.graph_adapter import (
    GRAPH_BASE,
    GraphCalendarAdapter,
)


def _adapter_with_transport(transport: httpx.MockTransport) -> GraphCalendarAdapter:
    a = GraphCalendarAdapter("token")
    a._client = httpx.Client(
        base_url=GRAPH_BASE,
        headers={"Authorization": "Bearer token"},
        transport=transport,
        timeout=5.0,
    )
    return a


def test_build_tree_leaves_have_identity_key_from_ical_uid():
    """Leaves' identity_key is compute_identity_key({'uid': iCalUId}, ['uid'])."""
    responses = [
        # GET /me/calendar (primary calendar)
        httpx.Response(200, json={"id": "default-cal", "name": "Kalender"}),
        # GET /me/calendars (all calendars)
        httpx.Response(200, json={"value": [
            {"id": "default-cal", "name": "Kalender"},
        ]}),
        # GET events in calendar
        httpx.Response(200, json={"value": [
            {"id": "graph-e1", "lastModifiedDateTime": "2026-04-17T00:00:00Z",
             "iCalUId": "uid-one"},
            {"id": "graph-e2", "lastModifiedDateTime": "2026-04-17T00:00:00Z",
             "iCalUId": "uid-two"},
        ]}),
    ]
    idx = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        resp = responses[idx["n"]]
        idx["n"] += 1
        return resp

    adapter = _adapter_with_transport(httpx.MockTransport(handler))
    root = adapter.build_tree(ItemType.CALENDAR_EVENT)

    cal = root.children[0]
    leaf_by_id = {leaf.node_id: leaf for leaf in cal.children}
    assert leaf_by_id["graph-e1"].identity_key == compute_identity_key(
        {"uid": "uid-one"}, ["uid"]
    )
    assert leaf_by_id["graph-e2"].identity_key == compute_identity_key(
        {"uid": "uid-two"}, ["uid"]
    )


def test_leaf_has_no_identity_key_when_ical_uid_missing():
    responses = [
        httpx.Response(200, json={"id": "default-cal", "name": "Kalender"}),
        httpx.Response(200, json={"value": [
            {"id": "default-cal", "name": "Kalender"},
        ]}),
        httpx.Response(200, json={"value": [
            {"id": "graph-e1", "lastModifiedDateTime": "2026-04-17T00:00:00Z"},
        ]}),
    ]
    idx = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        resp = responses[idx["n"]]
        idx["n"] += 1
        return resp

    adapter = _adapter_with_transport(httpx.MockTransport(handler))
    root = adapter.build_tree(ItemType.CALENDAR_EVENT)

    leaf = root.children[0].children[0]
    assert leaf.identity_key is None
