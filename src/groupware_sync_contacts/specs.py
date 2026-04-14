"""Contact type specification for the sync framework.

Defines which fields are synced, their merge strategies, and the
identity matching fields for first-sync contact matching.
"""
from groupware_sync.models import FieldDef, ItemType, MergeStrategy, TypeSpec

CONTACT_SPEC = TypeSpec(
    item_type=ItemType.CONTACT,
    fields=[
        # Name components
        FieldDef("full_name", MergeStrategy.SCALAR),
        FieldDef("given_name", MergeStrategy.SCALAR),
        FieldDef("surname", MergeStrategy.SCALAR),
        FieldDef("middle_name", MergeStrategy.SCALAR),
        FieldDef("prefix", MergeStrategy.SCALAR),
        FieldDef("suffix", MergeStrategy.SCALAR),
        FieldDef("nickname", MergeStrategy.SCALAR),
        # Contact info
        FieldDef("emails", MergeStrategy.SET),
        FieldDef("phones", MergeStrategy.SET),
        FieldDef("addresses", MergeStrategy.SET),
        FieldDef("website", MergeStrategy.SCALAR),
        # Work info
        FieldDef("organization", MergeStrategy.SCALAR),
        FieldDef("department", MergeStrategy.SCALAR),
        FieldDef("job_title", MergeStrategy.SCALAR),
        # Personal
        FieldDef("birthday", MergeStrategy.SCALAR),
        FieldDef("notes", MergeStrategy.SCALAR),
        FieldDef("photo", MergeStrategy.SCALAR),
        FieldDef("photo_type", MergeStrategy.SCALAR),
    ],
    identity_fields=["emails", "full_name"],
    timestamp_field="updated_at",
)
