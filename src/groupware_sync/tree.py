"""Recursive three-way Merkle tree comparison for two-way sync.

Compares two SyncNode trees (from providers A and B) against the stored
state DB, producing a list of SyncOp operations that the engine will execute.

Key behaviors:
  - PRUNE: if the combined Merkle hash of both sides matches the stored hash,
    the entire subtree is skipped.
  - Container matching by name (case-insensitive).
  - Leaf matching by identity_key (stable cross-provider hash).
  - Fingerprint comparison against stored per-side fingerprints.
  - Safety invariant: no DELETE_ITEM emitted on runs with no cache.
"""
from __future__ import annotations

import hashlib
import logging
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

log = logging.getLogger(__name__)


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
) -> tuple[list[SyncOp], int]:
    """Entry point: compare two provider trees, return (ops, healed_count)."""
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
) -> tuple[list[SyncOp], int]:
    """Recursively compare two nodes and produce sync operations."""
    result: list[SyncOp] = []
    healed = 0

    # --- One side only ---
    if node_a is not None and node_b is None:
        return _collect_one_side(node_a, "b", item_type, prov_a, prov_b, session), 0
    if node_b is not None and node_a is None:
        return _collect_one_side(node_b, "a", item_type, prov_b, prov_a, session), 0
    if node_a is None and node_b is None:
        return result, 0

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
            return result, healed

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
            child_ops, child_healed = _compare_node(
                ca, cb, prov_a, prov_b, item_type, session
            )
            result.extend(child_ops)
            healed += child_healed
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

    # --- Identity-based leaf pairing ---
    # Ops from this level's leaves are collected separately so the safety
    # invariant (no deletes on a run with no prior cache for THIS pair) can
    # filter them without affecting ops recursed from child containers.
    leaf_ops: list[SyncOp] = []

    # Route leaves into "paired by identity" vs "unpairable" buckets.
    # Collisions within a container (two leaves with the same identity_key)
    # are demoted to unpairable: picking "first wins" silently drops the
    # extra leaf, which would miss deletes/merges. Treating all colliders
    # as unpairable means they take the create-only path, which is safe.
    def _bucket(leaves: dict, side: str) -> tuple[dict[str, SyncNode], list[SyncNode]]:
        by_identity: dict[str, SyncNode] = {}
        collisions: dict[str, list[SyncNode]] = {}
        for leaf in leaves.values():
            if not leaf.identity_key:
                continue
            key = leaf.identity_key
            if key in by_identity:
                collisions.setdefault(key, [by_identity[key]]).append(leaf)
            else:
                by_identity[key] = leaf
        unpairable: list[SyncNode] = [
            leaf for leaf in leaves.values() if not leaf.identity_key
        ]
        for key, group in collisions.items():
            log.warning(
                "side %s: identity_key %s shared by %d leaves (%s) — "
                "demoting to unpairable",
                side, key, len(group), [leaf.node_id for leaf in group],
            )
            by_identity.pop(key, None)
            unpairable.extend(group)
        return by_identity, unpairable

    by_identity_a, unpairable_a = _bucket(leaves_a, "a")
    by_identity_b, unpairable_b = _bucket(leaves_b, "b")

    cached = ops.get_mappings_by_identity(session, pair.id)
    had_cache = bool(cached)

    if leaves_a or leaves_b:
        paired_keys = set(by_identity_a) & set(by_identity_b)
        log.info(
            "pair %r: a=%d (keyed=%d, unpairable=%d), "
            "b=%d (keyed=%d, unpairable=%d), paired_by_identity=%d, cache=%d",
            node_a.name,
            len(leaves_a), len(by_identity_a), len(unpairable_a),
            len(leaves_b), len(by_identity_b), len(unpairable_b),
            len(paired_keys), len(cached),
        )

    all_keys = set(by_identity_a) | set(by_identity_b) | set(cached)
    for key in sorted(all_keys):
        leaf_a = by_identity_a.get(key)
        leaf_b = by_identity_b.get(key)
        mapping = cached.get(key)

        if leaf_a is not None and leaf_b is not None:
            # Both sides present — pair (merge if drift, silently heal IDs if changed).
            if mapping is None:
                ops.create_mapping(
                    session, pair.id,
                    leaf_a.node_id, leaf_b.node_id,
                    identity_key=key,
                    fingerprint_a=leaf_a.fingerprint,
                    fingerprint_b=leaf_b.fingerprint,
                )
                continue

            if (mapping.a_item_id != leaf_a.node_id
                    or mapping.b_item_id != leaf_b.node_id):
                # Capture before heal_mapping_ids mutates the row, so the
                # log line shows old→new rather than new→new.
                old_a_id, old_b_id = mapping.a_item_id, mapping.b_item_id
                ops.heal_mapping_ids(
                    session, mapping, leaf_a.node_id, leaf_b.node_id
                )
                healed += 1
                log.debug(
                    "healed mapping identity=%s: %s,%s -> %s,%s",
                    key, old_a_id, old_b_id,
                    leaf_a.node_id, leaf_b.node_id,
                )

            fp_changed_a = (
                mapping.fingerprint_a is not None
                and leaf_a.fingerprint != mapping.fingerprint_a
            )
            fp_changed_b = (
                mapping.fingerprint_b is not None
                and leaf_b.fingerprint != mapping.fingerprint_b
            )
            if fp_changed_a or fp_changed_b:
                leaf_ops.append(
                    SyncOp(
                        op_type=OpType.MERGE_ITEM,
                        target_side="both",
                        node_id=leaf_a.node_id,
                        paired_node_id=leaf_b.node_id,
                        container_id_a=node_a.node_id,
                        container_id_b=node_b.node_id,
                        item_type=item_type,
                        identity_key=key,
                    )
                )
            continue

        if leaf_a is not None and leaf_b is None:
            # Only on A.
            if mapping is None:
                # Never seen before — new item on A → create on B.
                leaf_ops.append(
                    SyncOp(
                        op_type=OpType.CREATE_ITEM,
                        target_side="b",
                        node_id=leaf_a.node_id,
                        container_id_a=node_a.node_id,
                        container_id_b=node_b.node_id,
                        item_type=item_type,
                        identity_key=key,
                    )
                )
            else:
                # We saw it before and it's gone from B → propagate delete to A.
                leaf_ops.append(
                    SyncOp(
                        op_type=OpType.DELETE_ITEM,
                        target_side="a",
                        node_id=leaf_a.node_id,
                        paired_node_id=mapping.b_item_id,
                        container_id_a=node_a.node_id,
                        container_id_b=node_b.node_id,
                        item_type=item_type,
                        identity_key=key,
                    )
                )
            continue

        if leaf_b is not None and leaf_a is None:
            # Symmetric.
            if mapping is None:
                leaf_ops.append(
                    SyncOp(
                        op_type=OpType.CREATE_ITEM,
                        target_side="a",
                        node_id=leaf_b.node_id,
                        container_id_a=node_a.node_id,
                        container_id_b=node_b.node_id,
                        item_type=item_type,
                        identity_key=key,
                    )
                )
            else:
                leaf_ops.append(
                    SyncOp(
                        op_type=OpType.DELETE_ITEM,
                        target_side="b",
                        node_id=leaf_b.node_id,
                        paired_node_id=mapping.a_item_id,
                        container_id_a=node_a.node_id,
                        container_id_b=node_b.node_id,
                        item_type=item_type,
                        identity_key=key,
                    )
                )
            continue

        # Neither side has it, but cache does → both-gone cleanup.
        if mapping is not None:
            leaf_ops.append(
                SyncOp(
                    op_type=OpType.DELETE_ITEM,
                    target_side="both",
                    node_id=mapping.a_item_id,
                    paired_node_id=mapping.b_item_id,
                    container_id_a=node_a.node_id,
                    container_id_b=node_b.node_id,
                    item_type=item_type,
                    identity_key=key,
                )
            )
            ops.delete_mapping(session, mapping)

    # Unpairable leaves: always create on the other side. Legacy identity
    # matching in _execute_creates catches duplicates during execution.
    for leaf in unpairable_a:
        leaf_ops.append(
            SyncOp(
                op_type=OpType.CREATE_ITEM,
                target_side="b",
                node_id=leaf.node_id,
                container_id_a=node_a.node_id,
                container_id_b=node_b.node_id,
                item_type=item_type,
                identity_key=None,
            )
        )
    for leaf in unpairable_b:
        leaf_ops.append(
            SyncOp(
                op_type=OpType.CREATE_ITEM,
                target_side="a",
                node_id=leaf.node_id,
                container_id_a=node_a.node_id,
                container_id_b=node_b.node_id,
                item_type=item_type,
                identity_key=None,
            )
        )

    # --- Safety invariant: no deletes on a run with no prior cache. ---
    # Scoped to THIS pair's leaf ops only; child-container ops are unaffected.
    if not had_cache:
        pre_count = sum(
            1 for op in leaf_ops if op.op_type == OpType.DELETE_ITEM
        )
        if pre_count:
            log.warning(
                "no cache present for pair %s — suppressing %d planned deletes "
                "(cache being rebuilt this run)", pair.id, pre_count,
            )
            leaf_ops = [op for op in leaf_ops if op.op_type != OpType.DELETE_ITEM]

    result.extend(leaf_ops)

    # --- Update stored Merkle hash ---
    combined = _combine_merkle(node_a.merkle_hash, node_b.merkle_hash)
    ops.update_merkle(session, pair.id, combined)

    return result, healed


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
