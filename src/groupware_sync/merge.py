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
) -> tuple[SyncItem, bool, bool, int, dict[str, list[str]]]:
    """Merge two versions of an item using field-level strategies.

    Args:
        item_a: Item from side A.
        item_b: Item from side B.
        snapshot: Last-known common state (None on first sync).
        type_spec: Field definitions with merge strategies.

    Returns:
        (merged_item, changed_vs_a, changed_vs_b, conflict_count, drift)
        - changed_vs_a: True if merged result differs from item_a
        - changed_vs_b: True if merged result differs from item_b
        - conflict_count: number of fields that required last-write-wins arbitration
        - drift: {"a": [field names that diverge from item_a],
                  "b": [field names that diverge from item_b]}
    """
    merged_fields: dict[str, Any] = {}
    conflict_count = 0
    drift_a: list[str] = []
    drift_b: list[str] = []

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
            merged_val = val_a if val_a is not None else val_b
            merged_fields[fname] = merged_val
            if merged_val != val_a:
                drift_a.append(fname)
            if merged_val != val_b:
                drift_b.append(fname)
            continue

        if strategy == MergeStrategy.SET:
            if no_snapshot:
                a_changed = True
                b_changed = True
            else:
                a_changed = val_a != val_prev
                b_changed = val_b != val_prev

            result, chg_vs_a, chg_vs_b = _merge_set(
                val_a, val_b, val_prev, a_changed, b_changed,
            )
            merged_fields[fname] = result
            if chg_vs_a:
                drift_a.append(fname)
            if chg_vs_b:
                drift_b.append(fname)
            continue

        # SCALAR strategy
        if no_snapshot:
            a_changed = True
            b_changed = True
        else:
            a_changed = val_a != val_prev
            b_changed = val_b != val_prev

        if not a_changed and not b_changed:
            merged_val = val_prev
        elif a_changed and not b_changed:
            merged_val = val_a
        elif b_changed and not a_changed:
            merged_val = val_b
        elif val_a == val_b:
            merged_val = val_a
        else:
            conflict_count += 1
            winner = _pick_winner(item_a, item_b)
            merged_val = val_a if winner == "a" else val_b

        merged_fields[fname] = merged_val
        if merged_val != val_a:
            drift_a.append(fname)
        if merged_val != val_b:
            drift_b.append(fname)

    merged_updated_at = _max_timestamp(item_a.updated_at, item_b.updated_at)
    merged = SyncItem(
        provider_id=item_a.provider_id,
        item_type=item_a.item_type,
        fields=merged_fields,
        updated_at=merged_updated_at,
    )

    return (
        merged,
        bool(drift_a),
        bool(drift_b),
        conflict_count,
        {"a": drift_a, "b": drift_b},
    )
