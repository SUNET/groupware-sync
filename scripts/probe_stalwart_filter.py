"""Probe which Stalwart JMAP CalendarEvent filter shape actually works.

Tries several filter variants against the first calendar and reports the
match count + any method-level error.

Run:  python3 scripts/probe_stalwart_filter.py
"""
from __future__ import annotations

import json
import sys

from groupware_sync import auth as fw_auth
from groupware_sync.config import Config
from groupware_sync_calendar.adapters.jmap_adapter import JmapCalendarAdapter


def main() -> int:
    cfg = Config.from_env()
    token = fw_auth.get_access_token(
        cfg.stalwart.auth_database_url,
        cfg.stalwart.auth_uid,
        cfg.stalwart.auth_provider_name,
    )
    jmap = JmapCalendarAdapter(cfg.stalwart_jmap_url, token, calendar_filter=None)
    jmap._ensure_session()  # noqa: SLF001

    # Pick first calendar
    cal_id: str | None = None
    cal_name: str | None = None
    r = jmap._call([  # noqa: SLF001
        ["Calendar/get", {"accountId": jmap._account_id}, "c0"],  # noqa: SLF001
    ])
    for resp in r:
        if resp[0] == "Calendar/get":
            lst = resp[1].get("list", [])
            if lst:
                cal_id = lst[0]["id"]
                cal_name = lst[0].get("name")
    if not cal_id:
        print("no calendars visible")
        return 1
    print(f"probing calendar name={cal_name!r} id={cal_id!r}")

    variants: list[tuple[str, dict | None]] = [
        ("no filter", None),
        ("inCalendars list", {"inCalendars": [cal_id]}),
        ("inCalendar singular", {"inCalendar": cal_id}),
        ("calendarIds list", {"calendarIds": [cal_id]}),
        ("calendarId singular", {"calendarId": cal_id}),
        ("inCalendarIds list", {"inCalendarIds": [cal_id]}),
    ]

    for label, flt in variants:
        call_args: dict = {
            "accountId": jmap._account_id,  # noqa: SLF001
            "limit": 3,
            "calculateTotal": True,
        }
        if flt is not None:
            call_args["filter"] = flt
        resp = jmap._call([["CalendarEvent/query", call_args, "q"]])  # noqa: SLF001
        for r in resp:
            tag = r[0]
            body = r[1]
            if tag == "CalendarEvent/query":
                print(f"  {label:<22} total={body.get('total')} "
                      f"ids={body.get('ids', [])[:3]}")
            else:
                # error response
                print(f"  {label:<22} ERROR type={tag} body={json.dumps(body)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
