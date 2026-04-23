"""One-shot: list Stalwart JMAP calendars with event counts.

Uses the same config + auth as the CLI. Prints name, id, and event count.
Intended to diagnose cases where SYNC_STALWART_CALENDAR doesn't match.

NOTE: also prints a single sample event's title to help confirm the
authenticated account sees real data. Strip or redact that line before
sharing the output in a public issue/PR.

Run:  python3 scripts/list_stalwart_calendars.py
"""
from __future__ import annotations

import logging
import sys

from groupware_sync import auth as fw_auth
from groupware_sync.config import Config
from groupware_sync_calendar.adapters.jmap_adapter import JmapCalendarAdapter

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def main() -> int:
    cfg = Config.from_env()
    token = fw_auth.get_access_token(
        cfg.stalwart.auth_database_url,
        cfg.stalwart.auth_uid,
        cfg.stalwart.auth_provider_name,
    )
    jmap = JmapCalendarAdapter(cfg.stalwart_jmap_url, token, calendar_filter=None)
    jmap._ensure_session()  # noqa: SLF001 — diagnostic

    results = jmap._call([  # noqa: SLF001
        ["Calendar/get", {"accountId": jmap._account_id}, "cal0"],  # noqa: SLF001
    ])
    calendars: list[dict] = []
    for result in results:
        if result[0] == "Calendar/get":
            for item in result[1].get("list", []):
                calendars.append({"id": item["id"], "name": item.get("name", "<no-name>")})

    print(f"JMAP account_id: {jmap._account_id}")  # noqa: SLF001

    # Total events in this account, no filter at all
    q_all = jmap._call([  # noqa: SLF001
        [
            "CalendarEvent/query",
            {"accountId": jmap._account_id, "limit": 5, "calculateTotal": True},  # noqa: SLF001
            "qall",
        ],
    ])
    for r in q_all:
        if r[0] == "CalendarEvent/query":
            print(f"account-wide event count: total={r[1].get('total')} "
                  f"sample_ids={r[1].get('ids', [])[:3]}")

    # Peek at one event to see its calendarIds value
    sample_ids = []
    for r in q_all:
        if r[0] == "CalendarEvent/query":
            sample_ids = r[1].get("ids", [])[:1]
    if sample_ids:
        g = jmap._call([  # noqa: SLF001
            [
                "CalendarEvent/get",
                {
                    "accountId": jmap._account_id,  # noqa: SLF001
                    "ids": sample_ids,
                    "properties": ["id", "uid", "title", "calendarIds"],
                },
                "g0",
            ],
        ])
        for r in g:
            if r[0] == "CalendarEvent/get":
                for ev in r[1].get("list", []):
                    print(f"sample event: id={ev.get('id')!r} "
                          f"uid={ev.get('uid')!r} "
                          f"title={ev.get('title')!r} "
                          f"calendarIds={ev.get('calendarIds')!r}")

    print()
    print(f"{'EVENTS':>7}  {'NAME':<40}  ID")
    print(f"{'------':>7}  {'----':<40}  --")
    for cal in calendars:
        q = jmap._call([  # noqa: SLF001
            [
                "CalendarEvent/query",
                {
                    "accountId": jmap._account_id,  # noqa: SLF001
                    "filter": {"inCalendars": [cal["id"]]},
                    "limit": 1,
                    "calculateTotal": True,
                },
                "q0",
            ],
        ])
        total = None
        ids_len = 0
        for r in q:
            if r[0] == "CalendarEvent/query":
                total = r[1].get("total")
                ids_len = len(r[1].get("ids", []))
        count = total if total is not None else f"{ids_len}+"
        print(f"{str(count):>7}  {cal['name']:<40}  {cal['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
