"""Keyring storage for refresh tokens and their expiry.

Each logged-in (provider, uid) pair owns one keyring entry. The value is a
JSON blob containing the refresh token, the absolute expiry timestamp of the
*current* access token (so that `tick` can decide whether to refresh), and the
granted scope. Reading and writing happen as one atomic unit so the three
fields can never drift.

Service name: "groupware-sync-auth"
Username:     "<provider_name>:<uid>"
Password:     json.dumps({refresh_token, expires_at, scope})
"""
import json
from dataclasses import asdict, dataclass
from typing import Optional

import keyring
from keyring.errors import PasswordDeleteError

SERVICE_NAME = "groupware-sync-auth"


@dataclass
class SecretBlob:
    refresh_token: str
    expires_at: int  # absolute unix seconds
    scope: Optional[str]


def _username(provider_name: str, uid: str) -> str:
    return f"{provider_name}:{uid}"


def write(provider_name: str, uid: str, blob: SecretBlob) -> None:
    keyring.set_password(
        SERVICE_NAME,
        _username(provider_name, uid),
        json.dumps(asdict(blob)),
    )


def read(provider_name: str, uid: str) -> Optional[SecretBlob]:
    raw = keyring.get_password(SERVICE_NAME, _username(provider_name, uid))
    if raw is None:
        return None
    data = json.loads(raw)
    return SecretBlob(
        refresh_token=data["refresh_token"],
        expires_at=int(data["expires_at"]),
        scope=data.get("scope"),
    )


def delete(provider_name: str, uid: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, _username(provider_name, uid))
    except PasswordDeleteError:
        # already gone — fine
        pass
