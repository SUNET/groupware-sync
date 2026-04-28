"""CLI tests for groupware-sync-contacts: flag matrix and TTY guard."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from groupware_sync.config import Config, ProviderConfig
from groupware_sync.models import SyncSummary
from groupware_sync_contacts.cli import app

# click 8.2+ removed the mix_stderr kwarg; stderr is separated by default.
try:
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()


def _fake_cfg() -> Config:
    prov = ProviderConfig(auth_database_url="x", auth_uid="x", auth_provider_name="x")
    return Config(
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


@pytest.fixture
def stub_engine(monkeypatch):
    """Replace sync_trees and adapter/auth plumbing so the CLI body runs
    without touching the network or real auth."""
    calls: dict = {}

    def fake_sync_trees(a, b, item_type, type_spec, session, dry_run=False, confirm=None):
        calls["dry_run"] = dry_run
        calls["confirm"] = confirm
        return SyncSummary()

    monkeypatch.setattr("groupware_sync_contacts.cli.Config.from_env", classmethod(lambda cls: _fake_cfg()))
    monkeypatch.setattr("groupware_sync_contacts.cli.sync_trees", fake_sync_trees, raising=False)
    monkeypatch.setattr("groupware_sync_contacts.cli.fw_auth.get_access_token", lambda *a, **kw: "token", raising=False)
    monkeypatch.setattr("groupware_sync_contacts.adapters.jmap_adapter.JmapContactAdapter.__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr("groupware_sync_contacts.adapters.jmap_adapter.JmapContactAdapter.close", lambda self: None)
    monkeypatch.setattr("groupware_sync_contacts.adapters.graph_adapter.GraphContactAdapter.__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr("groupware_sync_contacts.adapters.graph_adapter.GraphContactAdapter.close", lambda self: None)
    # Patch the symbol on the CLI module (where it's used), not on the source,
    # because the CLI imports it at module top so the name is bound locally.
    class _FakeSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(
        "groupware_sync_contacts.cli.make_session_factory",
        lambda url: lambda: _FakeSession(),
        raising=False,
    )
    return calls


def test_non_tty_without_flags_exits_2(monkeypatch, stub_engine):
    with patch("groupware_sync_contacts.cli._stdin_isatty", return_value=False):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 2
    assert "interactive mode requires a TTY" in result.stderr


def test_non_interactive_runs_without_prompt(monkeypatch, stub_engine):
    with patch("groupware_sync_contacts.cli._stdin_isatty", return_value=False):
        result = runner.invoke(app, ["sync", "--non-interactive"])
    assert result.exit_code == 0
    assert stub_engine["dry_run"] is False
    assert stub_engine["confirm"] is None


def test_dry_run_takes_precedence_over_non_interactive(monkeypatch, stub_engine):
    with patch("groupware_sync_contacts.cli._stdin_isatty", return_value=False):
        result = runner.invoke(app, ["sync", "--dry-run", "--non-interactive"])
    assert result.exit_code == 0
    assert stub_engine["dry_run"] is True
    assert stub_engine["confirm"] is None


def test_dry_run_alone(monkeypatch, stub_engine):
    with patch("groupware_sync_contacts.cli._stdin_isatty", return_value=False):
        result = runner.invoke(app, ["sync", "--dry-run"])
    assert result.exit_code == 0
    assert stub_engine["dry_run"] is True
    assert stub_engine["confirm"] is None


def test_default_tty_passes_confirm_callback(monkeypatch, stub_engine):
    with patch("groupware_sync_contacts.cli._stdin_isatty", return_value=True):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert stub_engine["dry_run"] is False
    assert callable(stub_engine["confirm"])


def test_aborted_summary_exits_zero_with_message(monkeypatch):
    """When the engine returns summary.aborted=True (user declined the
    interactive prompt), the CLI prints a friendly message and exits 0."""
    def fake_sync_trees(a, b, item_type, type_spec, session, dry_run=False, confirm=None):
        return SyncSummary(aborted=True)

    monkeypatch.setattr("groupware_sync_contacts.cli.Config.from_env", classmethod(lambda cls: _fake_cfg()))
    monkeypatch.setattr("groupware_sync_contacts.cli.sync_trees", fake_sync_trees, raising=False)
    monkeypatch.setattr("groupware_sync_contacts.cli.fw_auth.get_access_token", lambda *a, **kw: "token", raising=False)
    monkeypatch.setattr("groupware_sync_contacts.adapters.jmap_adapter.JmapContactAdapter.__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr("groupware_sync_contacts.adapters.jmap_adapter.JmapContactAdapter.close", lambda self: None)
    monkeypatch.setattr("groupware_sync_contacts.adapters.graph_adapter.GraphContactAdapter.__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr("groupware_sync_contacts.adapters.graph_adapter.GraphContactAdapter.close", lambda self: None)

    class _FakeSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(
        "groupware_sync_contacts.cli.make_session_factory",
        lambda url: lambda: _FakeSession(),
        raising=False,
    )

    with patch("groupware_sync_contacts.cli._stdin_isatty", return_value=True):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "aborted by user" in result.stdout
