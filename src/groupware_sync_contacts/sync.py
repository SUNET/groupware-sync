"""Two-way contacts sync engine — 7-phase algorithm.

Operates on ContactProvider pairs and the state DB. Never calls HTTP directly.
The sync is fully symmetric — no source-of-truth concept. Field-level merge
with last-write-wins by timestamp for same-field conflicts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from groupware_sync_contacts.models import (
    MERGE_FIELDS,
    ChangeSet,
    Contact,
)
from groupware_sync_contacts.provider import ContactProvider
from groupware_sync_contacts.state import ops

log = logging.getLogger(__name__)


@dataclass
class SyncSummary:
    addressbooks: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    conflicts: int = 0
    errors: int = 0


def sync(
    provider_a: ContactProvider,
    provider_b: ContactProvider,
    session: Session,
) -> SyncSummary:
    summary = SyncSummary()

    # Phase 1: addressbook pairing
    a_books = provider_a.list_addressbooks()
    b_books = provider_b.list_addressbooks()
    a_by_name: dict[str, str] = {}  # name.lower() → provider_id
    b_by_name: dict[str, str] = {}

    for book in a_books:
        key = book.name.lower()
        if key in a_by_name:
            log.warning("duplicate addressbook name on %s: %s", provider_a.name, book.name)
            continue
        a_by_name[key] = book.provider_id

    for book in b_books:
        key = book.name.lower()
        if key in b_by_name:
            log.warning("duplicate addressbook name on %s: %s", provider_b.name, book.name)
            continue
        b_by_name[key] = book.provider_id

    pairs: list[tuple[str, str, str]] = []  # (a_book_id, b_book_id, name)

    for name_lower, a_id in a_by_name.items():
        if name_lower in b_by_name:
            pairs.append((a_id, b_by_name[name_lower], name_lower))
        else:
            try:
                new_book = provider_b.create_addressbook(
                    next(b.name for b in a_books if b.provider_id == a_id)
                )
                pairs.append((a_id, new_book.provider_id, name_lower))
                log.info("created addressbook %s on %s", name_lower, provider_b.name)
            except Exception as e:
                log.error("failed to create addressbook %s on %s: %s", name_lower, provider_b.name, e)
                summary.errors += 1

    for name_lower, b_id in b_by_name.items():
        if name_lower not in a_by_name:
            try:
                new_book = provider_a.create_addressbook(
                    next(b.name for b in b_books if b.provider_id == b_id)
                )
                pairs.append((new_book.provider_id, b_id, name_lower))
                log.info("created addressbook %s on %s", name_lower, provider_a.name)
            except Exception as e:
                log.error("failed to create addressbook %s on %s: %s", name_lower, provider_a.name, e)
                summary.errors += 1

    summary.addressbooks = len(pairs)

    for a_book_id, b_book_id, pair_name in pairs:
        try:
            _sync_addressbook_pair(
                provider_a, provider_b, a_book_id, b_book_id,
                pair_name, session, summary,
            )
            session.commit()
        except Exception as e:
            log.error("sync failed for addressbook %s: %s", pair_name, e)
            session.rollback()
            summary.errors += 1

    return summary


def _sync_addressbook_pair(
    prov_a: ContactProvider,
    prov_b: ContactProvider,
    a_book_id: str,
    b_book_id: str,
    pair_name: str,
    s: Session,
    summary: SyncSummary,
) -> None:
    pair = ops.get_or_create_pair(
        s, prov_a.name, a_book_id, prov_b.name, b_book_id, pair_name
    )

    # Phase 2: change detection
    a_cursor = ops.get_cursor(s, pair.id, prov_a.name)
    b_cursor = ops.get_cursor(s, pair.id, prov_b.name)

    a_changes: Optional[ChangeSet] = None
    b_changes: Optional[ChangeSet] = None
    a_all: Optional[list[Contact]] = None
    b_all: Optional[list[Contact]] = None

    if a_cursor:
        a_changes = prov_a.get_changes(a_book_id, a_cursor)
    if b_cursor:
        b_changes = prov_b.get_changes(b_book_id, b_cursor)

    # Fall back to full fetch if no cursor or stale cursor
    if a_changes is None:
        a_all = prov_a.get_all_contacts(a_book_id)
    if b_changes is None:
        b_all = prov_b.get_all_contacts(b_book_id)

    existing_mappings = ops.get_all_mappings(s, pair.id)

    if a_all is not None and b_all is not None:
        # Phase 3: full-fetch identity matching (both sides)
        _full_sync(prov_a, prov_b, a_book_id, b_book_id, pair, a_all, b_all,
                    existing_mappings, s, summary)
    else:
        # Incremental sync using change sets
        _incremental_sync(prov_a, prov_b, a_book_id, b_book_id, pair,
                          a_changes, b_changes, a_all, b_all,
                          existing_mappings, s, summary)

    # Phase 7: persist cursors
    if a_changes is not None and a_changes.new_cursor:
        ops.save_cursor(s, pair.id, prov_a.name, a_changes.new_cursor)
    elif a_all is not None:
        # After a full fetch, establish the cursor for next run
        _establish_cursor(prov_a, a_book_id, pair.id, s)
    if b_changes is not None and b_changes.new_cursor:
        ops.save_cursor(s, pair.id, prov_b.name, b_changes.new_cursor)
    elif b_all is not None:
        _establish_cursor(prov_b, b_book_id, pair.id, s)


def _establish_cursor(
    provider: ContactProvider, book_id: str, pair_id: int, s: Session
) -> None:
    """After a full fetch, establish the initial cursor for incremental sync."""
    if hasattr(provider, "get_state"):
        state = provider.get_state()
        if state:
            ops.save_cursor(s, pair_id, provider.name, state)
    if hasattr(provider, "get_initial_delta_link"):
        link = provider.get_initial_delta_link(book_id)
        if link:
            ops.save_cursor(s, pair_id, provider.name, link)


def _full_sync(
    prov_a: ContactProvider,
    prov_b: ContactProvider,
    a_book_id: str,
    b_book_id: str,
    pair,
    a_contacts: list[Contact],
    b_contacts: list[Contact],
    existing_mappings: list,
    s: Session,
    summary: SyncSummary,
) -> None:
    # Build lookup maps
    a_by_id = {c.provider_id: c for c in a_contacts}
    b_by_id = {c.provider_id: c for c in b_contacts}
    mapped_a_ids = {m.a_contact_id for m in existing_mappings}
    mapped_b_ids = {m.b_contact_id for m in existing_mappings}

    # Phase 4: merge existing mapped contacts
    for mapping in existing_mappings:
        curr_a = a_by_id.get(mapping.a_contact_id)
        curr_b = b_by_id.get(mapping.b_contact_id)

        if curr_a is None and curr_b is None:
            # Both sides deleted — clean up
            ops.delete_mapping(s, mapping)
            summary.deleted += 1
            continue
        if curr_a is None:
            # Phase 6: deleted on A → delete on B
            try:
                prov_b.delete_contact(b_book_id, mapping.b_contact_id)
                summary.deleted += 1
            except Exception as e:
                log.error("delete on B failed: %s", e)
                summary.errors += 1
            ops.delete_mapping(s, mapping)
            continue
        if curr_b is None:
            # Phase 6: deleted on B → delete on A
            try:
                prov_a.delete_contact(a_book_id, mapping.a_contact_id)
                summary.deleted += 1
            except Exception as e:
                log.error("delete on A failed: %s", e)
                summary.errors += 1
            ops.delete_mapping(s, mapping)
            continue

        # Both exist — field-level merge
        _merge_contact(prov_a, prov_b, a_book_id, b_book_id, mapping,
                        curr_a, curr_b, s, summary)

    # Phase 3: identity matching for unmapped contacts
    unmatched_a = [c for c in a_contacts if c.provider_id not in mapped_a_ids]
    unmatched_b = [c for c in b_contacts if c.provider_id not in mapped_b_ids]

    # Build email and name indexes for matching
    b_by_email: dict[str, Contact] = {}
    b_by_name: dict[str, Contact] = {}
    for c in unmatched_b:
        for e in c.emails:
            b_by_email[e.value.lower()] = c
        if c.full_name:
            b_by_name[c.full_name.lower()] = c

    matched_b_ids: set[str] = set()

    for a_contact in unmatched_a:
        match: Optional[Contact] = None
        # Try email match first
        for e in a_contact.emails:
            match = b_by_email.get(e.value.lower())
            if match:
                break
        # Fallback to display name
        if match is None and a_contact.full_name:
            match = b_by_name.get(a_contact.full_name.lower())

        if match and match.provider_id not in matched_b_ids:
            matched_b_ids.add(match.provider_id)
            mapping = ops.create_mapping(
                s, pair.id, a_contact.provider_id, match.provider_id
            )
            _merge_contact(prov_a, prov_b, a_book_id, b_book_id, mapping,
                            a_contact, match, s, summary)
        else:
            # Phase 5: new on A → create on B
            try:
                new_id = prov_b.create_contact(b_book_id, a_contact)
                mapping = ops.create_mapping(
                    s, pair.id, a_contact.provider_id, new_id
                )
                ops.save_snapshot(s, mapping.id, a_contact)
                summary.created += 1
            except Exception as e:
                log.error("create on B failed for %s: %s", a_contact.full_name, e)
                summary.errors += 1

    # Phase 5: new on B → create on A
    for b_contact in unmatched_b:
        if b_contact.provider_id in matched_b_ids:
            continue
        try:
            new_id = prov_a.create_contact(a_book_id, b_contact)
            mapping = ops.create_mapping(
                s, pair.id, new_id, b_contact.provider_id
            )
            ops.save_snapshot(s, mapping.id, b_contact)
            summary.created += 1
        except Exception as e:
            log.error("create on A failed for %s: %s", b_contact.full_name, e)
            summary.errors += 1


def _incremental_sync(
    prov_a: ContactProvider,
    prov_b: ContactProvider,
    a_book_id: str,
    b_book_id: str,
    pair,
    a_changes: Optional[ChangeSet],
    b_changes: Optional[ChangeSet],
    a_all: Optional[list[Contact]],
    b_all: Optional[list[Contact]],
    existing_mappings: list,
    s: Session,
    summary: SyncSummary,
) -> None:
    a_changed_ids: set[str] = set()
    b_changed_ids: set[str] = set()
    a_destroyed: set[str] = set()
    b_destroyed: set[str] = set()

    if a_changes:
        a_changed_ids = set(a_changes.created + a_changes.updated)
        a_destroyed = set(a_changes.destroyed)
    if b_changes:
        b_changed_ids = set(b_changes.created + b_changes.updated)
        b_destroyed = set(b_changes.destroyed)

    # Handle deletions (Phase 6)
    for mapping in existing_mappings:
        if mapping.a_contact_id in a_destroyed:
            try:
                prov_b.delete_contact(b_book_id, mapping.b_contact_id)
                summary.deleted += 1
            except Exception as e:
                log.error("delete on B failed: %s", e)
                summary.errors += 1
            ops.delete_mapping(s, mapping)
            continue
        if mapping.b_contact_id in b_destroyed:
            try:
                prov_a.delete_contact(a_book_id, mapping.a_contact_id)
                summary.deleted += 1
            except Exception as e:
                log.error("delete on A failed: %s", e)
                summary.errors += 1
            ops.delete_mapping(s, mapping)
            continue

    # Collect all contacts that need merging
    ids_to_fetch_a: list[str] = []
    ids_to_fetch_b: list[str] = []
    mappings_to_merge: list = []

    for mapping in existing_mappings:
        if mapping.a_contact_id in a_destroyed or mapping.b_contact_id in b_destroyed:
            continue  # already handled above
        if mapping.a_contact_id in a_changed_ids or mapping.b_contact_id in b_changed_ids:
            ids_to_fetch_a.append(mapping.a_contact_id)
            ids_to_fetch_b.append(mapping.b_contact_id)
            mappings_to_merge.append(mapping)

    # Fetch current state of changed contacts
    a_fetched = {c.provider_id: c for c in prov_a.get_contacts(a_book_id, ids_to_fetch_a)}
    b_fetched = {c.provider_id: c for c in prov_b.get_contacts(b_book_id, ids_to_fetch_b)}

    # Phase 4: merge
    for mapping in mappings_to_merge:
        curr_a = a_fetched.get(mapping.a_contact_id)
        curr_b = b_fetched.get(mapping.b_contact_id)
        if curr_a and curr_b:
            _merge_contact(prov_a, prov_b, a_book_id, b_book_id, mapping,
                            curr_a, curr_b, s, summary)

    # Phase 5: new contacts (in change sets but no mapping)
    mapped_a = {m.a_contact_id for m in existing_mappings}
    mapped_b = {m.b_contact_id for m in existing_mappings}

    new_on_a = [cid for cid in a_changed_ids if cid not in mapped_a and cid not in a_destroyed]
    new_on_b = [cid for cid in b_changed_ids if cid not in mapped_b and cid not in b_destroyed]

    if new_on_a:
        new_contacts_a = prov_a.get_contacts(a_book_id, new_on_a)
        for contact in new_contacts_a:
            try:
                new_id = prov_b.create_contact(b_book_id, contact)
                mapping = ops.create_mapping(s, pair.id, contact.provider_id, new_id)
                ops.save_snapshot(s, mapping.id, contact)
                summary.created += 1
            except Exception as e:
                log.error("create on B failed for %s: %s", contact.full_name, e)
                summary.errors += 1

    if new_on_b:
        new_contacts_b = prov_b.get_contacts(b_book_id, new_on_b)
        for contact in new_contacts_b:
            try:
                new_id = prov_a.create_contact(a_book_id, contact)
                mapping = ops.create_mapping(s, pair.id, new_id, contact.provider_id)
                ops.save_snapshot(s, mapping.id, contact)
                summary.created += 1
            except Exception as e:
                log.error("create on A failed for %s: %s", contact.full_name, e)
                summary.errors += 1


def _merge_contact(
    prov_a: ContactProvider,
    prov_b: ContactProvider,
    a_book_id: str,
    b_book_id: str,
    mapping,
    curr_a: Contact,
    curr_b: Contact,
    s: Session,
    summary: SyncSummary,
) -> None:
    """Field-level merge for one contact that exists on both sides."""
    snapshot = ops.get_snapshot(s, mapping.id)

    if snapshot is not None:
        prev = ops.load_snapshot_contact(snapshot)
    else:
        # No prior snapshot — treat as first sync for this contact.
        # Use timestamp tiebreaker for every field.
        prev = None

    merged = Contact(provider_id=curr_a.provider_id)
    merged_changed_vs_a = False
    merged_changed_vs_b = False

    for field_name in MERGE_FIELDS:
        val_a = getattr(curr_a, field_name)
        val_b = getattr(curr_b, field_name)
        val_prev = getattr(prev, field_name) if prev else None

        a_changed = (val_a != val_prev) if prev else True
        b_changed = (val_b != val_prev) if prev else True

        if a_changed and not b_changed:
            setattr(merged, field_name, val_a)
            if val_a != val_b:
                merged_changed_vs_b = True
        elif b_changed and not a_changed:
            setattr(merged, field_name, val_b)
            if val_b != val_a:
                merged_changed_vs_a = True
        elif a_changed and b_changed:
            if val_a == val_b:
                setattr(merged, field_name, val_a)
            else:
                # Conflict: same field changed on both sides
                winner = _pick_winner(curr_a, curr_b)
                if winner == "a":
                    setattr(merged, field_name, val_a)
                    merged_changed_vs_b = True
                else:
                    setattr(merged, field_name, val_b)
                    merged_changed_vs_a = True
                log.warning(
                    "conflict field=%s contact=%r kept=%s (%s vs %s)",
                    field_name,
                    curr_a.full_name or curr_a.provider_id,
                    winner,
                    curr_a.updated_at,
                    curr_b.updated_at,
                )
                summary.conflicts += 1
        else:
            # Neither changed (or both same as prev)
            setattr(merged, field_name, val_a)

    # Push updates where needed
    if merged_changed_vs_a:
        try:
            merged_for_a = _copy_with_id(merged, curr_a.provider_id)
            prov_a.update_contact(a_book_id, merged_for_a)
            summary.updated += 1
        except Exception as e:
            log.error("update on A failed: %s", e)
            summary.errors += 1

    if merged_changed_vs_b:
        try:
            merged_for_b = _copy_with_id(merged, curr_b.provider_id)
            prov_b.update_contact(b_book_id, merged_for_b)
            summary.updated += 1
        except Exception as e:
            log.error("update on B failed: %s", e)
            summary.errors += 1

    # Save snapshot with merged state
    merged.updated_at = max(
        curr_a.updated_at or datetime.min,
        curr_b.updated_at or datetime.min,
    ) if (curr_a.updated_at or curr_b.updated_at) else None
    ops.save_snapshot(s, mapping.id, merged)


def _pick_winner(a: Contact, b: Contact) -> str:
    """Last-write-wins by updated_at. Falls back to 'a' if no timestamps."""
    ts_a = a.updated_at
    ts_b = b.updated_at
    if ts_a and ts_b:
        return "a" if ts_a >= ts_b else "b"
    if ts_a and not ts_b:
        return "a"
    if ts_b and not ts_a:
        return "b"
    return "a"  # arbitrary fallback


def _copy_with_id(contact: Contact, provider_id: str) -> Contact:
    """Return a copy of contact with a different provider_id."""
    from dataclasses import replace
    return replace(contact, provider_id=provider_id)
