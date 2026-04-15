"""Tests for Windows↔IANA timezone mapping and UTC conversion."""

from groupware_sync_calendar.tz import (
    from_utc,
    iana_to_windows,
    to_utc,
    windows_to_iana,
)


def test_windows_to_iana_common():
    assert windows_to_iana("W. Europe Standard Time") == "Europe/Berlin"
    assert windows_to_iana("Eastern Standard Time") == "America/New_York"
    assert windows_to_iana("Pacific Standard Time") == "America/Los_Angeles"
    assert windows_to_iana("UTC") == "Etc/UTC"


def test_windows_to_iana_passthrough():
    """Already-IANA names (containing /) should pass through."""
    assert windows_to_iana("Europe/Stockholm") == "Europe/Stockholm"
    assert windows_to_iana("America/New_York") == "America/New_York"


def test_windows_to_iana_unknown():
    """Unknown Windows names should return Etc/UTC."""
    result = windows_to_iana("Nonexistent Standard Time")
    assert result == "Etc/UTC"


def test_iana_to_windows():
    assert iana_to_windows("Europe/Berlin") == "W. Europe Standard Time"
    assert iana_to_windows("America/New_York") == "Eastern Standard Time"


def test_iana_to_windows_unknown():
    assert iana_to_windows("Antarctica/Troll") == "UTC"  # fallback


def test_to_utc_stockholm():
    # Stockholm is UTC+2 in April (CEST)
    result = to_utc("2026-04-15T09:00:00", "Europe/Stockholm")
    assert result == "2026-04-15T07:00:00Z"


def test_to_utc_new_york():
    # New York is UTC-4 in April (EDT)
    result = to_utc("2026-04-15T09:00:00", "America/New_York")
    assert result == "2026-04-15T13:00:00Z"


def test_from_utc_stockholm():
    result = from_utc("2026-04-15T07:00:00Z", "Europe/Stockholm")
    assert result == "2026-04-15T09:00:00"


def test_from_utc_new_york():
    result = from_utc("2026-04-15T13:00:00Z", "America/New_York")
    assert result == "2026-04-15T09:00:00"


def test_from_utc_with_offset():
    """UTC strings with +00:00 instead of Z should work too."""
    result = from_utc("2026-04-15T07:00:00+00:00", "Europe/Stockholm")
    assert result == "2026-04-15T09:00:00"


def test_utc_roundtrip():
    original = "2026-04-15T14:30:00"
    tz = "Asia/Tokyo"
    utc = to_utc(original, tz)
    back = from_utc(utc, tz)
    assert back == original
