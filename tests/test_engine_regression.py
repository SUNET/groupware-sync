"""Regression tests for fixes called out in the 2026-04-27 audit.

Covers:
* ``_save_tree_cursors`` actually persists per-container cursors (the
  previous lookup used ``""`` for the other side and never matched any
  pair, silently disabling the skip-unchanged-containers optimization).
* ``_merge_one`` does not mark merged state as authoritative when one
  side's ``update_item`` raises (the previous code stored the pre-write
  fingerprint and snapshot, so the next sync saw no drift and silently
  dropped the failed write).
"""
from __future__ import annotations

import pytest

from groupware_sync.engine import _merge_one, _save_tree_cursors
from groupware_sync.models import (
    FieldDef,
    ItemType,
    MergeStrategy,
    NodeType,
    OpType,
    SyncItem,
    SyncNode,
    SyncOp,
    SyncSummary,
    TypeSpec,
)
from groupware_sync.provider import (
    NotificationCapability,
    NotificationPolicy,
    SyncProvider,
)
from groupware_sync.state import ops
from groupware_sync.state.db import make_session_factory


@pytest.fixture
def session(tmp_path):
    db_path = tmp_path / "regression.db"
    sf = make_session_factory(f"sqlite:///{db_path}")
    s = sf()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------


def test_save_tree_cursors_persists_state_for_real_pairs(session):
    """Real NodePairs have IDs on both sides; the helper must find the pair
    by this side's node_id alone and write the cursor row."""
    pair = ops.get_or_create_pair(
        session, "calendar_event",
        "stalwart", "cal-A",
        "m365", "cal-B",
        "Calendar",
    )

    container = SyncNode(
        node_id="cal-A",
        name="Calendar",
        node_type=NodeType.CONTAINER,
        state_cursor="state-token-1",
    )
    root = SyncNode(
        node_id="root", name="root", node_type=NodeType.CONTAINER,
        children=[container],
    )

    _save_tree_cursors(root, "stalwart", "m365", session)
    session.flush()

    assert ops.get_cursor(session, pair.id, "stalwart") == "state-token-1"
    # The other provider's slot stays empty — we only saved this side.
    assert ops.get_cursor(session, pair.id, "m365") is None


def test_save_tree_cursors_works_when_provider_is_b_side(session):
    """Pair stored as (m365 -> stalwart) must still be found when walking
    the stalwart tree."""
    pair = ops.get_or_create_pair(
        session, "calendar_event",
        "m365", "cal-B",
        "stalwart", "cal-A",
        "Calendar",
    )

    container = SyncNode(
        node_id="cal-A",
        name="Calendar",
        node_type=NodeType.CONTAINER,
        state_cursor="state-token-2",
    )
    root = SyncNode(
        node_id="root", name="root", node_type=NodeType.CONTAINER,
        children=[container],
    )

    _save_tree_cursors(root, "stalwart", "m365", session)
    session.flush()

    assert ops.get_cursor(session, pair.id, "stalwart") == "state-token-2"


def test_save_tree_cursors_skips_containers_without_cursor(session):
    pair = ops.get_or_create_pair(
        session, "calendar_event",
        "stalwart", "cal-A",
        "m365", "cal-B",
        "Calendar",
    )
    container = SyncNode(
        node_id="cal-A", name="Calendar", node_type=NodeType.CONTAINER,
    )
    root = SyncNode(
        node_id="root", name="root", node_type=NodeType.CONTAINER,
        children=[container],
    )
    _save_tree_cursors(root, "stalwart", "m365", session)
    session.flush()
    assert ops.get_cursor(session, pair.id, "stalwart") is None


# ---------------------------------------------------------------------------
# _merge_one write-failure handling
# ---------------------------------------------------------------------------


def _policy() -> NotificationPolicy:
    cap = NotificationCapability.SUPPRESSED
    return NotificationPolicy(
        create_item=cap, update_item=cap, delete_item=cap, delete_container=cap,
    )


class _RecordingProvider(SyncProvider):
    """Minimal SyncProvider whose ``update_item`` returns a fingerprint or
    raises a configured exception."""

    def __init__(self, name: str, *, raise_on_update: bool = False):
        self._name = name
        self.notification_policy = _policy()
        self._raise = raise_on_update
        self.update_calls: list[tuple[str, SyncItem]] = []

    @property
    def name(self) -> str:
        return self._name

    def build_tree(self, item_type, known_states=None):  # noqa: ANN001
        return SyncNode("root", "root", NodeType.CONTAINER)

    def get_items(self, container_id, ids):  # noqa: ANN001
        return []

    def create_container(self, name, parent_id=None):  # noqa: ANN001
        return f"c-{name}"

    def delete_container(self, container_id):  # noqa: ANN001
        return None

    def create_item(self, container_id, item):  # noqa: ANN001
        return (f"srv-{item.provider_id}", "fp-new")

    def update_item(self, container_id, item):  # noqa: ANN001
        self.update_calls.append((container_id, item))
        if self._raise:
            raise RuntimeError(f"{self._name} server boom")
        return f"fp-after-update-{self._name}"

    def delete_item(self, container_id, item_id):  # noqa: ANN001
        return None


def _spec() -> TypeSpec:
    return TypeSpec(
        item_type=ItemType.CONTACT,
        fields=[FieldDef("full_name", MergeStrategy.SCALAR)],
        identity_fields=["full_name"],
    )


def _setup_drifted_mapping(session):
    """Create a NodePair + ItemMapping where side A has drifted from the
    stored fingerprint, so the merge will plan an A-side write."""
    pair = ops.get_or_create_pair(
        session, "contact",
        "a", "container-A",
        "b", "container-B",
        "Contacts",
    )
    mapping = ops.create_mapping(
        session, pair.id, "item-A", "item-B",
        identity_key="ident-1",
        fingerprint_a="fp-a-stored",
        fingerprint_b="fp-b-stored",
    )
    snapshot = SyncItem("item-A", ItemType.CONTACT, {"full_name": "Old Name"})
    ops.save_snapshot(session, mapping.id, snapshot)
    session.flush()
    return pair, mapping


def test_merge_failure_does_not_persist_fingerprint_or_snapshot(session):
    pair, mapping = _setup_drifted_mapping(session)

    item_a = SyncItem(
        "item-A", ItemType.CONTACT,
        {"full_name": "New A Name"}, fingerprint="fp-a-current",
    )
    item_b = SyncItem(
        "item-B", ItemType.CONTACT,
        {"full_name": "Old Name"}, fingerprint="fp-b-stored",
    )

    provider_a = _RecordingProvider("a", raise_on_update=False)
    provider_b = _RecordingProvider("b", raise_on_update=True)

    op = SyncOp(
        op_type=OpType.MERGE_ITEM,
        target_side="both",
        node_id="item-A",
        paired_node_id="item-B",
        item_type=ItemType.CONTACT,
    )
    summary = SyncSummary()

    _merge_one(
        op,
        {"item-A": item_a},
        {"item-B": item_b},
        provider_a,
        provider_b,
        "container-A",
        "container-B",
        _spec(),
        session,
        summary,
    )
    session.flush()

    # B's update_item raised — fingerprints and snapshot must be untouched
    # so the next sync run still sees drift and re-plans the merge.
    session.refresh(mapping)
    assert mapping.fingerprint_a == "fp-a-stored"
    assert mapping.fingerprint_b == "fp-b-stored"

    snap_row = ops.get_snapshot(session, mapping.id)
    assert snap_row is not None
    snap = ops.load_snapshot_item(snap_row)
    assert snap.fields["full_name"] == "Old Name"

    assert summary.errors == 1
    assert summary.updated == 0


def test_merge_success_persists_fingerprints_and_snapshot(session):
    pair, mapping = _setup_drifted_mapping(session)

    item_a = SyncItem(
        "item-A", ItemType.CONTACT,
        {"full_name": "New A Name"}, fingerprint="fp-a-current",
    )
    item_b = SyncItem(
        "item-B", ItemType.CONTACT,
        {"full_name": "Old Name"}, fingerprint="fp-b-stored",
    )

    provider_a = _RecordingProvider("a", raise_on_update=False)
    provider_b = _RecordingProvider("b", raise_on_update=False)

    op = SyncOp(
        op_type=OpType.MERGE_ITEM,
        target_side="both",
        node_id="item-A",
        paired_node_id="item-B",
        item_type=ItemType.CONTACT,
    )
    summary = SyncSummary()

    _merge_one(
        op,
        {"item-A": item_a},
        {"item-B": item_b},
        provider_a,
        provider_b,
        "container-A",
        "container-B",
        _spec(),
        session,
        summary,
    )
    session.flush()

    session.refresh(mapping)
    # A had no drift to write (its value already matches merged), B did.
    assert mapping.fingerprint_b == "fp-after-update-b"

    snap_row = ops.get_snapshot(session, mapping.id)
    assert snap_row is not None
    snap = ops.load_snapshot_item(snap_row)
    assert snap.fields["full_name"] == "New A Name"

    assert summary.errors == 0
    assert summary.updated == 1
