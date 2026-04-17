"""SyncProvider — abstract base class for all protocol adapters.

Use-case packages (contacts, calendar, mail) subclass this to provide
protocol-specific tree building, item fetching, and CRUD operations.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from groupware_sync.models import ChangeSet, ItemType, SyncItem, SyncNode


class NotificationCapability(Enum):
    """Per-op-type capability for suppressing scheduling/notification traffic."""

    SUPPRESSED = "suppressed"
    BEST_EFFORT = "best-effort"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class NotificationPolicy:
    """An adapter's declared suppression capability for each write op."""

    create_item: NotificationCapability
    update_item: NotificationCapability
    delete_item: NotificationCapability
    delete_container: NotificationCapability


class SyncProvider(ABC):
    """Abstract base class for a groupware backend."""

    # Required class attribute: declare what suppression the adapter provides.
    # Adapters MUST override this; no default is provided because a silent
    # default of SUPPRESSED would hide real gaps.
    notification_policy: NotificationPolicy

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'stalwart' or 'm365'."""
        ...

    @abstractmethod
    def build_tree(
        self,
        item_type: ItemType,
        known_states: Optional[dict[str, tuple[str, str]]] = None,
    ) -> SyncNode:
        """Build a tree of containers and leaf metadata (IDs + fingerprints).

        Does NOT fetch full item data — only IDs and fingerprints.

        known_states: optional dict of container_id → (state_cursor, merkle_hash).
        If the protocol supports a state indicator and it matches the stored
        cursor for a container, the adapter SHOULD return that container with
        skipped=True and merkle_hash pre-set from the stored value, skipping
        the child fetch entirely.
        """
        ...

    @abstractmethod
    def get_items(self, container_id: str, ids: list[str]) -> list[SyncItem]:
        """Batch-fetch full item data for the given IDs."""
        ...

    def get_changes(
        self, container_id: str, cursor: str
    ) -> Optional[ChangeSet]:
        """Incremental changes since cursor. None if stale/unsupported.

        Default: not supported (returns None). Subclasses override if the
        protocol supports incremental change detection.
        """
        return None

    @abstractmethod
    def create_container(
        self, name: str, parent_id: Optional[str] = None
    ) -> str:
        """Create a container, return its provider ID."""
        ...

    @abstractmethod
    def delete_container(self, container_id: str) -> None:
        ...

    @abstractmethod
    def create_item(self, container_id: str, item: SyncItem) -> tuple[str, str]:
        """Create an item. Returns (new_provider_id, server_fingerprint).

        The fingerprint is whatever the server assigned (timestamp, etag,
        modseq) — captured from the write response so we can store the
        exact value the server will report on the next read.
        """
        ...

    @abstractmethod
    def update_item(self, container_id: str, item: SyncItem) -> str:
        """Update an existing item. item.provider_id identifies which.

        Returns the server-assigned fingerprint from the write response.
        """
        ...

    @abstractmethod
    def delete_item(self, container_id: str, item_id: str) -> None:
        ...

    def close(self) -> None:
        """Clean up resources (e.g. httpx client). Default: no-op."""
        pass
