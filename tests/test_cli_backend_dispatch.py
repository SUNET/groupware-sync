"""Issue #20: CLI/config wiring for CardDAV and CalDAV.

Pins the per-side backend dispatch in the contacts and calendar CLIs:
* default selectors still build the JMAP/Graph pair (back-compat),
* `carddav`/`caldav` selectors build the DAV adapter from
  ``cfg.side_X_dav``,
* missing DAV credentials exit cleanly,
* unknown backend names exit cleanly.

These tests stub adapter ``__init__`` so the dispatch logic is exercised
without touching the network or instantiating any real client.
"""
from __future__ import annotations

import pytest
import typer

from groupware_sync.config import Config, DavConfig, ProviderConfig
from groupware_sync_calendar.cli import _build_calendar_provider
from groupware_sync_contacts.cli import _build_contact_provider


def _base_cfg(**overrides) -> Config:
    prov = ProviderConfig(
        auth_database_url="x", auth_uid="x", auth_provider_name="x",
    )
    base = dict(
        sync_type="contacts",
        side_a_backend="jmap",
        side_b_backend="graph",
        stalwart=prov,
        stalwart_jmap_url="https://stalwart.invalid",
        stalwart_addressbook=None,
        stalwart_calendar=None,
        m365=prov,
        m365_addressbook=None,
        m365_calendar=None,
        side_a_dav=None,
        side_b_dav=None,
        state_database_url="sqlite:///:memory:",
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture(autouse=True)
def _stub_adapters(monkeypatch):
    """Replace adapter __init__/close with no-ops so dispatch can be
    exercised without httpx clients or real OAuth tokens."""
    targets = [
        "groupware_sync_contacts.adapters.jmap_adapter.JmapContactAdapter",
        "groupware_sync_contacts.adapters.graph_adapter.GraphContactAdapter",
        "groupware_sync_contacts.adapters.carddav_adapter.CardDavContactAdapter",
        "groupware_sync_calendar.adapters.jmap_adapter.JmapCalendarAdapter",
        "groupware_sync_calendar.adapters.graph_adapter.GraphCalendarAdapter",
        "groupware_sync_calendar.adapters.caldav_adapter.CalDavCalendarAdapter",
    ]
    for path in targets:
        try:
            monkeypatch.setattr(f"{path}.__init__", lambda self, *a, **kw: None)
            monkeypatch.setattr(f"{path}.close", lambda self: None, raising=False)
        except (ImportError, AttributeError):
            # vobject-dependent adapters may be unavailable in some envs;
            # tests that need those skip explicitly below.
            pass

    # Auth lookups must succeed regardless of the (fake) URLs in cfg.
    monkeypatch.setattr(
        "groupware_sync_contacts.cli.fw_auth.get_access_token",
        lambda *a, **kw: "token", raising=False,
    )
    monkeypatch.setattr(
        "groupware_sync_calendar.cli.fw_auth.get_access_token",
        lambda *a, **kw: "token", raising=False,
    )


# ---------- contacts ----------


def test_contacts_default_dispatch_builds_jmap_and_graph():
    from groupware_sync_contacts.adapters.graph_adapter import GraphContactAdapter
    from groupware_sync_contacts.adapters.jmap_adapter import JmapContactAdapter

    cfg = _base_cfg()
    a = _build_contact_provider(cfg, "a")
    b = _build_contact_provider(cfg, "b")
    assert isinstance(a, JmapContactAdapter)
    assert isinstance(b, GraphContactAdapter)


def test_contacts_carddav_on_side_a_uses_side_a_dav():
    try:
        from groupware_sync_contacts.adapters.carddav_adapter import (
            CardDavContactAdapter,
        )
    except ModuleNotFoundError:
        pytest.skip("vobject not installed")

    cfg = _base_cfg(
        side_a_backend="carddav",
        side_a_dav=DavConfig(
            base_url="https://dav.invalid", username="u", password="p",
        ),
    )
    a = _build_contact_provider(cfg, "a")
    assert isinstance(a, CardDavContactAdapter)


def test_contacts_carddav_without_credentials_exits_2():
    cfg = _base_cfg(side_a_backend="carddav", side_a_dav=None)
    with pytest.raises(typer.Exit) as exc_info:
        _build_contact_provider(cfg, "a")
    assert exc_info.value.exit_code == 2


def test_contacts_unknown_backend_exits_2():
    cfg = _base_cfg(side_a_backend="bogus")
    with pytest.raises(typer.Exit) as exc_info:
        _build_contact_provider(cfg, "a")
    assert exc_info.value.exit_code == 2


def test_contacts_caldav_is_not_a_contact_backend():
    cfg = _base_cfg(side_a_backend="caldav")
    with pytest.raises(typer.Exit) as exc_info:
        _build_contact_provider(cfg, "a")
    assert exc_info.value.exit_code == 2


# ---------- calendar ----------


def test_calendar_default_dispatch_builds_jmap_and_graph():
    from groupware_sync_calendar.adapters.graph_adapter import GraphCalendarAdapter
    from groupware_sync_calendar.adapters.jmap_adapter import JmapCalendarAdapter

    cfg = _base_cfg()
    a = _build_calendar_provider(cfg, "a")
    b = _build_calendar_provider(cfg, "b")
    assert isinstance(a, JmapCalendarAdapter)
    assert isinstance(b, GraphCalendarAdapter)


def test_calendar_caldav_on_side_b_uses_side_b_dav():
    try:
        from groupware_sync_calendar.adapters.caldav_adapter import (
            CalDavCalendarAdapter,
        )
    except ModuleNotFoundError:
        pytest.skip("vobject not installed")

    cfg = _base_cfg(
        side_b_backend="caldav",
        side_b_dav=DavConfig(
            base_url="https://dav.invalid", username="u", password="p",
        ),
    )
    b = _build_calendar_provider(cfg, "b")
    assert isinstance(b, CalDavCalendarAdapter)


def test_calendar_caldav_without_credentials_exits_2():
    cfg = _base_cfg(side_b_backend="caldav", side_b_dav=None)
    with pytest.raises(typer.Exit) as exc_info:
        _build_calendar_provider(cfg, "b")
    assert exc_info.value.exit_code == 2


def test_calendar_carddav_is_not_a_calendar_backend():
    cfg = _base_cfg(side_a_backend="carddav")
    with pytest.raises(typer.Exit) as exc_info:
        _build_calendar_provider(cfg, "a")
    assert exc_info.value.exit_code == 2
