"""Tests for Graph recurrence ↔ RRULE translation."""

from groupware_sync_calendar.rrule import (
    graph_recurrence_to_rrule,
    rrule_to_graph_recurrence,
)


def test_daily_to_rrule():
    graph = {"pattern": {"type": "daily", "interval": 1}, "range": {"type": "noEnd"}}
    result = graph_recurrence_to_rrule(graph)
    assert "FREQ=DAILY" in result
    assert "INTERVAL=1" in result


def test_weekly_to_rrule():
    graph = {
        "pattern": {
            "type": "weekly",
            "interval": 1,
            "daysOfWeek": ["monday", "wednesday", "friday"],
        },
        "range": {"type": "noEnd"},
    }
    result = graph_recurrence_to_rrule(graph)
    assert "FREQ=WEEKLY" in result
    assert "BYDAY=" in result
    # All three days should be present
    byday = [p for p in result.split(";") if p.startswith("BYDAY=")][0]
    days = set(byday.split("=")[1].split(","))
    assert days == {"MO", "WE", "FR"}


def test_absolute_monthly_to_rrule():
    graph = {
        "pattern": {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 15},
        "range": {"type": "numbered", "numberOfOccurrences": 10},
    }
    result = graph_recurrence_to_rrule(graph)
    assert "FREQ=MONTHLY" in result
    assert "BYMONTHDAY=15" in result
    assert "COUNT=10" in result


def test_relative_monthly_to_rrule():
    graph = {
        "pattern": {
            "type": "relativeMonthly",
            "interval": 1,
            "daysOfWeek": ["tuesday"],
            "index": "second",
        },
        "range": {"type": "noEnd"},
    }
    result = graph_recurrence_to_rrule(graph)
    assert "FREQ=MONTHLY" in result
    assert "BYDAY=2TU" in result


def test_absolute_yearly_to_rrule():
    graph = {
        "pattern": {
            "type": "absoluteYearly",
            "interval": 1,
            "month": 12,
            "dayOfMonth": 25,
        },
        "range": {"type": "noEnd"},
    }
    result = graph_recurrence_to_rrule(graph)
    assert "FREQ=YEARLY" in result
    assert "BYMONTH=12" in result
    assert "BYMONTHDAY=25" in result


def test_relative_yearly_to_rrule():
    graph = {
        "pattern": {
            "type": "relativeYearly",
            "interval": 1,
            "month": 11,
            "daysOfWeek": ["thursday"],
            "index": "fourth",
        },
        "range": {"type": "noEnd"},
    }
    result = graph_recurrence_to_rrule(graph)
    assert "FREQ=YEARLY" in result
    assert "BYMONTH=11" in result
    assert "BYDAY=4TH" in result


def test_end_date_range():
    graph = {
        "pattern": {"type": "daily", "interval": 1},
        "range": {"type": "endDate", "endDate": "2026-12-31"},
    }
    result = graph_recurrence_to_rrule(graph)
    assert "UNTIL=20261231" in result


def test_rrule_to_graph_daily():
    result = rrule_to_graph_recurrence("FREQ=DAILY;INTERVAL=2")
    assert result["pattern"]["type"] == "daily"
    assert result["pattern"]["interval"] == 2
    assert result["range"]["type"] == "noEnd"


def test_rrule_to_graph_weekly():
    result = rrule_to_graph_recurrence("FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,WE,FR")
    assert result["pattern"]["type"] == "weekly"
    assert set(result["pattern"]["daysOfWeek"]) == {"monday", "wednesday", "friday"}


def test_rrule_to_graph_monthly_absolute():
    result = rrule_to_graph_recurrence(
        "FREQ=MONTHLY;INTERVAL=1;BYMONTHDAY=15;COUNT=10"
    )
    assert result["pattern"]["type"] == "absoluteMonthly"
    assert result["pattern"]["dayOfMonth"] == 15
    assert result["range"]["type"] == "numbered"
    assert result["range"]["numberOfOccurrences"] == 10


def test_rrule_to_graph_monthly_relative():
    result = rrule_to_graph_recurrence("FREQ=MONTHLY;INTERVAL=1;BYDAY=2TU")
    assert result["pattern"]["type"] == "relativeMonthly"
    assert result["pattern"]["index"] == "second"
    assert "tuesday" in result["pattern"]["daysOfWeek"]


def test_rrule_to_graph_with_until():
    result = rrule_to_graph_recurrence("FREQ=DAILY;INTERVAL=1;UNTIL=20261231")
    assert result["range"]["type"] == "endDate"
    assert result["range"]["endDate"] == "2026-12-31"


def test_roundtrip_weekly():
    original = "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;COUNT=20"
    graph = rrule_to_graph_recurrence(original)
    back = graph_recurrence_to_rrule(graph)
    # Parse both into sets of components for order-independent comparison
    orig_parts = set(original.split(";"))
    back_parts = set(back.split(";"))
    assert orig_parts == back_parts


def test_roundtrip_daily_with_end():
    original = "FREQ=DAILY;INTERVAL=1;UNTIL=20261231"
    graph = rrule_to_graph_recurrence(original)
    back = graph_recurrence_to_rrule(graph)
    assert "FREQ=DAILY" in back
    assert "UNTIL=20261231" in back
