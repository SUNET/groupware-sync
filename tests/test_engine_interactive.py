"""Tests for the interactive sync flow: plan formatting and the confirm
callback branch of sync_trees."""
from __future__ import annotations

from groupware_sync.engine import _format_plan
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
