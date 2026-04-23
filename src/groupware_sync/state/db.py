"""SQLAlchemy ORM models for the tree-based sync framework state DB.

Tables:
  NodePair      — maps containers across two providers
  ItemMapping   — maps individual items across providers within a pair
  ItemSnapshot  — full merged field data for a mapping
  SyncCursor    — change-tracking cursors per pair/provider
  SchemaMeta    — schema version marker for cache-rebuild migrations
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

SCHEMA_VERSION = "3-rebuild-all"


class Base(DeclarativeBase):
    pass


class NodePair(Base):
    """Maps a container on provider A to a container on provider B."""

    __tablename__ = "node_pair"
    __table_args__ = (
        UniqueConstraint(
            "a_provider", "a_node_id", "b_provider", "b_node_id",
            name="uq_node_pair",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_type = Column(String(50), nullable=False)
    a_provider = Column(String(100), nullable=False)
    a_node_id = Column(String(255), nullable=False)
    b_provider = Column(String(100), nullable=False)
    b_node_id = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    merkle_hash = Column(String(64), nullable=True)

    mappings = relationship(
        "ItemMapping", back_populates="pair", cascade="all, delete-orphan"
    )
    cursors = relationship(
        "SyncCursor", back_populates="pair", cascade="all, delete-orphan"
    )


class ItemMapping(Base):
    """Maps a single item on provider A to its counterpart on provider B,
    keyed on cross-provider identity (hash of TypeSpec.identity_fields)."""

    __tablename__ = "item_mapping"
    __table_args__ = (
        UniqueConstraint("pair_id", "identity_key", name="uq_mapping_identity"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair_id = Column(Integer, ForeignKey("node_pair.id", ondelete="CASCADE"), nullable=False)
    identity_key = Column(String(64), nullable=False, index=True)
    a_item_id = Column(String(255), nullable=False)
    b_item_id = Column(String(255), nullable=False)
    fingerprint_a = Column(String(255), nullable=True)
    fingerprint_b = Column(String(255), nullable=True)

    pair = relationship("NodePair", back_populates="mappings")
    snapshot = relationship(
        "ItemSnapshot",
        back_populates="mapping",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ItemSnapshot(Base):
    """Stores the last-synced merged field data for an ItemMapping."""

    __tablename__ = "item_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mapping_id = Column(
        Integer,
        ForeignKey("item_mapping.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    fields_json = Column(Text, nullable=False)
    synced_at = Column(Integer, nullable=False)  # Unix timestamp (int)

    mapping = relationship("ItemMapping", back_populates="snapshot")


class SyncCursor(Base):
    """Stores a change-tracking cursor for a (pair, provider) combination."""

    __tablename__ = "sync_cursor"
    __table_args__ = (
        UniqueConstraint("pair_id", "provider", name="uq_sync_cursor"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair_id = Column(Integer, ForeignKey("node_pair.id", ondelete="CASCADE"), nullable=False)
    provider = Column(String(50), nullable=False)
    cursor = Column(Text, nullable=False)

    pair = relationship("NodePair", back_populates="cursors")


class SchemaMeta(Base):
    __tablename__ = "schema_meta"
    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(String(64), nullable=False)


def _drop_cache_tables_if_stale(engine) -> None:
    """Drop every cache table when the stored shape is incompatible.

    Runs before create_all. Triggers in two cases:
    1. Pre-versioning DB (no schema_meta): if item_mapping exists and
       lacks the identity_key column, it's from before IP-3 and must go.
    2. Versioned DB with a non-matching schema_meta row: future upgrades.

    All four cache tables (item_snapshot, item_mapping, sync_cursor,
    node_pair) are dropped together. They are fully reconstructible from
    the providers on the next sync, and keeping partial state behind
    confuses the adapter's state-skip path (a surviving sync_cursor +
    node_pair tricks build_tree into skipping container children, which
    then produces a tree with zero leaves that can never pair against
    the other side's fresh tree). The tree comparison's safety invariant
    ensures the first post-rebuild sync plans no deletes.
    """
    insp = inspect(engine)
    should_drop = False

    # Physical shape first: a prior buggy migration may have stamped
    # schema_meta at the current version without actually adding the
    # identity_key column, so don't trust the stamp alone.
    if insp.has_table("item_mapping"):
        cols = {c["name"] for c in insp.get_columns("item_mapping")}
        if "identity_key" not in cols:
            should_drop = True

    # Stamp mismatch (future upgrades that don't change item_mapping's
    # columns still route through here).
    if not should_drop and insp.has_table("schema_meta"):
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT version FROM schema_meta LIMIT 1")
            ).first()
        if row is not None and row[0] != SCHEMA_VERSION:
            should_drop = True

    if not should_drop:
        return

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS item_snapshot"))
        conn.execute(text("DROP TABLE IF EXISTS item_mapping"))
        conn.execute(text("DROP TABLE IF EXISTS sync_cursor"))
        conn.execute(text("DROP TABLE IF EXISTS node_pair"))


def _ensure_schema_version(engine) -> None:
    """Stamp schema_meta with the current version.

    Assumes _drop_cache_tables_if_stale + create_all have already aligned
    the table shape. This just writes the version marker.
    """
    with engine.begin() as conn:
        row = conn.execute(text("SELECT version FROM schema_meta LIMIT 1")).first()
        if row is None:
            conn.execute(
                text("INSERT INTO schema_meta (version) VALUES (:v)"),
                {"v": SCHEMA_VERSION},
            )
            return
        if row[0] != SCHEMA_VERSION:
            conn.execute(
                text("UPDATE schema_meta SET version = :v"),
                {"v": SCHEMA_VERSION},
            )


def make_session_factory(database_url: str) -> sessionmaker:
    """Create engine, ensure schema + version, return a sessionmaker."""
    engine = create_engine(database_url, future=True)
    _drop_cache_tables_if_stale(engine)
    Base.metadata.create_all(engine)
    _ensure_schema_version(engine)
    return sessionmaker(bind=engine, autoflush=True, autocommit=False)
