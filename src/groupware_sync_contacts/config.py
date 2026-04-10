"""Environment variable parsing for contacts sync.

All configuration comes from env vars (12-factor). No config files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_DB = Path.home() / ".local" / "share" / "groupware-sync" / "contacts-sync.db"


@dataclass
class ProviderConfig:
    auth_database_url: str
    auth_uid: str
    auth_provider_name: str


@dataclass
class Config:
    stalwart: ProviderConfig
    stalwart_jmap_url: str
    m365: ProviderConfig
    state_database_url: str

    @classmethod
    def from_env(cls) -> Config:
        def req(name: str) -> str:
            val = os.environ.get(name)
            if not val:
                raise ValueError(f"missing required env var: {name}")
            return val

        return cls(
            stalwart=ProviderConfig(
                auth_database_url=req("SYNC_STALWART_AUTH_DATABASE_URL"),
                auth_uid=req("SYNC_STALWART_AUTH_UID"),
                auth_provider_name=req("SYNC_STALWART_AUTH_PROVIDER_NAME"),
            ),
            stalwart_jmap_url=req("SYNC_STALWART_JMAP_URL"),
            m365=ProviderConfig(
                auth_database_url=req("SYNC_M365_AUTH_DATABASE_URL"),
                auth_uid=req("SYNC_M365_AUTH_UID"),
                auth_provider_name=req("SYNC_M365_AUTH_PROVIDER_NAME"),
            ),
            state_database_url=os.environ.get(
                "SYNC_STATE_DATABASE_URL", f"sqlite:///{DEFAULT_STATE_DB}"
            ),
        )
