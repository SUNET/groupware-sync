"""Environment variable config for the sync framework."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_STATE_DB = Path.home() / ".local" / "share" / "groupware-sync" / "sync.db"


@dataclass
class ProviderConfig:
    auth_database_url: str
    auth_uid: str
    auth_provider_name: str


@dataclass
class Config:
    sync_type: str
    stalwart: ProviderConfig
    stalwart_jmap_url: str
    stalwart_addressbook: Optional[str]
    stalwart_calendar: Optional[str]
    m365: ProviderConfig
    m365_addressbook: Optional[str]
    m365_calendar: Optional[str]
    state_database_url: str

    @classmethod
    def from_env(cls) -> Config:
        def req(name: str) -> str:
            val = os.environ.get(name)
            if not val:
                raise ValueError(f"missing required env var: {name}")
            return val

        return cls(
            sync_type=os.environ.get("SYNC_TYPE", "contacts"),
            stalwart=ProviderConfig(
                auth_database_url=req("SYNC_STALWART_AUTH_DATABASE_URL"),
                auth_uid=req("SYNC_STALWART_AUTH_UID"),
                auth_provider_name=req("SYNC_STALWART_AUTH_PROVIDER_NAME"),
            ),
            stalwart_jmap_url=req("SYNC_STALWART_JMAP_URL"),
            stalwart_addressbook=os.environ.get("SYNC_STALWART_ADDRESSBOOK"),
            stalwart_calendar=os.environ.get("SYNC_STALWART_CALENDAR"),
            m365=ProviderConfig(
                auth_database_url=req("SYNC_M365_AUTH_DATABASE_URL"),
                auth_uid=req("SYNC_M365_AUTH_UID"),
                auth_provider_name=req("SYNC_M365_AUTH_PROVIDER_NAME"),
            ),
            m365_addressbook=os.environ.get("SYNC_M365_ADDRESSBOOK"),
            m365_calendar=os.environ.get("SYNC_M365_CALENDAR"),
            state_database_url=os.environ.get(
                "SYNC_STATE_DATABASE_URL", f"sqlite:///{DEFAULT_STATE_DB}"
            ),
        )
