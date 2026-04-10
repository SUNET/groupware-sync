"""Stalwart JMAP contacts provider.

Implements ContactProvider by calling the JMAP API (RFC 8620) with JSContact
(RFC 9553 / RFC 9610). All requests are POST to the JMAP apiUrl.

JMAP session discovery: GET {jmap_url}/.well-known/jmap (with Bearer auth)
returns a session object with apiUrl and accountId.

JMAP methods used:
- AddressBook/get — list addressbooks
- AddressBook/set — create addressbook
- ContactCard/get — get contacts by ID
- ContactCard/query — list contact IDs in an addressbook
- ContactCard/set — create/update/delete contacts
- ContactCard/changes — incremental changes since a state
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from groupware_sync_contacts.models import (
    Address,
    Addressbook,
    ChangeSet,
    Contact,
    LabeledValue,
)

log = logging.getLogger(__name__)

TIMEOUT = 30.0
USING = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:contacts"]


class JmapContactProvider:
    def __init__(self, jmap_url: str, access_token: str) -> None:
        self._base_url = jmap_url.rstrip("/")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        self._api_url: Optional[str] = None
        self._account_id: Optional[str] = None

    @property
    def name(self) -> str:
        return "stalwart"

    def close(self) -> None:
        self._client.close()

    def _ensure_session(self) -> None:
        if self._api_url is not None:
            return
        r = self._client.get(f"{self._base_url}/.well-known/jmap")
        r.raise_for_status()
        session = r.json()
        self._api_url = session["apiUrl"]
        # Find the account that has the contacts capability
        for acct_id, acct in session.get("accounts", {}).items():
            caps = acct.get("accountCapabilities", {})
            if "urn:ietf:params:jmap:contacts" in caps:
                self._account_id = acct_id
                break
        if self._account_id is None:
            # Fall back to primaryAccounts
            primary = session.get("primaryAccounts", {})
            self._account_id = primary.get(
                "urn:ietf:params:jmap:contacts",
                next(iter(session.get("accounts", {})), None),
            )
        if self._account_id is None:
            raise ValueError("JMAP session has no account with contacts capability")
        log.debug("JMAP session: apiUrl=%s accountId=%s", self._api_url, self._account_id)

    def _call(self, method_calls: list[list[Any]]) -> list[list[Any]]:
        self._ensure_session()
        body = {
            "using": USING,
            "methodCalls": method_calls,
        }
        r = self._client.post(self._api_url, json=body)
        r.raise_for_status()
        return r.json()["methodResponses"]

    # ---- addressbooks --------------------------------------------------------

    def list_addressbooks(self) -> list[Addressbook]:
        self._ensure_session()
        results = self._call([
            ["AddressBook/get", {"accountId": self._account_id}, "ab0"],
        ])
        books: list[Addressbook] = []
        for result in results:
            if result[0] == "AddressBook/get":
                for item in result[1].get("list", []):
                    books.append(
                        Addressbook(
                            provider_id=item["id"],
                            name=item.get("name", "Default"),
                        )
                    )
        return books

    def create_addressbook(self, name: str) -> Addressbook:
        self._ensure_session()
        results = self._call([
            [
                "AddressBook/set",
                {
                    "accountId": self._account_id,
                    "create": {"new1": {"name": name}},
                },
                "ab1",
            ],
        ])
        for result in results:
            if result[0] == "AddressBook/set":
                created = result[1].get("created", {})
                item = created.get("new1", {})
                return Addressbook(provider_id=item["id"], name=name)
        raise ValueError(f"failed to create addressbook {name!r}")

    # ---- contacts ------------------------------------------------------------

    def get_all_contacts(self, addressbook_id: str) -> list[Contact]:
        self._ensure_session()
        # Step 1: query IDs
        results = self._call([
            [
                "ContactCard/query",
                {
                    "accountId": self._account_id,
                    "filter": {"inAddressBook": addressbook_id},
                },
                "q0",
            ],
        ])
        ids: list[str] = []
        for result in results:
            if result[0] == "ContactCard/query":
                ids = result[1].get("ids", [])
        if not ids:
            return []
        return self.get_contacts(addressbook_id, ids)

    def get_changes(
        self, addressbook_id: str, cursor: str
    ) -> Optional[ChangeSet]:
        self._ensure_session()
        try:
            results = self._call([
                [
                    "ContactCard/changes",
                    {
                        "accountId": self._account_id,
                        "sinceState": cursor,
                    },
                    "ch0",
                ],
            ])
        except httpx.HTTPStatusError:
            log.warning("JMAP changes request failed, falling back to full fetch")
            return None

        for result in results:
            if result[0] == "ContactCard/changes":
                data = result[1]
                if data.get("type") == "cannotCalculateChanges":
                    log.info("JMAP cannotCalculateChanges, falling back to full fetch")
                    return None
                return ChangeSet(
                    created=data.get("created", []),
                    updated=data.get("updated", []),
                    destroyed=data.get("destroyed", []),
                    new_cursor=data.get("newState", cursor),
                )
            if result[0] == "error":
                err_type = result[1].get("type", "")
                if err_type == "cannotCalculateChanges":
                    log.info("JMAP cannotCalculateChanges, falling back to full fetch")
                    return None
                log.warning("JMAP changes error: %s", result[1])
                return None
        return None

    def get_contacts(
        self, addressbook_id: str, ids: list[str]
    ) -> list[Contact]:
        self._ensure_session()
        if not ids:
            return []
        results = self._call([
            [
                "ContactCard/get",
                {"accountId": self._account_id, "ids": ids},
                "g0",
            ],
        ])
        contacts: list[Contact] = []
        for result in results:
            if result[0] == "ContactCard/get":
                for item in result[1].get("list", []):
                    contacts.append(_jmap_to_contact(item))
        return contacts

    def get_state(self) -> str:
        """Get the current JMAP state for contacts (used for initial cursor)."""
        self._ensure_session()
        results = self._call([
            [
                "ContactCard/get",
                {"accountId": self._account_id, "ids": []},
                "s0",
            ],
        ])
        for result in results:
            if result[0] == "ContactCard/get":
                return result[1].get("state", "")
        return ""

    def create_contact(self, addressbook_id: str, contact: Contact) -> str:
        self._ensure_session()
        card = _contact_to_jmap(contact)
        card["addressBookIds"] = {addressbook_id: True}
        results = self._call([
            [
                "ContactCard/set",
                {
                    "accountId": self._account_id,
                    "create": {"new1": card},
                },
                "c0",
            ],
        ])
        for result in results:
            if result[0] == "ContactCard/set":
                created = result[1].get("created", {})
                item = created.get("new1", {})
                return item["id"]
        raise ValueError("JMAP create contact failed")

    def update_contact(self, addressbook_id: str, contact: Contact) -> None:
        self._ensure_session()
        card = _contact_to_jmap(contact)
        results = self._call([
            [
                "ContactCard/set",
                {
                    "accountId": self._account_id,
                    "update": {contact.provider_id: card},
                },
                "u0",
            ],
        ])
        for result in results:
            if result[0] == "ContactCard/set":
                not_updated = result[1].get("notUpdated", {})
                if contact.provider_id in not_updated:
                    log.error(
                        "JMAP update contact %s failed: %s",
                        contact.provider_id,
                        not_updated[contact.provider_id],
                    )

    def delete_contact(self, addressbook_id: str, provider_id: str) -> None:
        self._ensure_session()
        results = self._call([
            [
                "ContactCard/set",
                {
                    "accountId": self._account_id,
                    "destroy": [provider_id],
                },
                "d0",
            ],
        ])
        for result in results:
            if result[0] == "ContactCard/set":
                not_destroyed = result[1].get("notDestroyed", {})
                if provider_id in not_destroyed:
                    log.warning(
                        "JMAP delete contact %s failed: %s",
                        provider_id,
                        not_destroyed[provider_id],
                    )


# ---- JSContact translation ---------------------------------------------------


def _jmap_to_contact(card: dict) -> Contact:
    # Name
    name_obj = card.get("name", {})
    full_name = name_obj.get("full")
    given_name = None
    surname = None
    for comp in name_obj.get("components", []):
        if comp.get("kind") == "given":
            given_name = comp.get("value")
        elif comp.get("kind") == "surname":
            surname = comp.get("value")

    # Emails — JSContact uses a map: {"id": {"contexts": {...}, "address": "..."}}
    emails: list[LabeledValue] = []
    for key, val in card.get("emails", {}).items():
        addr = val.get("address", "")
        if not addr:
            continue
        contexts = val.get("contexts", {})
        label = "work" if contexts.get("work") else "home" if contexts.get("private") else "other"
        emails.append(LabeledValue(label=label, value=addr))

    # Phones — map: {"id": {"contexts": {...}, "features": {...}, "number": "..."}}
    phones: list[LabeledValue] = []
    for key, val in card.get("phones", {}).items():
        number = val.get("number", "")
        if not number:
            continue
        # Strip tel: prefix if present
        if number.startswith("tel:"):
            number = number[4:]
        features = val.get("features", {})
        contexts = val.get("contexts", {})
        if features.get("cell"):
            label = "mobile"
        elif contexts.get("work"):
            label = "work"
        elif contexts.get("private"):
            label = "home"
        else:
            label = "other"
        phones.append(LabeledValue(label=label, value=number))

    # Organization — take the first
    organization = None
    for key, val in card.get("organizations", {}).items():
        organization = val.get("name")
        break

    # Job title — take the first
    job_title = None
    for key, val in card.get("titles", {}).items():
        job_title = val.get("name")
        break

    # Addresses — map of structured components
    addresses: list[Address] = []
    for key, val in card.get("addresses", {}).items():
        contexts = val.get("contexts", {})
        label = "work" if contexts.get("work") else "home" if contexts.get("private") else "other"
        components = val.get("components", [])
        street = city = state_val = postal_code = country = None
        for comp in components:
            kind = comp.get("kind", "")
            v = comp.get("value", "")
            if kind == "name":
                street = v
            elif kind == "locality":
                city = v
            elif kind == "region":
                state_val = v
            elif kind == "postcode":
                postal_code = v
            elif kind == "country":
                country = v
        addresses.append(Address(
            label=label,
            street=street,
            city=city,
            state=state_val,
            postal_code=postal_code,
            country=country,
        ))

    # Notes — take the first
    notes_val = None
    for key, val in card.get("notes", {}).items():
        notes_val = val.get("note")
        break

    # Updated timestamp
    updated_at = None
    if ts := card.get("updated"):
        try:
            updated_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    return Contact(
        provider_id=card["id"],
        full_name=full_name,
        given_name=given_name,
        surname=surname,
        emails=emails,
        phones=phones,
        organization=organization,
        job_title=job_title,
        addresses=addresses,
        notes=notes_val,
        updated_at=updated_at,
    )


def _contact_to_jmap(contact: Contact) -> dict:
    card: dict = {}

    # Name
    name: dict = {}
    if contact.full_name is not None:
        name["full"] = contact.full_name
    components = []
    if contact.given_name is not None:
        components.append({"kind": "given", "value": contact.given_name})
    if contact.surname is not None:
        components.append({"kind": "surname", "value": contact.surname})
    if components:
        name["components"] = components
    if name:
        card["name"] = name

    # Emails — map keyed by a generated id
    if contact.emails:
        card["emails"] = {}
        for i, e in enumerate(contact.emails):
            ctx = {}
            if e.label == "work":
                ctx["work"] = True
            elif e.label == "home":
                ctx["private"] = True
            card["emails"][f"e{i}"] = {"address": e.value, "contexts": ctx}

    # Phones
    if contact.phones:
        card["phones"] = {}
        for i, p in enumerate(contact.phones):
            ctx = {}
            features = {}
            if p.label == "work":
                ctx["work"] = True
                features["voice"] = True
            elif p.label == "home":
                ctx["private"] = True
                features["voice"] = True
            elif p.label == "mobile":
                features["cell"] = True
            card["phones"][f"p{i}"] = {
                "number": p.value,
                "contexts": ctx,
                "features": features,
            }

    # Organization
    if contact.organization is not None:
        card["organizations"] = {"o0": {"name": contact.organization}}

    # Job title
    if contact.job_title is not None:
        card["titles"] = {"t0": {"name": contact.job_title}}

    # Addresses
    if contact.addresses:
        card["addresses"] = {}
        for i, a in enumerate(contact.addresses):
            ctx = {}
            if a.label == "work":
                ctx["work"] = True
            elif a.label == "home":
                ctx["private"] = True
            components = []
            if a.street:
                components.append({"kind": "name", "value": a.street})
            if a.city:
                components.append({"kind": "locality", "value": a.city})
            if a.state:
                components.append({"kind": "region", "value": a.state})
            if a.postal_code:
                components.append({"kind": "postcode", "value": a.postal_code})
            if a.country:
                components.append({"kind": "country", "value": a.country})
            card["addresses"][f"a{i}"] = {
                "contexts": ctx,
                "components": components,
            }

    # Notes
    if contact.notes is not None:
        card["notes"] = {"n0": {"note": contact.notes}}

    return card
