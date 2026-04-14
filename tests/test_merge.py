"""Tests for field-level merge logic."""
from datetime import datetime, timezone

import pytest
from groupware_sync.merge import merge_item
from groupware_sync.models import (
    FieldDef,
    ItemType,
    MergeStrategy,
    SyncItem,
    TypeSpec,
)

SIMPLE_SPEC = TypeSpec(
    item_type=ItemType.CONTACT,
    fields=[
        FieldDef("full_name", MergeStrategy.SCALAR),
        FieldDef("emails", MergeStrategy.SET),
        FieldDef("uid", MergeStrategy.IMMUTABLE),
        FieldDef("internal", MergeStrategy.IGNORE),
    ],
)

TS_A = datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)
TS_B = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)


def test_scalar_a_changed_b_didnt():
    prev = SyncItem("x", ItemType.CONTACT, {"full_name": "Alice", "emails": [], "uid": "u1"})
    a = SyncItem("a1", ItemType.CONTACT, {"full_name": "Alice New", "emails": [], "uid": "u1"}, updated_at=TS_A)
    b = SyncItem("b1", ItemType.CONTACT, {"full_name": "Alice", "emails": [], "uid": "u1"}, updated_at=TS_B)
    merged, c_a, c_b, conflicts = merge_item(a, b, prev, SIMPLE_SPEC)
    assert merged.fields["full_name"] == "Alice New"
    assert c_a is False
    assert c_b is True
    assert conflicts == 0


def test_scalar_b_changed_a_didnt():
    prev = SyncItem("x", ItemType.CONTACT, {"full_name": "Alice", "emails": [], "uid": "u1"})
    a = SyncItem("a1", ItemType.CONTACT, {"full_name": "Alice", "emails": [], "uid": "u1"}, updated_at=TS_A)
    b = SyncItem("b1", ItemType.CONTACT, {"full_name": "Alice Updated", "emails": [], "uid": "u1"}, updated_at=TS_B)
    merged, c_a, c_b, conflicts = merge_item(a, b, prev, SIMPLE_SPEC)
    assert merged.fields["full_name"] == "Alice Updated"
    assert c_a is True
    assert c_b is False


def test_scalar_both_changed_same_value():
    prev = SyncItem("x", ItemType.CONTACT, {"full_name": "Alice", "emails": [], "uid": "u1"})
    a = SyncItem("a1", ItemType.CONTACT, {"full_name": "Bob", "emails": [], "uid": "u1"}, updated_at=TS_A)
    b = SyncItem("b1", ItemType.CONTACT, {"full_name": "Bob", "emails": [], "uid": "u1"}, updated_at=TS_B)
    merged, c_a, c_b, conflicts = merge_item(a, b, prev, SIMPLE_SPEC)
    assert merged.fields["full_name"] == "Bob"
    assert conflicts == 0


def test_scalar_conflict_a_wins_newer():
    prev = SyncItem("x", ItemType.CONTACT, {"full_name": "Alice", "emails": [], "uid": "u1"})
    a = SyncItem("a1", ItemType.CONTACT, {"full_name": "Alice A", "emails": [], "uid": "u1"}, updated_at=TS_A)
    b = SyncItem("b1", ItemType.CONTACT, {"full_name": "Alice B", "emails": [], "uid": "u1"}, updated_at=TS_B)
    merged, c_a, c_b, conflicts = merge_item(a, b, prev, SIMPLE_SPEC)
    assert merged.fields["full_name"] == "Alice A"  # A is newer
    assert conflicts == 1


def test_scalar_conflict_b_wins_newer():
    prev = SyncItem("x", ItemType.CONTACT, {"full_name": "Alice", "emails": [], "uid": "u1"})
    a = SyncItem("a1", ItemType.CONTACT, {"full_name": "Alice A", "emails": [], "uid": "u1"}, updated_at=TS_B)  # older
    b = SyncItem("b1", ItemType.CONTACT, {"full_name": "Alice B", "emails": [], "uid": "u1"}, updated_at=TS_A)  # newer
    merged, c_a, c_b, conflicts = merge_item(a, b, prev, SIMPLE_SPEC)
    assert merged.fields["full_name"] == "Alice B"


def test_no_snapshot_uses_timestamp():
    """Without a snapshot, every field is treated as 'both changed'."""
    a = SyncItem("a1", ItemType.CONTACT, {"full_name": "Alice A", "emails": [], "uid": "u1"}, updated_at=TS_A)
    b = SyncItem("b1", ItemType.CONTACT, {"full_name": "Alice B", "emails": [], "uid": "u1"}, updated_at=TS_B)
    merged, _, _, conflicts = merge_item(a, b, None, SIMPLE_SPEC)
    assert merged.fields["full_name"] == "Alice A"  # A newer wins
    assert conflicts == 1


def test_immutable_field_kept():
    prev = SyncItem("x", ItemType.CONTACT, {"full_name": "A", "emails": [], "uid": "original"})
    a = SyncItem("a1", ItemType.CONTACT, {"full_name": "A", "emails": [], "uid": "original"}, updated_at=TS_A)
    b = SyncItem("b1", ItemType.CONTACT, {"full_name": "A", "emails": [], "uid": "original"}, updated_at=TS_B)
    merged, _, _, _ = merge_item(a, b, prev, SIMPLE_SPEC)
    assert merged.fields["uid"] == "original"


def test_set_merge_union_additions():
    prev = SyncItem("x", ItemType.CONTACT, {"full_name": "A", "emails": ["a@b.com"], "uid": "u"})
    a = SyncItem("a1", ItemType.CONTACT, {"full_name": "A", "emails": ["a@b.com", "new_a@b.com"], "uid": "u"}, updated_at=TS_A)
    b = SyncItem("b1", ItemType.CONTACT, {"full_name": "A", "emails": ["a@b.com", "new_b@b.com"], "uid": "u"}, updated_at=TS_B)
    merged, c_a, c_b, _ = merge_item(a, b, prev, SIMPLE_SPEC)
    emails = merged.fields["emails"]
    assert "a@b.com" in emails
    assert "new_a@b.com" in emails
    assert "new_b@b.com" in emails


def test_neither_changed():
    prev = SyncItem("x", ItemType.CONTACT, {"full_name": "Same", "emails": [], "uid": "u"})
    a = SyncItem("a1", ItemType.CONTACT, {"full_name": "Same", "emails": [], "uid": "u"}, updated_at=TS_A)
    b = SyncItem("b1", ItemType.CONTACT, {"full_name": "Same", "emails": [], "uid": "u"}, updated_at=TS_B)
    merged, c_a, c_b, conflicts = merge_item(a, b, prev, SIMPLE_SPEC)
    assert merged.fields["full_name"] == "Same"
    assert c_a is False
    assert c_b is False
    assert conflicts == 0
