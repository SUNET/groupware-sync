"""Calendar event type specification for the sync framework."""
from groupware_sync.models import FieldDef, ItemType, MergeStrategy, TypeSpec

CALENDAR_EVENT_SPEC = TypeSpec(
    item_type=ItemType.CALENDAR_EVENT,
    fields=[
        # Identity. Ignored by the merge: Graph reassigns iCalUId on
        # POST/PATCH (it's read-only) so we cannot roundtrip uid through
        # Graph, and tree-level pairing already uses content_key as the
        # primary identity. Treating uid as IMMUTABLE caused the engine
        # to PATCH Graph with the Stalwart UID on every run, then read
        # back Graph's reassigned UID, then drift on the next run.
        FieldDef("uid", MergeStrategy.IGNORE),
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
        # free_busy: IGNORE because the value spaces don't align. Graph
        # regularly emits 'tentative', 'oof', 'workingElsewhere' (and we
        # surface them verbatim), but core JSCalendar only defines
        # `busy` and `free` (RFC 8984 §4.4.2) and Stalwart silently
        # drops anything else. Syncing it caused permanent ping-pong on
        # every event whose Graph showAs wasn't busy/free. Each side
        # keeps its own free_busy independently.
        FieldDef("free_busy", MergeStrategy.IGNORE),
        FieldDef("categories", MergeStrategy.SET),
        # sequence/updated/created are server-set timestamps. Each
        # side maintains its own copy and refuses to overwrite (Graph's
        # createdDateTime and lastModifiedDateTime are read-only;
        # Stalwart sets its own at create-time and bumps `updated` on
        # PATCH). Comparing them as user content guarantees drift on
        # every run. IGNORE keeps them out of the merge entirely.
        # SyncItem.updated_at (separate from the field copy) still
        # drives last-write-wins arbitration.
        FieldDef("sequence", MergeStrategy.IGNORE),
        FieldDef("created", MergeStrategy.IGNORE),
        FieldDef("updated", MergeStrategy.IGNORE),
        # Reminders
        FieldDef("reminder_minutes", MergeStrategy.SCALAR),
        FieldDef("reminder_action", MergeStrategy.SCALAR),
        # Links
        FieldDef("url", MergeStrategy.SCALAR),
        FieldDef("color", MergeStrategy.SCALAR),
        FieldDef("conference", MergeStrategy.SCALAR),
    ],
    # Calendar pairing prefers "content_key" (summary + normalised UTC
    # start) over "uid" because Graph reassigns iCalUId on create, so
    # uid cannot anchor a stable cross-provider identity. The JMAP and
    # Graph adapters compute a content-based SyncNode.identity_key at
    # tree-build time, falling back to uid only when summary/start are
    # missing. Execute-time _identity_match also consults both fields
    # as a belt-and-braces for items the tree layer missed. See
    # src/groupware_sync_calendar/identity.py.
    identity_fields=["uid", "content_key"],
    timestamp_field="updated",
)
