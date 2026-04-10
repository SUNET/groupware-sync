"""Typer CLI for groupware-sync-contacts.

One command: `sync`. Reads all config from env vars. Constructs providers,
opens the state DB, calls the sync engine, prints a summary, exits.
"""
from __future__ import annotations

import logging
import sys

import typer

from groupware_sync_contacts import auth
from groupware_sync_contacts.config import Config
from groupware_sync_contacts.providers.graph import GraphContactProvider
from groupware_sync_contacts.providers.jmap import JmapContactProvider
from groupware_sync_contacts.state.db import make_session_factory
from groupware_sync_contacts.sync import sync

app = typer.Typer(
    help="Two-way contacts sync between Stalwart and Microsoft 365",
    no_args_is_help=True,
)


@app.callback()
def _callback() -> None:
    """Two-way contacts sync between Stalwart and Microsoft 365."""


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command(name="sync")
def sync_cmd(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable debug logging"
    ),
) -> None:
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    # Load config
    try:
        cfg = Config.from_env()
    except ValueError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2)

    # Get access tokens
    try:
        stalwart_token = auth.get_access_token(
            cfg.stalwart.auth_database_url,
            cfg.stalwart.auth_uid,
            cfg.stalwart.auth_provider_name,
        )
    except ValueError as e:
        typer.echo(f"stalwart auth error: {e}", err=True)
        raise typer.Exit(2)

    try:
        m365_token = auth.get_access_token(
            cfg.m365.auth_database_url,
            cfg.m365.auth_uid,
            cfg.m365.auth_provider_name,
        )
    except ValueError as e:
        typer.echo(f"m365 auth error: {e}", err=True)
        raise typer.Exit(2)

    # Construct providers
    jmap_provider = JmapContactProvider(cfg.stalwart_jmap_url, stalwart_token)
    graph_provider = GraphContactProvider(m365_token)

    # Open state DB
    session_factory = make_session_factory(cfg.state_database_url)

    try:
        with session_factory() as session:
            summary = sync(jmap_provider, graph_provider, session)
    except Exception as e:
        log.error("sync failed: %s", e, exc_info=True)
        typer.echo(f"sync failed: {e}", err=True)
        raise typer.Exit(2)
    finally:
        jmap_provider.close()
        graph_provider.close()

    typer.echo(
        f"sync: addressbooks={summary.addressbooks}"
        f" created={summary.created}"
        f" updated={summary.updated}"
        f" deleted={summary.deleted}"
        f" conflicts={summary.conflicts}"
        f" errors={summary.errors}"
    )

    if summary.errors > 0:
        raise typer.Exit(1)
    raise typer.Exit(0)


if __name__ == "__main__":
    app()
