"""CLI tests for groupware-sync-calendar: flag matrix and TTY guard."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from groupware_sync.config import Config, ProviderConfig
from groupware_sync.models import SyncSummary
from groupware_sync_calendar.cli import app

# click 8.2+ removed the mix_stderr kwarg; stderr is separated by default.
try:
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()


def _fake_cfg() -> Config:
    prov = ProviderConfig(auth_database_url="x", auth_uid="x", auth_provider_name="x")
    return Config(
        sync_type="calendar",
        stalwart=prov,
        stalwart_jmap_url="https://stalwart.invalid",
        stalwart_addressbook=None,
        stalwart_calendar=None,
        m365=prov,
        m365_addressbook=None,
        m365_calendar=None,
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

    monkeypatch.setattr("groupware_sync_calendar.cli.Config.from_env", classmethod(lambda cls: _fake_cfg()))
    monkeypatch.setattr("groupware_sync_calendar.cli.sync_trees", fake_sync_trees, raising=False)
    monkeypatch.setattr("groupware_sync_calendar.cli.fw_auth.get_access_token", lambda *a, **kw: "token", raising=False)
    monkeypatch.setattr("groupware_sync_calendar.adapters.jmap_adapter.JmapCalendarAdapter.__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr("groupware_sync_calendar.adapters.jmap_adapter.JmapCalendarAdapter.close", lambda self: None)
    monkeypatch.setattr("groupware_sync_calendar.adapters.graph_adapter.GraphCalendarAdapter.__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr("groupware_sync_calendar.adapters.graph_adapter.GraphCalendarAdapter.close", lambda self: None)
    monkeypatch.setattr("groupware_sync.state.db.make_session_factory", lambda url: lambda: _FakeSession(), raising=False)
    return calls


class _FakeSession:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def commit(self): pass
    def close(self): pass


def test_non_tty_without_flags_exits_2(monkeypatch, stub_engine):
    with patch("groupware_sync_calendar.cli._stdin_isatty", return_value=False):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 2
    assert "interactive mode requires a TTY" in result.stderr


def test_non_interactive_runs_without_prompt(monkeypatch, stub_engine):
    with patch("groupware_sync_calendar.cli._stdin_isatty", return_value=False):
        result = runner.invoke(app, ["sync", "--non-interactive"])
    assert result.exit_code == 0
    assert stub_engine["dry_run"] is False
    assert stub_engine["confirm"] is None


def test_dry_run_takes_precedence_over_non_interactive(monkeypatch, stub_engine):
    with patch("groupware_sync_calendar.cli._stdin_isatty", return_value=False):
        result = runner.invoke(app, ["sync", "--dry-run", "--non-interactive"])
    assert result.exit_code == 0
    assert stub_engine["dry_run"] is True
    assert stub_engine["confirm"] is None


def test_dry_run_alone(monkeypatch, stub_engine):
    with patch("groupware_sync_calendar.cli._stdin_isatty", return_value=False):
        result = runner.invoke(app, ["sync", "--dry-run"])
    assert result.exit_code == 0
    assert stub_engine["dry_run"] is True
    assert stub_engine["confirm"] is None


def test_default_tty_passes_confirm_callback(monkeypatch, stub_engine):
    with patch("groupware_sync_calendar.cli._stdin_isatty", return_value=True):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert stub_engine["dry_run"] is False
    assert callable(stub_engine["confirm"])
