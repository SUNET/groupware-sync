"""CRUD operations for the tree-based sync framework state DB.

All functions take an explicit SQLAlchemy Session as their first argument
so that callers control transaction boundaries.
"""
import json
import time
from typing import Optional

from sqlalchemy.orm import Session

from groupware_sync.models import SyncItem
from groupware_sync.state.db import ItemMapping, ItemSnapshot, NodePair, SyncCursor

# ---------------------------------------------------------------------------
# NodePair helpers
# ---------------------------------------------------------------------------


def get_or_create_pair(
    s: Session,
    item_type: str,
    a_provider: str,
    a_node_id: str,
    b_provider: str,
    b_node_id: str,
    name: str,
) -> NodePair:
    """Return existing NodePair or create a new one (idempotent)."""
    pair = get_pair(s, a_provider, a_node_id, b_provider, b_node_id)
    if pair is None:
        pair = NodePair(
            item_type=item_type,
            a_provider=a_provider,
            a_node_id=a_node_id,
            b_provider=b_provider,
            b_node_id=b_node_id,
            name=name,
        )
        s.add(pair)
        s.flush()
    return pair


def get_pair(
    s: Session,
    a_provider: str,
    a_node_id: str,
    b_provider: str,
    b_node_id: str,
) -> Optional[NodePair]:
    """Look up a NodePair by its four key fields. Returns None if not found."""
    return (
        s.query(NodePair)
        .filter_by(
            a_provider=a_provider,
            a_node_id=a_node_id,
            b_provider=b_provider,
            b_node_id=b_node_id,
        )
        .first()
    )


def update_merkle(s: Session, pair_id: int, merkle_hash: str) -> None:
    """Update the merkle_hash on a NodePair."""
    pair = s.get(NodePair, pair_id)
    if pair is not None:
        pair.merkle_hash = merkle_hash


# ---------------------------------------------------------------------------
# ItemMapping helpers
# ---------------------------------------------------------------------------


def get_all_mappings(s: Session, pair_id: int) -> list[ItemMapping]:
    """Return all ItemMappings for a pair."""
    return s.query(ItemMapping).filter_by(pair_id=pair_id).all()


def get_mapping_by_a(
    s: Session, pair_id: int, a_item_id: str
) -> Optional[ItemMapping]:
    """Look up a mapping by provider-A item ID. Returns None if not found."""
    return (
        s.query(ItemMapping)
        .filter_by(pair_id=pair_id, a_item_id=a_item_id)
        .first()
    )


def get_mapping_by_b(
    s: Session, pair_id: int, b_item_id: str
) -> Optional[ItemMapping]:
    """Look up a mapping by provider-B item ID. Returns None if not found."""
    return (
        s.query(ItemMapping)
        .filter_by(pair_id=pair_id, b_item_id=b_item_id)
        .first()
    )


def create_mapping(
    s: Session,
    pair_id: int,
    a_item_id: str,
    b_item_id: str,
    identity_key: str,
    fingerprint_a: Optional[str] = None,
    fingerprint_b: Optional[str] = None,
) -> ItemMapping:
    """Create and persist a new ItemMapping keyed on identity."""
    mapping = ItemMapping(
        pair_id=pair_id,
        identity_key=identity_key,
        a_item_id=a_item_id,
        b_item_id=b_item_id,
        fingerprint_a=fingerprint_a,
        fingerprint_b=fingerprint_b,
    )
    s.add(mapping)
    s.flush()
    return mapping


def get_mappings_by_identity(
    s: Session, pair_id: int,
) -> dict[str, ItemMapping]:
    """Return all ItemMappings for a pair keyed by identity_key."""
    rows = s.query(ItemMapping).filter_by(pair_id=pair_id).all()
    return {m.identity_key: m for m in rows if m.identity_key is not None}


def get_mapping_by_identity(
    s: Session, pair_id: int, identity_key: str,
) -> Optional[ItemMapping]:
    """Look up a mapping by (pair_id, identity_key)."""
    return (
        s.query(ItemMapping)
        .filter_by(pair_id=pair_id, identity_key=identity_key)
        .first()
    )


def heal_mapping_ids(
    s: Session,
    mapping: ItemMapping,
    new_a_item_id: str,
    new_b_item_id: str,
) -> None:
    """Update provider IDs on a mapping when they've drifted. Identity stable."""
    mapping.a_item_id = new_a_item_id
    mapping.b_item_id = new_b_item_id


def update_fingerprints(
    s: Session,
    mapping: ItemMapping,
    fingerprint_a: Optional[str] = None,
    fingerprint_b: Optional[str] = None,
) -> None:
    """Update fingerprints on an existing mapping (in-place)."""
    if fingerprint_a is not None:
        mapping.fingerprint_a = fingerprint_a
    if fingerprint_b is not None:
        mapping.fingerprint_b = fingerprint_b


def delete_mapping(s: Session, mapping: ItemMapping) -> None:
    """Delete a mapping (cascade deletes the associated snapshot)."""
    s.delete(mapping)


# ---------------------------------------------------------------------------
# ItemSnapshot helpers
# ---------------------------------------------------------------------------


def get_snapshot(s: Session, mapping_id: int) -> Optional[ItemSnapshot]:
    """Return the snapshot for a mapping, or None if it doesn't exist."""
    return s.query(ItemSnapshot).filter_by(mapping_id=mapping_id).first()


def save_snapshot(s: Session, mapping_id: int, item: SyncItem) -> ItemSnapshot:
    """Create or update the snapshot for a mapping (upsert)."""
    snap = get_snapshot(s, mapping_id)
    fields_json = json.dumps(item.to_dict())
    now = int(time.time())
    if snap is None:
        snap = ItemSnapshot(
            mapping_id=mapping_id,
            fields_json=fields_json,
            synced_at=now,
        )
        s.add(snap)
    else:
        snap.fields_json = fields_json
        snap.synced_at = now
    s.flush()
    return snap


def load_snapshot_item(snapshot: ItemSnapshot) -> SyncItem:
    """Deserialize an ItemSnapshot back into a SyncItem."""
    d = json.loads(snapshot.fields_json)
    return SyncItem.from_dict(d)


# ---------------------------------------------------------------------------
# SyncCursor helpers
# ---------------------------------------------------------------------------


def get_cursor(s: Session, pair_id: int, provider: str) -> Optional[str]:
    """Return the cursor string for a (pair, provider), or None."""
    row = s.query(SyncCursor).filter_by(pair_id=pair_id, provider=provider).first()
    return row.cursor if row is not None else None


def save_cursor(s: Session, pair_id: int, provider: str, cursor: str) -> SyncCursor:
    """Create or update the cursor for a (pair, provider)."""
    row = s.query(SyncCursor).filter_by(pair_id=pair_id, provider=provider).first()
    if row is None:
        row = SyncCursor(pair_id=pair_id, provider=provider, cursor=cursor)
        s.add(row)
    else:
        row.cursor = cursor
    s.flush()
    return row


def get_known_states(s: Session, provider: str) -> dict[str, tuple[str, str]]:
    """Return all stored (cursor, merkle_hash) for a provider's containers.

    Returns dict of container_id → (state_cursor, merkle_hash).
    The engine passes this to build_tree so adapters can skip unchanged
    containers without fetching their children.
    """
    results: dict[str, tuple[str, str]] = {}
    rows = (
        s.query(NodePair, SyncCursor)
        .join(SyncCursor, SyncCursor.pair_id == NodePair.id)
        .filter(SyncCursor.provider == provider)
        .all()
    )
    for pair, cursor in rows:
        container_id = (
            pair.a_node_id if pair.a_provider == provider else pair.b_node_id
        )
        if pair.merkle_hash:
            results[container_id] = (cursor.cursor, pair.merkle_hash)
    return results
