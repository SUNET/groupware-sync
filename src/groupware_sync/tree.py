"""Recursive three-way Merkle tree comparison for two-way sync.

Compares two SyncNode trees (from providers A and B) against the stored
state DB, producing a list of SyncOp operations that the engine will execute.

Key behaviors:
  - PRUNE: if the combined Merkle hash of both sides matches the stored hash,
    the entire subtree is skipped.
  - Container matching by name (case-insensitive).
  - Leaf matching by stored ItemMapping.
  - Fingerprint comparison against stored per-side fingerprints.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from sqlalchemy.orm import Session

from groupware_sync.models import (
    ItemType,
    NodeType,
    OpType,
    SyncNode,
    SyncOp,
)
from groupware_sync.state import ops


def _combine_merkle(hash_a: Optional[str], hash_b: Optional[str]) -> str:
    """Combine two Merkle hashes into a single stored hash."""
    raw = f"{hash_a or ''}|{hash_b or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def compare_trees(
    node_a: SyncNode,
    node_b: SyncNode,
    provider_a_name: str,
    provider_b_name: str,
    item_type: ItemType,
    session: Session,
) -> list[SyncOp]:
    """Entry point: compare two provider trees and return sync operations."""
    return _compare_node(
        node_a, node_b, provider_a_name, provider_b_name, item_type, session
    )


def _compare_node(
    node_a: Optional[SyncNode],
    node_b: Optional[SyncNode],
    prov_a: str,
    prov_b: str,
    item_type: ItemType,
    session: Session,
) -> list[SyncOp]:
    """Recursively compare two nodes and produce sync operations."""
    result: list[SyncOp] = []

    # --- One side only ---
    if node_a is not None and node_b is None:
        return _collect_one_side(node_a, "b", item_type, prov_a, prov_b, session)
    if node_b is not None and node_a is None:
        return _collect_one_side(node_b, "a", item_type, prov_b, prov_a, session)
    if node_a is None and node_b is None:
        return result

    # Both exist — must both be containers at the top level of recursion
    if node_a is None or node_b is None:
        raise RuntimeError("expected both nodes to be non-None at this point")

    # --- Look up stored pair ---
    stored_pair = ops.get_pair(
        session, prov_a, node_a.node_id, prov_b, node_b.node_id
    )

    # --- PRUNE check ---
    if stored_pair is not None:
        stored_combined = _combine_merkle(
            node_a.merkle_hash, node_b.merkle_hash
        )
        if stored_pair.merkle_hash == stored_combined:
            result.append(
                SyncOp(
                    op_type=OpType.SKIP_SUBTREE,
                    target_side="both",
                    node_id=node_a.node_id,
                    paired_node_id=node_b.node_id,
                    item_type=item_type,
                )
            )
            return result

    # --- Get or create the pair ---
    pair = ops.get_or_create_pair(
        session,
        item_type.value,
        prov_a,
        node_a.node_id,
        prov_b,
        node_b.node_id,
        node_a.name,
    )

    # --- Partition children ---
    containers_a = {
        c.name.lower(): c
        for c in node_a.children
        if c.node_type == NodeType.CONTAINER
    }
    containers_b = {
        c.name.lower(): c
        for c in node_b.children
        if c.node_type == NodeType.CONTAINER
    }
    leaves_a = {
        c.node_id: c
        for c in node_a.children
        if c.node_type == NodeType.LEAF
    }
    leaves_b = {
        c.node_id: c
        for c in node_b.children
        if c.node_type == NodeType.LEAF
    }

    # --- Match containers by name (case-insensitive) ---
    all_container_names = set(containers_a.keys()) | set(containers_b.keys())
    for cname in sorted(all_container_names):
        ca = containers_a.get(cname)
        cb = containers_b.get(cname)
        if ca is not None and cb is not None:
            # Both sides have this container — recurse
            result.extend(
                _compare_node(ca, cb, prov_a, prov_b, item_type, session)
            )
        elif ca is not None and cb is None:
            # Container only on A — create on B, then recurse its children
            result.append(
                SyncOp(
                    op_type=OpType.CREATE_CONTAINER,
                    target_side="b",
                    node_id=ca.node_id,
                    container_name=ca.name,
                    container_id_a=node_a.node_id,
                    item_type=item_type,
                )
            )
            result.extend(
                _collect_one_side(ca, "b", item_type, prov_a, prov_b, session)
            )
        elif cb is not None and ca is None:
            # Container only on B — create on A
            result.append(
                SyncOp(
                    op_type=OpType.CREATE_CONTAINER,
                    target_side="a",
                    node_id=cb.node_id,
                    container_name=cb.name,
                    container_id_b=node_b.node_id,
                    item_type=item_type,
                )
            )
            result.extend(
                _collect_one_side(cb, "a", item_type, prov_b, prov_a, session)
            )

    # --- Match leaves via stored ItemMappings ---
    existing_mappings = ops.get_all_mappings(session, pair.id)

    # Build lookup indexes
    mapped_a_ids: set[str] = set()
    mapped_b_ids: set[str] = set()

    for mapping in existing_mappings:
        a_id = mapping.a_item_id
        b_id = mapping.b_item_id
        leaf_a = leaves_a.get(a_id)
        leaf_b = leaves_b.get(b_id)

        if leaf_a is not None and leaf_b is not None:
            # Both present — check fingerprints
            mapped_a_ids.add(a_id)
            mapped_b_ids.add(b_id)
            fp_changed_a = (
                mapping.fingerprint_a is not None
                and leaf_a.fingerprint != mapping.fingerprint_a
            )
            fp_changed_b = (
                mapping.fingerprint_b is not None
                and leaf_b.fingerprint != mapping.fingerprint_b
            )
            if fp_changed_a or fp_changed_b:
                result.append(
                    SyncOp(
                        op_type=OpType.MERGE_ITEM,
                        target_side="both",
                        node_id=a_id,
                        paired_node_id=b_id,
                        container_id_a=node_a.node_id,
                        container_id_b=node_b.node_id,
                        item_type=item_type,
                    )
                )
        elif leaf_a is not None and leaf_b is None:
            # Gone from B → delete on A (propagate B's deletion)
            mapped_a_ids.add(a_id)
            mapped_b_ids.add(b_id)
            result.append(
                SyncOp(
                    op_type=OpType.DELETE_ITEM,
                    target_side="a",
                    node_id=a_id,          # target item (on A)
                    paired_node_id=b_id,   # was on B
                    container_id_a=node_a.node_id,
                    container_id_b=node_b.node_id,
                    item_type=item_type,
                )
            )
        elif leaf_b is not None and leaf_a is None:
            # Gone from A → delete on B (propagate A's deletion)
            mapped_a_ids.add(a_id)
            mapped_b_ids.add(b_id)
            result.append(
                SyncOp(
                    op_type=OpType.DELETE_ITEM,
                    target_side="b",
                    node_id=b_id,          # target item (on B)
                    paired_node_id=a_id,   # was on A
                    container_id_a=node_a.node_id,
                    container_id_b=node_b.node_id,
                    item_type=item_type,
                )
            )
        else:
            # Both gone — cleanup mapping
            mapped_a_ids.add(a_id)
            mapped_b_ids.add(b_id)
            result.append(
                SyncOp(
                    op_type=OpType.DELETE_ITEM,
                    target_side="both",
                    node_id=a_id,
                    paired_node_id=b_id,
                    container_id_a=node_a.node_id,
                    container_id_b=node_b.node_id,
                    item_type=item_type,
                )
            )
            ops.delete_mapping(session, mapping)

    # --- Unmatched leaves ---
    # First, pair up leaves that exist on both sides by node_id (initial sync).
    # These already exist on both providers, so we create a mapping and check
    # fingerprints rather than generating CREATE ops.
    unmatched_a = {lid for lid in leaves_a if lid not in mapped_a_ids}
    unmatched_b = {lid for lid in leaves_b if lid not in mapped_b_ids}
    paired_by_id = unmatched_a & unmatched_b

    for leaf_id in paired_by_id:
        leaf_a = leaves_a[leaf_id]
        leaf_b = leaves_b[leaf_id]
        # Create a mapping to record that these items are paired
        ops.create_mapping(
            session,
            pair.id,
            leaf_id,
            leaf_id,
            # Placeholder: IP-9 will wire the real identity_key from
            # SyncOp.identity_key. Using leaf_id keeps uniqueness within
            # a pair so the uq_mapping_identity constraint is satisfied.
            identity_key=f"legacy:{leaf_id}",
            fingerprint_a=leaf_a.fingerprint,
            fingerprint_b=leaf_b.fingerprint,
        )

    # Leaves only on A (no match on B) → CREATE_ITEM on B
    for leaf_id in sorted(unmatched_a - paired_by_id):
        result.append(
            SyncOp(
                op_type=OpType.CREATE_ITEM,
                target_side="b",
                node_id=leaf_id,
                container_id_a=node_a.node_id,
                container_id_b=node_b.node_id,
                item_type=item_type,
            )
        )
    # Leaves only on B (no match on A) → CREATE_ITEM on A
    for leaf_id in sorted(unmatched_b - paired_by_id):
        result.append(
            SyncOp(
                op_type=OpType.CREATE_ITEM,
                target_side="a",
                node_id=leaf_id,
                container_id_a=node_a.node_id,
                container_id_b=node_b.node_id,
                item_type=item_type,
            )
        )

    # --- Update stored Merkle hash ---
    combined = _combine_merkle(node_a.merkle_hash, node_b.merkle_hash)
    ops.update_merkle(session, pair.id, combined)

    return result


def _collect_one_side(
    node: SyncNode,
    create_on_side: str,
    item_type: ItemType,
    source_prov: str,
    target_prov: str,
    session: Session,
) -> list[SyncOp]:
    """Generate CREATE ops for all descendants of a node that exists on only one side."""
    result: list[SyncOp] = []

    for child in node.children:
        if child.node_type == NodeType.CONTAINER:
            result.append(
                SyncOp(
                    op_type=OpType.CREATE_CONTAINER,
                    target_side=create_on_side,
                    node_id=child.node_id,
                    container_name=child.name,
                    item_type=item_type,
                )
            )
            result.extend(
                _collect_one_side(
                    child, create_on_side, item_type, source_prov, target_prov, session
                )
            )
        elif child.node_type == NodeType.LEAF:
            result.append(
                SyncOp(
                    op_type=OpType.CREATE_ITEM,
                    target_side=create_on_side,
                    node_id=child.node_id,
                    item_type=item_type,
                )
            )

    return result
