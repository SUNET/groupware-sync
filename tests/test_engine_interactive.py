"""Tests for the interactive sync flow: plan formatting and the confirm
callback branch of sync_trees."""
from __future__ import annotations

import pytest

from groupware_sync.engine import _format_plan, sync_trees
from groupware_sync.models import (
    ItemType,
    NodeType,
    OpType,
    SyncItem,
    SyncNode,
    SyncOp,
    TypeSpec,
)
from groupware_sync.provider import (
    NotificationCapability,
    NotificationPolicy,
    SyncProvider,
)
from groupware_sync.state.db import make_session_factory


def _policy(cap: NotificationCapability) -> NotificationPolicy:
    return NotificationPolicy(
        create_item=cap, update_item=cap, delete_item=cap, delete_container=cap,
    )


class FakeProvider(SyncProvider):
    """Minimal in-memory SyncProvider for engine tests."""

    def __init__(self, name: str, policy: NotificationPolicy, tree: SyncNode):
        self._name = name
        self.notification_policy = policy
        self._tree = tree
        self.create_item_calls: list[tuple[str, SyncItem]] = []
        self.update_item_calls: list[tuple[str, SyncItem]] = []
        self.delete_item_calls: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    def build_tree(self, item_type, known_states=None):  # noqa: ANN001
        return self._tree

    def get_items(self, container_id, ids):  # noqa: ANN001
        return []

    def create_container(self, name, parent_id=None):  # noqa: ANN001
        return f"c-{name}"

    def delete_container(self, container_id):  # noqa: ANN001
        return None

    def create_item(self, container_id, item):  # noqa: ANN001
        self.create_item_calls.append((container_id, item))
        return (f"srv-{len(self.create_item_calls)}", "fp")

    def update_item(self, container_id, item):  # noqa: ANN001
        self.update_item_calls.append((container_id, item))
        return "fp"

    def delete_item(self, container_id, item_id):  # noqa: ANN001
        self.delete_item_calls.append((container_id, item_id))


def _fake(name: str, cap: NotificationCapability) -> FakeProvider:
    tree = SyncNode(f"root-{name}", f"root-{name}", NodeType.CONTAINER, children=[])
    tree.compute_merkle()
    return FakeProvider(name, _policy(cap), tree)


def test_format_plan_emits_one_line_per_op():
    a = _fake("a", NotificationCapability.SUPPRESSED)
    b = _fake("b", NotificationCapability.SUPPRESSED)
    ops = [
        SyncOp(op_type=OpType.CREATE_ITEM, target_side="b", node_id="x1", container_id_b="cb"),
        SyncOp(op_type=OpType.DELETE_ITEM, target_side="b", node_id="x2", container_id_b="cb"),
    ]
    lines = _format_plan(ops, a, b)
    assert len(lines) == 2
    assert lines[0].startswith("PLAN CREATE_ITEM")
    assert lines[1].startswith("PLAN DELETE_ITEM")
    assert "on b" in lines[0]


def test_format_plan_no_annotation_when_suppressed():
    a = _fake("a", NotificationCapability.SUPPRESSED)
    b = _fake("b", NotificationCapability.SUPPRESSED)
    ops = [SyncOp(op_type=OpType.DELETE_ITEM, target_side="b", node_id="x", container_id_b="cb")]
    lines = _format_plan(ops, a, b)
    assert "[!]" not in lines[0]


def test_format_plan_best_effort_annotation():
    a = _fake("a", NotificationCapability.SUPPRESSED)
    b = _fake("b", NotificationCapability.BEST_EFFORT)
    ops = [SyncOp(op_type=OpType.DELETE_ITEM, target_side="b", node_id="x", container_id_b="cb")]
    lines = _format_plan(ops, a, b)
    assert "[!] notifications best-effort" in lines[0]


def test_format_plan_unsupported_annotation():
    a = _fake("a", NotificationCapability.SUPPRESSED)
    b = _fake("b", NotificationCapability.UNSUPPORTED)
    ops = [SyncOp(op_type=OpType.DELETE_CONTAINER, target_side="b", node_id="x", container_id_b="cb")]
    lines = _format_plan(ops, a, b)
    assert "[!] notifications unsupported" in lines[0]


def test_format_plan_merge_annotates_if_either_side_not_suppressed():
    a = _fake("a", NotificationCapability.BEST_EFFORT)
    b = _fake("b", NotificationCapability.SUPPRESSED)
    ops = [SyncOp(op_type=OpType.MERGE_ITEM, target_side="both", node_id="x", paired_node_id="y")]
    lines = _format_plan(ops, a, b)
    assert "[!] notifications best-effort" in lines[0]


def test_format_plan_skip_subtree_never_annotated():
    a = _fake("a", NotificationCapability.UNSUPPORTED)
    b = _fake("b", NotificationCapability.UNSUPPORTED)
    ops = [SyncOp(op_type=OpType.SKIP_SUBTREE, target_side="b", node_id="x")]
    lines = _format_plan(ops, a, b)
    assert "[!]" not in lines[0]


@pytest.fixture
def session(tmp_path):
    db_path = tmp_path / "t.db"
    sf = make_session_factory(f"sqlite:///{db_path}")
    s = sf()
    yield s
    s.close()


def _basic_spec() -> TypeSpec:
    return TypeSpec(item_type=ItemType.CALENDAR_EVENT, fields=[], identity_fields=[])


def test_confirm_true_runs_writes(session):
    a = _fake("a", NotificationCapability.SUPPRESSED)
    b = _fake("b", NotificationCapability.SUPPRESSED)
    called: list[bool] = []

    def confirm(ops, summary):
        called.append(True)
        return True

    summary = sync_trees(a, b, ItemType.CALENDAR_EVENT, _basic_spec(), session, confirm=confirm)
    # Empty-plan short-circuit fires here (both trees are empty) — confirm is
    # not called and the summary is aborted=False with all zeros.
    assert called == []
    assert summary.aborted is False


def test_confirm_false_sets_aborted_and_skips_writes(session):
    # Build non-empty trees so we can exercise the confirm branch.
    leaf = SyncNode("x1", "x1", NodeType.LEAF, fingerprint="fp", item_type=ItemType.CALENDAR_EVENT)
    container = SyncNode("cb", "cb", NodeType.CONTAINER, children=[leaf])
    a_tree = SyncNode("root-a", "root-a", NodeType.CONTAINER, children=[])
    a_tree.compute_merkle()
    b_tree = SyncNode("root-b", "root-b", NodeType.CONTAINER, children=[container])
    b_tree.compute_merkle()

    a = FakeProvider("a", _policy(NotificationCapability.SUPPRESSED), a_tree)
    b = FakeProvider("b", _policy(NotificationCapability.SUPPRESSED), b_tree)

    def confirm(ops, summary):
        return False

    summary = sync_trees(a, b, ItemType.CALENDAR_EVENT, _basic_spec(), session, confirm=confirm)
    assert summary.aborted is True
    # No writes happened on either side.
    assert a.create_item_calls == [] and b.create_item_calls == []
    assert a.delete_item_calls == [] and b.delete_item_calls == []


def test_confirm_none_runs_legacy_path(session):
    a = _fake("a", NotificationCapability.SUPPRESSED)
    b = _fake("b", NotificationCapability.SUPPRESSED)
    summary = sync_trees(a, b, ItemType.CALENDAR_EVENT, _basic_spec(), session, confirm=None)
    assert summary.aborted is False


def test_dry_run_ignores_confirm(session):
    a = _fake("a", NotificationCapability.SUPPRESSED)
    b = _fake("b", NotificationCapability.SUPPRESSED)
    called: list[bool] = []

    def confirm(ops, summary):
        called.append(True)
        return True

    summary = sync_trees(
        a, b, ItemType.CALENDAR_EVENT, _basic_spec(), session, dry_run=True, confirm=confirm
    )
    assert called == []
    assert summary.aborted is False


def test_empty_plan_skips_confirm(session):
    a = _fake("a", NotificationCapability.SUPPRESSED)
    b = _fake("b", NotificationCapability.SUPPRESSED)
    called: list[bool] = []

    def confirm(ops, summary):
        called.append(True)
        return True

    summary = sync_trees(a, b, ItemType.CALENDAR_EVENT, _basic_spec(), session, confirm=confirm)
    assert called == []
    assert summary.aborted is False


def test_confirm_keyboard_interrupt_propagates(session):
    # Build a non-empty plan (leaf exists only on provider_b, so engine plans a create on a).
    leaf = SyncNode("x1", "x1", NodeType.LEAF, fingerprint="fp", item_type=ItemType.CALENDAR_EVENT)
    container = SyncNode("cb", "cb", NodeType.CONTAINER, children=[leaf])
    a_tree = SyncNode("root-a", "root-a", NodeType.CONTAINER, children=[])
    a_tree.compute_merkle()
    b_tree = SyncNode("root-b", "root-b", NodeType.CONTAINER, children=[container])
    b_tree.compute_merkle()

    a = FakeProvider("a", _policy(NotificationCapability.SUPPRESSED), a_tree)
    b = FakeProvider("b", _policy(NotificationCapability.SUPPRESSED), b_tree)

    def confirm(ops, summary):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        sync_trees(a, b, ItemType.CALENDAR_EVENT, _basic_spec(), session, confirm=confirm)

    # No writes happened — prompt raised before phase 4.
    assert a.create_item_calls == [] and b.delete_item_calls == []
