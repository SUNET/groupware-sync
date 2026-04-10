"""ContactProvider protocol — the abstraction both JMAP and Graph implement.

The sync engine operates exclusively through this protocol. It never knows
which side is JMAP and which is Graph.
"""
from __future__ import annotations

from typing import Optional, Protocol

from groupware_sync_contacts.models import Addressbook, ChangeSet, Contact


class ContactProvider(Protocol):
    """Interface for a contacts backend (Stalwart JMAP or Microsoft Graph)."""

    @property
    def name(self) -> str:
        """Short identifier for this provider instance, e.g. 'stalwart' or 'm365'."""
        ...

    def list_addressbooks(self) -> list[Addressbook]:
        ...

    def create_addressbook(self, name: str) -> Addressbook:
        ...

    def get_all_contacts(self, addressbook_id: str) -> list[Contact]:
        ...

    def get_changes(self, addressbook_id: str, cursor: str) -> Optional[ChangeSet]:
        """Return changes since cursor, or None if cursor is stale/unsupported."""
        ...

    def get_contacts(self, addressbook_id: str, ids: list[str]) -> list[Contact]:
        ...

    def create_contact(self, addressbook_id: str, contact: Contact) -> str:
        """Create a contact, return its new provider_id."""
        ...

    def update_contact(self, addressbook_id: str, contact: Contact) -> None:
        """Update an existing contact. contact.provider_id identifies which."""
        ...

    def delete_contact(self, addressbook_id: str, provider_id: str) -> None:
        ...
