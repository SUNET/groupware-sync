"""End-to-end sync tests: CalDAV (alice) <-> CalDAV (bob) via Radicale.

Each test starts with empty calendar collections on both sides (cleaned by the
``_clean_calendars`` autouse fixture in conftest) and a fresh state DB.

Run with:  pytest tests/e2e/ -m e2e -v
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from groupware_sync.engine import sync_trees
from groupware_sync.models import ItemType, SyncItem
from groupware_sync_calendar.adapters.caldav_adapter import CalDavCalendarAdapter
from groupware_sync_calendar.specs import CALENDAR_EVENT_SPEC
from tests.e2e.conftest import radicale_available

pytestmark = [pytest.mark.e2e, radicale_available]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    summary: str,
    dtstart_utc: str = "2026-06-15T10:00:00Z",
    dtstart_tz: str = "Europe/Stockholm",
    duration_hours: int = 1,
    **extra_fields: object,
) -> SyncItem:
    """Build a SyncItem for a calendar event with sensible defaults."""
    start_dt = datetime.fromisoformat(dtstart_utc.replace("Z", "+00:00"))
    end_dt = start_dt + timedelta(hours=duration_hours)
    dtend_utc = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    fields: dict[str, object] = {
        "uid": str(uuid.uuid4()),
        "summary": summary,
        "dtstart_utc": dtstart_utc,
        "dtstart_tz": dtstart_tz,
        "dtend_utc": dtend_utc,
        "dtend_tz": dtstart_tz,
        "all_day": False,
        "status": "confirmed",
    }
    fields.update(extra_fields)
    return SyncItem(provider_id="", item_type=ItemType.CALENDAR_EVENT, fields=fields)


def _list_events(adapter: CalDavCalendarAdapter) -> list[SyncItem]:
    """Fetch all events from the adapter's first calendar collection."""
    tree = adapter.build_tree(ItemType.CALENDAR_EVENT)
    items: list[SyncItem] = []
    for container in tree.children:
        leaf_ids = [child.node_id for child in container.children]
        if leaf_ids:
            items.extend(adapter.get_items(container.node_id, leaf_ids))
    return items


def _count_events(adapter: CalDavCalendarAdapter) -> int:
    """Return the number of events visible to the adapter."""
    tree = adapter.build_tree(ItemType.CALENDAR_EVENT)
    return sum(len(c.children) for c in tree.children)


def _find_event_by_summary(
    events: list[SyncItem], summary: str
) -> SyncItem | None:
    """Find an event whose summary matches."""
    for e in events:
        if e.fields.get("summary") == summary:
            return e
    return None


def _first_container_id(adapter: CalDavCalendarAdapter) -> str:
    """Return the node_id of the adapter's first calendar container."""
    tree = adapter.build_tree(ItemType.CALENDAR_EVENT)
    assert tree.children, "adapter has no calendar containers"
    return tree.children[0].node_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInitialCalendarSync:
    """Initial sync creates events on both sides."""

    def test_initial_calendar_sync_creates_events(
        self,
        alice_calendar_adapter: CalDavCalendarAdapter,
        bob_calendar_adapter: CalDavCalendarAdapter,
        calendar_state_session,
    ) -> None:
        # Seed alice with 2 events
        alice_cid = _first_container_id(alice_calendar_adapter)
        alice_calendar_adapter.create_item(
            alice_cid, _make_event("Alice Meeting One")
        )
        alice_calendar_adapter.create_item(
            alice_cid, _make_event("Alice Meeting Two")
        )

        assert _count_events(alice_calendar_adapter) == 2
        assert _count_events(bob_calendar_adapter) == 0

        # Run sync
        summary = sync_trees(
            alice_calendar_adapter, bob_calendar_adapter,
            ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, calendar_state_session,
        )

        assert summary.errors == 0

        # Bob should now have alice's 2 events
        assert _count_events(bob_calendar_adapter) == 2

        # Verify specific events landed on bob's side
        bob_events = _list_events(bob_calendar_adapter)
        assert _find_event_by_summary(bob_events, "Alice Meeting One") is not None
        assert _find_event_by_summary(bob_events, "Alice Meeting Two") is not None


class TestEventModificationPropagation:
    """Modifications propagate across a sync."""

    def test_event_modification_propagates(
        self,
        alice_calendar_adapter: CalDavCalendarAdapter,
        bob_calendar_adapter: CalDavCalendarAdapter,
        calendar_state_session,
    ) -> None:
        # Seed and sync
        alice_cid = _first_container_id(alice_calendar_adapter)
        alice_calendar_adapter.create_item(
            alice_cid,
            _make_event("Original Title", location="Room A"),
        )

        summary = sync_trees(
            alice_calendar_adapter, bob_calendar_adapter,
            ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, calendar_state_session,
        )
        assert summary.errors == 0
        assert _count_events(bob_calendar_adapter) == 1

        # Modify summary on alice's side
        alice_events = _list_events(alice_calendar_adapter)
        target = _find_event_by_summary(alice_events, "Original Title")
        assert target is not None

        target.fields["summary"] = "Updated Title"
        alice_calendar_adapter.update_item(alice_cid, target)

        # Re-sync
        summary = sync_trees(
            alice_calendar_adapter, bob_calendar_adapter,
            ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, calendar_state_session,
        )
        assert summary.errors == 0

        # Verify bob sees the updated summary
        bob_events = _list_events(bob_calendar_adapter)
        assert _find_event_by_summary(bob_events, "Updated Title") is not None
        assert _find_event_by_summary(bob_events, "Original Title") is None


class TestEventDeletionPropagation:
    """Deletions propagate across a sync."""

    def test_event_deletion_propagates(
        self,
        alice_calendar_adapter: CalDavCalendarAdapter,
        bob_calendar_adapter: CalDavCalendarAdapter,
        calendar_state_session,
    ) -> None:
        # Seed and sync
        alice_cid = _first_container_id(alice_calendar_adapter)
        alice_calendar_adapter.create_item(
            alice_cid, _make_event("Will Stay"),
        )
        alice_calendar_adapter.create_item(
            alice_cid, _make_event("Will Go"),
        )

        summary = sync_trees(
            alice_calendar_adapter, bob_calendar_adapter,
            ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, calendar_state_session,
        )
        assert summary.errors == 0
        assert _count_events(alice_calendar_adapter) == 2
        assert _count_events(bob_calendar_adapter) == 2

        # Delete "Will Go" on alice's side
        alice_events = _list_events(alice_calendar_adapter)
        to_delete = _find_event_by_summary(alice_events, "Will Go")
        assert to_delete is not None
        alice_calendar_adapter.delete_item(alice_cid, to_delete.provider_id)
        assert _count_events(alice_calendar_adapter) == 1

        # Re-sync
        summary = sync_trees(
            alice_calendar_adapter, bob_calendar_adapter,
            ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, calendar_state_session,
        )
        assert summary.errors == 0

        # Verify bob no longer has "Will Go"
        assert _count_events(bob_calendar_adapter) == 1
        bob_events = _list_events(bob_calendar_adapter)
        assert _find_event_by_summary(bob_events, "Will Go") is None
        assert _find_event_by_summary(bob_events, "Will Stay") is not None


class TestCalendarSyncIdempotency:
    """A second sync with no intervening changes is a no-op."""

    def test_calendar_sync_idempotent(
        self,
        alice_calendar_adapter: CalDavCalendarAdapter,
        bob_calendar_adapter: CalDavCalendarAdapter,
        calendar_state_session,
    ) -> None:
        # Seed and sync
        alice_cid = _first_container_id(alice_calendar_adapter)
        alice_calendar_adapter.create_item(
            alice_cid, _make_event("Stable Event"),
        )

        summary1 = sync_trees(
            alice_calendar_adapter, bob_calendar_adapter,
            ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, calendar_state_session,
        )
        assert summary1.errors == 0
        assert summary1.created >= 1

        # Second sync with no changes
        summary2 = sync_trees(
            alice_calendar_adapter, bob_calendar_adapter,
            ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, calendar_state_session,
        )
        assert summary2.errors == 0
        assert summary2.created == 0
        assert summary2.updated == 0
        assert summary2.deleted == 0


class TestConcurrentEventEdits:
    """Concurrent edits to different fields merge cleanly."""

    def test_concurrent_event_edits_merge(
        self,
        alice_calendar_adapter: CalDavCalendarAdapter,
        bob_calendar_adapter: CalDavCalendarAdapter,
        calendar_state_session,
    ) -> None:
        # Seed and sync
        alice_cid = _first_container_id(alice_calendar_adapter)
        alice_calendar_adapter.create_item(
            alice_cid,
            _make_event(
                "Merge Target",
                location="Original Location",
                description="Original Description",
            ),
        )

        summary = sync_trees(
            alice_calendar_adapter, bob_calendar_adapter,
            ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, calendar_state_session,
        )
        assert summary.errors == 0

        # Alice edits location
        alice_events = _list_events(alice_calendar_adapter)
        alice_target = _find_event_by_summary(alice_events, "Merge Target")
        assert alice_target is not None
        alice_target.fields["location"] = "Alice's Location"
        alice_calendar_adapter.update_item(alice_cid, alice_target)

        # Bob edits description
        bob_cid = _first_container_id(bob_calendar_adapter)
        bob_events = _list_events(bob_calendar_adapter)
        bob_target = _find_event_by_summary(bob_events, "Merge Target")
        assert bob_target is not None
        bob_target.fields["description"] = "Bob's Description"
        bob_calendar_adapter.update_item(bob_cid, bob_target)

        # Re-sync: field-level merge should preserve both edits
        summary = sync_trees(
            alice_calendar_adapter, bob_calendar_adapter,
            ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, calendar_state_session,
        )
        assert summary.errors == 0

        # Verify both sides have both edits
        alice_events = _list_events(alice_calendar_adapter)
        merged_alice = _find_event_by_summary(alice_events, "Merge Target")
        assert merged_alice is not None
        assert merged_alice.fields.get("location") == "Alice's Location"
        assert merged_alice.fields.get("description") == "Bob's Description"

        bob_events = _list_events(bob_calendar_adapter)
        merged_bob = _find_event_by_summary(bob_events, "Merge Target")
        assert merged_bob is not None
        assert merged_bob.fields.get("location") == "Alice's Location"
        assert merged_bob.fields.get("description") == "Bob's Description"
