"""Unit tests for calendar_content_key — the pure fallback-identity function.

Returns a stable cross-provider identity key derived from summary +
dtstart_utc, so the execute-time _identity_match in the sync engine can
pair calendar events whose uid differs between providers (e.g. when
Graph has reassigned iCalUId server-side on create)."""
from __future__ import annotations

from groupware_sync_calendar.identity import calendar_content_key


def test_none_summary_returns_none():
    assert calendar_content_key(None, "2026-05-01T12:00:00Z") is None


def test_none_dtstart_returns_none():
    assert calendar_content_key("Lunch", None) is None


def test_empty_summary_returns_none():
    assert calendar_content_key("", "2026-05-01T12:00:00Z") is None


def test_empty_dtstart_returns_none():
    assert calendar_content_key("Lunch", "") is None


def test_whitespace_only_summary_returns_none():
    assert calendar_content_key("   ", "2026-05-01T12:00:00Z") is None


def test_happy_path_returns_lowercase_summary_pipe_dtstart():
    assert (
        calendar_content_key("Lunch", "2026-05-01T12:00:00Z")
        == "lunch|2026-05-01T12:00:00Z"
    )


def test_case_folding_unifies_variants():
    a = calendar_content_key("Lunch", "2026-05-01T12:00:00Z")
    b = calendar_content_key("lunch", "2026-05-01T12:00:00Z")
    c = calendar_content_key("LUNCH", "2026-05-01T12:00:00Z")
    assert a == b == c


def test_surrounding_whitespace_stripped():
    assert (
        calendar_content_key("  Lunch  ", "2026-05-01T12:00:00Z")
        == calendar_content_key("Lunch", "2026-05-01T12:00:00Z")
    )


def test_different_dtstart_produces_different_key():
    a = calendar_content_key("Lunch", "2026-05-01T12:00:00Z")
    b = calendar_content_key("Lunch", "2026-05-01T13:00:00Z")
    assert a != b


def test_different_summary_produces_different_key():
    a = calendar_content_key("Lunch", "2026-05-01T12:00:00Z")
    b = calendar_content_key("Dinner", "2026-05-01T12:00:00Z")
    assert a != b


def test_german_eszett_casefolds_to_ss():
    """casefold handles locale-sensitive case where lower() would not.
    Sanity check of the function's chosen case-folding operation."""
    a = calendar_content_key("straße", "2026-05-01T12:00:00Z")
    b = calendar_content_key("STRASSE", "2026-05-01T12:00:00Z")
    assert a == b
