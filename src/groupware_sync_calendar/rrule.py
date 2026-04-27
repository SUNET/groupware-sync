"""Graph recurrence ↔ RRULE translation.

Translates between Microsoft Graph's structured recurrence JSON and
RFC 5545 RRULE text format used in our intermediate representation.
"""

from __future__ import annotations

import re
from typing import Any

GRAPH_DAY_TO_RRULE: dict[str, str] = {
    "sunday": "SU",
    "monday": "MO",
    "tuesday": "TU",
    "wednesday": "WE",
    "thursday": "TH",
    "friday": "FR",
    "saturday": "SA",
}
RRULE_DAY_TO_GRAPH: dict[str, str] = {v: k for k, v in GRAPH_DAY_TO_RRULE.items()}

GRAPH_INDEX_TO_NUM: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "last": -1,
}
NUM_TO_GRAPH_INDEX: dict[int, str] = {v: k for k, v in GRAPH_INDEX_TO_NUM.items()}

# Regex to parse a BYDAY value like "2TU" or "-1FR"
_BYDAY_RE = re.compile(r"^([+-]?\d+)?([A-Z]{2})$")


def graph_recurrence_to_rrule(recurrence: dict[str, Any]) -> str:
    """Convert a Microsoft Graph recurrence object to an RFC 5545 RRULE string.

    Args:
        recurrence: Graph recurrence dict with ``pattern`` and ``range`` keys.

    Returns:
        RRULE string, e.g. ``FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,WE,FR``.
    """
    pattern = recurrence["pattern"]
    rng = recurrence["range"]
    pat_type: str = pattern["type"]
    interval: int = pattern.get("interval", 1)

    parts: list[str] = []

    if pat_type == "daily":
        parts.append("FREQ=DAILY")
        parts.append(f"INTERVAL={interval}")

    elif pat_type == "weekly":
        parts.append("FREQ=WEEKLY")
        parts.append(f"INTERVAL={interval}")
        days = pattern.get("daysOfWeek", [])
        if days:
            rrule_days = [GRAPH_DAY_TO_RRULE[d] for d in days]
            parts.append(f"BYDAY={','.join(rrule_days)}")

    elif pat_type == "absoluteMonthly":
        parts.append("FREQ=MONTHLY")
        parts.append(f"INTERVAL={interval}")
        parts.append(f"BYMONTHDAY={pattern['dayOfMonth']}")

    elif pat_type == "relativeMonthly":
        parts.append("FREQ=MONTHLY")
        parts.append(f"INTERVAL={interval}")
        index_num = GRAPH_INDEX_TO_NUM[pattern["index"]]
        day_abbr = GRAPH_DAY_TO_RRULE[pattern["daysOfWeek"][0]]
        parts.append(f"BYDAY={index_num}{day_abbr}")

    elif pat_type == "absoluteYearly":
        parts.append("FREQ=YEARLY")
        parts.append(f"INTERVAL={interval}")
        parts.append(f"BYMONTH={pattern['month']}")
        parts.append(f"BYMONTHDAY={pattern['dayOfMonth']}")

    elif pat_type == "relativeYearly":
        parts.append("FREQ=YEARLY")
        parts.append(f"INTERVAL={interval}")
        parts.append(f"BYMONTH={pattern['month']}")
        index_num = GRAPH_INDEX_TO_NUM[pattern["index"]]
        day_abbr = GRAPH_DAY_TO_RRULE[pattern["daysOfWeek"][0]]
        parts.append(f"BYDAY={index_num}{day_abbr}")

    else:
        msg = f"Unknown Graph recurrence pattern type: {pat_type}"
        raise ValueError(msg)

    # Range
    range_type: str = rng["type"]
    if range_type == "endDate":
        end_date: str = rng["endDate"].replace("-", "")
        parts.append(f"UNTIL={end_date}")
    elif range_type == "numbered":
        parts.append(f"COUNT={rng['numberOfOccurrences']}")
    elif range_type == "noEnd":
        pass  # No UNTIL or COUNT
    else:
        msg = f"Unknown Graph recurrence range type: {range_type}"
        raise ValueError(msg)

    return ";".join(parts)


def rrule_to_graph_recurrence(
    rrule: str, start_date: str | None = None
) -> dict[str, Any]:
    """Convert an RFC 5545 RRULE string to a Microsoft Graph recurrence object.

    Args:
        rrule: RRULE string, e.g. ``FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,WE,FR``.
        start_date: Anchor date for ``range.startDate`` in YYYY-MM-DD form.
            Microsoft Graph requires ``range.startDate`` on every recurrence
            object — it's optional in the JSON shape but the API rejects
            PATCH requests without it (returning ``ErrorInvalidOperation:
            The recurrence start date is too early`` because Graph falls
            back to an epoch-class sentinel). Callers MUST pass the
            event's start date (the YYYY-MM-DD prefix of dtstart_utc) so
            the resulting object survives both POST and PATCH.

    Returns:
        Graph recurrence dict with ``pattern`` and ``range`` keys.
    """
    params: dict[str, str] = {}
    for part in rrule.split(";"):
        key, _, value = part.partition("=")
        params[key] = value

    freq = params.get("FREQ", "")
    interval = int(params.get("INTERVAL", "1"))

    pattern: dict[str, Any] = {"interval": interval}
    rng: dict[str, Any] = {}

    # Determine pattern type from FREQ + other params
    if freq == "DAILY":
        pattern["type"] = "daily"

    elif freq == "WEEKLY":
        pattern["type"] = "weekly"
        if "BYDAY" in params:
            days = params["BYDAY"].split(",")
            pattern["daysOfWeek"] = [RRULE_DAY_TO_GRAPH[d] for d in days]

    elif freq == "MONTHLY":
        if "BYMONTHDAY" in params:
            pattern["type"] = "absoluteMonthly"
            pattern["dayOfMonth"] = int(params["BYMONTHDAY"])
        elif "BYDAY" in params:
            pattern["type"] = "relativeMonthly"
            _parse_relative_byday(params["BYDAY"], pattern)
        else:
            pattern["type"] = "absoluteMonthly"

    elif freq == "YEARLY":
        if "BYMONTH" in params:
            pattern["month"] = int(params["BYMONTH"])
        if "BYMONTHDAY" in params:
            pattern["type"] = "absoluteYearly"
            pattern["dayOfMonth"] = int(params["BYMONTHDAY"])
        elif "BYDAY" in params:
            pattern["type"] = "relativeYearly"
            _parse_relative_byday(params["BYDAY"], pattern)
        else:
            pattern["type"] = "absoluteYearly"

    else:
        msg = f"Unknown RRULE FREQ: {freq}"
        raise ValueError(msg)

    # Range
    if "UNTIL" in params:
        rng["type"] = "endDate"
        raw = params["UNTIL"]
        rng["endDate"] = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    elif "COUNT" in params:
        rng["type"] = "numbered"
        rng["numberOfOccurrences"] = int(params["COUNT"])
    else:
        rng["type"] = "noEnd"
    if start_date:
        rng["startDate"] = start_date

    return {"pattern": pattern, "range": rng}


def _parse_relative_byday(byday: str, pattern: dict[str, Any]) -> None:
    """Parse a BYDAY value like ``2TU`` or ``-1FR`` into pattern fields."""
    m = _BYDAY_RE.match(byday)
    if not m:
        msg = f"Cannot parse BYDAY value for relative pattern: {byday}"
        raise ValueError(msg)

    num_str, day_abbr = m.group(1), m.group(2)
    num = int(num_str) if num_str else 1
    pattern["index"] = NUM_TO_GRAPH_INDEX[num]
    pattern["daysOfWeek"] = [RRULE_DAY_TO_GRAPH[day_abbr]]
