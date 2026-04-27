"""Regression coverage for ping-pong drift between Graph and Stalwart.

Each bug fixed here came from a live run where four paired events
updated on alternating sides every sync — Graph's representation of a
field disagreed with Stalwart's representation of the same field, so
the merge swung back and forth indefinitely. Fixing them required
normalising the read side of one provider so the two adapters return
identical canonical values for the same underlying data.
"""
from __future__ import annotations

from groupware_sync_calendar.adapters.graph_adapter import _graph_to_sync_item
from groupware_sync_calendar.adapters.jmap_adapter import (
    _jmap_to_sync_item,
    _jscal_rrule_to_text,
)

# -- RRULE UNTIL: end-of-day datetime renders as bare date --------------------

def test_jscal_until_at_end_of_day_renders_as_date_only():
    """JSCalendar `until` is a LocalDateTime; we coerce date-only RRULE
    UNTIL to T23:59:59 on the way in for spec compliance. The reverse
    must elide the same time component so the round-tripped RRULE
    matches Graph's date-only output (Graph stores endDate=YYYY-MM-DD)."""
    rule = {"frequency": "weekly", "until": "2026-06-19T23:59:59"}
    text = _jscal_rrule_to_text(rule)
    assert "UNTIL=20260619" in text
    assert "T235959" not in text


def test_jscal_until_with_real_time_keeps_time_component():
    """If a server returns a non-end-of-day until (e.g. 12:00:00), keep
    it — only the T235959 sentinel is stripped."""
    rule = {"frequency": "weekly", "until": "2026-06-19T12:00:00"}
    text = _jscal_rrule_to_text(rule)
    assert "UNTIL=20260619T120000" in text


# -- all-day events: dtstart_tz/dtend_tz must round-trip as Etc/UTC -----------

def test_jmap_all_day_event_surfaces_etc_utc_timezone():
    """RFC 8984 §4.4.1: all-day events MUST NOT carry timeZone, so
    Stalwart returns showWithoutTime=true with no timeZone. Graph
    returns these same events as Windows tz 'UTC' which we map to
    'Etc/UTC'. Without symmetry the field drifts on every paired
    all-day event."""
    event = {
        "@type": "Event",
        "id": "ev1",
        "uid": "u1",
        "title": "Holiday",
        "start": "2026-04-23T00:00:00",
        "duration": "P1D",
        "showWithoutTime": True,
    }
    item = _jmap_to_sync_item(event)
    assert item.fields["dtstart_tz"] == "Etc/UTC"
    assert item.fields["dtend_tz"] == "Etc/UTC"
    assert item.fields["all_day"] is True


# -- reminder_minutes=0: dropped on Graph read -------------------------------

def test_graph_reader_drops_zero_minute_reminder():
    """Graph stores `isReminderOn=true reminderMinutesBeforeStart=0`
    as 'alert at start'. Stalwart silently drops alerts with offset
    PT0S, so syncing the value would ping-pong forever. Treat 0 as
    no reminder to match Stalwart's behaviour."""
    event = {
        "id": "ev2",
        "iCalUId": "u2",
        "subject": "Standup",
        "isReminderOn": True,
        "reminderMinutesBeforeStart": 0,
    }
    item = _graph_to_sync_item(event)
    assert "reminder_minutes" not in item.fields


def test_graph_reader_keeps_nonzero_reminder():
    event = {
        "id": "ev3",
        "iCalUId": "u3",
        "subject": "Standup",
        "isReminderOn": True,
        "reminderMinutesBeforeStart": 15,
    }
    item = _graph_to_sync_item(event)
    assert item.fields["reminder_minutes"] == 15
