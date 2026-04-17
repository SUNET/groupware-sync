"""Tests for state DB identity_key column + cache-rebuild migration."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from groupware_sync.state import ops
from groupware_sync.state.db import (
    ItemMapping,
    NodePair,
    SCHEMA_VERSION,
    make_session_factory,
)


@pytest.fixture
def session(tmp_path):
    db_path = tmp_path / "test.db"
    sf = make_session_factory(f"sqlite:///{db_path}")
    s = sf()
    yield s
    s.close()


def test_schema_version_stamped(session):
    row = session.execute(text("SELECT version FROM schema_meta")).first()
    assert row is not None
    assert row[0] == SCHEMA_VERSION


def test_item_mapping_has_identity_key_column(session):
    columns = session.execute(
        text("PRAGMA table_info(item_mapping)")
    ).fetchall()
    names = {row[1] for row in columns}
    assert "identity_key" in names


def test_create_mapping_requires_identity_key(session):
    pair = ops.get_or_create_pair(
        session, "calendar_event", "a", "ca", "b", "cb", "calname"
    )
    m = ops.create_mapping(
        session, pair.id, "a1", "b1",
        identity_key="sha256hex-value",
        fingerprint_a="fpa", fingerprint_b="fpb",
    )
    assert m.identity_key == "sha256hex-value"


def test_get_mappings_by_identity_returns_dict(session):
    pair = ops.get_or_create_pair(
        session, "calendar_event", "a", "ca", "b", "cb", "calname"
    )
    ops.create_mapping(
        session, pair.id, "a1", "b1", identity_key="k1",
        fingerprint_a="fpa", fingerprint_b="fpb",
    )
    ops.create_mapping(
        session, pair.id, "a2", "b2", identity_key="k2",
        fingerprint_a="fpa2", fingerprint_b="fpb2",
    )
    session.flush()

    by_id = ops.get_mappings_by_identity(session, pair.id)
    assert set(by_id.keys()) == {"k1", "k2"}
    assert by_id["k1"].a_item_id == "a1"
    assert by_id["k2"].b_item_id == "b2"


def test_get_mapping_by_identity_returns_one(session):
    pair = ops.get_or_create_pair(
        session, "calendar_event", "a", "ca", "b", "cb", "calname"
    )
    ops.create_mapping(
        session, pair.id, "a1", "b1", identity_key="k1",
        fingerprint_a="fpa", fingerprint_b="fpb",
    )
    session.flush()

    m = ops.get_mapping_by_identity(session, pair.id, "k1")
    assert m is not None
    assert m.a_item_id == "a1"

    assert ops.get_mapping_by_identity(session, pair.id, "nonexistent") is None


def test_heal_mapping_ids_updates_provider_ids(session):
    pair = ops.get_or_create_pair(
        session, "calendar_event", "a", "ca", "b", "cb", "calname"
    )
    m = ops.create_mapping(
        session, pair.id, "old-a", "old-b", identity_key="k1",
        fingerprint_a="fpa", fingerprint_b="fpb",
    )
    session.flush()

    ops.heal_mapping_ids(session, m, "new-a", "new-b")
    assert m.a_item_id == "new-a"
    assert m.b_item_id == "new-b"


def test_cache_rebuild_on_schema_version_mismatch(tmp_path):
    """Simulate an older schema by stamping a different version, then
    re-opening the DB. Mappings should be cleared; NodePairs should
    survive."""
    db_path = tmp_path / "old.db"

    # First session: populate
    sf = make_session_factory(f"sqlite:///{db_path}")
    s = sf()
    pair = ops.get_or_create_pair(
        s, "calendar_event", "a", "ca", "b", "cb", "calname"
    )
    ops.create_mapping(
        s, pair.id, "a1", "b1", identity_key="k1",
        fingerprint_a="fpa", fingerprint_b="fpb",
    )
    s.commit()
    # Pretend this DB was stamped with an older version
    s.execute(text("UPDATE schema_meta SET version = '0-legacy'"))
    s.commit()
    s.close()

    # Second session: factory should detect mismatch and rebuild mappings
    sf2 = make_session_factory(f"sqlite:///{db_path}")
    s2 = sf2()
    mappings = s2.query(ItemMapping).all()
    assert mappings == [], "mappings should be rebuilt on version mismatch"
    pairs = s2.query(NodePair).all()
    assert len(pairs) == 1, "NodePairs should survive"
    row = s2.execute(text("SELECT version FROM schema_meta")).first()
    assert row[0] == SCHEMA_VERSION
    s2.close()
