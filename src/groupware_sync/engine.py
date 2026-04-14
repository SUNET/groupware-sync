"""Sync engine orchestrator — the integration layer.

Builds trees from both providers, computes Merkle hashes, compares them
to produce SyncOps, then executes those operations (creates, merges,
deletes) while maintaining the state DB.
"""
from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict
from typing import Optional

from sqlalchemy.orm import Session

from groupware_sync.merge import merge_item
from groupware_sync.models import (
    ItemType,
    OpType,
    SyncItem,
    SyncOp,
    SyncSummary,
    TypeSpec,
)
from groupware_sync.provider import SyncProvider
from groupware_sync.state import ops
from groupware_sync.tree import compare_trees

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_trees(
    provider_a: SyncProvider,
    provider_b: SyncProvider,
    item_type: ItemType,
    type_spec: TypeSpec,
    session: Session,
) -> SyncSummary:
    """Run a full two-way tree sync between two providers.

    Phases:
      1. Build trees from both providers (IDs + fingerprints only).
      2. Compute Merkle hashes bottom-up.
      3. Compare trees against stored state -> list of SyncOps.
      4. Execute operations (creates, merges, deletes).

    Returns a SyncSummary with counts of each operation type.
    """
    summary = SyncSummary()

    # Phase 1 — Build trees
    log.info("Phase 1: building trees from %s and %s", provider_a.name, provider_b.name)
    try:
        tree_a = provider_a.build_tree(item_type)
    except Exception:
        log.exception("Failed to build tree from %s", provider_a.name)
        summary.errors += 1
        return summary

    try:
        tree_b = provider_b.build_tree(item_type)
    except Exception:
        log.exception("Failed to build tree from %s", provider_b.name)
        summary.errors += 1
        return summary

    # Phase 2 — Compute Merkle hashes
    log.info("Phase 2: computing Merkle hashes")
    tree_a.compute_merkle()
    tree_b.compute_merkle()

    # Phase 3 — Compare trees
    log.info("Phase 3: comparing trees")
    operations = compare_trees(
        tree_a, tree_b, provider_a.name, provider_b.name, item_type, session
    )
    log.info("Phase 3 produced %d operations", len(operations))

    # Phase 4 — Execute operations
    log.info("Phase 4: executing operations")
    _execute_ops(
        operations, provider_a, provider_b, item_type, type_spec, session, summary
    )

    session.commit()
    log.info(
        "Sync complete: created=%d updated=%d deleted=%d conflicts=%d "
        "skipped=%d errors=%d",
        summary.created,
        summary.updated,
        summary.deleted,
        summary.conflicts,
        summary.skipped,
        summary.errors,
    )
    return summary


# ---------------------------------------------------------------------------
# Operation dispatcher
# ---------------------------------------------------------------------------


def _execute_ops(
    operations: list[SyncOp],
    provider_a: SyncProvider,
    provider_b: SyncProvider,
    item_type: ItemType,
    type_spec: TypeSpec,
    session: Session,
    summary: SyncSummary,
) -> None:
    """Dispatch operations in the correct order.

    Order:
      1. Container creates  (CREATE_CONTAINER)
      2. Merges             (MERGE_ITEM)
      3. Item creates       (CREATE_ITEM)
      4. Item deletes       (DELETE_ITEM)
      5. Container deletes  (DELETE_CONTAINER)
      6. Count skips        (SKIP_SUBTREE)
    """
    container_creates: list[SyncOp] = []
    merges: list[SyncOp] = []
    creates: list[SyncOp] = []
    deletes: list[SyncOp] = []
    container_deletes: list[SyncOp] = []
    skips: list[SyncOp] = []

    for op in operations:
        if op.op_type == OpType.CREATE_CONTAINER:
            container_creates.append(op)
        elif op.op_type == OpType.MERGE_ITEM:
            merges.append(op)
        elif op.op_type == OpType.CREATE_ITEM:
            creates.append(op)
        elif op.op_type == OpType.DELETE_ITEM:
            deletes.append(op)
        elif op.op_type == OpType.DELETE_CONTAINER:
            container_deletes.append(op)
        elif op.op_type == OpType.SKIP_SUBTREE:
            skips.append(op)

    # 1. Container creates
    for op in container_creates:
        _execute_container_create(op, provider_a, provider_b, session, summary)

    # 2. Merges (batch-fetch and three-way merge)
    if merges:
        _execute_merges(
            merges, provider_a, provider_b, item_type, type_spec, session, summary
        )

    # 3. Item creates (with identity matching)
    if creates:
        _execute_creates(
            creates, provider_a, provider_b, item_type, type_spec, session, summary
        )

    # 4. Item deletes
    for op in deletes:
        _execute_delete(op, provider_a, provider_b, session, summary)

    # 5. Container deletes
    for op in container_deletes:
        _execute_container_delete(op, provider_a, provider_b, session, summary)

    # 6. Count skips
    summary.skipped += len(skips)


# ---------------------------------------------------------------------------
# Container operations
# ---------------------------------------------------------------------------


def _execute_container_create(
    op: SyncOp,
    provider_a: SyncProvider,
    provider_b: SyncProvider,
    session: Session,
    summary: SyncSummary,
) -> None:
    """Create a container on the target side."""
    target = provider_b if op.target_side == "b" else provider_a
    name = op.container_name or op.node_id
    try:
        new_id = target.create_container(name)
        log.info("Created container %r on %s -> %s", name, target.name, new_id)
        summary.containers += 1
    except Exception:
        log.exception("Failed to create container %r on %s", name, target.name)
        summary.errors += 1


def _execute_container_delete(
    op: SyncOp,
    provider_a: SyncProvider,
    provider_b: SyncProvider,
    session: Session,
    summary: SyncSummary,
) -> None:
    """Delete a container on the target side."""
    target = provider_b if op.target_side == "b" else provider_a
    try:
        target.delete_container(op.node_id)
        log.info("Deleted container %s on %s", op.node_id, target.name)
    except Exception:
        log.exception(
            "Failed to delete container %s on %s", op.node_id, target.name
        )
        summary.errors += 1


# ---------------------------------------------------------------------------
# Merge operations
# ---------------------------------------------------------------------------


def _execute_merges(
    merge_ops: list[SyncOp],
    provider_a: SyncProvider,
    provider_b: SyncProvider,
    item_type: ItemType,
    type_spec: TypeSpec,
    session: Session,
    summary: SyncSummary,
) -> None:
    """Batch-fetch items from both sides and run three-way merges.

    Groups merge ops by container pair for efficient batch fetching.
    """
    # Group by (container_id_a, container_id_b)
    groups: dict[tuple[Optional[str], Optional[str]], list[SyncOp]] = defaultdict(list)
    for op in merge_ops:
        key = (op.container_id_a, op.container_id_b)
        groups[key].append(op)

    for (cid_a, cid_b), group_ops in groups.items():
        a_ids = [op.node_id for op in group_ops]
        b_ids = [op.paired_node_id for op in group_ops if op.paired_node_id]

        # Batch-fetch from both providers
        items_a: dict[str, SyncItem] = {}
        items_b: dict[str, SyncItem] = {}

        if cid_a and a_ids:
            try:
                fetched = provider_a.get_items(cid_a, a_ids)
                items_a = {item.provider_id: item for item in fetched}
            except Exception:
                log.exception(
                    "Failed to fetch items from %s container %s",
                    provider_a.name,
                    cid_a,
                )
                summary.errors += len(a_ids)
                continue

        if cid_b and b_ids:
            try:
                fetched = provider_b.get_items(cid_b, b_ids)
                items_b = {item.provider_id: item for item in fetched}
            except Exception:
                log.exception(
                    "Failed to fetch items from %s container %s",
                    provider_b.name,
                    cid_b,
                )
                summary.errors += len(b_ids)
                continue

        # Process each merge op
        for op in group_ops:
            _merge_one(
                op,
                items_a,
                items_b,
                provider_a,
                provider_b,
                cid_a,
                cid_b,
                type_spec,
                session,
                summary,
            )


def _merge_one(
    op: SyncOp,
    items_a: dict[str, SyncItem],
    items_b: dict[str, SyncItem],
    provider_a: SyncProvider,
    provider_b: SyncProvider,
    cid_a: Optional[str],
    cid_b: Optional[str],
    type_spec: TypeSpec,
    session: Session,
    summary: SyncSummary,
) -> None:
    """Merge a single item pair and push updates to changed sides."""
    a_id = op.node_id
    b_id = op.paired_node_id
    if not b_id:
        log.warning("Merge op missing paired_node_id for %s", a_id)
        summary.errors += 1
        return

    item_a = items_a.get(a_id)
    item_b = items_b.get(b_id)

    if item_a is None or item_b is None:
        log.warning(
            "Could not fetch one or both items for merge: a=%s (%s) b=%s (%s)",
            a_id,
            "found" if item_a else "missing",
            b_id,
            "found" if item_b else "missing",
        )
        summary.errors += 1
        return

    # Look up the mapping to find the snapshot
    pair = ops.get_pair(
        session, provider_a.name, cid_a or "", provider_b.name, cid_b or ""
    )
    if pair is None:
        log.warning("No NodePair found for containers %s / %s", cid_a, cid_b)
        summary.errors += 1
        return

    mapping = ops.get_mapping_by_a(session, pair.id, a_id)
    if mapping is None:
        log.warning("No mapping found for item %s in pair %d", a_id, pair.id)
        summary.errors += 1
        return

    # Load snapshot (may be None on first merge after initial pairing)
    snapshot: Optional[SyncItem] = None
    snap_row = ops.get_snapshot(session, mapping.id)
    if snap_row is not None:
        snapshot = ops.load_snapshot_item(snap_row)

    # Three-way merge
    merged, changed_vs_a, changed_vs_b, conflict_count = merge_item(
        item_a, item_b, snapshot, type_spec
    )
    summary.conflicts += conflict_count

    # Push updates to sides that changed
    if changed_vs_a and cid_a:
        merged_for_a = dataclasses.replace(merged, provider_id=a_id)
        try:
            provider_a.update_item(cid_a, merged_for_a)
            log.debug("Updated item %s on %s", a_id, provider_a.name)
        except Exception:
            log.exception("Failed to update %s on %s", a_id, provider_a.name)
            summary.errors += 1

    if changed_vs_b and cid_b:
        merged_for_b = dataclasses.replace(merged, provider_id=b_id)
        try:
            provider_b.update_item(cid_b, merged_for_b)
            log.debug("Updated item %s on %s", b_id, provider_b.name)
        except Exception:
            log.exception("Failed to update %s on %s", b_id, provider_b.name)
            summary.errors += 1

    # Update stored fingerprints and snapshot
    ops.update_fingerprints(
        session,
        mapping,
        fingerprint_a=item_a.fingerprint,
        fingerprint_b=item_b.fingerprint,
    )
    ops.save_snapshot(session, mapping.id, merged)

    if changed_vs_a or changed_vs_b:
        summary.updated += 1


# ---------------------------------------------------------------------------
# Create operations (with identity matching)
# ---------------------------------------------------------------------------


def _execute_creates(
    create_ops: list[SyncOp],
    provider_a: SyncProvider,
    provider_b: SyncProvider,
    item_type: ItemType,
    type_spec: TypeSpec,
    session: Session,
    summary: SyncSummary,
) -> None:
    """Execute item creates with identity matching between unmatched sides.

    Separates ops by target_side, fetches full data from source providers,
    runs identity matching between unmatched A and B items, then creates
    or merges as appropriate.
    """
    # Separate by target side
    # target_side="b" means item exists on A, needs to be created on B
    # target_side="a" means item exists on B, needs to be created on A
    create_on_b: list[SyncOp] = []  # items from A
    create_on_a: list[SyncOp] = []  # items from B

    for op in create_ops:
        if op.target_side == "b":
            create_on_b.append(op)
        elif op.target_side == "a":
            create_on_a.append(op)

    # Group by container for batch fetching
    # For create_on_b: fetch from provider_a (source)
    a_by_container: dict[Optional[str], list[SyncOp]] = defaultdict(list)
    for op in create_on_b:
        a_by_container[op.container_id_a].append(op)

    # For create_on_a: fetch from provider_b (source)
    b_by_container: dict[Optional[str], list[SyncOp]] = defaultdict(list)
    for op in create_on_a:
        b_by_container[op.container_id_b].append(op)

    # Fetch full items from A
    a_items: dict[str, tuple[SyncItem, SyncOp]] = {}
    for cid, ops_group in a_by_container.items():
        if cid is None:
            continue
        ids = [op.node_id for op in ops_group]
        try:
            fetched = provider_a.get_items(cid, ids)
            fetched_map = {item.provider_id: item for item in fetched}
            for op in ops_group:
                item = fetched_map.get(op.node_id)
                if item:
                    a_items[op.node_id] = (item, op)
        except Exception:
            log.exception(
                "Failed to fetch items from %s container %s",
                provider_a.name,
                cid,
            )
            summary.errors += len(ids)

    # Fetch full items from B
    b_items: dict[str, tuple[SyncItem, SyncOp]] = {}
    for cid, ops_group in b_by_container.items():
        if cid is None:
            continue
        ids = [op.node_id for op in ops_group]
        try:
            fetched = provider_b.get_items(cid, ids)
            fetched_map = {item.provider_id: item for item in fetched}
            for op in ops_group:
                item = fetched_map.get(op.node_id)
                if item:
                    b_items[op.node_id] = (item, op)
        except Exception:
            log.exception(
                "Failed to fetch items from %s container %s",
                provider_b.name,
                cid,
            )
            summary.errors += len(ids)

    # Identity matching between unmatched A and B items
    a_item_list = [(iid, item) for iid, (item, _) in a_items.items()]
    b_item_list = [(iid, item) for iid, (item, _) in b_items.items()]
    matched, only_a, only_b = _identity_match(a_item_list, b_item_list, type_spec)

    # Process matched pairs (first-sync merge)
    for a_id, b_id, item_a, item_b in matched:
        op_a = a_items[a_id][1]
        op_b = b_items[b_id][1]

        # Three-way merge with no snapshot (first sync)
        merged, changed_vs_a, changed_vs_b, conflict_count = merge_item(
            item_a, item_b, None, type_spec
        )
        summary.conflicts += conflict_count

        # Push updates if needed
        if changed_vs_a and op_a.container_id_a:
            merged_for_a = dataclasses.replace(merged, provider_id=a_id)
            try:
                provider_a.update_item(op_a.container_id_a, merged_for_a)
            except Exception:
                log.exception("Failed to update matched %s on %s", a_id, provider_a.name)
                summary.errors += 1

        if changed_vs_b and op_b.container_id_b:
            merged_for_b = dataclasses.replace(merged, provider_id=b_id)
            try:
                provider_b.update_item(op_b.container_id_b, merged_for_b)
            except Exception:
                log.exception("Failed to update matched %s on %s", b_id, provider_b.name)
                summary.errors += 1

        # Create mapping + snapshot
        pair = ops.get_or_create_pair(
            session,
            item_type.value,
            provider_a.name,
            op_a.container_id_a or "",
            provider_b.name,
            op_b.container_id_b or "",
            "",
        )
        mapping = ops.create_mapping(
            session,
            pair.id,
            a_id,
            b_id,
            fingerprint_a=item_a.fingerprint,
            fingerprint_b=item_b.fingerprint,
        )
        ops.save_snapshot(session, mapping.id, merged)
        summary.updated += 1
        log.info("Identity-matched %s <-> %s", a_id, b_id)

    # Process unmatched A items: create on B
    for a_id in only_a:
        item_a, op = a_items[a_id]
        cid_b = op.container_id_b
        if not cid_b:
            log.warning("No target container_id_b for creating %s on %s", a_id, provider_b.name)
            summary.errors += 1
            continue

        try:
            new_b_id = provider_b.create_item(cid_b, item_a)
            log.info("Created item %s on %s -> %s", a_id, provider_b.name, new_b_id)
        except Exception:
            log.exception("Failed to create %s on %s", a_id, provider_b.name)
            summary.errors += 1
            continue

        # Create mapping + snapshot
        pair = ops.get_or_create_pair(
            session,
            item_type.value,
            provider_a.name,
            op.container_id_a or "",
            provider_b.name,
            cid_b,
            "",
        )
        mapping = ops.create_mapping(
            session,
            pair.id,
            a_id,
            new_b_id,
            fingerprint_a=item_a.fingerprint,
        )
        ops.save_snapshot(session, mapping.id, item_a)
        summary.created += 1

    # Process unmatched B items: create on A
    for b_id in only_b:
        item_b, op = b_items[b_id]
        cid_a = op.container_id_a
        if not cid_a:
            log.warning("No target container_id_a for creating %s on %s", b_id, provider_a.name)
            summary.errors += 1
            continue

        try:
            new_a_id = provider_a.create_item(cid_a, item_b)
            log.info("Created item %s on %s -> %s", b_id, provider_a.name, new_a_id)
        except Exception:
            log.exception("Failed to create %s on %s", b_id, provider_a.name)
            summary.errors += 1
            continue

        # Create mapping + snapshot
        pair = ops.get_or_create_pair(
            session,
            item_type.value,
            provider_a.name,
            cid_a,
            provider_b.name,
            op.container_id_b or "",
            "",
        )
        mapping = ops.create_mapping(
            session,
            pair.id,
            new_a_id,
            b_id,
            fingerprint_b=item_b.fingerprint,
        )
        ops.save_snapshot(session, mapping.id, item_b)
        summary.created += 1


# ---------------------------------------------------------------------------
# Delete operations
# ---------------------------------------------------------------------------


def _execute_delete(
    op: SyncOp,
    provider_a: SyncProvider,
    provider_b: SyncProvider,
    session: Session,
    summary: SyncSummary,
) -> None:
    """Delete an item on the target side.

    For target_side='both', the item was deleted from both providers
    already (the mapping cleanup was done by compare_trees).
    For one-side deletes, we propagate the deletion.
    """
    if op.target_side == "both":
        # Both sides already deleted; mapping was cleaned in compare_trees
        summary.deleted += 1
        return

    # Determine which provider to delete from and the container ID
    if op.target_side == "b":
        target = provider_b
        item_id = op.paired_node_id or op.node_id
        cid = op.container_id_b
    else:
        target = provider_a
        item_id = op.paired_node_id or op.node_id
        cid = op.container_id_a

    if not cid:
        log.warning(
            "No container ID for deleting %s on %s", item_id, target.name
        )
        summary.errors += 1
        return

    try:
        target.delete_item(cid, item_id)
        log.info("Deleted item %s on %s", item_id, target.name)
        summary.deleted += 1
    except Exception:
        log.exception("Failed to delete %s on %s", item_id, target.name)
        summary.errors += 1


# ---------------------------------------------------------------------------
# Identity matching
# ---------------------------------------------------------------------------


def _identity_match(
    a_items: list[tuple[str, SyncItem]],
    b_items: list[tuple[str, SyncItem]],
    type_spec: TypeSpec,
) -> tuple[
    list[tuple[str, str, SyncItem, SyncItem]],
    list[str],
    list[str],
]:
    """Match items from A and B by identity fields (e.g., email, full_name).

    Args:
        a_items: List of (id, SyncItem) from provider A.
        b_items: List of (id, SyncItem) from provider B.
        type_spec: Type specification with identity_fields.

    Returns:
        (matched_pairs, only_a_ids, only_b_ids)
        matched_pairs: list of (a_id, b_id, item_a, item_b)
        only_a_ids: list of unmatched A item IDs
        only_b_ids: list of unmatched B item IDs
    """
    if not type_spec.identity_fields:
        # No identity fields configured — no matching possible
        return (
            [],
            [aid for aid, _ in a_items],
            [bid for bid, _ in b_items],
        )

    # Build index from B items by identity keys
    # Each identity key maps to (b_id, item_b)
    b_index: dict[str, tuple[str, SyncItem]] = {}
    for b_id, item_b in b_items:
        for key in _extract_identity_keys(item_b, type_spec.identity_fields):
            if key and key not in b_index:
                b_index[key] = (b_id, item_b)

    matched: list[tuple[str, str, SyncItem, SyncItem]] = []
    matched_a_ids: set[str] = set()
    matched_b_ids: set[str] = set()

    for a_id, item_a in a_items:
        if a_id in matched_a_ids:
            continue
        for key in _extract_identity_keys(item_a, type_spec.identity_fields):
            if not key:
                continue
            if key in b_index:
                b_id, item_b = b_index[key]
                if b_id not in matched_b_ids:
                    matched.append((a_id, b_id, item_a, item_b))
                    matched_a_ids.add(a_id)
                    matched_b_ids.add(b_id)
                    break

    only_a = [aid for aid, _ in a_items if aid not in matched_a_ids]
    only_b = [bid for bid, _ in b_items if bid not in matched_b_ids]
    return matched, only_a, only_b


def _extract_identity_keys(
    item: SyncItem, identity_fields: list[str]
) -> list[str]:
    """Extract lowercased identity keys from an item's fields.

    For list-type fields (e.g., emails), each element becomes a separate key.
    For scalar fields, the value itself is the key.
    """
    keys: list[str] = []
    for field_name in identity_fields:
        value = item.fields.get(field_name)
        if value is None:
            continue
        if isinstance(value, list):
            for v in value:
                text = _identity_key_text(v)
                if text:
                    keys.append(text.lower())
        else:
            text = _identity_key_text(value)
            if text:
                keys.append(text.lower())
    return keys


def _identity_key_text(value: object) -> str:
    """Convert a field value to a string for identity matching.

    Handles dicts with a 'value' key (e.g., email objects like
    {"type": "work", "value": "user@example.com"}).
    """
    if isinstance(value, dict):
        return str(value.get("value", ""))
    return str(value)
