"""Tests for the core models."""
from groupware_sync.models import (
    ItemType,
    NodeType,
    SyncItem,
    SyncNode,
)


def test_merkle_leaf_uses_fingerprint():
    leaf = SyncNode("a", "a", NodeType.LEAF, fingerprint="fp1")
    assert leaf.compute_merkle() == "fp1"
    assert leaf.merkle_hash == "fp1"


def test_merkle_leaf_empty_fingerprint():
    leaf = SyncNode("a", "a", NodeType.LEAF, fingerprint=None)
    assert leaf.compute_merkle() == ""


def test_merkle_container_from_children():
    root = SyncNode("r", "root", NodeType.CONTAINER, children=[
        SyncNode("a", "a", NodeType.LEAF, fingerprint="fp1"),
        SyncNode("b", "b", NodeType.LEAF, fingerprint="fp2"),
    ])
    h = root.compute_merkle()
    assert h is not None
    assert len(h) == 16  # sha256 hex truncated to 16 chars
    assert root.children[0].merkle_hash == "fp1"
    assert root.children[1].merkle_hash == "fp2"


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
