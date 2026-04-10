"""Microsoft Graph contacts provider.

Implements ContactProvider by calling the Graph REST API v1.0:
- GET /me/contactFolders → list addressbooks
- POST /me/contactFolders → create addressbook
- GET /me/contactFolders/{id}/contacts → list contacts
- GET /me/contactFolders/{id}/contacts/delta → incremental changes
- POST /me/contactFolders/{id}/contacts → create contact
- PATCH /me/contacts/{id} → update contact
- DELETE /me/contacts/{id} → delete contact

Authentication is via Bearer token in the Authorization header.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from groupware_sync_contacts.models import (
    Address,
    Addressbook,
    ChangeSet,
    Contact,
    LabeledValue,
)

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT = 30.0

# Graph address types → canonical labels
_ADDR_FIELDS = {
    "homeAddress": "home",
    "businessAddress": "work",
    "otherAddress": "other",
}


class GraphContactProvider:
    def __init__(self, access_token: str) -> None:
        self._client = httpx.Client(
            base_url=GRAPH_BASE,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT,
        )

    @property
    def name(self) -> str:
        return "m365"

    def close(self) -> None:
        self._client.close()

    # ---- addressbooks --------------------------------------------------------

    def list_addressbooks(self) -> list[Addressbook]:
        books: list[Addressbook] = []
        url = "/me/contactFolders"
        while url:
            r = self._client.get(url)
            r.raise_for_status()
            data = r.json()
            for item in data.get("value", []):
                books.append(
                    Addressbook(provider_id=item["id"], name=item["displayName"])
                )
            url = data.get("@odata.nextLink")
        return books

    def create_addressbook(self, name: str) -> Addressbook:
        r = self._client.post("/me/contactFolders", json={"displayName": name})
        r.raise_for_status()
        item = r.json()
        return Addressbook(provider_id=item["id"], name=item["displayName"])

    # ---- contacts ------------------------------------------------------------

    def get_all_contacts(self, addressbook_id: str) -> list[Contact]:
        contacts: list[Contact] = []
        url = f"/me/contactFolders/{addressbook_id}/contacts?$top=100"
        while url:
            r = self._client.get(url)
            r.raise_for_status()
            data = r.json()
            for item in data.get("value", []):
                contacts.append(_graph_to_contact(item))
            url = data.get("@odata.nextLink")
        return contacts

    def get_changes(
        self, addressbook_id: str, cursor: str
    ) -> Optional[ChangeSet]:
        created: list[str] = []
        updated: list[str] = []
        destroyed: list[str] = []
        url = cursor  # cursor IS the deltaLink from a previous sync
        try:
            while url:
                r = self._client.get(url)
                if r.status_code == 410:
                    log.info("graph delta link expired, falling back to full fetch")
                    return None
                r.raise_for_status()
                data = r.json()
                for item in data.get("value", []):
                    cid = item["id"]
                    if item.get("@removed"):
                        destroyed.append(cid)
                    else:
                        # Graph delta doesn't distinguish create vs update —
                        # we treat all non-removed as updated. The sync engine
                        # checks the mapping to decide if it's truly new.
                        updated.append(cid)
                url = data.get("@odata.nextLink")
                if not url:
                    url = None
                    new_cursor = data.get("@odata.deltaLink", cursor)
        except httpx.HTTPStatusError:
            log.warning("graph delta query failed, falling back to full fetch")
            return None
        return ChangeSet(
            created=created,
            updated=updated,
            destroyed=destroyed,
            new_cursor=new_cursor,
        )

    def get_initial_delta_link(self, addressbook_id: str) -> str:
        """Fetch the first delta link by requesting delta with no prior state.

        This drains the full delta response (which returns all current contacts)
        and returns the deltaLink for subsequent incremental calls.
        """
        url: Optional[str] = (
            f"/me/contactFolders/{addressbook_id}/contacts/delta?$top=100"
        )
        delta_link = ""
        while url:
            r = self._client.get(url)
            r.raise_for_status()
            data = r.json()
            url = data.get("@odata.nextLink")
            if not url:
                delta_link = data.get("@odata.deltaLink", "")
        return delta_link

    def get_contacts(
        self, addressbook_id: str, ids: list[str]
    ) -> list[Contact]:
        contacts: list[Contact] = []
        for cid in ids:
            try:
                r = self._client.get(f"/me/contacts/{cid}")
                if r.status_code == 404:
                    log.warning("graph contact %s not found, skipping", cid)
                    continue
                r.raise_for_status()
                contacts.append(_graph_to_contact(r.json()))
            except httpx.HTTPStatusError as e:
                log.error("graph get contact %s failed: %s", cid, e)
        return contacts

    def create_contact(self, addressbook_id: str, contact: Contact) -> str:
        body = _contact_to_graph(contact)
        r = self._client.post(
            f"/me/contactFolders/{addressbook_id}/contacts", json=body
        )
        r.raise_for_status()
        return r.json()["id"]

    def update_contact(self, addressbook_id: str, contact: Contact) -> None:
        body = _contact_to_graph(contact)
        r = self._client.patch(f"/me/contacts/{contact.provider_id}", json=body)
        r.raise_for_status()

    def delete_contact(self, addressbook_id: str, provider_id: str) -> None:
        r = self._client.delete(f"/me/contacts/{provider_id}")
        if r.status_code == 404:
            log.info("graph contact %s already deleted", provider_id)
            return
        r.raise_for_status()


# ---- translation helpers -----------------------------------------------------


def _graph_to_contact(item: dict) -> Contact:
    emails = [
        LabeledValue(
            label=e.get("name", "other").lower() or "other",
            value=e["address"],
        )
        for e in item.get("emailAddresses", [])
        if e.get("address")
    ]

    phones = [
        LabeledValue(
            label=p.get("type", "other").lower().replace("business", "work"),
            value=p["number"],
        )
        for p in item.get("phones", [])
        if p.get("number")
    ]

    addresses: list[Address] = []
    for graph_field, label in _ADDR_FIELDS.items():
        addr = item.get(graph_field)
        if addr and any(addr.values()):
            addresses.append(
                Address(
                    label=label,
                    street=addr.get("street"),
                    city=addr.get("city"),
                    state=addr.get("state"),
                    postal_code=addr.get("postalCode"),
                    country=addr.get("countryOrRegion"),
                )
            )

    updated_at = None
    if ts := item.get("lastModifiedDateTime"):
        try:
            updated_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    return Contact(
        provider_id=item["id"],
        full_name=item.get("displayName"),
        given_name=item.get("givenName"),
        surname=item.get("surname"),
        emails=emails,
        phones=phones,
        organization=item.get("companyName"),
        job_title=item.get("jobTitle"),
        addresses=addresses,
        notes=item.get("personalNotes"),
        updated_at=updated_at,
    )


def _contact_to_graph(contact: Contact) -> dict:
    body: dict = {}
    if contact.full_name is not None:
        body["displayName"] = contact.full_name
    if contact.given_name is not None:
        body["givenName"] = contact.given_name
    if contact.surname is not None:
        body["surname"] = contact.surname
    if contact.organization is not None:
        body["companyName"] = contact.organization
    if contact.job_title is not None:
        body["jobTitle"] = contact.job_title
    if contact.notes is not None:
        body["personalNotes"] = contact.notes

    body["emailAddresses"] = [
        {"name": e.label.capitalize(), "address": e.value}
        for e in contact.emails
    ]
    body["phones"] = [
        {"type": p.label.replace("work", "business"), "number": p.value}
        for p in contact.phones
    ]

    # Graph has fixed address slots, not an array
    addr_map = {a.label: a for a in contact.addresses}
    for graph_field, label in _ADDR_FIELDS.items():
        addr = addr_map.get(label)
        if addr:
            body[graph_field] = {
                "street": addr.street or "",
                "city": addr.city or "",
                "state": addr.state or "",
                "postalCode": addr.postal_code or "",
                "countryOrRegion": addr.country or "",
            }

    return body
