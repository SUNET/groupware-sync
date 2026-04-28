"""Environment variable config for the sync framework."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_STATE_DB = Path.home() / ".local" / "share" / "groupware-sync" / "sync.db"

# Backend identifiers accepted by SYNC_SIDE_A_BACKEND / SYNC_SIDE_B_BACKEND
# and the matching CLI flags. The contacts CLI accepts {jmap, graph, carddav};
# the calendar CLI accepts {jmap, graph, caldav}. Validation lives at the
# call site (the framework Config doesn't know which CLI is loading it).
CONTACT_BACKENDS = {"jmap", "graph", "carddav"}
CALENDAR_BACKENDS = {"jmap", "graph", "caldav"}


@dataclass
class ProviderConfig:
    auth_database_url: str
    auth_uid: str
    auth_provider_name: str


@dataclass
class DavConfig:
    """Connection details for a CardDAV or CalDAV side.

    Populated from `SYNC_SIDE_{A,B}_DAV_*` env vars. None when the matching
    side's backend is not carddav/caldav.
    """
    base_url: str
    username: str
    password: str


@dataclass
class Config:
    # Per-side backend selectors. Defaults preserve the original
    # M365 (Graph) ↔ Stalwart (JMAP) topology so existing deployments are
    # unaffected by the introduction of these fields.
    side_a_backend: str  # "jmap" by default
    side_b_backend: str  # "graph" by default
    stalwart: ProviderConfig
    stalwart_jmap_url: str
    stalwart_addressbook: Optional[str]
    stalwart_calendar: Optional[str]
    m365: ProviderConfig
    m365_addressbook: Optional[str]
    m365_calendar: Optional[str]
    # Side-specific DAV connection details. Only consulted when the matching
    # side's backend is "carddav" or "caldav".
    side_a_dav: Optional[DavConfig]
    side_b_dav: Optional[DavConfig]
    state_database_url: str

    @classmethod
    def from_env(cls) -> Config:
        def req(name: str) -> str:
            val = os.environ.get(name)
            if not val:
                raise ValueError(f"missing required env var: {name}")
            return val

        side_a_backend = os.environ.get("SYNC_SIDE_A_BACKEND", "jmap").lower()
        side_b_backend = os.environ.get("SYNC_SIDE_B_BACKEND", "graph").lower()

        # Stalwart and M365 OAuth config is only required when the matching
        # backend is selected. To keep the env-var schema unchanged for the
        # default jmap/graph topology, we still require them under the
        # default selectors but treat them as optional otherwise.
        def maybe_provider(env_prefix: str, when: bool) -> ProviderConfig:
            if when:
                return ProviderConfig(
                    auth_database_url=req(f"{env_prefix}_AUTH_DATABASE_URL"),
                    auth_uid=req(f"{env_prefix}_AUTH_UID"),
                    auth_provider_name=req(f"{env_prefix}_AUTH_PROVIDER_NAME"),
                )
            return ProviderConfig(
                auth_database_url=os.environ.get(f"{env_prefix}_AUTH_DATABASE_URL", ""),
                auth_uid=os.environ.get(f"{env_prefix}_AUTH_UID", ""),
                auth_provider_name=os.environ.get(f"{env_prefix}_AUTH_PROVIDER_NAME", ""),
            )

        need_stalwart = "jmap" in (side_a_backend, side_b_backend)
        need_m365 = "graph" in (side_a_backend, side_b_backend)

        stalwart_jmap_url = (
            req("SYNC_STALWART_JMAP_URL") if need_stalwart
            else os.environ.get("SYNC_STALWART_JMAP_URL", "")
        )

        def maybe_dav(prefix: str, when: bool) -> Optional[DavConfig]:
            if not when:
                return None
            return DavConfig(
                base_url=req(f"{prefix}_DAV_URL"),
                username=req(f"{prefix}_DAV_USERNAME"),
                password=req(f"{prefix}_DAV_PASSWORD"),
            )

        side_a_dav = maybe_dav(
            "SYNC_SIDE_A", side_a_backend in {"carddav", "caldav"},
        )
        side_b_dav = maybe_dav(
            "SYNC_SIDE_B", side_b_backend in {"carddav", "caldav"},
        )

        return cls(
            side_a_backend=side_a_backend,
            side_b_backend=side_b_backend,
            stalwart=maybe_provider("SYNC_STALWART", need_stalwart),
            stalwart_jmap_url=stalwart_jmap_url,
            stalwart_addressbook=os.environ.get("SYNC_STALWART_ADDRESSBOOK"),
            stalwart_calendar=os.environ.get("SYNC_STALWART_CALENDAR"),
            m365=maybe_provider("SYNC_M365", need_m365),
            m365_addressbook=os.environ.get("SYNC_M365_ADDRESSBOOK"),
            m365_calendar=os.environ.get("SYNC_M365_CALENDAR"),
            side_a_dav=side_a_dav,
            side_b_dav=side_b_dav,
            state_database_url=os.environ.get(
                "SYNC_STATE_DATABASE_URL", f"sqlite:///{DEFAULT_STATE_DB}"
            ),
        )
