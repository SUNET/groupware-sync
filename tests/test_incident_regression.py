"""Regression test for the 2026-04-16 incident.

Scenario: first sync pairs 100 events (Graph ↔ Stalwart). Between syncs,
Stalwart's provider IDs drift (the JMAP ids change between query responses).
With identity-based pairing + cache heals, the second sync must plan zero
deletes.
"""
from __future__ import annotations

import pytest

from groupware_sync.models import ItemType, NodeType, OpType, SyncNode, compute_identity_key
from groupware_sync.state import ops
from groupware_sync.state.db import make_session_factory
from groupware_sync.tree import compare_trees


@pytest.fixture
def session(tmp_path):
    db_path = tmp_path / "regression.db"
    sf = make_session_factory(f"sqlite:///{db_path}")
    s = sf()
    yield s
    s.close()


def _leaf(nid: str, fp: str, uid: str) -> SyncNode:
    return SyncNode(
        nid, nid, NodeType.LEAF, fingerprint=fp,
        identity_key=compute_identity_key({"uid": uid}, ["uid"]),
        item_type=ItemType.CALENDAR_EVENT,
    )


def _tree(leaves: list[SyncNode]) -> SyncNode:
    c = SyncNode("cal", "cal", NodeType.CONTAINER, children=leaves)
    r = SyncNode("root", "root", NodeType.CONTAINER, children=[c])
    r.compute_merkle()
    return r


def test_id_drift_does_not_produce_deletes(session):
    """1. First sync pairs 100 events by identity → mappings populated.
    2. Second sync: stalwart IDs drift, Graph IDs stable. Both sides have
       same 100 events by identity.
    3. Assert: zero DELETE ops, 100 healed.
    """
    # --- First sync: populate mappings ---
    graph_leaves_1 = [
        _leaf(f"graph-{i}", "fp-original", f"uid-{i}") for i in range(100)
    ]
    jmap_leaves_1 = [
        _leaf(f"jmap-v1-{i}", "fp-original", f"uid-{i}") for i in range(100)
    ]
    tree_a_1 = _tree(jmap_leaves_1)
    tree_b_1 = _tree(graph_leaves_1)
    ops_1, healed_1 = compare_trees(
        tree_a_1, tree_b_1, "stalwart", "m365", ItemType.CALENDAR_EVENT, session
    )
    # First sync has no prior cache — safety invariant applies, deletes suppressed.
    # Items on both sides pair silently (no ops emitted).
    assert [o for o in ops_1 if o.op_type == OpType.DELETE_ITEM] == []
    assert healed_1 == 0
    session.flush()

    # Verify 100 mappings created
    pair = ops.get_pair(session, "stalwart", "cal", "m365", "cal")
    cached = ops.get_mappings_by_identity(session, pair.id)
    assert len(cached) == 100

    # --- Second sync: stalwart IDs drift ---
    graph_leaves_2 = [
        _leaf(f"graph-{i}", "fp-original", f"uid-{i}") for i in range(100)
    ]
    jmap_leaves_2 = [
        # Every ID drifted
        _leaf(f"jmap-v2-{i}", "fp-original", f"uid-{i}") for i in range(100)
    ]
    tree_a_2 = _tree(jmap_leaves_2)
    tree_b_2 = _tree(graph_leaves_2)
    ops_2, healed_2 = compare_trees(
        tree_a_2, tree_b_2, "stalwart", "m365", ItemType.CALENDAR_EVENT, session
    )

    # No deletes, no creates, no merges — all items paired by identity and healed
    deletes = [o for o in ops_2 if o.op_type == OpType.DELETE_ITEM]
    creates = [o for o in ops_2 if o.op_type == OpType.CREATE_ITEM]
    merges = [o for o in ops_2 if o.op_type == OpType.MERGE_ITEM]
    assert deletes == [], f"regression: {len(deletes)} delete ops planned"
    assert creates == [], f"unexpected: {len(creates)} create ops planned"
    assert merges == [], f"unexpected: {len(merges)} merge ops planned"
    assert healed_2 == 100, (
        f"expected all 100 mappings healed, got {healed_2}"
    )

    # Mappings now reference new JMAP IDs
    healed_cache = ops.get_mappings_by_identity(session, pair.id)
    for i in range(100):
        key = compute_identity_key({"uid": f"uid-{i}"}, ["uid"])
        m = healed_cache[key]
        assert m.a_item_id == f"jmap-v2-{i}"
