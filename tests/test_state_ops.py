"""Tests for state DB operations."""
import os

import pytest

from groupware_sync.models import ItemType, SyncItem
from groupware_sync.state import ops
from groupware_sync.state.db import make_session_factory


@pytest.fixture
def session():
    db_path = "test_state.db"
    sf = make_session_factory(f"sqlite:///{db_path}")
    s = sf()
    yield s
    s.close()
    if os.path.exists(db_path):
        os.remove(db_path)


def test_get_or_create_pair(session):
    pair = ops.get_or_create_pair(
        session, "contact", "stalwart", "book1", "m365", "folder1", "Contacts"
    )
    assert pair.id is not None
    assert pair.name == "Contacts"
    # Second call returns same pair
    pair2 = ops.get_or_create_pair(
        session, "contact", "stalwart", "book1", "m365", "folder1", "Contacts"
    )
    assert pair2.id == pair.id


def test_create_and_get_mapping(session):
    pair = ops.get_or_create_pair(
        session, "contact", "a", "b1", "b", "b2", "test"
    )
    m = ops.create_mapping(
        session, pair.id, "a1", "b1",
        identity_key="test-identity-1",
        fingerprint_a="fp_a", fingerprint_b="fp_b",
    )
    assert m.fingerprint_a == "fp_a"
    assert m.fingerprint_b == "fp_b"
    assert ops.get_mapping_by_a(session, pair.id, "a1") is not None
    assert ops.get_mapping_by_b(session, pair.id, "b1") is not None
    assert ops.get_mapping_by_a(session, pair.id, "nonexistent") is None


def test_delete_mapping_cascades_snapshot(session):
    pair = ops.get_or_create_pair(
        session, "contact", "a", "b1", "b", "b2", "test"
    )
    m = ops.create_mapping(
        session, pair.id, "a1", "b1", identity_key="test-identity-2",
    )
    item = SyncItem("a1", ItemType.CONTACT, {"full_name": "Test"})
    ops.save_snapshot(session, m.id, item)
    assert ops.get_snapshot(session, m.id) is not None
    ops.delete_mapping(session, m)
    session.flush()
    assert ops.get_snapshot(session, m.id) is None


def test_snapshot_round_trip(session):
    pair = ops.get_or_create_pair(
        session, "contact", "a", "b1", "b", "b2", "test"
    )
    m = ops.create_mapping(
        session, pair.id, "a1", "b1", identity_key="test-identity-3",
    )
    item = SyncItem("a1", ItemType.CONTACT, {"full_name": "Alice", "emails": ["a@b.com"]})
    ops.save_snapshot(session, m.id, item)
    snap = ops.get_snapshot(session, m.id)
    loaded = ops.load_snapshot_item(snap)
    assert loaded.fields["full_name"] == "Alice"
    assert loaded.fields["emails"] == ["a@b.com"]


def test_snapshot_upsert(session):
    pair = ops.get_or_create_pair(
        session, "contact", "a", "b1", "b", "b2", "test"
    )
    m = ops.create_mapping(
        session, pair.id, "a1", "b1", identity_key="test-identity-4",
    )
    item1 = SyncItem("a1", ItemType.CONTACT, {"full_name": "Alice"})
    ops.save_snapshot(session, m.id, item1)
    item2 = SyncItem("a1", ItemType.CONTACT, {"full_name": "Alice Updated"})
    ops.save_snapshot(session, m.id, item2)
    loaded = ops.load_snapshot_item(ops.get_snapshot(session, m.id))
    assert loaded.fields["full_name"] == "Alice Updated"


def test_cursor_round_trip(session):
    pair = ops.get_or_create_pair(
        session, "contact", "a", "b1", "b", "b2", "test"
    )
    assert ops.get_cursor(session, pair.id, "stalwart") is None
    ops.save_cursor(session, pair.id, "stalwart", "state-abc")
    assert ops.get_cursor(session, pair.id, "stalwart") == "state-abc"
    ops.save_cursor(session, pair.id, "stalwart", "state-xyz")
    assert ops.get_cursor(session, pair.id, "stalwart") == "state-xyz"


def test_update_merkle(session):
    pair = ops.get_or_create_pair(
        session, "contact", "a", "b1", "b", "b2", "test"
    )
    assert pair.merkle_hash is None
    ops.update_merkle(session, pair.id, "deadbeef")
    session.flush()
    from groupware_sync.state.db import NodePair
    refreshed = session.get(NodePair, pair.id)
    assert refreshed.merkle_hash == "deadbeef"


def test_update_fingerprints(session):
    pair = ops.get_or_create_pair(
        session, "contact", "a", "b1", "b", "b2", "test"
    )
    m = ops.create_mapping(
        session, pair.id, "a1", "b1", identity_key="test-identity-5",
    )
    assert m.fingerprint_a is None
    ops.update_fingerprints(session, m, fingerprint_a="new_a", fingerprint_b="new_b")
    assert m.fingerprint_a == "new_a"
    assert m.fingerprint_b == "new_b"


def test_get_pair_by_node_matches_either_side(session):
    pair = ops.get_or_create_pair(
        session, "contact", "stalwart", "book-A", "m365", "folder-B", "Contacts"
    )
    assert ops.get_pair_by_node(session, "stalwart", "book-A").id == pair.id
    assert ops.get_pair_by_node(session, "m365", "folder-B").id == pair.id
    assert ops.get_pair_by_node(session, "stalwart", "missing") is None
    # Provider must match the side: stalwart never sits on the b side here.
    assert ops.get_pair_by_node(session, "stalwart", "folder-B") is None
