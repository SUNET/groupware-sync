"""JMAP contact adapter — SyncProvider subclass for Stalwart JMAP.

Translates between the tree-based sync framework (SyncNode/SyncItem) and the
JMAP protocol (RFC 8620) with JSContact (RFC 9553 / RFC 9610).

Adapted from groupware_sync_contacts.providers.jmap which works with the older
Contact model. This adapter works with SyncItem dicts instead.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from groupware_sync.models import (
    ChangeSet,
    ItemType,
    NodeType,
    SyncItem,
    SyncNode,
)
from groupware_sync.provider import SyncProvider

log = logging.getLogger(__name__)

TIMEOUT = 30.0
USING = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:contacts"]


class JmapContactAdapter(SyncProvider):
    """SyncProvider implementation backed by a Stalwart JMAP server."""

    def __init__(self, jmap_url: str, access_token: str) -> None:
        self._base_url = jmap_url.rstrip("/")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        self._api_url: Optional[str] = None
        self._account_id: Optional[str] = None

    # -- SyncProvider interface ------------------------------------------------

    @property
    def name(self) -> str:
        return "stalwart"

    def build_tree(self, item_type: ItemType) -> SyncNode:
        """Build a container/leaf tree for all addressbooks and contacts.

        Only fetches IDs and fingerprints (the ``updated`` timestamp), not full
        contact data.
        """
        self._ensure_session()

        # Root container
        root = SyncNode(
            node_id="root",
            name="root",
            node_type=NodeType.CONTAINER,
        )

        # 1. List addressbooks
        results = self._call([
            ["AddressBook/get", {"accountId": self._account_id}, "ab0"],
        ])
        addressbooks: list[dict[str, str]] = []
        for result in results:
            if result[0] == "AddressBook/get":
                for item in result[1].get("list", []):
                    addressbooks.append({
                        "id": item["id"],
                        "name": item.get("name", "Default"),
                    })

        # 2. For each addressbook, query contact IDs and fetch fingerprints
        for ab in addressbooks:
            ab_node = SyncNode(
                node_id=ab["id"],
                name=ab["name"],
                node_type=NodeType.CONTAINER,
            )

            # Query contact IDs in this addressbook
            query_results = self._call([
                [
                    "ContactCard/query",
                    {
                        "accountId": self._account_id,
                        "filter": {"inAddressBook": ab["id"]},
                    },
                    "q0",
                ],
            ])
            contact_ids: list[str] = []
            for result in query_results:
                if result[0] == "ContactCard/query":
                    contact_ids = result[1].get("ids", [])

            if contact_ids:
                # Fetch only id + updated for fingerprinting
                get_results = self._call([
                    [
                        "ContactCard/get",
                        {
                            "accountId": self._account_id,
                            "ids": contact_ids,
                            "properties": ["id", "updated"],
                        },
                        "g0",
                    ],
                ])
                for result in get_results:
                    if result[0] == "ContactCard/get":
                        for card in result[1].get("list", []):
                            leaf = SyncNode(
                                node_id=card["id"],
                                name=card["id"],
                                node_type=NodeType.LEAF,
                                fingerprint=card.get("updated", ""),
                                item_type=ItemType.CONTACT,
                            )
                            ab_node.children.append(leaf)

            root.children.append(ab_node)

        root.compute_merkle()
        return root

    def get_items(self, container_id: str, ids: list[str]) -> list[SyncItem]:
        """Fetch full contact data for the given IDs."""
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
        items: list[SyncItem] = []
        for result in results:
            if result[0] == "ContactCard/get":
                for card in result[1].get("list", []):
                    items.append(_jmap_to_sync_item(card))
        return items

    def get_changes(
        self, container_id: str, cursor: str
    ) -> Optional[ChangeSet]:
        """Incremental changes since *cursor* via ContactCard/changes."""
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

    def create_container(
        self, name: str, parent_id: Optional[str] = None
    ) -> str:
        """Create an addressbook, return its provider ID."""
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
                return item["id"]
        raise ValueError(f"failed to create addressbook {name!r}")

    def delete_container(self, container_id: str) -> None:
        """Delete an addressbook."""
        self._ensure_session()
        results = self._call([
            [
                "AddressBook/set",
                {
                    "accountId": self._account_id,
                    "destroy": [container_id],
                },
                "abd0",
            ],
        ])
        for result in results:
            if result[0] == "AddressBook/set":
                not_destroyed = result[1].get("notDestroyed", {})
                if container_id in not_destroyed:
                    log.warning(
                        "JMAP delete addressbook %s failed: %s",
                        container_id,
                        not_destroyed[container_id],
                    )

    def create_item(self, container_id: str, item: SyncItem) -> str:
        """Create a contact card, return its provider ID."""
        self._ensure_session()
        card = _sync_item_to_jmap(item)
        card["addressBookIds"] = {container_id: True}
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
                new_item = created.get("new1", {})
                return new_item["id"]
        raise ValueError("JMAP create contact failed")

    def update_item(self, container_id: str, item: SyncItem) -> None:
        """Update an existing contact card."""
        self._ensure_session()
        card = _sync_item_to_jmap(item)
        results = self._call([
            [
                "ContactCard/set",
                {
                    "accountId": self._account_id,
                    "update": {item.provider_id: card},
                },
                "u0",
            ],
        ])
        for result in results:
            if result[0] == "ContactCard/set":
                not_updated = result[1].get("notUpdated", {})
                if item.provider_id in not_updated:
                    log.error(
                        "JMAP update contact %s failed: %s",
                        item.provider_id,
                        not_updated[item.provider_id],
                    )

    def delete_item(self, container_id: str, item_id: str) -> None:
        """Delete a contact card."""
        self._ensure_session()
        results = self._call([
            [
                "ContactCard/set",
                {
                    "accountId": self._account_id,
                    "destroy": [item_id],
                },
                "d0",
            ],
        ])
        for result in results:
            if result[0] == "ContactCard/set":
                not_destroyed = result[1].get("notDestroyed", {})
                if item_id in not_destroyed:
                    log.warning(
                        "JMAP delete contact %s failed: %s",
                        item_id,
                        not_destroyed[item_id],
                    )

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()

    # -- JMAP internals --------------------------------------------------------

    def _ensure_session(self) -> None:
        """Discover JMAP session (apiUrl + accountId) if not cached."""
        if self._api_url is not None:
            return
        r = self._client.get(f"{self._base_url}/.well-known/jmap")
        r.raise_for_status()
        session = r.json()
        self._api_url = session["apiUrl"]

        # Find the account with contacts capability
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

        log.debug(
            "JMAP session: apiUrl=%s accountId=%s",
            self._api_url,
            self._account_id,
        )

    def _call(self, method_calls: list[list[Any]]) -> list[list[Any]]:
        """POST a JMAP request and return methodResponses."""
        self._ensure_session()
        body = {
            "using": USING,
            "methodCalls": method_calls,
        }
        r = self._client.post(self._api_url, json=body)  # type: ignore[arg-type]
        r.raise_for_status()
        return r.json()["methodResponses"]


# -- JSContact <-> SyncItem translation ----------------------------------------


def _jmap_to_sync_item(card: dict[str, Any]) -> SyncItem:
    """Translate a JSContact card dict into a SyncItem."""
    fields: dict[str, Any] = {}

    # Name
    name_obj = card.get("name", {})
    full_name = name_obj.get("full")
    if full_name is not None:
        fields["full_name"] = full_name

    given_name: Optional[str] = None
    surname: Optional[str] = None
    for comp in name_obj.get("components", []):
        if comp.get("kind") == "given":
            given_name = comp.get("value")
        elif comp.get("kind") == "surname":
            surname = comp.get("value")
    if given_name is not None:
        fields["given_name"] = given_name
    if surname is not None:
        fields["surname"] = surname

    # Emails — JSContact map: {"id": {"contexts": {...}, "address": "..."}}
    emails: list[dict[str, str]] = []
    for _key, val in card.get("emails", {}).items():
        addr = val.get("address", "")
        if not addr:
            continue
        contexts = val.get("contexts", {})
        label = (
            "work"
            if contexts.get("work")
            else "home"
            if contexts.get("private")
            else "other"
        )
        emails.append({"label": label, "value": addr})
    if emails:
        fields["emails"] = emails

    # Phones — map: {"id": {"contexts": {...}, "features": {...}, "number": "..."}}
    phones: list[dict[str, str]] = []
    for _key, val in card.get("phones", {}).items():
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
        phones.append({"label": label, "value": number})
    if phones:
        fields["phones"] = phones

    # Organization — take the first
    for _key, val in card.get("organizations", {}).items():
        org_name = val.get("name")
        if org_name is not None:
            fields["organization"] = org_name
        break

    # Job title — take the first
    for _key, val in card.get("titles", {}).items():
        title_name = val.get("name")
        if title_name is not None:
            fields["job_title"] = title_name
        break

    # Addresses — map of structured components
    addresses: list[dict[str, Optional[str]]] = []
    for _key, val in card.get("addresses", {}).items():
        contexts = val.get("contexts", {})
        label = (
            "work"
            if contexts.get("work")
            else "home"
            if contexts.get("private")
            else "other"
        )
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
        addresses.append({
            "label": label,
            "street": street,
            "city": city,
            "state": state_val,
            "postal_code": postal_code,
            "country": country,
        })
    if addresses:
        fields["addresses"] = addresses

    # Notes — take the first
    for _key, val in card.get("notes", {}).items():
        note_text = val.get("note")
        if note_text is not None:
            fields["notes"] = note_text
        break

    # Updated timestamp
    updated_at: Optional[datetime] = None
    if ts := card.get("updated"):
        try:
            updated_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    return SyncItem(
        provider_id=card["id"],
        item_type=ItemType.CONTACT,
        fields=fields,
        updated_at=updated_at,
        fingerprint=card.get("updated", ""),
    )


def _sync_item_to_jmap(item: SyncItem) -> dict[str, Any]:
    """Translate a SyncItem into a JSContact card dict for JMAP."""
    card: dict[str, Any] = {}
    fields = item.fields

    # Name
    name: dict[str, Any] = {}
    if fields.get("full_name") is not None:
        name["full"] = fields["full_name"]
    components: list[dict[str, str]] = []
    if fields.get("given_name") is not None:
        components.append({"kind": "given", "value": fields["given_name"]})
    if fields.get("surname") is not None:
        components.append({"kind": "surname", "value": fields["surname"]})
    if components:
        name["components"] = components
    if name:
        card["name"] = name

    # Emails — map keyed by a generated id
    if fields.get("emails"):
        card["emails"] = {}
        for i, e in enumerate(fields["emails"]):
            ctx: dict[str, bool] = {}
            if e.get("label") == "work":
                ctx["work"] = True
            elif e.get("label") == "home":
                ctx["private"] = True
            card["emails"][f"e{i}"] = {"address": e["value"], "contexts": ctx}

    # Phones
    if fields.get("phones"):
        card["phones"] = {}
        for i, p in enumerate(fields["phones"]):
            ctx = {}
            features: dict[str, bool] = {}
            if p.get("label") == "work":
                ctx["work"] = True
                features["voice"] = True
            elif p.get("label") == "home":
                ctx["private"] = True
                features["voice"] = True
            elif p.get("label") == "mobile":
                features["cell"] = True
            card["phones"][f"p{i}"] = {
                "number": p["value"],
                "contexts": ctx,
                "features": features,
            }

    # Organization
    if fields.get("organization") is not None:
        card["organizations"] = {"o0": {"name": fields["organization"]}}

    # Job title
    if fields.get("job_title") is not None:
        card["titles"] = {"t0": {"name": fields["job_title"]}}

    # Addresses
    if fields.get("addresses"):
        card["addresses"] = {}
        for i, a in enumerate(fields["addresses"]):
            ctx = {}
            if a.get("label") == "work":
                ctx["work"] = True
            elif a.get("label") == "home":
                ctx["private"] = True
            addr_components: list[dict[str, str]] = []
            if a.get("street"):
                addr_components.append({"kind": "name", "value": a["street"]})
            if a.get("city"):
                addr_components.append({"kind": "locality", "value": a["city"]})
            if a.get("state"):
                addr_components.append({"kind": "region", "value": a["state"]})
            if a.get("postal_code"):
                addr_components.append({"kind": "postcode", "value": a["postal_code"]})
            if a.get("country"):
                addr_components.append({"kind": "country", "value": a["country"]})
            card["addresses"][f"a{i}"] = {
                "contexts": ctx,
                "components": addr_components,
            }

    # Notes
    if fields.get("notes") is not None:
        card["notes"] = {"n0": {"note": fields["notes"]}}

    return card
