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


def test_upgrade_when_schema_meta_stamped_but_column_missing(tmp_path):
    """Real-world case: an earlier run of the buggy migration stamped
    schema_meta with the current version without actually adding the
    identity_key column. The factory must not trust the stamp alone —
    it must inspect the physical table shape.
    """
    from sqlalchemy import create_engine
    db_path = tmp_path / "lying-stamp.db"
    legacy_url = f"sqlite:///{db_path}"

    engine = create_engine(legacy_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE node_pair (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type VARCHAR(50) NOT NULL,
                a_provider VARCHAR(100) NOT NULL,
                a_node_id VARCHAR(255) NOT NULL,
                b_provider VARCHAR(100) NOT NULL,
                b_node_id VARCHAR(255) NOT NULL,
                name VARCHAR(255) NOT NULL,
                merkle_hash VARCHAR(64)
            )
        """))
        # Legacy item_mapping: no identity_key column
        conn.execute(text("""
            CREATE TABLE item_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id INTEGER NOT NULL,
                a_item_id VARCHAR(255) NOT NULL,
                b_item_id VARCHAR(255) NOT NULL,
                fingerprint_a VARCHAR(255),
                fingerprint_b VARCHAR(255)
            )
        """))
        # schema_meta stamped with the CURRENT version — this is the lie
        conn.execute(text("""
            CREATE TABLE schema_meta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version VARCHAR(64) NOT NULL
            )
        """))
        conn.execute(
            text("INSERT INTO schema_meta (version) VALUES (:v)"),
            {"v": SCHEMA_VERSION},
        )
    engine.dispose()

    sf = make_session_factory(legacy_url)
    s = sf()

    cols = {
        row[1]
        for row in s.execute(text("PRAGMA table_info(item_mapping)")).fetchall()
    }
    assert "identity_key" in cols, (
        "stale schema: factory trusted the lying version stamp"
    )
    s.close()


def test_upgrade_from_pre_versioning_db_drops_old_item_mapping(tmp_path):
    """Reproduces the field bug: a pre-IP-3 DB has an item_mapping table
    without an identity_key column and no schema_meta table. The factory
    must drop + recreate the cache tables so the new column exists.
    """
    from sqlalchemy import create_engine
    db_path = tmp_path / "legacy.db"
    legacy_url = f"sqlite:///{db_path}"

    # Build the pre-IP-3 shape: node_pair + the old item_mapping (no identity_key)
    # + item_snapshot + sync_cursor. No schema_meta table.
    engine = create_engine(legacy_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE node_pair (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type VARCHAR(50) NOT NULL,
                a_provider VARCHAR(100) NOT NULL,
                a_node_id VARCHAR(255) NOT NULL,
                b_provider VARCHAR(100) NOT NULL,
                b_node_id VARCHAR(255) NOT NULL,
                name VARCHAR(255) NOT NULL,
                merkle_hash VARCHAR(64)
            )
        """))
        conn.execute(text("""
            CREATE TABLE item_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id INTEGER NOT NULL,
                a_item_id VARCHAR(255) NOT NULL,
                b_item_id VARCHAR(255) NOT NULL,
                fingerprint_a VARCHAR(255),
                fingerprint_b VARCHAR(255),
                FOREIGN KEY(pair_id) REFERENCES node_pair(id)
            )
        """))
        conn.execute(text("""
            INSERT INTO node_pair
                (item_type, a_provider, a_node_id, b_provider, b_node_id, name)
            VALUES ('calendar_event', 'a', 'ca', 'b', 'cb', 'calname')
        """))
        conn.execute(text("""
            INSERT INTO item_mapping
                (pair_id, a_item_id, b_item_id, fingerprint_a, fingerprint_b)
            VALUES (1, 'old-a', 'old-b', 'fpa', 'fpb')
        """))
    engine.dispose()

    # Open with the real factory — this must not crash, and the column must exist
    sf = make_session_factory(legacy_url)
    s = sf()

    cols = {
        row[1]
        for row in s.execute(text("PRAGMA table_info(item_mapping)")).fetchall()
    }
    assert "identity_key" in cols, (
        "identity_key column missing — factory didn't migrate the legacy table"
    )

    # Cache rebuilt — no mappings
    assert s.query(ItemMapping).all() == []
    # Structural data (NodePairs) preserved
    assert len(s.query(NodePair).all()) == 1
    # Schema stamped
    version_row = s.execute(text("SELECT version FROM schema_meta")).first()
    assert version_row[0] == SCHEMA_VERSION
    s.close()
