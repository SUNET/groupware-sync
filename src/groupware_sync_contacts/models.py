"""Canonical data model for contacts sync.

These dataclasses are the shared vocabulary for the entire package. Both
providers translate to/from this model. The sync engine operates exclusively
on these types. The state DB stores snapshots as serialized Contact instances.

The model is deliberately neutral — it is neither JSContact nor Graph.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class LabeledValue:
    label: str   # "work", "home", "mobile", "other"
    value: str


@dataclass
class Address:
    label: str   # "home", "work", "other"
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


@dataclass
class Contact:
    provider_id: str                              # opaque ID from the source provider
    full_name: Optional[str] = None
    given_name: Optional[str] = None
    surname: Optional[str] = None
    emails: list[LabeledValue] = field(default_factory=list)
    phones: list[LabeledValue] = field(default_factory=list)
    organization: Optional[str] = None
    job_title: Optional[str] = None
    addresses: list[Address] = field(default_factory=list)
    notes: Optional[str] = None
    updated_at: Optional[datetime] = None         # provider's last-modified timestamp

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON storage in snapshots."""
        d = asdict(self)
        if d.get("updated_at"):
            d["updated_at"] = d["updated_at"].isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Contact:
        """Deserialize from a snapshot dict."""
        if d.get("updated_at"):
            d["updated_at"] = datetime.fromisoformat(d["updated_at"])
        emails = [LabeledValue(**e) for e in d.get("emails", [])]
        phones = [LabeledValue(**p) for p in d.get("phones", [])]
        addresses = [Address(**a) for a in d.get("addresses", [])]
        return cls(
            provider_id=d["provider_id"],
            full_name=d.get("full_name"),
            given_name=d.get("given_name"),
            surname=d.get("surname"),
            emails=emails,
            phones=phones,
            organization=d.get("organization"),
            job_title=d.get("job_title"),
            addresses=addresses,
            notes=d.get("notes"),
            updated_at=d.get("updated_at"),
        )


# Fields that participate in field-level merge. Each is an attribute name on
# Contact. "provider_id" and "updated_at" are metadata, not merged.
MERGE_FIELDS: list[str] = [
    "full_name",
    "given_name",
    "surname",
    "emails",
    "phones",
    "organization",
    "job_title",
    "addresses",
    "notes",
]


@dataclass
class Addressbook:
    provider_id: str
    name: str


@dataclass
class ChangeSet:
    created: list[str]       # provider_ids of new contacts
    updated: list[str]       # provider_ids of changed contacts
    destroyed: list[str]     # provider_ids of deleted contacts
    new_cursor: str          # store for the next sync
