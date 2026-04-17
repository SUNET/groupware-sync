"""Tests for the tree comparison algorithm."""
import os

import pytest

from groupware_sync.models import (
    ItemType,
    NodeType,
    OpType,
    SyncNode,
)
from groupware_sync.state.db import make_session_factory
from groupware_sync.tree import compare_trees


@pytest.fixture
def session():
    db_path = "test_tree.db"
    sf = make_session_factory(f"sqlite:///{db_path}")
    s = sf()
    yield s
    s.close()
    if os.path.exists(db_path):
        os.remove(db_path)


def _make_tree(containers: dict[str, list[tuple[str, str]]]) -> SyncNode:
    """Helper: build a root with named containers, each with (id, fingerprint) leaves.

    identity_key is derived from the leaf id so identity-based pairing works
    on initial sync with identical IDs on both sides.
    """
    children = []
    for cname, leaves in containers.items():
        leaf_nodes = [
            SyncNode(
                lid, lid, NodeType.LEAF, fingerprint=fp,
                identity_key=f"ik:{lid}",
                item_type=ItemType.CONTACT,
            )
            for lid, fp in leaves
        ]
        children.append(
            SyncNode(cname, cname, NodeType.CONTAINER, children=leaf_nodes)
        )
    root = SyncNode("root", "root", NodeType.CONTAINER, children=children)
    root.compute_merkle()
    return root


def test_identical_trees_produce_skip(session):
    """When both trees are identical and match stored state, SKIP_SUBTREE."""
    tree_a = _make_tree({"contacts": [("c1", "fp1"), ("c2", "fp2")]})
    tree_b = _make_tree({"contacts": [("c1", "fp1"), ("c2", "fp2")]})

    # First sync: no stored state, so won't prune
    compare_trees(tree_a, tree_b, "prov_a", "prov_b", ItemType.CONTACT, session)
    session.commit()

    # Second sync: stored state matches → should prune
    tree_a2 = _make_tree({"contacts": [("c1", "fp1"), ("c2", "fp2")]})
    tree_b2 = _make_tree({"contacts": [("c1", "fp1"), ("c2", "fp2")]})
    ops_list2 = compare_trees(tree_a2, tree_b2, "prov_a", "prov_b", ItemType.CONTACT, session)

    skip_ops = [op for op in ops_list2 if op.op_type == OpType.SKIP_SUBTREE]
    assert len(skip_ops) > 0


def test_new_leaf_on_a_creates_on_b(session):
    """A leaf that exists on A but not B should produce CREATE_ITEM on B."""
    tree_a = _make_tree({"contacts": [("c1", "fp1"), ("c2", "fp2")]})
    tree_b = _make_tree({"contacts": [("c1", "fp1")]})

    ops_list = compare_trees(tree_a, tree_b, "prov_a", "prov_b", ItemType.CONTACT, session)
    creates = [op for op in ops_list if op.op_type == OpType.CREATE_ITEM and op.target_side == "b"]
    assert any(op.node_id == "c2" for op in creates)


def test_new_leaf_on_b_creates_on_a(session):
    tree_a = _make_tree({"contacts": [("c1", "fp1")]})
    tree_b = _make_tree({"contacts": [("c1", "fp1"), ("c2", "fp2")]})

    ops_list = compare_trees(tree_a, tree_b, "prov_a", "prov_b", ItemType.CONTACT, session)
    creates = [op for op in ops_list if op.op_type == OpType.CREATE_ITEM and op.target_side == "a"]
    assert any(op.node_id == "c2" for op in creates)


def test_deleted_on_a_deletes_on_b(session):
    """If a mapped item disappears from A, it should be deleted on B."""
    tree_a = _make_tree({"contacts": [("c1", "fp1"), ("c2", "fp2")]})
    tree_b = _make_tree({"contacts": [("c1", "fp1"), ("c2", "fp2")]})

    # First sync: establish mappings
    compare_trees(tree_a, tree_b, "prov_a", "prov_b", ItemType.CONTACT, session)
    session.commit()

    # Second sync: c2 gone from A
    tree_a2 = _make_tree({"contacts": [("c1", "fp1")]})
    tree_b2 = _make_tree({"contacts": [("c1", "fp1"), ("c2", "fp2")]})
    ops2 = compare_trees(tree_a2, tree_b2, "prov_a", "prov_b", ItemType.CONTACT, session)

    deletes = [op for op in ops2 if op.op_type == OpType.DELETE_ITEM]
    assert any(op.target_side == "b" for op in deletes)


def test_fingerprint_change_triggers_merge(session):
    """If a mapped item's fingerprint changes, MERGE_ITEM is produced."""
    tree_a = _make_tree({"contacts": [("c1", "fp1")]})
    tree_b = _make_tree({"contacts": [("c1", "fp1")]})

    # First sync: establish mapping
    compare_trees(tree_a, tree_b, "prov_a", "prov_b", ItemType.CONTACT, session)
    session.commit()

    # Second sync: c1 fingerprint changed on A
    tree_a2 = _make_tree({"contacts": [("c1", "fp1_changed")]})
    tree_b2 = _make_tree({"contacts": [("c1", "fp1")]})
    ops2 = compare_trees(tree_a2, tree_b2, "prov_a", "prov_b", ItemType.CONTACT, session)

    merges = [op for op in ops2 if op.op_type == OpType.MERGE_ITEM]
    assert len(merges) == 1


def test_new_container_on_a_creates_on_b(session):
    """A container that exists on A but not B should produce CREATE_CONTAINER on B."""
    tree_a = _make_tree({"contacts": [("c1", "fp1")], "work": [("c2", "fp2")]})
    tree_b = _make_tree({"contacts": [("c1", "fp1")]})

    ops_list = compare_trees(tree_a, tree_b, "prov_a", "prov_b", ItemType.CONTACT, session)
    container_creates = [op for op in ops_list if op.op_type == OpType.CREATE_CONTAINER]
    assert any(op.target_side == "b" for op in container_creates)


def test_both_deleted_cleans_up(session):
    """If a mapped item is gone from both sides, mapping is cleaned up."""
    tree_a = _make_tree({"contacts": [("c1", "fp1")]})
    tree_b = _make_tree({"contacts": [("c1", "fp1")]})
    compare_trees(tree_a, tree_b, "prov_a", "prov_b", ItemType.CONTACT, session)
    session.commit()

    tree_a2 = _make_tree({"contacts": []})
    tree_b2 = _make_tree({"contacts": []})
    ops2 = compare_trees(tree_a2, tree_b2, "prov_a", "prov_b", ItemType.CONTACT, session)
    deletes = [op for op in ops2 if op.op_type == OpType.DELETE_ITEM and op.target_side == "both"]
    assert len(deletes) == 1
