"""End-to-end sync tests: CardDAV (alice) <-> CardDAV (bob) via Radicale.

Each test starts with empty addressbooks on both sides (cleaned by the
``_clean_addressbooks`` autouse fixture in conftest) and a fresh state DB.

Run with:  pytest tests/e2e/ -m e2e -v
"""
from __future__ import annotations

import pytest

from groupware_sync.engine import sync_trees
from groupware_sync.models import ItemType, SyncItem
from groupware_sync_contacts.adapters.carddav_adapter import CardDavContactAdapter
from groupware_sync_contacts.specs import CONTACT_SPEC
from tests.e2e.conftest import radicale_available

pytestmark = [pytest.mark.e2e, radicale_available]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_contact(name: str, email: str, **extra: object) -> SyncItem:
    """Build a SyncItem for a contact with sensible defaults."""
    fields: dict[str, object] = {
        "full_name": name,
        "given_name": name.split()[0] if " " in name else name,
        "surname": name.split()[-1] if " " in name else "",
        "emails": [{"label": "work", "value": email}],
    }
    fields.update(extra)
    return SyncItem(provider_id="", item_type=ItemType.CONTACT, fields=fields)


def _list_contacts(adapter: CardDavContactAdapter) -> list[SyncItem]:
    """Fetch all contacts from the adapter's first addressbook."""
    tree = adapter.build_tree(ItemType.CONTACT)
    items: list[SyncItem] = []
    for container in tree.children:
        leaf_ids = [child.node_id for child in container.children]
        if leaf_ids:
            items.extend(adapter.get_items(container.node_id, leaf_ids))
    return items


def _count_contacts(adapter: CardDavContactAdapter) -> int:
    """Return the number of contacts visible to the adapter."""
    tree = adapter.build_tree(ItemType.CONTACT)
    return sum(len(c.children) for c in tree.children)


def _find_contact_by_email(
    contacts: list[SyncItem], email: str
) -> SyncItem | None:
    """Find a contact whose emails list contains *email*."""
    for c in contacts:
        for e in c.fields.get("emails", []):
            if e.get("value") == email:
                return c
    return None


def _first_container_id(adapter: CardDavContactAdapter) -> str:
    """Return the node_id of the adapter's first addressbook container."""
    tree = adapter.build_tree(ItemType.CONTACT)
    assert tree.children, "adapter has no addressbook containers"
    return tree.children[0].node_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInitialSync:
    """Initial sync creates contacts on both sides."""

    def test_initial_sync_creates_contacts_on_both_sides(
        self,
        alice_adapter: CardDavContactAdapter,
        bob_adapter: CardDavContactAdapter,
        alice_book_href: str,
        bob_book_href: str,
        state_session,
    ) -> None:
        # Seed alice with 2 contacts
        alice_cid = _first_container_id(alice_adapter)
        alice_adapter.create_item(
            alice_cid, _make_contact("Alice One", "alice1@example.com")
        )
        alice_adapter.create_item(
            alice_cid, _make_contact("Alice Two", "alice2@example.com")
        )

        # Seed bob with 1 different contact
        bob_cid = _first_container_id(bob_adapter)
        bob_adapter.create_item(
            bob_cid, _make_contact("Bob Only", "bob@example.com")
        )

        assert _count_contacts(alice_adapter) == 2
        assert _count_contacts(bob_adapter) == 1

        # Run sync
        summary = sync_trees(
            alice_adapter, bob_adapter,
            ItemType.CONTACT, CONTACT_SPEC, state_session,
        )

        assert summary.errors == 0

        # Both sides should now have 3 contacts
        assert _count_contacts(alice_adapter) == 3
        assert _count_contacts(bob_adapter) == 3

        # Verify specific contacts landed on both sides
        alice_contacts = _list_contacts(alice_adapter)
        bob_contacts = _list_contacts(bob_adapter)

        assert _find_contact_by_email(alice_contacts, "bob@example.com") is not None
        assert _find_contact_by_email(bob_contacts, "alice1@example.com") is not None
        assert _find_contact_by_email(bob_contacts, "alice2@example.com") is not None


class TestModificationPropagation:
    """Modifications propagate across a sync."""

    def test_modification_propagates(
        self,
        alice_adapter: CardDavContactAdapter,
        bob_adapter: CardDavContactAdapter,
        state_session,
    ) -> None:
        # Seed and sync
        alice_cid = _first_container_id(alice_adapter)
        alice_adapter.create_item(
            alice_cid,
            _make_contact("Shared Person", "shared@example.com", organization="OldCorp"),
        )

        summary = sync_trees(
            alice_adapter, bob_adapter,
            ItemType.CONTACT, CONTACT_SPEC, state_session,
        )
        assert summary.errors == 0
        assert _count_contacts(bob_adapter) == 1

        # Modify on alice's side: change organization
        alice_contacts = _list_contacts(alice_adapter)
        target = _find_contact_by_email(alice_contacts, "shared@example.com")
        assert target is not None

        target.fields["organization"] = "NewCorp"
        alice_adapter.update_item(alice_cid, target)

        # Re-sync
        summary = sync_trees(
            alice_adapter, bob_adapter,
            ItemType.CONTACT, CONTACT_SPEC, state_session,
        )
        assert summary.errors == 0

        # Verify bob sees the updated organization
        bob_contacts = _list_contacts(bob_adapter)
        bob_target = _find_contact_by_email(bob_contacts, "shared@example.com")
        assert bob_target is not None
        assert bob_target.fields.get("organization") == "NewCorp"


class TestDeletionPropagation:
    """Deletions propagate across a sync."""

    def test_deletion_propagates(
        self,
        alice_adapter: CardDavContactAdapter,
        bob_adapter: CardDavContactAdapter,
        state_session,
    ) -> None:
        # Seed and sync
        alice_cid = _first_container_id(alice_adapter)
        alice_adapter.create_item(
            alice_cid, _make_contact("Will Stay", "stay@example.com"),
        )
        alice_adapter.create_item(
            alice_cid, _make_contact("Will Go", "go@example.com"),
        )

        summary = sync_trees(
            alice_adapter, bob_adapter,
            ItemType.CONTACT, CONTACT_SPEC, state_session,
        )
        assert summary.errors == 0
        assert _count_contacts(alice_adapter) == 2
        assert _count_contacts(bob_adapter) == 2

        # Delete "Will Go" on alice's side
        alice_contacts = _list_contacts(alice_adapter)
        to_delete = _find_contact_by_email(alice_contacts, "go@example.com")
        assert to_delete is not None
        alice_adapter.delete_item(alice_cid, to_delete.provider_id)
        assert _count_contacts(alice_adapter) == 1

        # Re-sync
        summary = sync_trees(
            alice_adapter, bob_adapter,
            ItemType.CONTACT, CONTACT_SPEC, state_session,
        )
        assert summary.errors == 0

        # Verify bob no longer has "Will Go"
        assert _count_contacts(bob_adapter) == 1
        bob_contacts = _list_contacts(bob_adapter)
        assert _find_contact_by_email(bob_contacts, "go@example.com") is None
        assert _find_contact_by_email(bob_contacts, "stay@example.com") is not None


class TestIdempotency:
    """A second sync with no intervening changes is a no-op."""

    def test_second_sync_is_noop(
        self,
        alice_adapter: CardDavContactAdapter,
        bob_adapter: CardDavContactAdapter,
        state_session,
    ) -> None:
        # Seed and sync
        alice_cid = _first_container_id(alice_adapter)
        alice_adapter.create_item(
            alice_cid, _make_contact("Stable Person", "stable@example.com"),
        )

        summary1 = sync_trees(
            alice_adapter, bob_adapter,
            ItemType.CONTACT, CONTACT_SPEC, state_session,
        )
        assert summary1.errors == 0
        assert summary1.created >= 1

        # Second sync with no changes
        summary2 = sync_trees(
            alice_adapter, bob_adapter,
            ItemType.CONTACT, CONTACT_SPEC, state_session,
        )
        assert summary2.errors == 0
        assert summary2.created == 0
        assert summary2.updated == 0
        assert summary2.deleted == 0


class TestConcurrentEdits:
    """Concurrent edits to different fields merge cleanly."""

    def test_concurrent_edits_merge(
        self,
        alice_adapter: CardDavContactAdapter,
        bob_adapter: CardDavContactAdapter,
        state_session,
    ) -> None:
        # Seed and sync
        alice_cid = _first_container_id(alice_adapter)
        alice_adapter.create_item(
            alice_cid,
            _make_contact(
                "Merge Target", "merge@example.com",
                organization="OrigCorp", job_title="OrigTitle",
            ),
        )

        summary = sync_trees(
            alice_adapter, bob_adapter,
            ItemType.CONTACT, CONTACT_SPEC, state_session,
        )
        assert summary.errors == 0

        # Alice edits organization
        alice_contacts = _list_contacts(alice_adapter)
        alice_target = _find_contact_by_email(alice_contacts, "merge@example.com")
        assert alice_target is not None
        alice_target.fields["organization"] = "AliceCorp"
        alice_adapter.update_item(alice_cid, alice_target)

        # Bob edits job_title
        bob_cid = _first_container_id(bob_adapter)
        bob_contacts = _list_contacts(bob_adapter)
        bob_target = _find_contact_by_email(bob_contacts, "merge@example.com")
        assert bob_target is not None
        bob_target.fields["job_title"] = "BobTitle"
        bob_adapter.update_item(bob_cid, bob_target)

        # Re-sync: field-level merge should preserve both edits
        summary = sync_trees(
            alice_adapter, bob_adapter,
            ItemType.CONTACT, CONTACT_SPEC, state_session,
        )
        assert summary.errors == 0

        # Verify both sides have both edits
        alice_contacts = _list_contacts(alice_adapter)
        merged_alice = _find_contact_by_email(alice_contacts, "merge@example.com")
        assert merged_alice is not None
        assert merged_alice.fields.get("organization") == "AliceCorp"
        assert merged_alice.fields.get("job_title") == "BobTitle"

        bob_contacts = _list_contacts(bob_adapter)
        merged_bob = _find_contact_by_email(bob_contacts, "merge@example.com")
        assert merged_bob is not None
        assert merged_bob.fields.get("organization") == "AliceCorp"
        assert merged_bob.fields.get("job_title") == "BobTitle"
