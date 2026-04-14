"""Tests for the core models."""
from groupware_sync.models import (
    ItemType,
    NodeType,
    SyncItem,
    SyncNode,
)


def test_merkle_leaf_is_hash_of_id_and_fingerprint():
    leaf = SyncNode("a", "a", NodeType.LEAF, fingerprint="fp1")
    h = leaf.compute_merkle()
    assert len(h) == 16  # sha256 hex truncated to 16 chars
    assert h != "fp1"    # it's a proper hash, not the raw fingerprint


def test_merkle_leaf_includes_id():
    """Two leaves with same fingerprint but different IDs produce different hashes."""
    leaf1 = SyncNode("id1", "a", NodeType.LEAF, fingerprint="same_fp")
    leaf2 = SyncNode("id2", "b", NodeType.LEAF, fingerprint="same_fp")
    assert leaf1.compute_merkle() != leaf2.compute_merkle()


def test_merkle_leaf_empty_fingerprint():
    leaf = SyncNode("a", "a", NodeType.LEAF, fingerprint=None)
    h = leaf.compute_merkle()
    assert len(h) == 16  # still a proper hash


def test_merkle_container_from_children():
    root = SyncNode("r", "root", NodeType.CONTAINER, children=[
        SyncNode("a", "a", NodeType.LEAF, fingerprint="fp1"),
        SyncNode("b", "b", NodeType.LEAF, fingerprint="fp2"),
    ])
    h = root.compute_merkle()
    assert h is not None
    assert len(h) == 16
    assert len(root.children[0].merkle_hash) == 16  # children are also proper hashes
    assert len(root.children[1].merkle_hash) == 16


def test_merkle_is_deterministic():
    """Same children in any insertion order produce the same hash (sorted)."""
    root1 = SyncNode("r", "root", NodeType.CONTAINER, children=[
        SyncNode("a", "a", NodeType.LEAF, fingerprint="fp1"),
        SyncNode("b", "b", NodeType.LEAF, fingerprint="fp2"),
    ])
    root2 = SyncNode("r", "root", NodeType.CONTAINER, children=[
        SyncNode("b", "b", NodeType.LEAF, fingerprint="fp2"),
        SyncNode("a", "a", NodeType.LEAF, fingerprint="fp1"),
    ])
    assert root1.compute_merkle() == root2.compute_merkle()


def test_merkle_changes_when_child_changes():
    root1 = SyncNode("r", "root", NodeType.CONTAINER, children=[
        SyncNode("a", "a", NodeType.LEAF, fingerprint="fp1"),
    ])
    root2 = SyncNode("r", "root", NodeType.CONTAINER, children=[
        SyncNode("a", "a", NodeType.LEAF, fingerprint="fp2"),
    ])
    assert root1.compute_merkle() != root2.compute_merkle()


def test_merkle_nested_containers():
    root = SyncNode("r", "root", NodeType.CONTAINER, children=[
        SyncNode("c1", "sub", NodeType.CONTAINER, children=[
            SyncNode("a", "a", NodeType.LEAF, fingerprint="fp1"),
        ]),
    ])
    h = root.compute_merkle()
    assert h is not None
    assert root.children[0].merkle_hash is not None


def test_merkle_skipped_container_preserves_hash():
    """A skipped container keeps its pre-set merkle_hash instead of recomputing."""
    node = SyncNode("c1", "contacts", NodeType.CONTAINER,
                    merkle_hash="stored_hash_abc", skipped=True)
    # No children — but skipped=True means compute_merkle returns stored hash
    h = node.compute_merkle()
    assert h == "stored_hash_abc"


def test_merkle_skipped_container_without_hash_recomputes():
    """A skipped container with no pre-set hash still computes from children."""
    node = SyncNode("c1", "contacts", NodeType.CONTAINER, skipped=True,
                    children=[SyncNode("a", "a", NodeType.LEAF, fingerprint="fp1")])
    h = node.compute_merkle()
    assert len(h) == 16  # computed from child, not preserved


def test_sync_item_round_trip():
    item = SyncItem("id1", ItemType.CONTACT, {"full_name": "Alice", "emails": ["a@b.com"]})
    d = item.to_dict()
    item2 = SyncItem.from_dict(d)
    assert item2.provider_id == "id1"
    assert item2.item_type == ItemType.CONTACT
    assert item2.fields["full_name"] == "Alice"
    assert item2.fields["emails"] == ["a@b.com"]


def test_sync_item_round_trip_with_datetime():
    from datetime import datetime, timezone
    ts = datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)
    item = SyncItem("id1", ItemType.CONTACT, updated_at=ts)
    d = item.to_dict()
    item2 = SyncItem.from_dict(d)
    assert item2.updated_at == ts
