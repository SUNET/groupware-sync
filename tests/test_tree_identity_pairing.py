"""Identity-based leaf pairing in compare_trees."""
from __future__ import annotations

import pytest

from groupware_sync.models import (
    ItemType,
    NodeType,
    OpType,
    SyncNode,
)
from groupware_sync.state import ops
from groupware_sync.state.db import make_session_factory
from groupware_sync.tree import compare_trees


@pytest.fixture
def session(tmp_path):
    db_path = tmp_path / "tree.db"
    sf = make_session_factory(f"sqlite:///{db_path}")
    s = sf()
    yield s
    s.close()


def _make_tree(leaves: list[tuple[str, str, str | None]]) -> SyncNode:
    """leaves: list of (node_id, fingerprint, identity_key)."""
    leaf_nodes = [
        SyncNode(
            lid, lid, NodeType.LEAF, fingerprint=fp, identity_key=ik,
            item_type=ItemType.CALENDAR_EVENT,
        )
        for lid, fp, ik in leaves
    ]
    container = SyncNode("c1", "c1", NodeType.CONTAINER, children=leaf_nodes)
    root = SyncNode("root", "root", NodeType.CONTAINER, children=[container])
    root.compute_merkle()
    return root


def _pair(s, a_provider="a", b_provider="b"):
    return ops.get_or_create_pair(
        s, ItemType.CALENDAR_EVENT.value, a_provider, "c1", b_provider, "c1", "c1"
    )


def test_matched_identity_no_cache_creates_mapping_no_op(session):
    """First sync: identity matches on both sides → create mapping, no item ops."""
    a = _make_tree([("a1", "fpa", "k1")])
    b = _make_tree([("b1", "fpb", "k1")])
    ops_list, _ = compare_trees(a, b, "a", "b", ItemType.CALENDAR_EVENT, session)

    item_ops = [o for o in ops_list
                if o.op_type in (OpType.CREATE_ITEM, OpType.DELETE_ITEM,
                                 OpType.MERGE_ITEM)]
    assert item_ops == [], f"unexpected ops: {item_ops!r}"

    pair = _pair(session)
    m = ops.get_mapping_by_identity(session, pair.id, "k1")
    assert m is not None
    assert m.a_item_id == "a1"
    assert m.b_item_id == "b1"


def test_matched_identity_fingerprint_changed_merges(session):
    """Cache exists, same identity, one fingerprint drifted → MERGE_ITEM."""
    pair = _pair(session)
    ops.create_mapping(
        session, pair.id, "a1", "b1", identity_key="k1",
        fingerprint_a="fpa-old", fingerprint_b="fpb-old",
    )
    session.flush()

    a = _make_tree([("a1", "fpa-new", "k1")])
    b = _make_tree([("b1", "fpb-old", "k1")])
    ops_list, _ = compare_trees(a, b, "a", "b", ItemType.CALENDAR_EVENT, session)
    merges = [o for o in ops_list if o.op_type == OpType.MERGE_ITEM]
    assert len(merges) == 1
    assert merges[0].identity_key == "k1"


def test_mapping_ids_healed_on_drift(session):
    """Cache has stale provider IDs; new tree has the same identity with
    different IDs. Mapping is healed silently (no op)."""
    pair = _pair(session)
    ops.create_mapping(
        session, pair.id, "old-a", "old-b", identity_key="k1",
        fingerprint_a="fpa", fingerprint_b="fpb",
    )
    session.flush()

    a = _make_tree([("new-a", "fpa", "k1")])
    b = _make_tree([("new-b", "fpb", "k1")])
    ops_list, _ = compare_trees(a, b, "a", "b", ItemType.CALENDAR_EVENT, session)
    item_ops = [o for o in ops_list if o.op_type in
                (OpType.DELETE_ITEM, OpType.CREATE_ITEM, OpType.MERGE_ITEM)]
    assert item_ops == []

    healed = ops.get_mapping_by_identity(session, pair.id, "k1")
    assert healed.a_item_id == "new-a"
    assert healed.b_item_id == "new-b"


def test_identity_only_on_a_no_cache_creates_on_b(session):
    """No cache, item only on A → CREATE on B."""
    a = _make_tree([("a1", "fpa", "k1")])
    b = _make_tree([])
    ops_list, _ = compare_trees(a, b, "a", "b", ItemType.CALENDAR_EVENT, session)
    creates = [o for o in ops_list if o.op_type == OpType.CREATE_ITEM]
    assert len(creates) == 1
    assert creates[0].target_side == "b"
    assert creates[0].identity_key == "k1"


def test_identity_only_on_a_with_cache_deletes_on_a(session):
    """Cache has the pair, item disappeared from B → DELETE on A."""
    pair = _pair(session)
    ops.create_mapping(
        session, pair.id, "a1", "b1", identity_key="k1",
        fingerprint_a="fpa", fingerprint_b="fpb",
    )
    session.flush()

    a = _make_tree([("a1", "fpa", "k1")])
    b = _make_tree([])  # gone from B
    ops_list, _ = compare_trees(a, b, "a", "b", ItemType.CALENDAR_EVENT, session)
    deletes = [o for o in ops_list if o.op_type == OpType.DELETE_ITEM]
    assert len(deletes) == 1
    assert deletes[0].target_side == "a"


def test_safety_invariant_suppresses_deletes_on_empty_cache(session):
    """First sync (no cache) must never emit DELETE_ITEM even if state
    suggests items vanished from one side. Protects against the 2026-04-16
    cascade after cache loss or schema rebuild."""
    # No cache entries exist.
    a = _make_tree([("a1", "fpa", "k1"), ("a2", "fpa", "k2")])
    b = _make_tree([("b1", "fpb", "k1")])  # k2 not yet on B

    ops_list, _ = compare_trees(a, b, "a", "b", ItemType.CALENDAR_EVENT, session)
    deletes = [o for o in ops_list if o.op_type == OpType.DELETE_ITEM]
    assert deletes == [], f"safety invariant violated: {deletes!r}"

    # Normal creates still happen for the unpaired item
    creates = [o for o in ops_list if o.op_type == OpType.CREATE_ITEM]
    assert len(creates) == 1
    assert creates[0].identity_key == "k2"


def test_both_gone_emits_cleanup_delete(session):
    """Cache has the pair, item gone from both sides → both-gone cleanup."""
    pair = _pair(session)
    ops.create_mapping(
        session, pair.id, "a1", "b1", identity_key="k1",
        fingerprint_a="fpa", fingerprint_b="fpb",
    )
    session.flush()

    a = _make_tree([])
    b = _make_tree([])
    ops_list, _ = compare_trees(a, b, "a", "b", ItemType.CALENDAR_EVENT, session)
    both_gone = [o for o in ops_list if o.op_type == OpType.DELETE_ITEM
                 and o.target_side == "both"]
    assert len(both_gone) == 1


def test_unpairable_leaf_creates_on_other_side(session):
    """Leaf with identity_key=None falls back to unconditional create."""
    # Prior cache so the safety invariant doesn't strip anything
    pair = _pair(session)
    ops.create_mapping(
        session, pair.id, "a0", "b0", identity_key="k0",
        fingerprint_a="fpa", fingerprint_b="fpb",
    )
    session.flush()

    a = _make_tree([("a0", "fpa", "k0"), ("aX", "fp", None)])  # aX has no id key
    b = _make_tree([("b0", "fpb", "k0")])
    ops_list, _ = compare_trees(a, b, "a", "b", ItemType.CALENDAR_EVENT, session)
    creates = [o for o in ops_list if o.op_type == OpType.CREATE_ITEM]
    assert len(creates) == 1
    assert creates[0].node_id == "aX"
    assert creates[0].target_side == "b"
    assert creates[0].identity_key is None


def test_duplicate_identity_keys_within_container_demoted_to_unpairable(session):
    """Two leaves on the same side sharing an identity_key are routed to
    the unpairable path instead of one silently winning (which would drop
    the other entirely, missing creates/deletes/merges)."""
    a = _make_tree([
        ("a1", "fp1", "dup-key"),
        ("a2", "fp2", "dup-key"),
        ("a3", "fp3", "unique-key"),
    ])
    b = _make_tree([])

    ops_result, healed = compare_trees(
        a, b, "a", "b", ItemType.CALENDAR_EVENT, session
    )

    # No cache, safety invariant suppresses deletes. We expect three
    # CREATE_ITEMs (both collider leaves + the unique-key one), all
    # targeting side b.
    creates = [op for op in ops_result if op.op_type == OpType.CREATE_ITEM]
    assert {op.node_id for op in creates} == {"a1", "a2", "a3"}
    assert all(op.target_side == "b" for op in creates)
    assert healed == 0


def test_heal_count_surfaces_via_return(session):
    """compare_trees returns (ops, healed_count) as a tuple; the engine
    aggregates the count into SyncSummary.identity_pairs_healed."""
    pair = _pair(session)
    ops.create_mapping(
        session, pair.id, "old-a", "old-b", identity_key="k1",
        fingerprint_a="fpa", fingerprint_b="fpb",
    )
    session.flush()

    a = _make_tree([("new-a", "fpa", "k1")])
    b = _make_tree([("new-b", "fpb", "k1")])
    result = compare_trees(
        a, b, "a", "b", ItemType.CALENDAR_EVENT, session
    )
    # Result must be a (ops, healed_count) tuple
    assert isinstance(result, tuple), "compare_trees must return a tuple"
    ops_list, healed = result
    assert healed == 1
    assert isinstance(ops_list, list)
