"""State DB operations — the interface the sync engine uses.

Every function takes an explicit Session so the sync engine can control
transaction boundaries. The sync engine calls ops, never the ORM models.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from groupware_sync_contacts.models import Contact
from groupware_sync_contacts.state.db import (
    AddressbookPair,
    ContactMapping,
    ContactSnapshot,
    SyncCursor,
)

# ---- addressbook pairs ------------------------------------------------------


def get_or_create_pair(
    s: Session,
    a_provider: str,
    a_book_id: str,
    b_provider: str,
    b_book_id: str,
    name: str,
) -> AddressbookPair:
    pair = s.scalar(
        select(AddressbookPair).where(
            AddressbookPair.a_provider == a_provider,
            AddressbookPair.a_book_id == a_book_id,
            AddressbookPair.b_provider == b_provider,
            AddressbookPair.b_book_id == b_book_id,
        )
    )
    if pair is None:
        pair = AddressbookPair(
            a_provider=a_provider,
            a_book_id=a_book_id,
            b_provider=b_provider,
            b_book_id=b_book_id,
            name=name,
        )
        s.add(pair)
        s.flush()
    return pair


# ---- contact mappings --------------------------------------------------------


def get_mapping_by_a(
    s: Session, pair_id: int, a_contact_id: str
) -> Optional[ContactMapping]:
    return s.scalar(
        select(ContactMapping).where(
            ContactMapping.pair_id == pair_id,
            ContactMapping.a_contact_id == a_contact_id,
        )
    )


def get_mapping_by_b(
    s: Session, pair_id: int, b_contact_id: str
) -> Optional[ContactMapping]:
    return s.scalar(
        select(ContactMapping).where(
            ContactMapping.pair_id == pair_id,
            ContactMapping.b_contact_id == b_contact_id,
        )
    )


def get_all_mappings(s: Session, pair_id: int) -> list[ContactMapping]:
    return list(
        s.scalars(
            select(ContactMapping).where(ContactMapping.pair_id == pair_id)
        )
    )


def create_mapping(
    s: Session, pair_id: int, a_contact_id: str, b_contact_id: str
) -> ContactMapping:
    m = ContactMapping(
        pair_id=pair_id,
        a_contact_id=a_contact_id,
        b_contact_id=b_contact_id,
    )
    s.add(m)
    s.flush()
    return m


def delete_mapping(s: Session, mapping: ContactMapping) -> None:
    snap = get_snapshot(s, mapping.id)
    if snap is not None:
        s.delete(snap)
    s.delete(mapping)


# ---- snapshots ---------------------------------------------------------------


def get_snapshot(s: Session, mapping_id: int) -> Optional[ContactSnapshot]:
    return s.scalar(
        select(ContactSnapshot).where(
            ContactSnapshot.mapping_id == mapping_id
        )
    )


def save_snapshot(s: Session, mapping_id: int, contact: Contact) -> None:
    existing = get_snapshot(s, mapping_id)
    fields = json.dumps(contact.to_dict())
    now = int(time.time())
    if existing is not None:
        existing.fields_json = fields
        existing.synced_at = now
    else:
        s.add(
            ContactSnapshot(
                mapping_id=mapping_id, fields_json=fields, synced_at=now
            )
        )


def load_snapshot_contact(snapshot: ContactSnapshot) -> Contact:
    return Contact.from_dict(json.loads(snapshot.fields_json))


# ---- cursors -----------------------------------------------------------------


def get_cursor(
    s: Session, pair_id: int, provider: str
) -> Optional[str]:
    cur = s.scalar(
        select(SyncCursor).where(
            SyncCursor.pair_id == pair_id,
            SyncCursor.provider == provider,
        )
    )
    return cur.cursor if cur is not None else None


def save_cursor(
    s: Session, pair_id: int, provider: str, cursor: str
) -> None:
    existing = s.scalar(
        select(SyncCursor).where(
            SyncCursor.pair_id == pair_id,
            SyncCursor.provider == provider,
        )
    )
    if existing is not None:
        existing.cursor = cursor
    else:
        s.add(SyncCursor(pair_id=pair_id, provider=provider, cursor=cursor))
