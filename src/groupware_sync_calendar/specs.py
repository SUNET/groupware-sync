"""Calendar event type specification for the sync framework."""
from groupware_sync.models import FieldDef, ItemType, MergeStrategy, TypeSpec

CALENDAR_EVENT_SPEC = TypeSpec(
    item_type=ItemType.CALENDAR_EVENT,
    fields=[
        # Identity
        FieldDef("uid", MergeStrategy.IMMUTABLE),
        # Timing
        FieldDef("summary", MergeStrategy.SCALAR),
        FieldDef("description", MergeStrategy.SCALAR),
        FieldDef("dtstart_utc", MergeStrategy.SCALAR),
        FieldDef("dtstart_tz", MergeStrategy.SCALAR),
        FieldDef("dtend_utc", MergeStrategy.SCALAR),
        FieldDef("dtend_tz", MergeStrategy.SCALAR),
        FieldDef("all_day", MergeStrategy.SCALAR),
        # Location
        FieldDef("location", MergeStrategy.SCALAR),
        FieldDef("geo", MergeStrategy.SCALAR),
        # Recurrence
        FieldDef("rrule", MergeStrategy.SCALAR),
        FieldDef("exdates", MergeStrategy.SET),
        # Participants
        FieldDef("organizer", MergeStrategy.SCALAR),
        FieldDef("attendees", MergeStrategy.SET),
        # Status & metadata
        FieldDef("status", MergeStrategy.SCALAR),
        FieldDef("priority", MergeStrategy.SCALAR),
        FieldDef("privacy", MergeStrategy.SCALAR),
        FieldDef("free_busy", MergeStrategy.SCALAR),
        FieldDef("categories", MergeStrategy.SET),
        FieldDef("sequence", MergeStrategy.SCALAR),
        FieldDef("created", MergeStrategy.IMMUTABLE),
        FieldDef("updated", MergeStrategy.SCALAR),
        # Reminders
        FieldDef("reminder_minutes", MergeStrategy.SCALAR),
        FieldDef("reminder_action", MergeStrategy.SCALAR),
        # Links
        FieldDef("url", MergeStrategy.SCALAR),
        FieldDef("color", MergeStrategy.SCALAR),
        FieldDef("conference", MergeStrategy.SCALAR),
    ],
    # Tree-level pairing uses "uid". When that fails (e.g. Graph
    # reassigns iCalUId on create), execute-time _identity_match falls
    # back to "content_key" — a stable summary+dtstart_utc derivation
    # populated by each adapter's _*_to_sync_item helper. See
    # src/groupware_sync_calendar/identity.py and
    # docs:2026-04-24-calendar-content-fallback-pairing-design.md.
    identity_fields=["uid", "content_key"],
    timestamp_field="updated",
)
