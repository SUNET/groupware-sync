"""Content-based fallback identity for calendar events.

Graph's iCalUId is server-assigned and read-only: any value we POST is
silently replaced. That means ``uid``-based identity pairing (PR #2)
fails for every event our sync creates on Graph — on the next run,
Graph.iCalUId != Stalwart.uid and the tree engine sees the two sides
as orphans.

The engine's ``_identity_match`` (in ``groupware_sync.engine``) already
has a content-based fallback path used by the contacts type spec. This
module provides the calendar equivalent: a stable (summary, start-time)
key used by ``CALENDAR_EVENT_SPEC.identity_fields`` when the uid key
doesn't match.

See docs:2026-04-24-calendar-content-fallback-pairing-design.md for the
full rationale and the live-probe evidence.
"""
from __future__ import annotations


def calendar_content_key(
    summary: str | None, dtstart_utc: str | None
) -> str | None:
    """Return a stable cross-provider identity fallback for a calendar event.

    The key is ``f"{summary.strip().casefold()}|{dtstart_utc}"`` when both
    inputs are present and non-empty; otherwise ``None``.

    Returns None when either input is missing or whitespace-only. Callers
    must tolerate a None return (the engine's ``_identity_match`` skips
    None identity values).

    Note: two distinct events that genuinely share the same title and
    start-time (e.g. the same "Standup" on the same day in two different
    calendars) will collide on this key. ``_identity_match`` pairs each
    item at most once, so collisions degrade to "first pair wins, rest
    fall through as creates" — no duplicate-pairing regression.
    """
    if not summary or not summary.strip():
        return None
    if not dtstart_utc:
        return None
    return f"{summary.strip().casefold()}|{dtstart_utc}"
