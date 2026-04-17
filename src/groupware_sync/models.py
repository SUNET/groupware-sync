"""Core data model for the tree-based sync framework.

SyncNode: tree nodes with IDs, fingerprints, and Merkle hashes.
SyncItem: generic item data with dict-based fields.
SyncOp: operations produced by tree comparison.
TypeSpec/FieldDef: per-type merge configuration.
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class NodeType(Enum):
    CONTAINER = "container"
    LEAF = "leaf"


class ItemType(Enum):
    CONTACT = "contact"
    CALENDAR_EVENT = "calendar_event"
    MESSAGE = "message"


class OpType(Enum):
    CREATE_CONTAINER = "create_container"
    DELETE_CONTAINER = "delete_container"
    CREATE_ITEM = "create_item"
    DELETE_ITEM = "delete_item"
    MERGE_ITEM = "merge_item"
    SKIP_SUBTREE = "skip_subtree"


class MergeStrategy(Enum):
    SCALAR = "scalar"
    SET = "set"
    IMMUTABLE = "immutable"
    IGNORE = "ignore"


def compute_identity_key(
    fields: dict,
    identity_fields: list[str],
) -> Optional[str]:
    """Canonical SHA-256 hex digest of the named fields, or None if unusable.

    - Case-insensitive, whitespace-stripped canonicalization.
    - List values: each element is normalized; order-independent.
    - Dict values with a 'value' key: extract the value (for email/phone shapes).
    """
    def canonical(v: object) -> str:
        if isinstance(v, dict):
            v = v.get("value", "")
        # NFC normalize before casefold so visually-identical strings with
        # different Unicode decompositions hash equally. casefold is stricter
        # than lower (handles ß → ss etc). Identity values are opaque
        # identifiers; this is defensive, not free-form text handling.
        return unicodedata.normalize("NFC", str(v)).strip().casefold()

    parts: list[str] = []
    for name in identity_fields:
        val = fields.get(name)
        if val is None:
            continue
        if isinstance(val, list):
            entries = sorted(filter(None, (canonical(v) for v in val)))
            for s in entries:
                parts.append(f"{name}={s}")
        else:
            s = canonical(val)
            if s:
                parts.append(f"{name}={s}")
    if not parts:
        return None
    parts.sort()
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


@dataclass
class SyncNode:
    """A node in the sync tree. Containers have children; leaves don't."""

    node_id: str
    name: str
    node_type: NodeType
    fingerprint: Optional[str] = None
    merkle_hash: Optional[str] = None
    item_type: Optional[ItemType] = None
    children: list[SyncNode] = field(default_factory=list)
    state_cursor: Optional[str] = None  # protocol's state indicator for this container
    skipped: bool = False  # True if children were not fetched (state unchanged)
    identity_key: Optional[str] = None  # cross-provider pairing key for leaves

    def compute_merkle(self) -> str:
        """Compute subtree hash bottom-up.

        Leaves: sha256(node_id | fingerprint).
        Containers: sha256(node_id | sorted child hashes) — includes the
        container's own identity so two containers with the same children
        but different IDs produce different hashes.
        Skipped containers: keep their pre-set merkle_hash (carried from
        stored state — children were not fetched because the protocol's
        state indicator showed nothing changed).
        """
        if self.node_type == NodeType.LEAF:
            raw = f"{self.node_id}|{self.fingerprint or ''}"
            self.merkle_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
            return self.merkle_hash
        if self.skipped and self.merkle_hash is not None:
            return self.merkle_hash
        child_hashes = sorted(c.compute_merkle() for c in self.children)
        raw = f"{self.node_id}|{'|'.join(child_hashes)}"
        self.merkle_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return self.merkle_hash


@dataclass
class SyncItem:
    """Protocol-neutral item representation with generic dict fields."""

    provider_id: str
    item_type: ItemType
    fields: dict[str, Any] = field(default_factory=dict)
    updated_at: Optional[datetime] = None
    fingerprint: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "provider_id": self.provider_id,
            "item_type": self.item_type.value,
            "fields": self.fields,
            "fingerprint": self.fingerprint,
        }
        if self.updated_at:
            d["updated_at"] = self.updated_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> SyncItem:
        updated_at = None
        if d.get("updated_at"):
            updated_at = datetime.fromisoformat(d["updated_at"])
        return cls(
            provider_id=d["provider_id"],
            item_type=ItemType(d["item_type"]),
            fields=d.get("fields", {}),
            updated_at=updated_at,
            fingerprint=d.get("fingerprint"),
        )


@dataclass
class SyncOp:
    """An operation produced by tree comparison, executed later."""

    op_type: OpType
    target_side: str  # "a", "b", or "both"
    node_id: str
    paired_node_id: Optional[str] = None
    container_id_a: Optional[str] = None
    container_id_b: Optional[str] = None
    container_name: Optional[str] = None
    item_type: Optional[ItemType] = None
    identity_key: Optional[str] = None


@dataclass
class FieldDef:
    name: str
    merge_strategy: MergeStrategy = MergeStrategy.SCALAR


@dataclass
class TypeSpec:
    item_type: ItemType
    fields: list[FieldDef] = field(default_factory=list)
    identity_fields: list[str] = field(default_factory=list)
    timestamp_field: str = "updated_at"


@dataclass
class ChangeSet:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)
    new_cursor: str = ""


@dataclass
class SyncSummary:
    containers: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    conflicts: int = 0
    skipped: int = 0
    errors: int = 0
    aborted: bool = False
    identity_pairs_healed: int = 0
