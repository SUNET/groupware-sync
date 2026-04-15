"""Typer CLI for groupware-sync-calendar."""
from __future__ import annotations

import logging

import typer

app = typer.Typer(
    help="Two-way calendar sync between Stalwart and Microsoft 365",
    no_args_is_help=True,
)


@app.callback()
def _callback() -> None:
    pass


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


@app.command(name="sync")
def sync_cmd(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done without making changes"),
) -> None:
    """Two-way calendar sync using the tree-based framework engine."""
    _setup_logging(verbose)
    log = logging.getLogger(__name__)

    from groupware_sync import auth as fw_auth
    from groupware_sync.config import Config
    from groupware_sync.engine import sync_trees
    from groupware_sync.models import ItemType
    from groupware_sync.state.db import make_session_factory
    from groupware_sync_calendar.adapters.graph_adapter import GraphCalendarAdapter
    from groupware_sync_calendar.adapters.jmap_adapter import JmapCalendarAdapter
    from groupware_sync_calendar.specs import CALENDAR_EVENT_SPEC

    try:
        cfg = Config.from_env()
    except ValueError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2)

    try:
        stalwart_token = fw_auth.get_access_token(
            cfg.stalwart.auth_database_url, cfg.stalwart.auth_uid, cfg.stalwart.auth_provider_name,
        )
    except ValueError as e:
        typer.echo(f"stalwart auth error: {e}", err=True)
        raise typer.Exit(2)

    try:
        m365_token = fw_auth.get_access_token(
            cfg.m365.auth_database_url, cfg.m365.auth_uid, cfg.m365.auth_provider_name,
        )
    except (ValueError, Exception) as e:
        typer.echo(f"m365 auth error: {e}", err=True)
        raise typer.Exit(2)

    jmap = JmapCalendarAdapter(cfg.stalwart_jmap_url, stalwart_token, calendar_filter=cfg.stalwart_calendar)
    graph = GraphCalendarAdapter(m365_token, calendar_filter=cfg.m365_calendar)
    sf = make_session_factory(cfg.state_database_url)

    try:
        with sf() as session:
            summary = sync_trees(jmap, graph, ItemType.CALENDAR_EVENT, CALENDAR_EVENT_SPEC, session, dry_run=dry_run)
    except Exception as e:
        log.error("sync failed: %s", e, exc_info=True)
        typer.echo(f"sync failed: {e}", err=True)
        raise typer.Exit(2)
    finally:
        jmap.close()
        graph.close()

    typer.echo(
        f"sync: containers={summary.containers}"
        f" created={summary.created}"
        f" updated={summary.updated}"
        f" deleted={summary.deleted}"
        f" conflicts={summary.conflicts}"
        f" skipped={summary.skipped}"
        f" errors={summary.errors}"
    )

    if summary.errors > 0:
        raise typer.Exit(1)
    raise typer.Exit(0)


if __name__ == "__main__":
    app()
