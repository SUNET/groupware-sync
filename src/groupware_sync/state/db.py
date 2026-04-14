"""SQLAlchemy ORM models for the tree-based sync framework state DB.

Tables:
  NodePair      — maps containers across two providers
  ItemMapping   — maps individual items across providers within a pair
  ItemSnapshot  — full merged field data for a mapping
  SyncCursor    — change-tracking cursors per pair/provider
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
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker


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
    """Maps a single item on provider A to its counterpart on provider B."""

    __tablename__ = "item_mapping"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair_id = Column(Integer, ForeignKey("node_pair.id", ondelete="CASCADE"), nullable=False)
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


def make_session_factory(database_url: str) -> sessionmaker:
    """Create engine, ensure all tables exist, and return a sessionmaker."""
    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=True, autocommit=False)
