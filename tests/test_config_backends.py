"""Issue #20: Config.from_env() must accept per-side backend selectors.

Pins the env-var contract introduced for CardDAV/CalDAV support:
* defaults preserve the original Stalwart-JMAP / M365-Graph topology
* selecting `carddav`/`caldav` for a side requires its own DAV credentials
* selecting a non-default backend on either side relaxes the OAuth
  requirements for the backend that is no longer in use
"""
from __future__ import annotations

import pytest

from groupware_sync.config import Config


def _set(monkeypatch, **env):
    """Set or unset env vars (None deletes)."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Clear every variable the Config reads so each test starts fresh.
    for var in [
        "SYNC_TYPE",
        "SYNC_SIDE_A_BACKEND", "SYNC_SIDE_B_BACKEND",
        "SYNC_STALWART_AUTH_DATABASE_URL", "SYNC_STALWART_AUTH_UID",
        "SYNC_STALWART_AUTH_PROVIDER_NAME", "SYNC_STALWART_JMAP_URL",
        "SYNC_STALWART_ADDRESSBOOK", "SYNC_STALWART_CALENDAR",
        "SYNC_M365_AUTH_DATABASE_URL", "SYNC_M365_AUTH_UID",
        "SYNC_M365_AUTH_PROVIDER_NAME",
        "SYNC_M365_ADDRESSBOOK", "SYNC_M365_CALENDAR",
        "SYNC_SIDE_A_DAV_URL", "SYNC_SIDE_A_DAV_USERNAME",
        "SYNC_SIDE_A_DAV_PASSWORD",
        "SYNC_SIDE_B_DAV_URL", "SYNC_SIDE_B_DAV_USERNAME",
        "SYNC_SIDE_B_DAV_PASSWORD",
        "SYNC_STATE_DATABASE_URL",
    ]:
        monkeypatch.delenv(var, raising=False)


def test_default_topology_preserves_legacy_env_vars(monkeypatch):
    _set(
        monkeypatch,
        SYNC_STALWART_AUTH_DATABASE_URL="sqlite:///s.db",
        SYNC_STALWART_AUTH_UID="alice",
        SYNC_STALWART_AUTH_PROVIDER_NAME="stalwart",
        SYNC_STALWART_JMAP_URL="https://stalwart.invalid",
        SYNC_M365_AUTH_DATABASE_URL="sqlite:///m.db",
        SYNC_M365_AUTH_UID="alice@example.com",
        SYNC_M365_AUTH_PROVIDER_NAME="m365",
    )
    cfg = Config.from_env()
    assert cfg.side_a_backend == "jmap"
    assert cfg.side_b_backend == "graph"
    assert cfg.side_a_dav is None
    assert cfg.side_b_dav is None
    assert cfg.stalwart.auth_uid == "alice"
    assert cfg.m365.auth_uid == "alice@example.com"


def test_carddav_on_side_a_loads_dav_credentials(monkeypatch):
    _set(
        monkeypatch,
        SYNC_SIDE_A_BACKEND="carddav",
        # side B is still graph in this scenario, so M365 OAuth required.
        SYNC_M365_AUTH_DATABASE_URL="sqlite:///m.db",
        SYNC_M365_AUTH_UID="alice@example.com",
        SYNC_M365_AUTH_PROVIDER_NAME="m365",
        SYNC_SIDE_A_DAV_URL="https://radicale.invalid",
        SYNC_SIDE_A_DAV_USERNAME="alice",
        SYNC_SIDE_A_DAV_PASSWORD="hunter2",
    )
    cfg = Config.from_env()
    assert cfg.side_a_backend == "carddav"
    assert cfg.side_a_dav is not None
    assert cfg.side_a_dav.base_url == "https://radicale.invalid"
    assert cfg.side_a_dav.username == "alice"
    assert cfg.side_a_dav.password == "hunter2"
    # JMAP no longer needed → its OAuth fields are tolerated as missing.
    assert cfg.stalwart.auth_uid == ""


def test_carddav_without_dav_url_raises(monkeypatch):
    _set(
        monkeypatch,
        SYNC_SIDE_A_BACKEND="carddav",
        SYNC_M365_AUTH_DATABASE_URL="sqlite:///m.db",
        SYNC_M365_AUTH_UID="alice@example.com",
        SYNC_M365_AUTH_PROVIDER_NAME="m365",
        # SYNC_SIDE_A_DAV_* deliberately unset
    )
    with pytest.raises(ValueError, match="SYNC_SIDE_A_DAV_URL"):
        Config.from_env()


def test_carddav_only_topology_drops_oauth_requirement(monkeypatch):
    """Both sides DAV → neither Stalwart nor M365 OAuth env vars needed."""
    _set(
        monkeypatch,
        SYNC_SIDE_A_BACKEND="carddav",
        SYNC_SIDE_B_BACKEND="carddav",
        SYNC_SIDE_A_DAV_URL="https://a.invalid",
        SYNC_SIDE_A_DAV_USERNAME="alice",
        SYNC_SIDE_A_DAV_PASSWORD="pwa",
        SYNC_SIDE_B_DAV_URL="https://b.invalid",
        SYNC_SIDE_B_DAV_USERNAME="bob",
        SYNC_SIDE_B_DAV_PASSWORD="pwb",
    )
    cfg = Config.from_env()
    assert cfg.side_a_dav is not None and cfg.side_b_dav is not None
    assert cfg.stalwart_jmap_url == ""  # not required when no JMAP side


def test_default_still_requires_stalwart_oauth(monkeypatch):
    """Back-compat guard: dropping the default Stalwart env vars without
    selecting a different backend still surfaces a clear error."""
    _set(
        monkeypatch,
        SYNC_M365_AUTH_DATABASE_URL="sqlite:///m.db",
        SYNC_M365_AUTH_UID="alice@example.com",
        SYNC_M365_AUTH_PROVIDER_NAME="m365",
    )
    with pytest.raises(ValueError, match="SYNC_STALWART"):
        Config.from_env()
