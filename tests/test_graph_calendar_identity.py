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


def test_duplicate_icaluid_rows_are_deduped():
    """Graph sometimes returns two rows with the same iCalUId in the same
    calendar (import artefacts). Only one row must become a tree leaf —
    otherwise _bucket demotes both as colliders, the tree plans CREATEs,
    and Stalwart rejects them with 'already exists'.

    The events URL asks Graph for $orderby=lastModifiedDateTime desc,id
    asc so the freshest row arrives first and the dedupe keeps it.
    The mock returns rows in that order; the test asserts both the
    dedupe outcome and that the adapter actually sent $orderby."""
    responses = [
        httpx.Response(200, json={"id": "default-cal", "name": "Kalender"}),
        httpx.Response(200, json={"value": [
            {"id": "default-cal", "name": "Kalender"},
        ]}),
        # Two rows, same iCalUId, different REST id + different
        # lastModifiedDateTime. The newer row appears first to match
        # what Graph returns under $orderby=lastModifiedDateTime desc.
        httpx.Response(200, json={"value": [
            {"id": "graph-rest-NEWER",
             "lastModifiedDateTime": "2026-04-18T00:00:00Z",
             "iCalUId": "shared-uid", "subject": "Lunch",
             "start": {"dateTime": "2026-05-01T12:00:00", "timeZone": "UTC"}},
            {"id": "graph-rest-OLDER",
             "lastModifiedDateTime": "2026-04-17T00:00:00Z",
             "iCalUId": "shared-uid", "subject": "Lunch",
             "start": {"dateTime": "2026-05-01T12:00:00", "timeZone": "UTC"}},
        ]}),
    ]
    idx = {"n": 0}
    events_urls_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/events" in url:
            events_urls_seen.append(url)
        resp = responses[idx["n"]]
        idx["n"] += 1
        return resp

    adapter = _adapter_with_transport(httpx.MockTransport(handler))
    root = adapter.build_tree(ItemType.CALENDAR_EVENT)

    cal = root.children[0]
    assert len(cal.children) == 1, (
        f"expected exactly one leaf after dedupe, got {len(cal.children)}: "
        f"{[leaf.node_id for leaf in cal.children]}"
    )
    assert cal.children[0].node_id == "graph-rest-NEWER"
    # Confirm the adapter asked Graph for a deterministic order.
    assert events_urls_seen, "no /events request was observed"
    assert "orderby=lastmodifieddatetime" in events_urls_seen[0].lower(), (
        f"expected $orderby=lastModifiedDateTime in URL; got {events_urls_seen[0]}"
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
