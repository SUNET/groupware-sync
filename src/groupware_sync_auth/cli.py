"""Typer CLI for groupware-sync-auth.

Commands are thin wrappers around groupware_sync_auth.service. The CLI module
should contain no business logic — only argument parsing, output formatting,
and exit codes.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import typer

from groupware_sync_auth import service
from groupware_sync_auth.oauth import DeviceAuthorization
from groupware_sync_auth.storage import secrets

app = typer.Typer(
    help="OAuth helper for groupware-sync development",
    no_args_is_help=True,
)

provider_app = typer.Typer(help="Manage OAuth providers", no_args_is_help=True)
app.add_typer(provider_app, name="provider")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.callback()
def main(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable debug logging"
    ),
) -> None:
    _setup_logging(verbose)


# ---- provider subcommands ---------------------------------------------------


@provider_app.command("add")
def provider_add(
    name: str = typer.Option(..., "--name"),
    client_id: str = typer.Option(..., "--client-id"),
    client_secret: Optional[str] = typer.Option(None, "--client-secret"),
    device_authorization_endpoint: str = typer.Option(
        ..., "--device-authorization-endpoint"
    ),
    token_endpoint: str = typer.Option(..., "--token-endpoint"),
    user_endpoint: str = typer.Option(..., "--user-endpoint"),
    scope: str = typer.Option(..., "--scope"),
) -> None:
    p = service.add_provider(
        name=name,
        client_id=client_id,
        client_secret=client_secret,
        device_authorization_endpoint=device_authorization_endpoint,
        token_endpoint=token_endpoint,
        user_endpoint=user_endpoint,
        scope=scope,
    )
    typer.echo(f"added provider {p.name} (id={p.id})")


@provider_app.command("list")
def provider_list() -> None:
    rows = service.list_providers()
    if not rows:
        typer.echo("no providers")
        return
    for p in rows:
        typer.echo(f"{p.id}\t{p.name}\t{p.client_id}\t{p.scope}")


@provider_app.command("show")
def provider_show(name: str = typer.Argument(...)) -> None:
    p = service.get_provider(name)
    if p is None:
        typer.echo(f"no provider named {name}", err=True)
        raise typer.Exit(1)
    for field in (
        "id",
        "name",
        "client_id",
        "client_secret",
        "device_authorization_endpoint",
        "token_endpoint",
        "user_endpoint",
        "scope",
    ):
        typer.echo(f"{field}: {getattr(p, field)}")


@provider_app.command("remove")
def provider_remove(name: str = typer.Argument(...)) -> None:
    try:
        service.remove_provider(name)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"removed {name}")


# ---- login / logout ---------------------------------------------------------


def _print_device_code(device_auth: DeviceAuthorization) -> None:
    typer.echo("")
    typer.echo(
        f"Visit {device_auth.verification_uri} and enter code: "
        f"{device_auth.user_code}"
    )
    if device_auth.verification_uri_complete:
        typer.echo(f"Or open directly: {device_auth.verification_uri_complete}")
    typer.echo("Waiting for approval...")
    typer.echo("")


@app.command()
def login(
    provider: str = typer.Option(..., "--provider"),
    uid: Optional[str] = typer.Option(None, "--uid"),
) -> None:
    try:
        uc = service.login(
            provider_name=provider, uid=uid, on_device_code=_print_device_code
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(
        f"logged in: uid={uc.uid} email={uc.email} provider_id={uc.provider_id}"
    )


@app.command()
def logout(
    uid: str = typer.Option(..., "--uid"),
    provider: Optional[str] = typer.Option(None, "--provider"),
) -> None:
    n = service.logout(uid, provider)
    if n == 0:
        typer.echo(f"no accounts matched uid={uid}", err=True)
        raise typer.Exit(1)
    typer.echo(f"removed {n} account(s)")


# ---- list / show ------------------------------------------------------------


@app.command("list")
def list_cmd() -> None:
    rows = service.list_accounts()
    if not rows:
        typer.echo("no accounts")
        return
    typer.echo("id\tprovider\tuid\temail\texpires_at")
    for uc, p in rows:
        blob = secrets.read(p.name, uc.uid)
        expires = blob.expires_at if blob is not None else "?"
        typer.echo(f"{uc.id}\t{p.name}\t{uc.uid}\t{uc.email}\t{expires}")


@app.command()
def show(uid: str = typer.Option(..., "--uid")) -> None:
    matches = service.get_accounts_by_uid(uid)
    if not matches:
        typer.echo(f"no account with uid={uid}", err=True)
        raise typer.Exit(1)
    for uc, p in matches:
        blob = secrets.read(p.name, uc.uid)
        typer.echo(f"id: {uc.id}")
        typer.echo(f"provider: {p.name}")
        typer.echo(f"uid: {uc.uid}")
        typer.echo(f"email: {uc.email}")
        typer.echo(f"access_token: {uc.access_token[:40]}... ({len(uc.access_token)} chars)")
        if blob is not None:
            now = int(time.time())
            ttl = blob.expires_at - now
            typer.echo(
                f"expires_at: {blob.expires_at} (in {ttl}s)"
            )
            typer.echo(f"scope: {blob.scope}")
            typer.echo(
                f"refresh_token: <in keyring, {len(blob.refresh_token)} chars>"
            )
        else:
            typer.echo("WARNING: no keyring entry for this account", err=True)
        typer.echo("")


# ---- refresh / tick ---------------------------------------------------------


@app.command()
def refresh(
    uid: Optional[str] = typer.Option(None, "--uid"),
    all_: bool = typer.Option(False, "--all"),
) -> None:
    if (uid is None) == (not all_):
        typer.echo("specify exactly one of --uid or --all", err=True)
        raise typer.Exit(2)
    rows = service.list_accounts()
    targets = rows if all_ else [pair for pair in rows if pair[0].uid == uid]
    if not targets:
        typer.echo("no matching accounts", err=True)
        raise typer.Exit(1)
    now = int(time.time())
    failed = 0
    for uc, p in targets:
        ok = service.refresh_one(uc, p, now)
        if ok:
            typer.echo(f"refreshed {uc.uid} ({p.name})")
        else:
            typer.echo(f"FAILED {uc.uid} ({p.name})", err=True)
            failed += 1
    raise typer.Exit(1 if failed else 0)


def _parse_horizon(s: str) -> int:
    s = s.strip()
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("s"):
        return int(s[:-1])
    return int(s)


@app.command()
def tick(
    horizon: str = typer.Option(
        "15m", "--horizon", help="Refresh tokens expiring within this window"
    ),
) -> None:
    seconds = _parse_horizon(horizon)
    succeeded, failed = service.refresh_due(seconds)
    typer.echo(f"tick: succeeded={succeeded} failed={failed}")
    raise typer.Exit(1 if failed else 0)


# ---- import-token -----------------------------------------------------------


@app.command("import-token")
def import_token_cmd(
    provider: str = typer.Option(..., "--provider"),
    uid: str = typer.Option(..., "--uid"),
    refresh_token: str = typer.Option(..., "--refresh-token"),
    email: Optional[str] = typer.Option(None, "--email"),
) -> None:
    try:
        uc = service.import_token(provider, uid, refresh_token, email)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"imported: uid={uc.uid} email={uc.email}")


if __name__ == "__main__":
    app()
