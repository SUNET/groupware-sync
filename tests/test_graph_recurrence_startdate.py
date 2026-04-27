"""Microsoft Graph requires `range.startDate` on every recurrence
object. CREATE accepted a missing startDate (Graph defaulted to the
event's start), but PATCH on an existing event rejected with
``ErrorInvalidOperation: The recurrence start date is too early.``

The Graph adapter's _sync_item_to_graph must derive startDate from
the event's dtstart_utc when emitting recurrence."""
from __future__ import annotations

from groupware_sync.models import ItemType, SyncItem
from groupware_sync_calendar.adapters.graph_adapter import _sync_item_to_graph


def _item(rrule: str | None, dtstart_utc: str = "2026-04-16T14:00:00Z") -> SyncItem:
    fields: dict = {
        "uid": "ev-1",
        "summary": "weekly standup",
        "dtstart_utc": dtstart_utc,
        "dtstart_tz": "Etc/UTC",
        "dtend_utc": "2026-04-16T14:30:00Z",
        "dtend_tz": "Etc/UTC",
    }
    if rrule:
        fields["rrule"] = rrule
    return SyncItem(
        provider_id="graph-id",
        item_type=ItemType.CALENDAR_EVENT,
        fields=fields,
        fingerprint="",
    )


def test_recurrence_includes_startDate_from_dtstart_utc():
    body = _sync_item_to_graph(_item("FREQ=WEEKLY;BYDAY=MO"))
    assert "recurrence" in body
    rng = body["recurrence"]["range"]
    assert rng.get("startDate") == "2026-04-16"


def test_recurrence_startDate_is_date_only_yyyy_mm_dd():
    """Graph wants a date string, not a datetime."""
    body = _sync_item_to_graph(
        _item("FREQ=DAILY", dtstart_utc="2030-12-31T23:59:00Z"),
    )
    assert body["recurrence"]["range"]["startDate"] == "2030-12-31"


def test_recurrence_with_until_keeps_both_dates():
    body = _sync_item_to_graph(
        _item("FREQ=WEEKLY;BYDAY=MO;UNTIL=20261231",
              dtstart_utc="2026-04-16T14:00:00Z"),
    )
    rng = body["recurrence"]["range"]
    assert rng["type"] == "endDate"
    assert rng["startDate"] == "2026-04-16"
    assert rng["endDate"] == "2026-12-31"


def test_no_recurrence_means_no_recurrence_field():
    body = _sync_item_to_graph(_item(None))
    assert "recurrence" not in body


def test_recurrence_without_dtstart_utc_omits_startDate():
    """Defensive: if dtstart_utc somehow isn't populated, the recurrence
    still emits without crashing — startDate just isn't set. Graph's
    PATCH would reject in that scenario, but the adapter shouldn't
    raise when emitting."""
    item = SyncItem(
        provider_id="graph-id",
        item_type=ItemType.CALENDAR_EVENT,
        fields={
            "uid": "ev-1",
            "summary": "X",
            "rrule": "FREQ=WEEKLY",
        },
        fingerprint="",
    )
    body = _sync_item_to_graph(item)
    assert "recurrence" in body
    assert "startDate" not in body["recurrence"]["range"]
