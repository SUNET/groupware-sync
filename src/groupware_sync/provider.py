"""SyncProvider — abstract base class for all protocol adapters.

Use-case packages (contacts, calendar, mail) subclass this to provide
protocol-specific tree building, item fetching, and CRUD operations.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from groupware_sync.models import ChangeSet, ItemType, SyncItem, SyncNode


class SyncProvider(ABC):
    """Abstract base class for a groupware backend."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'stalwart' or 'm365'."""
        ...

    @abstractmethod
    def build_tree(self, item_type: ItemType) -> SyncNode:
        """Build a tree of containers and leaf metadata (IDs + fingerprints).

        Does NOT fetch full item data — only IDs and fingerprints.
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
    def create_item(self, container_id: str, item: SyncItem) -> str:
        """Create an item, return its provider ID."""
        ...

    @abstractmethod
    def update_item(self, container_id: str, item: SyncItem) -> None:
        """Update an existing item. item.provider_id identifies which."""
        ...

    @abstractmethod
    def delete_item(self, container_id: str, item_id: str) -> None:
        ...

    def close(self) -> None:
        """Clean up resources (e.g. httpx client). Default: no-op."""
        pass
