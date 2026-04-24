"""Tree-level identity-key regression: a Stalwart-originated event that was
synced to Graph (where Graph reassigned iCalUId) must still produce the
SAME identity_key on both sides at tree-build time.

This is the regression test for the Copilot #1 finding on PR #9:
without a content-based tree-level identity, the next sync would plan
DELETE_ITEM target_side='a' on the Stalwart side plus a re-CREATE from
Graph, losing any user edits and churning the state DB on every run.
"""
from __future__ import annotations

from groupware_sync_calendar.adapters.graph_adapter import (
    _tree_identity_key as graph_tree_identity_key,
)
from groupware_sync_calendar.adapters.jmap_adapter import (
    _tree_identity_key as jmap_tree_identity_key,
)


def test_stalwart_and_graph_pair_by_content_when_uids_diverge():
    """Stalwart stored the uid we sent; Graph reassigned iCalUId.
    Both sides must derive the same tree-level identity_key from
    subject + UTC start."""
    stalwart_event = {
        "id": "stalwart-srv-id",
        "uid": "our-chosen-uid-when-we-created-on-graph",
        "title": "Design review",
        "start": "2026-05-01T10:00:00",
        "timeZone": "Europe/Stockholm",
        "updated": "2026-05-01T09:00:00Z",
    }
    graph_event = {
        "id": "graph-rest-id",
        # Fresh GOID that Graph assigned when we POST'd the event —
        # deliberately different from stalwart_event["uid"] to prove
        # uid can't anchor the pair.
        "iCalUId": "040000008200E00074C5B7101A82E008"
                   "00000000BEEFDEADBEEFDEAD010000"
                   "00000000000000001000000099F428"
                   "052CBB324EB74170347B798EA0",
        "subject": "Design review",
        "start": {"dateTime": "2026-05-01T10:00:00", "timeZone": "W. Europe Standard Time"},
        "lastModifiedDateTime": "2026-05-01T09:00:00Z",
    }
    stalwart_key = jmap_tree_identity_key(stalwart_event)
    graph_key = graph_tree_identity_key(graph_event)
    assert stalwart_key is not None
    assert graph_key is not None
    assert stalwart_key == graph_key, (
        f"tree-level identity_keys must match across providers\n"
        f"  stalwart: {stalwart_key}\n"
        f"  graph:    {graph_key}"
    )


def test_jmap_falls_back_to_uid_when_title_or_start_missing():
    """When content_key can't be derived, the adapter must still produce
    a stable identity_key from uid — preserving the pre-fix behaviour
    for events without summary/start."""
    key_with_uid = jmap_tree_identity_key({"uid": "just-a-uid"})
    key_with_empty_event = jmap_tree_identity_key({})
    assert key_with_uid is not None
    assert key_with_empty_event is None  # compute_identity_key returns None on empty


def test_graph_falls_back_to_icaluid_when_subject_or_start_missing():
    key_with_icaluid = graph_tree_identity_key({"iCalUId": "040000008200E00074C5B7101A82E008ABC"})
    key_with_empty_event = graph_tree_identity_key({})
    assert key_with_icaluid is not None
    assert key_with_empty_event is None


def test_different_content_produces_different_identity_key():
    """Two different real events don't accidentally collide at tree level."""
    a = jmap_tree_identity_key({
        "title": "Design review",
        "start": "2026-05-01T10:00:00",
        "timeZone": "Etc/UTC",
    })
    b = jmap_tree_identity_key({
        "title": "Retrospective",
        "start": "2026-05-01T10:00:00",
        "timeZone": "Etc/UTC",
    })
    assert a is not None and b is not None
    assert a != b
