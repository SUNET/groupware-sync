"""End-to-end check: _identity_match pairs calendar events via content_key
when uid differs between the two sides.

This is the load-bearing behavioural test for the fallback: it exercises
CALENDAR_EVENT_SPEC.identity_fields through the real engine helper, not
a mock."""
from __future__ import annotations

from groupware_sync.engine import _identity_match
from groupware_sync.models import ItemType, SyncItem
from groupware_sync_calendar.specs import CALENDAR_EVENT_SPEC


def _event(
    uid: str,
    summary: str,
    dtstart_utc: str,
    content_key: str | None = None,
) -> SyncItem:
    fields: dict = {
        "uid": uid,
        "summary": summary,
        "dtstart_utc": dtstart_utc,
    }
    if content_key is not None:
        fields["content_key"] = content_key
    return SyncItem(
        provider_id=uid,
        item_type=ItemType.CALENDAR_EVENT,
        fields=fields,
        fingerprint="",
    )


def test_uid_match_pairs_events():
    """uid identity still wins at execute time when both sides carry it."""
    a = _event("abc-123", "Lunch", "2026-05-01T12:00:00Z")
    b = _event("abc-123", "Lunch", "2026-05-01T12:00:00Z")
    matched, only_a, only_b = _identity_match(
        [("a1", a)], [("b1", b)], CALENDAR_EVENT_SPEC
    )
    assert len(matched) == 1
    assert matched[0][0] == "a1"
    assert matched[0][1] == "b1"
    assert only_a == []
    assert only_b == []


def test_content_key_match_pairs_events_with_different_uids():
    """When uid differs but content_key matches, they still pair —
    the scenario that motivated this PR (Graph-assigned iCalUId vs
    Stalwart-stored uid for the same event)."""
    a = _event(
        "stalwart-uid-xyz",
        "Lunch",
        "2026-05-01T12:00:00Z",
        content_key="lunch|2026-05-01T12:00:00Z",
    )
    b = _event(
        "040000008200E00074C5B7101A82E008-graph-goid",
        "Lunch",
        "2026-05-01T12:00:00Z",
        content_key="lunch|2026-05-01T12:00:00Z",
    )
    matched, only_a, only_b = _identity_match(
        [("a1", a)], [("b1", b)], CALENDAR_EVENT_SPEC
    )
    assert len(matched) == 1, f"expected pair, got only_a={only_a} only_b={only_b}"
    assert matched[0][0] == "a1"
    assert matched[0][1] == "b1"


def test_missing_content_key_does_not_pair_on_different_uids():
    """If the adapter didn't populate content_key and uids also differ,
    the two events must not pair."""
    a = _event("uid-a", "Lunch", "2026-05-01T12:00:00Z")
    b = _event("uid-b", "Lunch", "2026-05-01T12:00:00Z")
    matched, only_a, only_b = _identity_match(
        [("a1", a)], [("b1", b)], CALENDAR_EVENT_SPEC
    )
    assert matched == []
    assert only_a == ["a1"]
    assert only_b == ["b1"]


def test_different_content_key_does_not_pair():
    """Events with different content (different title or time) do not pair
    even when both sides have content_key populated."""
    a = _event(
        "uid-a",
        "Lunch",
        "2026-05-01T12:00:00Z",
        content_key="lunch|2026-05-01T12:00:00Z",
    )
    b = _event(
        "uid-b",
        "Dinner",
        "2026-05-01T18:00:00Z",
        content_key="dinner|2026-05-01T18:00:00Z",
    )
    matched, only_a, only_b = _identity_match(
        [("a1", a)], [("b1", b)], CALENDAR_EVENT_SPEC
    )
    assert matched == []
    assert only_a == ["a1"]
    assert only_b == ["b1"]


def test_each_item_pairs_at_most_once():
    """If two A items share a content_key, the first one pairs; the second
    falls through as only_a. Guarantees no false pair-cloning when genuine
    duplicates exist on one side."""
    a1 = _event(
        "uid-a1",
        "Lunch",
        "2026-05-01T12:00:00Z",
        content_key="lunch|2026-05-01T12:00:00Z",
    )
    a2 = _event(
        "uid-a2",
        "Lunch",
        "2026-05-01T12:00:00Z",
        content_key="lunch|2026-05-01T12:00:00Z",
    )
    b = _event(
        "uid-b",
        "Lunch",
        "2026-05-01T12:00:00Z",
        content_key="lunch|2026-05-01T12:00:00Z",
    )
    matched, only_a, only_b = _identity_match(
        [("a1", a1), ("a2", a2)], [("b1", b)], CALENDAR_EVENT_SPEC
    )
    assert len(matched) == 1
    assert matched[0][0] == "a1"
    assert matched[0][1] == "b1"
    assert only_a == ["a2"]
    assert only_b == []
