"""Microsoft Graph contact adapter -- SyncProvider subclass.

Translates between the tree-based sync framework (SyncNode/SyncItem) and the
Microsoft Graph REST API v1.0 for contacts.

Adapted from groupware_sync_contacts.providers.graph which works with the older
Contact model. This adapter works with SyncItem dicts instead.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from groupware_sync.models import (
    ChangeSet,
    ItemType,
    NodeType,
    SyncItem,
    SyncNode,
)
from groupware_sync.provider import (
    NotificationCapability,
    NotificationPolicy,
    SyncProvider,
)

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_HOST = "graph.microsoft.com"
TIMEOUT = 30.0
MAX_429_RETRIES = 3

# Graph address types -> canonical labels
_ADDR_FIELDS = {
    "homeAddress": "home",
    "businessAddress": "work",
    "otherAddress": "other",
}


class GraphContactAdapter(SyncProvider):
    """SyncProvider implementation backed by Microsoft Graph v1.0."""

    notification_policy = NotificationPolicy(
        create_item=NotificationCapability.SUPPRESSED,
        update_item=NotificationCapability.SUPPRESSED,
        delete_item=NotificationCapability.SUPPRESSED,
        delete_container=NotificationCapability.SUPPRESSED,
    )

    def __init__(self, access_token: str, addressbook_filter: Optional[str] = None) -> None:
        self._client = httpx.Client(
            base_url=GRAPH_BASE,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT,
        )
        self._addressbook_filter = addressbook_filter  # filter by displayName if set

    # -- Internal helpers ------------------------------------------------------

    @staticmethod
    def _validate_url(url: str) -> bool:
        """Check that an absolute URL points to graph.microsoft.com."""
        parsed = urlparse(url)
        if parsed.hostname != GRAPH_HOST:
            log.warning("rejecting URL with unexpected host: %s", parsed.hostname)
            return False
        return True

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """HTTP request with 429 rate-limit retry and Retry-After backoff."""
        for attempt in range(MAX_429_RETRIES + 1):
            resp = self._client.request(method, url, **kwargs)
            if resp.status_code != 429 or attempt == MAX_429_RETRIES:
                return resp
            retry_after = int(resp.headers.get("Retry-After", "5"))
            log.warning(
                "graph 429 rate-limited, retrying in %ds (attempt %d/%d)",
                retry_after,
                attempt + 1,
                MAX_429_RETRIES,
            )
            time.sleep(retry_after)
        return resp  # unreachable, but keeps mypy happy

    # -- SyncProvider interface ------------------------------------------------

    @property
    def name(self) -> str:
        return "m365"

    def build_tree(
        self,
        item_type: ItemType,
        known_states: Optional[dict[str, tuple[str, str]]] = None,
    ) -> SyncNode:
        """Build a container/leaf tree for all contact folders and contacts.

        Only fetches IDs and lastModifiedDateTime for fingerprinting, not full
        contact data. Graph has no reliable container-level state indicator,
        so known_states is accepted but not used for pruning.
        """
        root = SyncNode(
            node_id="root",
            name="root",
            node_type=NodeType.CONTAINER,
        )

        # 1. Fetch the default contacts folder (not returned by /contactFolders)
        folders: list[dict[str, str]] = []
        try:
            r = self._request("GET", "/me/contactFolders/Contacts")
            r.raise_for_status()
            default = r.json()
            folders.append({
                "id": default["id"],
                "name": default.get("displayName", "Contacts"),
            })
        except Exception:
            log.warning("could not fetch default contacts folder, trying /me/contactFolders only")

        # 2. List user-created contact folders (paginated)
        url: Optional[str] = "/me/contactFolders"
        while url:
            r = self._request("GET", url)
            r.raise_for_status()
            data = r.json()
            for item in data.get("value", []):
                # Skip the default folder if we already have it
                if any(f["id"] == item["id"] for f in folders):
                    continue
                folders.append({
                    "id": item["id"],
                    "name": item.get("displayName", "Contacts"),
                })
            next_link = data.get("@odata.nextLink")
            url = next_link if next_link and self._validate_url(next_link) else None

        # Filter to specified addressbook if configured
        if self._addressbook_filter:
            folders = [
                f for f in folders
                if f["name"].lower() == self._addressbook_filter.lower()
            ]
            if not folders:
                log.warning("addressbook filter %r matched nothing", self._addressbook_filter)
            else:
                # Normalize name so both sides match when paired
                folders[0]["name"] = "__synced__"

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
                r = self._request("GET", contacts_url)
                r.raise_for_status()
                data = r.json()
                for contact in data.get("value", []):
                    leaf = SyncNode(
                        node_id=contact["id"],
                        name=contact["id"],
                        node_type=NodeType.LEAF,
                        fingerprint=contact.get("lastModifiedDateTime", ""),
                        item_type=ItemType.CONTACT,
                        identity_key=None,
                    )
                    folder_node.children.append(leaf)
                next_link = data.get("@odata.nextLink")
                contacts_url = next_link if next_link and self._validate_url(next_link) else None

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
                r = self._request("GET", f"/me/contacts/{cid}")
                if r.status_code == 404:
                    log.warning("graph contact %s not found, skipping", cid)
                    continue
                r.raise_for_status()
                item = _graph_to_sync_item(r.json())
                # Fetch photo separately (stored outside the contact JSON)
                try:
                    photo_resp = self._request("GET", f"/me/contacts/{cid}/photo/$value")
                    if photo_resp.status_code == 200:
                        import base64
                        item.fields["photo"] = base64.b64encode(photo_resp.content).decode("ascii")
                        content_type = photo_resp.headers.get("content-type", "image/jpeg")
                        item.fields["photo_type"] = content_type
                except Exception:
                    pass  # no photo or error, skip silently
                items.append(item)
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

        # Validate the initial deltaLink URL
        if url and url.startswith("http") and not self._validate_url(url):
            log.warning("delta link has unexpected host, falling back to full fetch")
            return None

        try:
            while url:
                r = self._request("GET", url)
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
                next_link = data.get("@odata.nextLink")
                if next_link and self._validate_url(next_link):
                    url = next_link
                else:
                    url = None
                    delta_link = data.get("@odata.deltaLink")
                    if delta_link and self._validate_url(delta_link):
                        new_cursor = delta_link
                    else:
                        new_cursor = cursor
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
        r = self._request("POST", "/me/contactFolders", json={"displayName": name})
        r.raise_for_status()
        return r.json()["id"]

    def delete_container(self, container_id: str) -> None:
        """Delete a contact folder."""
        r = self._request("DELETE", f"/me/contactFolders/{container_id}")
        r.raise_for_status()

    def create_item(self, container_id: str, item: SyncItem) -> tuple[str, str]:
        """Create a contact. Returns (new_id, server_fingerprint)."""
        body = _sync_item_to_graph(item)
        r = self._request(
            "POST", f"/me/contactFolders/{container_id}/contacts", json=body
        )
        r.raise_for_status()
        data = r.json()
        contact_id = data["id"]
        # Upload photo if present
        if item.fields.get("photo"):
            import base64
            photo_bytes = base64.b64decode(item.fields["photo"])
            content_type = item.fields.get("photo_type", "image/jpeg")
            try:
                self._request(
                    "PUT",
                    f"/me/contacts/{contact_id}/photo/$value",
                    content=photo_bytes,
                    headers={"Content-Type": content_type},
                )
            except Exception:
                log.warning("failed to upload photo for %s", contact_id)
        return contact_id, data.get("lastModifiedDateTime", "")

    def update_item(self, container_id: str, item: SyncItem) -> str:
        """Update an existing contact. Returns server-assigned fingerprint."""
        body = _sync_item_to_graph(item)
        r = self._request("PATCH", f"/me/contacts/{item.provider_id}", json=body)
        r.raise_for_status()
        # Upload photo if present
        if item.fields.get("photo"):
            import base64
            photo_bytes = base64.b64decode(item.fields["photo"])
            content_type = item.fields.get("photo_type", "image/jpeg")
            try:
                self._request(
                    "PUT",
                    f"/me/contacts/{item.provider_id}/photo/$value",
                    content=photo_bytes,
                    headers={"Content-Type": content_type},
                )
            except Exception:
                log.warning("failed to upload photo for %s", item.provider_id)
        return r.json().get("lastModifiedDateTime", "")

    def delete_item(self, container_id: str, item_id: str) -> None:
        """Delete a contact. Silently ignores 404 (already gone)."""
        r = self._request("DELETE", f"/me/contacts/{item_id}")
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
    if item.get("middleName") is not None:
        fields["middle_name"] = item["middleName"]
    if item.get("title") is not None:  # Graph "title" is honorific prefix (Mr/Ms), NOT job title
        fields["prefix"] = item["title"]
    if item.get("suffix") is not None:
        fields["suffix"] = item["suffix"]
    if item.get("nickName") is not None:
        fields["nickname"] = item["nickName"]
    if item.get("birthday") is not None:
        # Graph returns birthday as ISO datetime "1990-01-15T00:00:00Z" — extract just the date
        bday = item["birthday"]
        if "T" in str(bday):
            bday = str(bday).split("T")[0]
        fields["birthday"] = bday
    if item.get("department") is not None:
        fields["department"] = item["department"]
    if item.get("businessHomePage") is not None:
        fields["website"] = item["businessHomePage"]

    # Emails — Graph's emailAddresses[].name is the person's display name,
    # NOT a type label. We don't have a reliable type, so use "other".
    emails: list[dict[str, str]] = []
    seen_emails: set[str] = set()
    for e in item.get("emailAddresses", []):
        addr = e.get("address")
        if not addr or addr.lower() in seen_emails:
            continue
        seen_emails.add(addr.lower())
        emails.append({"label": "other", "value": addr})
    if emails:
        fields["emails"] = emails

    # Phones — Graph stores phones in separate top-level fields,
    # not (only) in the phones[] array.
    phones: list[dict[str, str]] = []
    if item.get("mobilePhone"):
        phones.append({"label": "mobile", "value": item["mobilePhone"]})
    for bp in item.get("businessPhones", []):
        if bp:
            phones.append({"label": "work", "value": bp})
    for hp in item.get("homePhones", []):
        if hp:
            phones.append({"label": "home", "value": hp})
    # Also check the phones[] array as fallback
    for p in item.get("phones", []):
        number = p.get("number")
        if not number:
            continue
        # Avoid duplicates
        if any(ph["value"] == number for ph in phones):
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
    if fields.get("middle_name") is not None:
        body["middleName"] = fields["middle_name"]
    if fields.get("prefix") is not None:
        body["title"] = fields["prefix"]
    if fields.get("suffix") is not None:
        body["suffix"] = fields["suffix"]
    if fields.get("nickname") is not None:
        body["nickName"] = fields["nickname"]
    if fields.get("birthday") is not None:
        body["birthday"] = fields["birthday"]
    if fields.get("department") is not None:
        body["department"] = fields["department"]
    if fields.get("website") is not None:
        body["businessHomePage"] = fields["website"]

    # Emails
    body["emailAddresses"] = [
        {"name": e.get("label", "").capitalize() or "", "address": e["value"]}
        for e in fields.get("emails", [])
    ]

    # Phones — Graph uses separate top-level fields
    mobile = None
    business_phones: list[str] = []
    home_phones: list[str] = []
    for p in fields.get("phones", []):
        label = p.get("label", "other")
        number = p["value"]
        if label == "mobile" and mobile is None:
            mobile = number
        elif label == "work":
            business_phones.append(number)
        elif label == "home":
            home_phones.append(number)
        else:
            # Put unknown types in business phones as fallback
            business_phones.append(number)
    body["mobilePhone"] = mobile
    body["businessPhones"] = business_phones
    body["homePhones"] = home_phones

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
