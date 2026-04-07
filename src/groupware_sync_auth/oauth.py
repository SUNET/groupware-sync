"""RFC 8628 OAuth 2.0 Device Authorization Grant + refresh token grant.

Storage-agnostic. Knows nothing about SQLAlchemy or keyring. Takes a
ProviderConfig (a plain dataclass, not the SQLAlchemy model — keeps this module
independent of storage) and an httpx.Client passed in by the caller.

Errors are raised as typed exceptions so the service layer can decide which
ones are terminal (InvalidGrantError) versus transient (network errors,
HTTP 5xx via httpx.HTTPError).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
HTTP_TIMEOUT_SECONDS = 30.0


# ---- exceptions -------------------------------------------------------------


class OAuthError(Exception):
    """Base for all OAuth-flow-level errors raised by this module."""


class AuthorizationPendingError(OAuthError):
    """Server said authorization_pending — expected during polling."""


class SlowDownError(OAuthError):
    """Server said slow_down — caller should increase poll interval by 5s."""


class ExpiredTokenError(OAuthError):
    """Device code expired before the user approved."""


class AccessDeniedError(OAuthError):
    """User explicitly denied the authorization."""


class InvalidGrantError(OAuthError):
    """Refresh token is dead — re-login required. TERMINAL."""


# ---- data carriers ----------------------------------------------------------


@dataclass
class ProviderConfig:
    name: str
    client_id: str
    client_secret: Optional[str]
    device_authorization_endpoint: str
    token_endpoint: str
    user_endpoint: str
    scope: str  # space-separated


@dataclass
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str]
    expires_in: int
    interval: int


@dataclass
class TokenSet:
    access_token: str
    refresh_token: Optional[str]  # may be omitted on refresh response
    expires_in: int
    scope: Optional[str]
    token_type: str


# ---- protocol ---------------------------------------------------------------


def request_device_code(
    provider: ProviderConfig, client: httpx.Client
) -> DeviceAuthorization:
    data = {"client_id": provider.client_id, "scope": provider.scope}
    if provider.client_secret:
        data["client_secret"] = provider.client_secret
    r = client.post(
        provider.device_authorization_endpoint,
        data=data,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if r.status_code != 200:
        try:
            raise _parse_error(r.json())
        except OAuthError:
            raise
        except Exception:
            r.raise_for_status()
            raise OAuthError(
                f"unexpected device code response {r.status_code}: {r.text}"
            )
    body = r.json()
    return DeviceAuthorization(
        device_code=body["device_code"],
        user_code=body["user_code"],
        verification_uri=body["verification_uri"],
        verification_uri_complete=body.get("verification_uri_complete"),
        expires_in=int(body["expires_in"]),
        interval=int(body.get("interval", 5)),
    )


def poll_for_token(
    provider: ProviderConfig,
    device_auth: DeviceAuthorization,
    client: httpx.Client,
    on_pending: Optional[Callable[[], None]] = None,
) -> TokenSet:
    interval = device_auth.interval
    deadline = time.monotonic() + device_auth.expires_in

    while True:
        if time.monotonic() >= deadline:
            raise ExpiredTokenError("device code expired locally")

        time.sleep(interval)

        data = {
            "grant_type": DEVICE_CODE_GRANT,
            "device_code": device_auth.device_code,
            "client_id": provider.client_id,
        }
        if provider.client_secret:
            data["client_secret"] = provider.client_secret

        r = client.post(
            provider.token_endpoint,
            data=data,
            timeout=HTTP_TIMEOUT_SECONDS,
        )

        if r.status_code == 200:
            return _parse_token_response(r.json())

        try:
            err = _parse_error(r.json())
        except Exception:
            r.raise_for_status()
            raise OAuthError(
                f"unexpected token poll response {r.status_code}: {r.text}"
            )

        if isinstance(err, AuthorizationPendingError):
            if on_pending is not None:
                on_pending()
            continue
        if isinstance(err, SlowDownError):
            interval += 5
            continue
        # ExpiredTokenError, AccessDeniedError, anything else: terminal
        raise err


def refresh(
    provider: ProviderConfig, refresh_token: str, client: httpx.Client
) -> TokenSet:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": provider.client_id,
    }
    if provider.client_secret:
        data["client_secret"] = provider.client_secret
    # Some providers want scope on refresh, others reject it. Microsoft
    # accepts it; omit to keep things simple and let the IdP return whatever
    # the original token had.

    r = client.post(
        provider.token_endpoint,
        data=data,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if r.status_code != 200:
        try:
            raise _parse_error(r.json())
        except OAuthError:
            raise
        except Exception:
            r.raise_for_status()
            raise OAuthError(f"unexpected refresh response {r.status_code}: {r.text}")
    return _parse_token_response(r.json())


def fetch_userinfo(
    provider: ProviderConfig, access_token: str, client: httpx.Client
) -> dict:
    r = client.get(
        provider.user_endpoint,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    r.raise_for_status()
    return r.json()


# ---- helpers ----------------------------------------------------------------


_ERROR_MAP = {
    "authorization_pending": AuthorizationPendingError,
    "slow_down": SlowDownError,
    "expired_token": ExpiredTokenError,
    "access_denied": AccessDeniedError,
    "invalid_grant": InvalidGrantError,
}


def _parse_error(body: dict) -> OAuthError:
    err = body.get("error", "")
    desc = body.get("error_description", "")
    cls = _ERROR_MAP.get(err)
    if cls is not None:
        return cls(desc or err)
    return OAuthError(f"{err}: {desc}" if err else f"unknown error: {body}")


def _parse_token_response(body: dict) -> TokenSet:
    return TokenSet(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_in=int(body["expires_in"]),
        scope=body.get("scope"),
        token_type=body.get("token_type", "Bearer"),
    )
