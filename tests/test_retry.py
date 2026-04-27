"""Tests for the shared Retry-After parser."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from groupware_sync.retry import MAX_RETRY_AFTER_SECONDS, parse_retry_after


def test_missing_header_uses_default():
    assert parse_retry_after(None, default=7) == 7


def test_integer_header():
    assert parse_retry_after("30", default=5) == 30


def test_caps_excessive_value():
    # A misbehaving server returning a day-long delay must not stall the sync.
    assert parse_retry_after("86400", default=5) == MAX_RETRY_AFTER_SECONDS


def test_default_is_also_capped():
    assert parse_retry_after(None, default=10_000) == MAX_RETRY_AFTER_SECONDS


def test_garbage_falls_back_to_default():
    assert parse_retry_after("not-a-number", default=9) == 9


def test_negative_falls_back_to_default():
    assert parse_retry_after("-5", default=4) == 4


def test_http_date_in_future():
    future = datetime.now(timezone.utc) + timedelta(seconds=42)
    seconds = parse_retry_after(format_datetime(future), default=99)
    # ~42, allow a few seconds of tolerance for clock skew during the test.
    assert 35 <= seconds <= 45


def test_http_date_in_past_returns_zero():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert parse_retry_after(format_datetime(past), default=99) == 0
