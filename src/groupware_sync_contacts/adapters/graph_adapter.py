"""Microsoft Graph contact adapter -- SyncProvider subclass.

Translates between the tree-based sync framework (SyncNode/SyncItem) and the
Microsoft Graph REST API v1.0 for contacts.

Adapted from groupware_sync_contacts.providers.graph which works with the older
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

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT = 30.0

# Graph address types -> canonical labels
_ADDR_FIELDS = {
    "homeAddress": "home",
    "businessAddress": "work",
    "otherAddress": "other",
}


class GraphContactAdapter(SyncProvider):
    """SyncProvider implementation backed by Microsoft Graph v1.0."""

    def __init__(self, access_token: str) -> None:
        self._client = httpx.Client(
            base_url=GRAPH_BASE,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT,
        )

    # -- SyncProvider interface ------------------------------------------------

    @property
    def name(self) -> str:
        return "m365"

    def build_tree(self, item_type: ItemType) -> SyncNode:
        """Build a container/leaf tree for all contact folders and contacts.

        Only fetches IDs and lastModifiedDateTime for fingerprinting, not full
        contact data.
        """
        root = SyncNode(
            node_id="root",
            name="root",
            node_type=NodeType.CONTAINER,
        )

        # 1. List all contact folders (paginated)
        folders: list[dict[str, str]] = []
        url: Optional[str] = "/me/contactFolders"
        while url:
            r = self._client.get(url)
            r.raise_for_status()
            data = r.json()
            for item in data.get("value", []):
                folders.append({
                    "id": item["id"],
                    "name": item.get("displayName", "Contacts"),
                })
            url = data.get("@odata.nextLink")

        # 2. For each folder, fetch contact IDs + timestamps (lightweight)
        for folder in folders:
            folder_node = SyncNode(
                node_id=folder["id"],
                name=folder["name"],
                node_type=NodeType.CONTAINER,
            )

            contacts_url: Optional[str] = (
                f"/me/contactFolders/{folder['id']}/contacts"
                f"?$select=id,lastModifiedDateTime&$top=100"
            )
            while contacts_url:
                r = self._client.get(contacts_url)
                r.raise_for_status()
                data = r.json()
                for contact in data.get("value", []):
                    leaf = SyncNode(
                        node_id=contact["id"],
                        name=contact["id"],
                        node_type=NodeType.LEAF,
                        fingerprint=contact.get("lastModifiedDateTime", ""),
                        item_type=ItemType.CONTACT,
                    )
                    folder_node.children.append(leaf)
                contacts_url = data.get("@odata.nextLink")

            root.children.append(folder_node)

        root.compute_merkle()
        return root

    def get_items(self, container_id: str, ids: list[str]) -> list[SyncItem]:
        """Fetch full contact data for the given IDs."""
        if not ids:
            return []

        items: list[SyncItem] = []
        for cid in ids:
            try:
                r = self._client.get(f"/me/contacts/{cid}")
                if r.status_code == 404:
                    log.warning("graph contact %s not found, skipping", cid)
                    continue
                r.raise_for_status()
                items.append(_graph_to_sync_item(r.json()))
            except httpx.HTTPStatusError as e:
                log.error("graph get contact %s failed: %s", cid, e)
        return items

    def get_changes(
        self, container_id: str, cursor: str
    ) -> Optional[ChangeSet]:
        """Incremental changes via Graph delta link.

        The cursor IS the deltaLink from a previous sync. Returns None if
        the delta link has expired (410 Gone) or on error.
        """
        updated: list[str] = []
        destroyed: list[str] = []
        url: Optional[str] = cursor  # cursor IS the deltaLink
        new_cursor = cursor

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
                        # Graph delta doesn't distinguish create vs update --
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
            created=[],
            updated=updated,
            destroyed=destroyed,
            new_cursor=new_cursor,
        )

    def create_container(
        self, name: str, parent_id: Optional[str] = None
    ) -> str:
        """Create a contact folder, return its provider ID."""
        r = self._client.post("/me/contactFolders", json={"displayName": name})
        r.raise_for_status()
        return r.json()["id"]

    def delete_container(self, container_id: str) -> None:
        """Delete a contact folder."""
        r = self._client.delete(f"/me/contactFolders/{container_id}")
        r.raise_for_status()

    def create_item(self, container_id: str, item: SyncItem) -> str:
        """Create a contact in the given folder, return its provider ID."""
        body = _sync_item_to_graph(item)
        r = self._client.post(
            f"/me/contactFolders/{container_id}/contacts", json=body
        )
        r.raise_for_status()
        return r.json()["id"]

    def update_item(self, container_id: str, item: SyncItem) -> None:
        """Update an existing contact."""
        body = _sync_item_to_graph(item)
        r = self._client.patch(f"/me/contacts/{item.provider_id}", json=body)
        r.raise_for_status()

    def delete_item(self, container_id: str, item_id: str) -> None:
        """Delete a contact. Silently ignores 404 (already gone)."""
        r = self._client.delete(f"/me/contacts/{item_id}")
        if r.status_code == 404:
            log.info("graph contact %s already deleted", item_id)
            return
        r.raise_for_status()

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()


# -- Graph JSON <-> SyncItem translation ---------------------------------------


def _graph_to_sync_item(item: dict[str, Any]) -> SyncItem:
    """Translate a Graph contact JSON dict into a SyncItem."""
    fields: dict[str, Any] = {}

    if item.get("displayName") is not None:
        fields["full_name"] = item["displayName"]
    if item.get("givenName") is not None:
        fields["given_name"] = item["givenName"]
    if item.get("surname") is not None:
        fields["surname"] = item["surname"]
    if item.get("companyName") is not None:
        fields["organization"] = item["companyName"]
    if item.get("jobTitle") is not None:
        fields["job_title"] = item["jobTitle"]
    if item.get("personalNotes") is not None:
        fields["notes"] = item["personalNotes"]

    # Emails
    emails: list[dict[str, str]] = []
    for e in item.get("emailAddresses", []):
        addr = e.get("address")
        if not addr:
            continue
        label = (e.get("name", "other") or "other").lower()
        emails.append({"label": label, "value": addr})
    if emails:
        fields["emails"] = emails

    # Phones
    phones: list[dict[str, str]] = []
    for p in item.get("phones", []):
        number = p.get("number")
        if not number:
            continue
        label = (p.get("type", "other") or "other").lower().replace("business", "work")
        phones.append({"label": label, "value": number})
    if phones:
        fields["phones"] = phones

    # Addresses -- Graph has three fixed slots, not an array
    addresses: list[dict[str, Optional[str]]] = []
    for graph_field, label in _ADDR_FIELDS.items():
        addr = item.get(graph_field)
        if addr and any(addr.values()):
            addresses.append({
                "label": label,
                "street": addr.get("street"),
                "city": addr.get("city"),
                "state": addr.get("state"),
                "postal_code": addr.get("postalCode"),
                "country": addr.get("countryOrRegion"),
            })
    if addresses:
        fields["addresses"] = addresses

    # Updated timestamp
    updated_at: Optional[datetime] = None
    fingerprint = item.get("lastModifiedDateTime", "")
    if fingerprint:
        try:
            updated_at = datetime.fromisoformat(
                fingerprint.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            pass

    return SyncItem(
        provider_id=item["id"],
        item_type=ItemType.CONTACT,
        fields=fields,
        updated_at=updated_at,
        fingerprint=fingerprint,
    )


def _sync_item_to_graph(item: SyncItem) -> dict[str, Any]:
    """Translate a SyncItem into Graph contact JSON for create/update."""
    body: dict[str, Any] = {}
    fields = item.fields

    if fields.get("full_name") is not None:
        body["displayName"] = fields["full_name"]
    if fields.get("given_name") is not None:
        body["givenName"] = fields["given_name"]
    if fields.get("surname") is not None:
        body["surname"] = fields["surname"]
    if fields.get("organization") is not None:
        body["companyName"] = fields["organization"]
    if fields.get("job_title") is not None:
        body["jobTitle"] = fields["job_title"]
    if fields.get("notes") is not None:
        body["personalNotes"] = fields["notes"]

    # Emails
    body["emailAddresses"] = [
        {"name": e["label"].capitalize(), "address": e["value"]}
        for e in fields.get("emails", [])
    ]

    # Phones
    body["phones"] = [
        {
            "type": p["label"].replace("work", "business"),
            "number": p["value"],
        }
        for p in fields.get("phones", [])
    ]

    # Addresses -- Graph has fixed slots, not an array
    addr_map = {a["label"]: a for a in fields.get("addresses", [])}
    for graph_field, label in _ADDR_FIELDS.items():
        addr = addr_map.get(label)
        if addr:
            body[graph_field] = {
                "street": addr.get("street") or "",
                "city": addr.get("city") or "",
                "state": addr.get("state") or "",
                "postalCode": addr.get("postal_code") or "",
                "countryOrRegion": addr.get("country") or "",
            }

    return body
