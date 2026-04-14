"""Field-level merge engine for the tree-based sync framework.

Supports SCALAR (three-way), SET (union additions / intersect removals),
IMMUTABLE (keep A), and IGNORE strategies, as configured by TypeSpec.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from groupware_sync.models import MergeStrategy, SyncItem, TypeSpec


def _hashable(v: Any) -> Any:
    """Make a value hashable for set operations. Dicts become JSON strings."""
    if isinstance(v, dict):
        return json.dumps(v, sort_keys=True)
    if isinstance(v, list):
        return tuple(_hashable(i) for i in v)
    return v


def _max_timestamp(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    """Return the later of two optional datetimes."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _pick_winner(item_a: SyncItem, item_b: SyncItem) -> str:
    """Last-write-wins by updated_at. Returns 'a' or 'b'."""
    ts_a = item_a.updated_at
    ts_b = item_b.updated_at
    if ts_a is None and ts_b is None:
        return "a"
    if ts_a is None:
        return "b"
    if ts_b is None:
        return "a"
    return "a" if ts_a >= ts_b else "b"


def _merge_set(
    val_a: Any,
    val_b: Any,
    val_prev: Any,
    a_changed: bool,
    b_changed: bool,
) -> tuple[list, bool, bool]:
    """Merge two set-valued fields.

    Union of additions from each side; intersection of removals.
    Returns (merged_list, changed_vs_a, changed_vs_b).
    """
    prev_set: set = set(_hashable(v) for v in (val_prev or []))
    a_set: set = set(_hashable(v) for v in (val_a or []))
    b_set: set = set(_hashable(v) for v in (val_b or []))

    # Items added by each side (not present in snapshot)
    a_added = a_set - prev_set
    b_added = b_set - prev_set

    # Items removed by each side (present in snapshot but not in their current value)
    a_removed = prev_set - a_set
    b_removed = prev_set - b_set

    # Start from snapshot; add union of additions; remove intersection of removals
    merged_set = (prev_set | a_added | b_added) - (a_removed & b_removed)

    # Reconstruct as a list using original values where possible
    # Map from hashable key back to original value (prefer a, then b, then prev)
    key_to_val: dict[Any, Any] = {}
    for v in (val_prev or []):
        key_to_val[_hashable(v)] = v
    for v in (val_b or []):
        key_to_val[_hashable(v)] = v
    for v in (val_a or []):
        key_to_val[_hashable(v)] = v

    result = [key_to_val[k] for k in merged_set if k in key_to_val]

    changed_vs_a = set(_hashable(v) for v in result) != a_set
    changed_vs_b = set(_hashable(v) for v in result) != b_set
    return result, changed_vs_a, changed_vs_b


def merge_item(
    item_a: SyncItem,
    item_b: SyncItem,
    snapshot: Optional[SyncItem],
    type_spec: TypeSpec,
) -> tuple[SyncItem, bool, bool, int]:
    """Merge two versions of an item using field-level strategies.

    Args:
        item_a: Item from side A.
        item_b: Item from side B.
        snapshot: Last-known common state (None on first sync).
        type_spec: Field definitions with merge strategies.

    Returns:
        (merged_item, changed_vs_a, changed_vs_b, conflict_count)
        - changed_vs_a: True if merged result differs from item_a
        - changed_vs_b: True if merged result differs from item_b
        - conflict_count: number of fields that required last-write-wins arbitration
    """
    merged_fields: dict[str, Any] = {}
    item_changed_vs_a = False
    item_changed_vs_b = False
    conflict_count = 0

    no_snapshot = snapshot is None

    for field_def in type_spec.fields:
        fname = field_def.name
        strategy = field_def.merge_strategy

        if strategy == MergeStrategy.IGNORE:
            continue

        val_a = item_a.fields.get(fname)
        val_b = item_b.fields.get(fname)
        val_prev = snapshot.fields.get(fname) if snapshot is not None else None

        if strategy == MergeStrategy.IMMUTABLE:
            merged_fields[fname] = val_a if val_a is not None else val_b
            continue

        if strategy == MergeStrategy.SET:
            if no_snapshot:
                # Treat as both changed: union everything
                a_changed = True
                b_changed = True
            else:
                a_changed = val_a != val_prev
                b_changed = val_b != val_prev

            result, chg_vs_a, chg_vs_b = _merge_set(val_a, val_b, val_prev, a_changed, b_changed)
            merged_fields[fname] = result
            if chg_vs_a:
                item_changed_vs_a = True
            if chg_vs_b:
                item_changed_vs_b = True
            continue

        # SCALAR strategy
        if no_snapshot:
            a_changed = True
            b_changed = True
        else:
            a_changed = val_a != val_prev
            b_changed = val_b != val_prev

        if not a_changed and not b_changed:
            # Neither changed: keep snapshot value (same as both)
            merged_fields[fname] = val_prev
        elif a_changed and not b_changed:
            # Only A changed: take A
            merged_fields[fname] = val_a
            item_changed_vs_b = True
        elif b_changed and not a_changed:
            # Only B changed: take B
            merged_fields[fname] = val_b
            item_changed_vs_a = True
        else:
            # Both changed
            if val_a == val_b:
                # Same value: no conflict
                merged_fields[fname] = val_a
            else:
                # True conflict: last-write-wins
                conflict_count += 1
                winner = _pick_winner(item_a, item_b)
                merged_fields[fname] = val_a if winner == "a" else val_b

    merged_updated_at = _max_timestamp(item_a.updated_at, item_b.updated_at)
    merged = SyncItem(
        provider_id=item_a.provider_id,
        item_type=item_a.item_type,
        fields=merged_fields,
        updated_at=merged_updated_at,
    )

    # Final check: compare merged fields to each side's full fields
    # (only for fields we actually processed)
    processed_names = {f.name for f in type_spec.fields if f.merge_strategy != MergeStrategy.IGNORE}
    merged_subset_a = {k: item_a.fields.get(k) for k in processed_names}
    merged_subset_b = {k: item_b.fields.get(k) for k in processed_names}
    merged_subset = {k: merged_fields.get(k) for k in processed_names if k in merged_fields}

    changed_vs_a = merged_subset != merged_subset_a
    changed_vs_b = merged_subset != merged_subset_b

    return merged, changed_vs_a, changed_vs_b, conflict_count
