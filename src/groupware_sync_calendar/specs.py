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
    identity_fields=["uid"],
    timestamp_field="updated",
)
