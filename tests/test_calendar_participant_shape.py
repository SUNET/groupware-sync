"""Regression: Graph and JMAP adapters must produce identical organizer
and attendees shapes. If they diverge, merge_item sees the dicts as
unequal and PATCHes both sides on every run, causing perpetual drift
on `organizer` and `attendees`."""
from groupware_sync_calendar.adapters.graph_adapter import _graph_to_sync_item
from groupware_sync_calendar.adapters.jmap_adapter import _jmap_to_sync_item


def test_organizer_shape_matches_between_adapters():
    graph_event = {
        "id": "g1",
        "iCalUId": "uid-1",
        "subject": "Meeting",
        "organizer": {
            "emailAddress": {"address": "alice@example.com", "name": "Alice"},
        },
        "start": {"dateTime": "2026-04-23T10:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-04-23T11:00:00.0000000", "timeZone": "UTC"},
    }
    graph_item = _graph_to_sync_item(graph_event)

    jmap_event = {
        "id": "j1",
        "uid": "uid-1",
        "title": "Meeting",
        "participants": {
            "p0": {
                "@type": "Participant",
                "roles": {"owner": True, "attendee": True},
                "sendTo": {"imip": "mailto:alice@example.com"},
                "name": "Alice",
                "participationStatus": "accepted",
                "expectReply": False,
            },
        },
        "start": "2026-04-23T10:00:00",
        "timeZone": "Etc/UTC",
        "duration": "PT1H",
    }
    jmap_fields = _jmap_to_sync_item(jmap_event).fields

    assert graph_item.fields["organizer"] == jmap_fields["organizer"]
    assert set(graph_item.fields["organizer"].keys()) == {"email", "name"}


def test_attendees_shape_matches_between_adapters():
    graph_event = {
        "id": "g1",
        "iCalUId": "uid-1",
        "subject": "Meeting",
        "attendees": [
            {
                "emailAddress": {"address": "bob@example.com", "name": "Bob"},
                "type": "required",
                "status": {"response": "accepted"},
            },
            {
                "emailAddress": {"address": "carol@example.com", "name": "Carol"},
                "type": "optional",
                "status": {"response": "tentativelyAccepted"},
            },
        ],
        "start": {"dateTime": "2026-04-23T10:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-04-23T11:00:00.0000000", "timeZone": "UTC"},
    }
    graph_item = _graph_to_sync_item(graph_event)

    jmap_event = {
        "id": "j1",
        "uid": "uid-1",
        "title": "Meeting",
        "participants": {
            "p0": {
                "@type": "Participant",
                "roles": {"attendee": True},
                "sendTo": {"imip": "mailto:bob@example.com"},
                "name": "Bob",
                "participationStatus": "accepted",
            },
            "p1": {
                "@type": "Participant",
                "roles": {"optional": True},
                "sendTo": {"imip": "mailto:carol@example.com"},
                "name": "Carol",
                "participationStatus": "tentative",
            },
        },
        "start": "2026-04-23T10:00:00",
        "timeZone": "Etc/UTC",
        "duration": "PT1H",
    }
    jmap_fields = _jmap_to_sync_item(jmap_event).fields

    graph_atts = sorted(graph_item.fields["attendees"], key=lambda a: a["email"])
    jmap_atts = sorted(jmap_fields["attendees"], key=lambda a: a["email"])
    assert graph_atts == jmap_atts
    assert all(set(a.keys()) == {"email", "name", "role", "partstat"} for a in graph_atts)
