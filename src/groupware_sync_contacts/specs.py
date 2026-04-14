"""Contact type specification for the sync framework.

Defines which fields are synced, their merge strategies, and the
identity matching fields for first-sync contact matching.
"""
from groupware_sync.models import FieldDef, ItemType, MergeStrategy, TypeSpec

CONTACT_SPEC = TypeSpec(
    item_type=ItemType.CONTACT,
    fields=[
        FieldDef("full_name", MergeStrategy.SCALAR),
        FieldDef("given_name", MergeStrategy.SCALAR),
        FieldDef("surname", MergeStrategy.SCALAR),
        FieldDef("emails", MergeStrategy.SET),
        FieldDef("phones", MergeStrategy.SET),
        FieldDef("organization", MergeStrategy.SCALAR),
        FieldDef("job_title", MergeStrategy.SCALAR),
        FieldDef("addresses", MergeStrategy.SET),
        FieldDef("notes", MergeStrategy.SCALAR),
    ],
    identity_fields=["emails", "full_name"],
    timestamp_field="updated_at",
)
