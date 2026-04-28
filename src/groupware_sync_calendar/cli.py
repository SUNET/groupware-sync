"""Typer CLI for groupware-sync-calendar."""
from __future__ import annotations

import logging
import sys
from typing import Optional

import typer

from groupware_sync import auth as fw_auth
from groupware_sync.config import CALENDAR_BACKENDS, Config
from groupware_sync.engine import sync_trees
from groupware_sync.models import ItemType
from groupware_sync.provider import SyncProvider
from groupware_sync.state.db import make_session_factory

app = typer.Typer(
    help="Two-way calendar sync between supported backends "
         "(JMAP / Graph / CalDAV)",
    no_args_is_help=True,
)


@app.callback()
def _callback() -> None:
    pass


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _stdin_isatty() -> bool:
    """Indirection so tests can stub TTY detection reliably.

    The Click/Typer test runner replaces ``sys.stdin`` with a pipe-backed
    wrapper inside ``invoke()``, so patching ``sys.stdin.isatty`` from the
    outside doesn't take effect during the command body. Routing through
    this module-level function gives tests a stable patch target.
    """
    return sys.stdin.isatty()


def _build_calendar_provider(cfg: Config, side: str) -> SyncProvider:
    """Construct the calendar provider for a given side based on the
    backend selector. Raises typer.Exit on auth or config errors."""
    backend = cfg.side_a_backend if side == "a" else cfg.side_b_backend

    if backend not in CALENDAR_BACKENDS:
        typer.echo(
            f"side {side}: unsupported calendar backend '{backend}' "
            f"(expected one of {sorted(CALENDAR_BACKENDS)})", err=True,
        )
        raise typer.Exit(2)

    if backend == "jmap":
        from groupware_sync_calendar.adapters.jmap_adapter import JmapCalendarAdapter
        try:
            token = fw_auth.get_access_token(
                cfg.stalwart.auth_database_url,
                cfg.stalwart.auth_uid,
                cfg.stalwart.auth_provider_name,
            )
        except ValueError as e:
            typer.echo(f"stalwart auth error: {e}", err=True)
            raise typer.Exit(2)
        return JmapCalendarAdapter(
            cfg.stalwart_jmap_url, token,
            calendar_filter=cfg.stalwart_calendar,
        )

    if backend == "graph":
        from groupware_sync_calendar.adapters.graph_adapter import GraphCalendarAdapter
        try:
            token = fw_auth.get_access_token(
                cfg.m365.auth_database_url,
                cfg.m365.auth_uid,
                cfg.m365.auth_provider_name,
            )
        except Exception as e:
            typer.echo(f"m365 auth error: {e}", err=True)
            raise typer.Exit(2)
        return GraphCalendarAdapter(
            token, calendar_filter=cfg.m365_calendar,
        )

    # backend == "caldav"
    dav = cfg.side_a_dav if side == "a" else cfg.side_b_dav
    if dav is None:
        typer.echo(
            f"side {side}: caldav backend selected but "
            f"SYNC_SIDE_{side.upper()}_DAV_* env vars are missing", err=True,
        )
        raise typer.Exit(2)
    try:
        from groupware_sync_calendar.adapters.caldav_adapter import (
            CalDavCalendarAdapter,
        )
    except ImportError as e:
        # CalDAV adapter pulls in vobject for iCalendar parsing. It's not
        # required by the default jmap/graph topology, so translate the
        # import error into a clear exit instead of letting it crash.
        typer.echo(
            f"side {side}: caldav backend requires the 'vobject' package "
            f"(pip install vobject): {e}", err=True,
        )
        raise typer.Exit(2)
    # Reuse the side-specific calendar_filter env var (stalwart_calendar
    # for side A, m365_calendar for side B) so existing operators don't
    # learn a third name.
    calendar_filter = cfg.stalwart_calendar if side == "a" else cfg.m365_calendar
    return CalDavCalendarAdapter(
        dav.base_url, dav.username, dav.password,
        calendar_filter=calendar_filter,
    )


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
             f"One of: {', '.join(sorted(CALENDAR_BACKENDS))}.",
    ),
    side_b_backend: Optional[str] = typer.Option(
        None, "--side-b-backend",
        help=f"Backend for side B. Overrides SYNC_SIDE_B_BACKEND. "
             f"One of: {', '.join(sorted(CALENDAR_BACKENDS))}.",
    ),
) -> None:
    """Two-way calendar sync using the tree-based framework engine."""
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

    if side_a_backend is not None:
        import os
        os.environ["SYNC_SIDE_A_BACKEND"] = side_a_backend
    if side_b_backend is not None:
        import os
        os.environ["SYNC_SIDE_B_BACKEND"] = side_b_backend

    from groupware_sync_calendar.specs import CALENDAR_EVENT_SPEC

    try:
        cfg = Config.from_env()
    except ValueError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2)

    provider_a = _build_calendar_provider(cfg, "a")
    provider_b = _build_calendar_provider(cfg, "b")
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
                provider_a, provider_b, ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, session,
                dry_run=dry_run, confirm=confirm,
            )
    except Exception as e:
        log.error("sync failed: %s", e, exc_info=True)
        typer.echo(f"sync failed: {e}", err=True)
        raise typer.Exit(2)
    finally:
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


@app.command(name="repair-jmap-alerts")
def repair_jmap_alerts_cmd(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report counts without making changes"),
) -> None:
    """One-shot: rewrite malformed Alert objects on Stalwart.

    Earlier emitter versions wrote Alerts without @type="Alert" and
    older VALARM imports landed without a trigger; both crash Stalwart's
    calendar UI with `r.trigger is undefined`. Steady-state sync won't
    rewrite these events on its own (nothing else about them drifts),
    so this walks every event and patches alerts in place.
    """
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    from groupware_sync_calendar.adapters.jmap_adapter import JmapCalendarAdapter

    try:
        cfg = Config.from_env()
    except ValueError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2)

    # `repair-jmap-alerts` is JMAP-specific by design and runs against
    # Stalwart regardless of the side selectors, but the per-side backend
    # work made `cfg.stalwart.*` optional — when neither side selects
    # `jmap`, those fields are empty strings and downstream auth code
    # raises a SQLAlchemy URL parse error rather than a clean ValueError.
    # Validate up-front so the operator sees the missing-config message.
    if not (cfg.stalwart.auth_database_url and cfg.stalwart.auth_uid
            and cfg.stalwart.auth_provider_name and cfg.stalwart_jmap_url):
        typer.echo(
            "repair-jmap-alerts requires Stalwart config: set "
            "SYNC_STALWART_JMAP_URL, SYNC_STALWART_AUTH_DATABASE_URL, "
            "SYNC_STALWART_AUTH_UID, SYNC_STALWART_AUTH_PROVIDER_NAME",
            err=True,
        )
        raise typer.Exit(2)

    try:
        stalwart_token = fw_auth.get_access_token(
            cfg.stalwart.auth_database_url, cfg.stalwart.auth_uid, cfg.stalwart.auth_provider_name,
        )
    except Exception as e:
        typer.echo(f"stalwart auth error: {e}", err=True)
        raise typer.Exit(2)

    jmap = JmapCalendarAdapter(
        cfg.stalwart_jmap_url, stalwart_token, calendar_filter=cfg.stalwart_calendar,
    )
    try:
        counts = jmap.repair_malformed_alerts(dry_run=dry_run)
    except Exception as e:
        log.error("repair failed: %s", e, exc_info=True)
        typer.echo(f"repair failed: {e}", err=True)
        raise typer.Exit(2)
    finally:
        jmap.close()

    prefix = "repair (dry-run)" if dry_run else "repair"
    typer.echo(
        f"{prefix}: scanned={counts['scanned']}"
        f" malformed={counts['malformed']}"
        f" repaired={counts['repaired']}"
        f" cleared={counts['cleared']}"
        f" errors={counts['errors']}"
    )
    raise typer.Exit(0 if counts["errors"] == 0 else 1)


if __name__ == "__main__":
    app()
