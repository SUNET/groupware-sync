"""Content-based identity for calendar events.

Microsoft Graph assigns a fresh ``iCalUId`` to every event created via
the API (the REST field is documented as read-only; the value a client
POSTs is silently discarded). A 2026-04-24 round-trip probe confirmed
this: send ``foo@bar.invalid``, get back a freshly generated GOID.

Consequence: ``uid``-based identity pairing (PR #2) cannot match a
Stalwart-originated event to its Graph counterpart after first sync —
Stalwart stores the uid we sent, Graph stores its own GOID, and
``hash(uid_a) != hash(uid_b)``. The tree engine then sees the same
event as orphans on both sides.

The fix is a cross-provider identity derived from content — title +
normalised UTC start. Adapters use this as the primary tree-level
identity when it can be computed, falling back to uid only when
summary or start is missing. ``CALENDAR_EVENT_SPEC.identity_fields``
also lists ``content_key`` so the engine's execute-time
``_identity_match`` catches edge cases where the tree-level fast path
missed.

See SUNET/groupware-sync PR #9 for the design + live-probe evidence.
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
