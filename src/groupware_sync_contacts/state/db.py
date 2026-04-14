"""SQLAlchemy models for contacts sync state.

Four tables: addressbook_pairs, contact_mappings, contact_snapshots, sync_cursors.
Stores the state needed between sync runs — ID mappings, field snapshots for
diff computation, and change-tracking cursors.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import ForeignKey, String, Text, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
)


class Base(DeclarativeBase):
    pass


class AddressbookPair(Base):
    __tablename__ = "addressbook_pairs"
    id: Mapped[int] = mapped_column(primary_key=True)
    a_provider: Mapped[str] = mapped_column(String(50))
    a_book_id: Mapped[str] = mapped_column(String(255))
    b_provider: Mapped[str] = mapped_column(String(50))
    b_book_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))


class ContactMapping(Base):
    __tablename__ = "contact_mappings"
    id: Mapped[int] = mapped_column(primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("addressbook_pairs.id"))
    a_contact_id: Mapped[str] = mapped_column(String(255))
    b_contact_id: Mapped[str] = mapped_column(String(255))


class ContactSnapshot(Base):
    __tablename__ = "contact_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("contact_mappings.id"), unique=True
    )
    fields_json: Mapped[str] = mapped_column(Text)
    synced_at: Mapped[int] = mapped_column()


class SyncCursor(Base):
    __tablename__ = "sync_cursors"
    id: Mapped[int] = mapped_column(primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("addressbook_pairs.id"))
    provider: Mapped[str] = mapped_column(String(50))
    cursor: Mapped[str] = mapped_column(Text)


def make_engine(database_url: str):
    if database_url.startswith("sqlite:///"):
        path = database_url.replace("sqlite:///", "")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(database_url: str) -> sessionmaker:
    engine = make_engine(database_url)
    return sessionmaker(bind=engine, expire_on_commit=False)
