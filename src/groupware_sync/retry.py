"""HTTP retry helpers shared across adapters."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

MAX_RETRY_AFTER_SECONDS = 120


def parse_retry_after(header_value: Optional[str], default: int) -> int:
    """Parse a ``Retry-After`` header value into a bounded integer of seconds.

    Accepts either delta-seconds or an HTTP-date per RFC 7231. Falls back to
    ``default`` on missing or malformed values, and caps the result at
    ``MAX_RETRY_AFTER_SECONDS`` so a misbehaving server can't stall the sync
    for arbitrary durations.
    """
    seconds = default
    if header_value is not None:
        try:
            seconds = int(header_value)
        except ValueError:
            dt = None
            try:
                dt = parsedate_to_datetime(header_value)
            except (TypeError, ValueError):
                pass
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                seconds = max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
    if seconds < 0:
        seconds = default
    return min(seconds, MAX_RETRY_AFTER_SECONDS)
