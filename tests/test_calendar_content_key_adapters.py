"""Per-adapter tests: each _*_to_sync_item populates fields[content_key]
when summary + dtstart_utc are both present, and leaves it absent otherwise."""
from __future__ import annotations

from groupware_sync_calendar.adapters.caldav_adapter import _ical_to_sync_item
from groupware_sync_calendar.adapters.graph_adapter import _graph_to_sync_item
from groupware_sync_calendar.adapters.jmap_adapter import _jmap_to_sync_item

# -- JMAP ----------------------------------------------------------------------

def test_jmap_populates_content_key_when_summary_and_start_present():
    raw = {
        "id": "srv-1",
        "uid": "uid-1",
        "title": "Lunch",
        "start": "2026-05-01T12:00:00",
        "timeZone": "Etc/UTC",
        "duration": "PT1H",
    }
    item = _jmap_to_sync_item(raw)
    assert item.fields.get("content_key") == "lunch|2026-05-01T12:00:00Z"


def test_jmap_omits_content_key_when_summary_missing():
    raw = {
        "id": "srv-1",
        "uid": "uid-1",
        "start": "2026-05-01T12:00:00",
        "timeZone": "Etc/UTC",
    }
    item = _jmap_to_sync_item(raw)
    assert "content_key" not in item.fields


def test_jmap_omits_content_key_when_start_missing():
    raw = {
        "id": "srv-1",
        "uid": "uid-1",
        "title": "Lunch",
    }
    item = _jmap_to_sync_item(raw)
    assert "content_key" not in item.fields


def test_jmap_populates_content_key_when_timezone_missing():
    """No timeZone → the adapter's no-tz branch must still produce a
    usable dtstart_utc, and content_key should reflect it. Exercises the
    path that the other three tests skip."""
    raw = {
        "id": "srv-1",
        "uid": "uid-1",
        "title": "Lunch",
        "start": "2026-05-01T12:00:00",
    }
    item = _jmap_to_sync_item(raw)
    # dtstart_utc gets a trailing Z appended in the no-tz branch.
    assert item.fields.get("content_key") == "lunch|2026-05-01T12:00:00Z"


# -- Graph ---------------------------------------------------------------------

def test_graph_populates_content_key_when_subject_and_start_present():
    raw = {
        "id": "graph-1",
        "iCalUId": "040000008200E00074C5B7101A82E008...",
        "subject": "Lunch",
        "start": {"dateTime": "2026-05-01T12:00:00", "timeZone": "UTC"},
        "end":   {"dateTime": "2026-05-01T13:00:00", "timeZone": "UTC"},
    }
    item = _graph_to_sync_item(raw)
    assert item.fields.get("content_key") == "lunch|2026-05-01T12:00:00Z"


def test_graph_omits_content_key_when_subject_missing():
    raw = {
        "id": "graph-1",
        "iCalUId": "x",
        "start": {"dateTime": "2026-05-01T12:00:00", "timeZone": "UTC"},
        "end":   {"dateTime": "2026-05-01T13:00:00", "timeZone": "UTC"},
    }
    item = _graph_to_sync_item(raw)
    assert "content_key" not in item.fields


def test_graph_omits_content_key_when_start_missing():
    raw = {
        "id": "graph-1",
        "iCalUId": "x",
        "subject": "Lunch",
    }
    item = _graph_to_sync_item(raw)
    assert "content_key" not in item.fields


# -- CalDAV --------------------------------------------------------------------

def _ical(summary: str = "Lunch", include_dtstart: bool = True) -> str:
    body = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "UID:caldav-uid-1",
    ]
    if summary is not None:
        body.append(f"SUMMARY:{summary}")
    if include_dtstart:
        body.append("DTSTART:20260501T120000Z")
        body.append("DTEND:20260501T130000Z")
    body.append("END:VEVENT")
    body.append("END:VCALENDAR")
    return "\r\n".join(body) + "\r\n"


def test_caldav_populates_content_key_when_summary_and_start_present():
    item = _ical_to_sync_item(_ical(), "/cal/abc.ics", "etag-1")
    assert item.fields.get("content_key") == "lunch|2026-05-01T12:00:00Z"


def test_caldav_omits_content_key_when_summary_missing():
    item = _ical_to_sync_item(_ical(summary=None), "/cal/abc.ics", "etag-1")
    assert "content_key" not in item.fields


def test_caldav_omits_content_key_when_start_missing():
    item = _ical_to_sync_item(
        _ical(include_dtstart=False), "/cal/abc.ics", "etag-1"
    )
    assert "content_key" not in item.fields
