"""Typer CLI for groupware-sync-contacts.

One command: `sync`. Reads all config from env vars. Constructs providers,
opens the state DB, calls the sync engine, prints a summary, exits.
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

import typer

from groupware_sync import auth as fw_auth
from groupware_sync.config import CONTACT_BACKENDS, Config
from groupware_sync.engine import sync_trees
from groupware_sync.models import ItemType
from groupware_sync.provider import SyncProvider
from groupware_sync.state.db import make_session_factory

app = typer.Typer(
    help="Two-way contacts sync between supported backends "
         "(JMAP / Graph / CardDAV)",
    no_args_is_help=True,
)


@app.callback()
def _callback() -> None:
    """Two-way contacts sync between supported backends."""


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _stdin_isatty() -> bool:
    """Indirection so tests can stub TTY detection reliably.

    The Click/Typer test runner replaces ``sys.stdin`` with a pipe-backed
    wrapper inside ``invoke()``, so patching ``sys.stdin.isatty`` from the
    outside doesn't take effect during the command body. Routing through
    this module-level function gives tests a stable patch target.
    """
    return sys.stdin.isatty()


def _build_contact_provider(cfg: Config, side: str) -> SyncProvider:
    """Construct the contact provider for a given side based on the
    backend selector. Raises typer.Exit on auth or config errors so the
    CLI can route them through the existing error-reporting pattern."""
    backend = cfg.side_a_backend if side == "a" else cfg.side_b_backend

    if backend not in CONTACT_BACKENDS:
        typer.echo(
            f"side {side}: unsupported contacts backend '{backend}' "
            f"(expected one of {sorted(CONTACT_BACKENDS)})", err=True,
        )
        raise typer.Exit(2)

    if backend == "jmap":
        from groupware_sync_contacts.adapters.jmap_adapter import JmapContactAdapter
        try:
            token = fw_auth.get_access_token(
                cfg.stalwart.auth_database_url,
                cfg.stalwart.auth_uid,
                cfg.stalwart.auth_provider_name,
            )
        except ValueError as e:
            typer.echo(f"stalwart auth error: {e}", err=True)
            raise typer.Exit(2)
        return JmapContactAdapter(
            cfg.stalwart_jmap_url, token,
            addressbook_filter=cfg.stalwart_addressbook,
        )

    if backend == "graph":
        from groupware_sync_contacts.adapters.graph_adapter import GraphContactAdapter
        try:
            token = fw_auth.get_access_token(
                cfg.m365.auth_database_url,
                cfg.m365.auth_uid,
                cfg.m365.auth_provider_name,
            )
        except Exception as e:
            typer.echo(f"m365 auth error: {e}", err=True)
            raise typer.Exit(2)
        return GraphContactAdapter(
            token, addressbook_filter=cfg.m365_addressbook,
        )

    # backend == "carddav"
    dav = cfg.side_a_dav if side == "a" else cfg.side_b_dav
    if dav is None:
        typer.echo(
            f"side {side}: carddav backend selected but "
            f"SYNC_SIDE_{side.upper()}_DAV_* env vars are missing", err=True,
        )
        raise typer.Exit(2)
    from groupware_sync_contacts.adapters.carddav_adapter import CardDavContactAdapter
    return CardDavContactAdapter(dav.base_url, dav.username, dav.password)


@app.command(name="sync")
def sync_cmd(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done without making changes"),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Skip the interactive confirmation and execute immediately. Intended for cron and CI.",
    ),
    side_a_backend: Optional[str] = typer.Option(
        None, "--side-a-backend",
        help=f"Backend for side A. Overrides SYNC_SIDE_A_BACKEND. "
             f"One of: {', '.join(sorted(CONTACT_BACKENDS))}.",
    ),
    side_b_backend: Optional[str] = typer.Option(
        None, "--side-b-backend",
        help=f"Backend for side B. Overrides SYNC_SIDE_B_BACKEND. "
             f"One of: {', '.join(sorted(CONTACT_BACKENDS))}.",
    ),
) -> None:
    """Two-way contacts sync using the tree-based framework engine."""
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    # TTY guard: if default mode (not dry-run, not non-interactive) and
    # stdin isn't a TTY, fail fast before touching any remote state.
    if not dry_run and not non_interactive and not _stdin_isatty():
        typer.echo(
            "interactive mode requires a TTY; pass --non-interactive to auto-execute or --dry-run to preview only",
            err=True,
        )
        raise typer.Exit(2)

    # CLI flags override env-derived selectors before we reload the rest
    # of the config (which validates per-backend env vars).
    if side_a_backend is not None:
        import os
        os.environ["SYNC_SIDE_A_BACKEND"] = side_a_backend
    if side_b_backend is not None:
        import os
        os.environ["SYNC_SIDE_B_BACKEND"] = side_b_backend

    from groupware_sync_contacts.specs import CONTACT_SPEC

    try:
        cfg = Config.from_env()
    except ValueError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2)

    provider_a = _build_contact_provider(cfg, "a")
    provider_b = _build_contact_provider(cfg, "b")
    sf = make_session_factory(cfg.state_database_url)

    # Choose engine inputs. --dry-run wins silently over --non-interactive.
    confirm = None
    if not dry_run and not non_interactive:
        def confirm(ops, plan_summary):  # noqa: E306
            return typer.confirm(
                f"Execute {plan_summary.deleted} deletes, "
                f"{plan_summary.created} creates, "
                f"{plan_summary.updated} updates, "
                f"{plan_summary.containers} container ops?",
                default=False,
            )

    try:
        with sf() as session:
            summary = sync_trees(
                provider_a, provider_b, ItemType.CONTACT, CONTACT_SPEC, session,
                dry_run=dry_run, confirm=confirm,
            )
    except Exception as e:
        log.error("sync failed: %s", e, exc_info=True)
        typer.echo(f"sync failed: {e}", err=True)
        raise typer.Exit(2)
    finally:
        # Adapters expose .close() if they hold an httpx client; tolerate
        # absence so future protocol-only adapters needn't bother.
        for p in (provider_a, provider_b):
            close = getattr(p, "close", None)
            if callable(close):
                close()

    if summary.aborted:
        typer.echo("sync aborted by user — no changes made")
        raise typer.Exit(0)

    typer.echo(
        f"sync: containers={summary.containers}"
        f" created={summary.created}"
        f" updated={summary.updated}"
        f" deleted={summary.deleted}"
        f" conflicts={summary.conflicts}"
        f" skipped={summary.skipped}"
        f" errors={summary.errors}"
        f" healed={summary.identity_pairs_healed}"
    )

    if summary.errors > 0:
        raise typer.Exit(1)
    raise typer.Exit(0)


if __name__ == "__main__":
    app()
