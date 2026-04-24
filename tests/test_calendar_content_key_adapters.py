"""Per-adapter tests: each _*_to_sync_item populates fields[content_key]
when summary + dtstart_utc are both present, and leaves it absent otherwise."""
from __future__ import annotations

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
