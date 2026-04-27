"""Two leaves on each side share a content_key (identity_key collision).
A previous run paired them via the execute-time fallback and stored
ItemMappings under `_legacy_identity_key(a_id, b_id)`. The next run's
`_bucket` demotes them to unpairable again because they still collide
on identity_key — but the cached mappings record the actual pairing.
The tree must trust the cached mappings and avoid the CREATE+DELETE
churn cycle.

Without the fix, a stable pair of collisions plans:
  - DELETE_ITEM target_side='both' for each cached mapping
    (because no leaf has the legacy identity_key)
  - CREATE_ITEM for each unpairable leaf
on every sync, forever — even though the data is stable."""
from __future__ import annotations

import pytest

from groupware_sync.engine import _legacy_identity_key
from groupware_sync.models import ItemType, NodeType, OpType, SyncNode
from groupware_sync.state import ops as state_ops
from groupware_sync.state.db import make_session_factory
from groupware_sync.tree import compare_trees


@pytest.fixture
def session(tmp_path):
    sf = make_session_factory(f"sqlite:///{tmp_path / 'test.db'}")
    s = sf()
    yield s
    s.close()


def _tree_with_collision_pair(
    a_ids: list[tuple[str, str, str]],
    b_ids: list[tuple[str, str, str]],
) -> tuple[SyncNode, SyncNode]:
    """Build two trees, one container each. Each leaf is (node_id,
    identity_key, fingerprint). Use the same identity_key on multiple
    leaves to simulate a content_key collision."""
    a_leaves = [
        SyncNode(nid, nid, NodeType.LEAF, fingerprint=fp,
                 identity_key=ik, item_type=ItemType.CONTACT)
        for nid, ik, fp in a_ids
    ]
    b_leaves = [
        SyncNode(nid, nid, NodeType.LEAF, fingerprint=fp,
                 identity_key=ik, item_type=ItemType.CONTACT)
        for nid, ik, fp in b_ids
    ]
    a = SyncNode("root", "root", NodeType.CONTAINER, children=[
        SyncNode("c", "c", NodeType.CONTAINER, children=a_leaves),
    ])
    b = SyncNode("root", "root", NodeType.CONTAINER, children=[
        SyncNode("c", "c", NodeType.CONTAINER, children=b_leaves),
    ])
    a.compute_merkle()
    b.compute_merkle()
    return a, b


def test_collision_pairs_with_cached_mapping_emit_no_ops(session):
    """Steady-state regression: two paired leaves on each side share
    identity_key, both ItemMappings exist with legacy keys, fingerprints
    haven't drifted → tree must emit zero CREATE/DELETE/MERGE ops."""
    pair = state_ops.get_or_create_pair(
        session, ItemType.CONTACT.value, "prov_a", "c", "prov_b", "c", "",
    )
    state_ops.create_mapping(
        session, pair.id,
        a_item_id="a1", b_item_id="b1",
        identity_key=_legacy_identity_key("a1", "b1"),
        fingerprint_a="fp1", fingerprint_b="fp1",
    )
    state_ops.create_mapping(
        session, pair.id,
        a_item_id="a2", b_item_id="b2",
        identity_key=_legacy_identity_key("a2", "b2"),
        fingerprint_a="fp2", fingerprint_b="fp2",
    )
    session.commit()

    a, b = _tree_with_collision_pair(
        a_ids=[("a1", "ik:shared", "fp1"), ("a2", "ik:shared", "fp2")],
        b_ids=[("b1", "ik:shared", "fp1"), ("b2", "ik:shared", "fp2")],
    )

    ops_list, _ = compare_trees(
        a, b, "prov_a", "prov_b", ItemType.CONTACT, session,
    )

    creates = [op for op in ops_list if op.op_type == OpType.CREATE_ITEM]
    deletes = [op for op in ops_list if op.op_type == OpType.DELETE_ITEM]
    merges = [op for op in ops_list if op.op_type == OpType.MERGE_ITEM]
    assert creates == [], f"unexpected creates: {creates}"
    assert deletes == [], f"unexpected deletes: {deletes}"
    assert merges == [], f"unexpected merges: {merges}"


def test_collision_pair_with_drifted_fingerprint_emits_merge(session):
    """When fingerprints drifted on either side, the cache-resolved
    pair gets a MERGE_ITEM op so the engine can converge — but still
    no CREATE or DELETE for that pair."""
    pair = state_ops.get_or_create_pair(
        session, ItemType.CONTACT.value, "prov_a", "c", "prov_b", "c", "",
    )
    state_ops.create_mapping(
        session, pair.id,
        a_item_id="a1", b_item_id="b1",
        identity_key=_legacy_identity_key("a1", "b1"),
        fingerprint_a="fp1-old", fingerprint_b="fp1-old",
    )
    session.commit()

    a, b = _tree_with_collision_pair(
        a_ids=[("a1", "ik:shared", "fp1-NEW"), ("a2", "ik:shared", "fp2")],
        b_ids=[("b1", "ik:shared", "fp1-old"), ("b2", "ik:shared", "fp2")],
    )

    ops_list, _ = compare_trees(
        a, b, "prov_a", "prov_b", ItemType.CONTACT, session,
    )

    merges = [op for op in ops_list if op.op_type == OpType.MERGE_ITEM]
    creates = [op for op in ops_list if op.op_type == OpType.CREATE_ITEM]
    deletes = [op for op in ops_list if op.op_type == OpType.DELETE_ITEM]
    assert len(merges) == 1
    assert merges[0].node_id == "a1"
    assert merges[0].paired_node_id == "b1"
    # a2/b2 have no cached mapping → standard unpairable handling.
    assert {op.node_id for op in creates} == {"a2", "b2"}
    assert deletes == []


def test_cached_mapping_with_one_side_gone_still_cleaned_up(session):
    """The fix targets stable cached pairs only. When one leaf is
    genuinely gone but the cache still records it, normal cleanup
    must still run."""
    pair = state_ops.get_or_create_pair(
        session, ItemType.CONTACT.value, "prov_a", "c", "prov_b", "c", "",
    )
    state_ops.create_mapping(
        session, pair.id,
        a_item_id="a1", b_item_id="b1",
        identity_key=_legacy_identity_key("a1", "b1"),
        fingerprint_a="fp1", fingerprint_b="fp1",
    )
    session.commit()

    # b1 has been deleted; only a1 remains on side A. No collision now.
    a, b = _tree_with_collision_pair(
        a_ids=[("a1", "ik:lone", "fp1")],
        b_ids=[],
    )

    ops_list, _ = compare_trees(
        a, b, "prov_a", "prov_b", ItemType.CONTACT, session,
    )
    # a1 has identity_key="ik:lone" (in by_identity_a), not in any
    # cached mapping (the cached one is _legacy_identity_key("a1","b1")).
    # So a1 takes the standard "leaf_a exists, leaf_b None, no mapping"
    # path → CREATE_ITEM on B. The cached mapping (no leaves at the
    # legacy key) gets cleaned up as `(both gone)`.
    create_b = [op for op in ops_list
                if op.op_type == OpType.CREATE_ITEM and op.target_side == "b"]
    delete_both = [op for op in ops_list
                   if op.op_type == OpType.DELETE_ITEM and op.target_side == "both"]
    assert len(create_b) == 1
    assert create_b[0].node_id == "a1"
    assert len(delete_both) == 1
