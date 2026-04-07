"""Use-case layer. The only module that touches both oauth and storage.

Public functions:
    add_provider, list_providers, get_provider, remove_provider
    login, logout, list_accounts, get_account
    refresh_one, refresh_due
    import_token

Each function is a self-contained use case for the CLI to call. The CLI does
no business logic of its own.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import httpx
from sqlalchemy import select

from groupware_sync_auth import oauth
from groupware_sync_auth.oauth import (
    DeviceAuthorization,
    InvalidGrantError,
    OAuthError,
    ProviderConfig,
)
from groupware_sync_auth.storage import secrets
from groupware_sync_auth.storage.db import Provider, UserConfig, get_session

log = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3


# ---- providers --------------------------------------------------------------


def _to_provider_config(p: Provider) -> ProviderConfig:
    return ProviderConfig(
        name=p.name,
        client_id=p.client_id,
        client_secret=p.client_secret,
        device_authorization_endpoint=p.device_authorization_endpoint,
        token_endpoint=p.token_endpoint,
        user_endpoint=p.user_endpoint,
        scope=p.scope,
    )


def add_provider(
    name: str,
    client_id: str,
    client_secret: Optional[str],
    device_authorization_endpoint: str,
    token_endpoint: str,
    user_endpoint: str,
    scope: str,
) -> Provider:
    with get_session() as s:
        p = Provider(
            name=name,
            client_id=client_id,
            client_secret=client_secret or None,
            device_authorization_endpoint=device_authorization_endpoint,
            token_endpoint=token_endpoint,
            user_endpoint=user_endpoint,
            scope=scope,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        return p


def list_providers() -> list[Provider]:
    with get_session() as s:
        return list(s.scalars(select(Provider).order_by(Provider.id)))


def get_provider(name: str) -> Optional[Provider]:
    with get_session() as s:
        return s.scalar(select(Provider).where(Provider.name == name))


def remove_provider(name: str) -> None:
    with get_session() as s:
        p = s.scalar(select(Provider).where(Provider.name == name))
        if p is None:
            raise ValueError(f"no provider named {name!r}")
        in_use = s.scalar(
            select(UserConfig.id).where(UserConfig.provider_id == p.id).limit(1)
        )
        if in_use is not None:
            raise ValueError(
                f"provider {name!r} has userconfig rows; logout those accounts first"
            )
        s.delete(p)
        s.commit()


# ---- accounts ---------------------------------------------------------------


def list_accounts() -> list[tuple[UserConfig, Provider]]:
    """All logged-in accounts as (UserConfig, Provider) pairs."""
    with get_session() as s:
        rows = s.execute(
            select(UserConfig, Provider).join(
                Provider, UserConfig.provider_id == Provider.id
            )
        ).all()
        return [(uc, p) for uc, p in rows]


def get_accounts_by_uid(uid: str) -> list[tuple[UserConfig, Provider]]:
    return [pair for pair in list_accounts() if pair[0].uid == uid]


# ---- login / logout ---------------------------------------------------------


def _derive_email(userinfo: dict) -> Optional[str]:
    return (
        userinfo.get("email")
        or userinfo.get("preferred_username")
        or userinfo.get("upn")
    )


def login(
    provider_name: str,
    uid: Optional[str] = None,
    on_device_code: Optional[Callable[[DeviceAuthorization], None]] = None,
) -> UserConfig:
    """Run the device code flow against `provider_name`.

    `on_device_code(device_authorization)` is called once the IdP returns the
    user code so the CLI can print it. If omitted, prints to stdout.
    """
    p = get_provider(provider_name)
    if p is None:
        raise ValueError(
            f"unknown provider {provider_name!r}; run `provider add` first"
        )
    pc = _to_provider_config(p)

    with httpx.Client() as client:
        device_auth = oauth.request_device_code(pc, client)
        if on_device_code is not None:
            on_device_code(device_auth)
        else:
            print(
                f"\nVisit {device_auth.verification_uri} "
                f"and enter code: {device_auth.user_code}"
            )
            if device_auth.verification_uri_complete:
                print(
                    f"Or open directly: {device_auth.verification_uri_complete}"
                )
            print("Waiting for approval...\n")
        token_set = oauth.poll_for_token(pc, device_auth, client)
        userinfo = oauth.fetch_userinfo(pc, token_set.access_token, client)

    email = _derive_email(userinfo)
    if uid is None:
        if not email:
            raise ValueError(
                "provider returned no email and --uid was not given; "
                "specify --uid explicitly"
            )
        uid = email.split("@", 1)[0]

    now = int(time.time())
    with get_session() as s:
        existing = s.scalar(
            select(UserConfig).where(
                UserConfig.provider_id == p.id, UserConfig.uid == uid
            )
        )
        if existing is not None:
            existing.access_token = token_set.access_token
            if email is not None:
                existing.email = email
            uc = existing
        else:
            uc = UserConfig(
                email=email,
                provider_id=p.id,
                access_token=token_set.access_token,
                uid=uid,
            )
            s.add(uc)
        s.commit()
        s.refresh(uc)

    if token_set.refresh_token is None:
        raise OAuthError(
            "provider did not return a refresh_token — is offline_access in scope?"
        )
    secrets.write(
        p.name,
        uid,
        secrets.SecretBlob(
            refresh_token=token_set.refresh_token,
            expires_at=now + token_set.expires_in,
            scope=token_set.scope,
        ),
    )
    return uc


def logout(uid: str, provider_name: Optional[str] = None) -> int:
    """Remove userconfig rows + keyring entries. Returns count removed."""
    removed = 0
    with get_session() as s:
        q = select(UserConfig).where(UserConfig.uid == uid)
        if provider_name is not None:
            p = s.scalar(select(Provider).where(Provider.name == provider_name))
            if p is None:
                return 0
            q = q.where(UserConfig.provider_id == p.id)
        rows = list(s.scalars(q))
        for uc in rows:
            p = s.get(Provider, uc.provider_id)
            if p is not None:
                secrets.delete(p.name, uc.uid)
            s.delete(uc)
            removed += 1
        s.commit()
    return removed


# ---- refresh ----------------------------------------------------------------


def refresh_one(uc: UserConfig, p: Provider, now: int) -> bool:
    """Refresh one account's access token. Returns True on success.

    On InvalidGrantError, leaves DB row and keyring entry alone (so the
    operator can see the broken state and re-login deliberately).
    """
    blob = secrets.read(p.name, uc.uid)
    if blob is None:
        log.error(
            "account %s (%s): missing keyring entry — re-login required",
            uc.email,
            p.name,
        )
        return False

    pc = _to_provider_config(p)
    new = None
    with httpx.Client() as client:
        for attempt in range(RETRY_ATTEMPTS):
            try:
                new = oauth.refresh(pc, blob.refresh_token, client)
                break
            except InvalidGrantError as e:
                log.error(
                    "account %s (%s): refresh token rejected (%s) — re-login required",
                    uc.email,
                    p.name,
                    e,
                )
                return False
            except (httpx.HTTPError, OAuthError) as e:
                if attempt == RETRY_ATTEMPTS - 1:
                    log.error(
                        "account %s (%s): refresh failed after %d attempts: %s",
                        uc.email,
                        p.name,
                        RETRY_ATTEMPTS,
                        e,
                    )
                    return False
                backoff = 2**attempt
                log.warning(
                    "account %s (%s): refresh attempt %d failed (%s); retrying in %ds",
                    uc.email,
                    p.name,
                    attempt + 1,
                    e,
                    backoff,
                )
                time.sleep(backoff)

    assert new is not None
    with get_session() as s:
        row = s.get(UserConfig, uc.id)
        if row is not None:
            row.access_token = new.access_token
            s.commit()

    secrets.write(
        p.name,
        uc.uid,
        secrets.SecretBlob(
            refresh_token=new.refresh_token or blob.refresh_token,
            expires_at=now + new.expires_in,
            scope=new.scope or blob.scope,
        ),
    )
    return True


def refresh_due(horizon_seconds: int) -> tuple[int, int]:
    """Refresh accounts whose token expires within `horizon_seconds`.

    Returns (succeeded, failed). 'succeeded' counts only the accounts that
    actually needed and got a refresh; accounts not yet due are ignored.
    """
    now = int(time.time())
    succeeded = 0
    failed = 0
    for uc, p in list_accounts():
        blob = secrets.read(p.name, uc.uid)
        if blob is None:
            log.warning(
                "account %s (%s): no keyring entry, skipping",
                uc.email,
                p.name,
            )
            failed += 1
            continue
        if blob.expires_at >= now + horizon_seconds:
            continue  # not due yet
        if refresh_one(uc, p, now):
            succeeded += 1
        else:
            failed += 1
    return succeeded, failed


# ---- import token (fallback path D from spec) -------------------------------


def import_token(
    provider_name: str,
    uid: str,
    refresh_token: str,
    email: Optional[str] = None,
) -> UserConfig:
    """Take a refresh token (e.g. copied from prod) and bootstrap an account.

    Immediately exchanges it for an access token, optionally calls userinfo to
    discover the email if one was not provided.
    """
    p = get_provider(provider_name)
    if p is None:
        raise ValueError(f"unknown provider {provider_name!r}")
    pc = _to_provider_config(p)

    with httpx.Client() as client:
        new = oauth.refresh(pc, refresh_token, client)
        if email is None:
            try:
                userinfo = oauth.fetch_userinfo(pc, new.access_token, client)
                email = _derive_email(userinfo)
            except Exception as e:
                log.warning("could not fetch userinfo for imported token: %s", e)
                email = None

    now = int(time.time())
    with get_session() as s:
        existing = s.scalar(
            select(UserConfig).where(
                UserConfig.provider_id == p.id, UserConfig.uid == uid
            )
        )
        if existing is not None:
            existing.access_token = new.access_token
            if email is not None:
                existing.email = email
            uc = existing
        else:
            uc = UserConfig(
                email=email,
                provider_id=p.id,
                access_token=new.access_token,
                uid=uid,
            )
            s.add(uc)
        s.commit()
        s.refresh(uc)

    secrets.write(
        p.name,
        uid,
        secrets.SecretBlob(
            refresh_token=new.refresh_token or refresh_token,
            expires_at=now + new.expires_in,
            scope=new.scope,
        ),
    )
    return uc
