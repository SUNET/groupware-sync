"""Contacts adapters: identity_key on leaves is currently None by design.

Contacts identity is a composite of emails + full_name (per CONTACT_SPEC),
which requires a full-item fetch that contact build_tree doesn't do today.
Until the follow-up spec upgrades contacts to identity-based pairing, the
adapters surface identity_key=None explicitly so the unpairable-leaf code
path in compare_trees handles them safely (create-only, never delete).

This test asserts the current contract: leaves have identity_key=None.
"""
from __future__ import annotations

import inspect


def test_jmap_contact_adapter_leaf_construction_sets_identity_key_none():
    """Structural check: build_tree source references identity_key on leaf
    construction (sentinel: the field is explicitly addressed rather than
    defaulted silently)."""
    import groupware_sync_contacts.adapters.jmap_adapter as mod
    src = inspect.getsource(mod.JmapContactAdapter.build_tree)
    assert "identity_key=None" in src, (
        "JmapContactAdapter.build_tree should set identity_key=None explicitly "
        "to document the follow-up path for contacts identity."
    )


def test_graph_contact_adapter_leaf_construction_sets_identity_key_none():
    import groupware_sync_contacts.adapters.graph_adapter as mod
    src = inspect.getsource(mod.GraphContactAdapter.build_tree)
    assert "identity_key=None" in src


def test_carddav_contact_adapter_leaf_construction_sets_identity_key_none():
    try:
        import groupware_sync_contacts.adapters.carddav_adapter as mod
        src = inspect.getsource(mod.CardDavContactAdapter.build_tree)
    except ModuleNotFoundError:
        # vobject might not be installed in test environment; read source directly
        import pathlib
        carddav_path = pathlib.Path(__file__).parent.parent / "src" / "groupware_sync_contacts" / "adapters" / "carddav_adapter.py"
        src = carddav_path.read_text()
    assert "identity_key=None" in src
