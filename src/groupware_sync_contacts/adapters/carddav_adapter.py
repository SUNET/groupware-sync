"""CardDAV contact adapter — SyncProvider subclass for CardDAV servers.

Translates between the tree-based sync framework (SyncNode/SyncItem) and the
CardDAV protocol (WebDAV + vCard).  Uses httpx for HTTP and vobject for vCard
parsing/generation.
"""
from __future__ import annotations

import logging
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape as xml_escape

import httpx
import vobject  # type: ignore[import-untyped]

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

DAV_NS = "DAV:"
CARD_NS = "urn:ietf:params:xml:ns:carddav"

TIMEOUT = 30.0


class CardDavContactAdapter(SyncProvider):
    """SyncProvider implementation backed by a CardDAV server."""

    notification_policy = NotificationPolicy(
        create_item=NotificationCapability.SUPPRESSED,
        update_item=NotificationCapability.SUPPRESSED,
        delete_item=NotificationCapability.SUPPRESSED,
        delete_container=NotificationCapability.SUPPRESSED,
    )

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            auth=httpx.BasicAuth(username, password),
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        self._addressbook_home: Optional[str] = None

    # -- SyncProvider interface ------------------------------------------------

    @property
    def name(self) -> str:
        return "carddav"

    def build_tree(
        self,
        item_type: ItemType,
        known_states: Optional[dict[str, tuple[str, str]]] = None,
    ) -> SyncNode:
        """Build a container/leaf tree for all addressbooks and contacts.

        Only fetches IDs and fingerprints (ETags), not full contact data.
        If known_states provides a stored sync-token for an addressbook that
        matches the current server sync-token, that addressbook's children are
        skipped entirely.
        """
        if known_states is None:
            known_states = {}

        addressbooks = self._discover()

        root = SyncNode(
            node_id="root",
            name="root",
            node_type=NodeType.CONTAINER,
        )

        for ab_href, ab_name in addressbooks:
            stored = known_states.get(ab_href)

            if stored is not None:
                stored_cursor, stored_merkle = stored
                # Check if the sync-token is unchanged
                current_token = self._get_sync_token(ab_href)
                if current_token and current_token == stored_cursor:
                    log.debug(
                        "skipping addressbook %s (sync-token unchanged: %s)",
                        ab_name,
                        current_token,
                    )
                    ab_node = SyncNode(
                        node_id=ab_href,
                        name=ab_name,
                        node_type=NodeType.CONTAINER,
                        merkle_hash=stored_merkle,
                        state_cursor=current_token,
                        skipped=True,
                    )
                    root.children.append(ab_node)
                    continue

            # Fetch children for this addressbook
            ab_node = SyncNode(
                node_id=ab_href,
                name=ab_name,
                node_type=NodeType.CONTAINER,
            )

            # Store the current sync-token for next sync
            current_token = self._get_sync_token(ab_href)
            if current_token:
                ab_node.state_cursor = current_token

            # PROPFIND Depth:1 to list contacts with ETags
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
                "<d:prop>"
                "<d:getetag/>"
                "<d:resourcetype/>"
                "</d:prop>"
                "</d:propfind>"
            )
            url = self._abs_url(ab_href)
            resp = self._client.request(
                "PROPFIND",
                url,
                headers={"Depth": "1", "Content-Type": "application/xml"},
                content=body.encode(),
            )
            resp.raise_for_status()

            multistatus = ET.fromstring(resp.text)
            for response_el in multistatus.findall(f"{{{DAV_NS}}}response"):
                href = _text(response_el, f"{{{DAV_NS}}}href")
                if not href:
                    continue

                # Skip collection entries (the addressbook itself)
                propstat = response_el.find(f"{{{DAV_NS}}}propstat")
                if propstat is None:
                    continue
                prop = propstat.find(f"{{{DAV_NS}}}prop")
                if prop is None:
                    continue
                rt = prop.find(f"{{{DAV_NS}}}resourcetype")
                if rt is not None and rt.find(f"{{{DAV_NS}}}collection") is not None:
                    continue

                etag = _text(prop, f"{{{DAV_NS}}}getetag")
                leaf = SyncNode(
                    node_id=href,
                    name=href,
                    node_type=NodeType.LEAF,
                    fingerprint=etag or "",
                    item_type=ItemType.CONTACT,
                    identity_key=None,
                )
                ab_node.children.append(leaf)

            root.children.append(ab_node)

        root.compute_merkle()
        return root

    def get_items(self, container_id: str, ids: list[str]) -> list[SyncItem]:
        """Batch-fetch full contact data for the given IDs using addressbook-multiget."""
        if not ids:
            return []

        href_elements = "".join(f"<d:href>{xml_escape(href)}</d:href>" for href in ids)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<card:addressbook-multiget xmlns:d="DAV:" '
            'xmlns:card="urn:ietf:params:xml:ns:carddav">'
            "<d:prop>"
            "<d:getetag/>"
            "<card:address-data/>"
            "</d:prop>"
            f"{href_elements}"
            "</card:addressbook-multiget>"
        )
        url = self._abs_url(container_id)
        resp = self._client.request(
            "REPORT",
            url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()

        items: list[SyncItem] = []
        multistatus = ET.fromstring(resp.text)
        for response_el in multistatus.findall(f"{{{DAV_NS}}}response"):
            href = _text(response_el, f"{{{DAV_NS}}}href")
            if not href:
                continue

            propstat = response_el.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue

            etag = _text(prop, f"{{{DAV_NS}}}getetag")
            vcard_text = _text(prop, f"{{{CARD_NS}}}address-data")
            if not vcard_text:
                continue

            item = _vcard_to_sync_item(vcard_text, href, etag or "")
            items.append(item)

        return items

    def get_changes(
        self, container_id: str, cursor: str
    ) -> Optional[ChangeSet]:
        """Incremental changes since cursor via WebDAV sync-collection REPORT."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:sync-collection xmlns:d="DAV:">'
            f"<d:sync-token>{xml_escape(cursor)}</d:sync-token>"
            "<d:sync-level>1</d:sync-level>"
            "<d:prop>"
            "<d:getetag/>"
            "</d:prop>"
            "</d:sync-collection>"
        )
        url = self._abs_url(container_id)
        resp = self._client.request(
            "REPORT",
            url,
            headers={"Content-Type": "application/xml"},
            content=body.encode(),
        )

        # Server rejects the sync-token (stale or unsupported)
        if resp.status_code in (403, 409, 412):
            log.info(
                "sync-token rejected (status %d), falling back to full fetch",
                resp.status_code,
            )
            return None
        resp.raise_for_status()

        multistatus = ET.fromstring(resp.text)
        created_or_updated: list[str] = []
        destroyed: list[str] = []

        for response_el in multistatus.findall(f"{{{DAV_NS}}}response"):
            href = _text(response_el, f"{{{DAV_NS}}}href")
            if not href:
                continue
            status = _text(response_el, f"{{{DAV_NS}}}status")
            propstat = response_el.find(f"{{{DAV_NS}}}propstat")

            if status and "404" in status:
                destroyed.append(href)
            elif propstat is not None:
                ps_status = _text(propstat, f"{{{DAV_NS}}}status")
                if ps_status and "404" in ps_status:
                    destroyed.append(href)
                else:
                    created_or_updated.append(href)
            else:
                created_or_updated.append(href)

        new_token_el = multistatus.find(f"{{{DAV_NS}}}sync-token")
        new_token = new_token_el.text if new_token_el is not None and new_token_el.text else cursor

        return ChangeSet(
            created=created_or_updated,
            updated=[],
            destroyed=destroyed,
            new_cursor=new_token,
        )

    def create_container(
        self, name: str, parent_id: Optional[str] = None
    ) -> str:
        """Create an addressbook collection via extended MKCOL."""
        self._ensure_addressbook_home()
        if self._addressbook_home is None:
            raise RuntimeError("addressbook home discovery failed")

        col_href = f"{self._addressbook_home.rstrip('/')}/{name}/"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:mkcol xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
            "<d:set>"
            "<d:prop>"
            "<d:resourcetype>"
            "<d:collection/>"
            "<card:addressbook/>"
            "</d:resourcetype>"
            f"<d:displayname>{xml_escape(name)}</d:displayname>"
            "</d:prop>"
            "</d:set>"
            "</d:mkcol>"
        )
        url = self._abs_url(col_href)
        resp = self._client.request(
            "MKCOL",
            url,
            headers={"Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()
        return col_href

    def delete_container(self, container_id: str) -> None:
        """Delete an addressbook collection."""
        url = self._abs_url(container_id)
        resp = self._client.request("DELETE", url)
        resp.raise_for_status()

    def create_item(self, container_id: str, item: SyncItem) -> tuple[str, str]:
        """Create a contact via PUT. Returns (href, etag)."""
        uid = str(uuid.uuid4())
        vcard_text = _sync_item_to_vcard(item, uid=uid)
        href = f"{container_id.rstrip('/')}/{uid}.vcf"
        url = self._abs_url(href)
        resp = self._client.put(
            url,
            headers={
                "Content-Type": "text/vcard",
                "If-None-Match": "*",
            },
            content=vcard_text.encode("utf-8"),
        )
        resp.raise_for_status()
        etag = resp.headers.get("ETag", "")
        return href, etag

    def update_item(self, container_id: str, item: SyncItem) -> str:
        """Update an existing contact via PUT. Returns the new ETag."""
        vcard_text = _sync_item_to_vcard(item)
        url = self._abs_url(item.provider_id)
        headers: dict[str, str] = {"Content-Type": "text/vcard"}
        if item.fingerprint:
            headers["If-Match"] = item.fingerprint
        resp = self._client.put(
            url,
            headers=headers,
            content=vcard_text.encode("utf-8"),
        )
        resp.raise_for_status()
        return resp.headers.get("ETag", "")

    def delete_item(self, container_id: str, item_id: str) -> None:
        """Delete a contact via DELETE."""
        url = self._abs_url(item_id)
        resp = self._client.request("DELETE", url)
        resp.raise_for_status()

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()

    # -- CardDAV internals -----------------------------------------------------

    def _abs_url(self, path: str) -> str:
        """Resolve a path (possibly relative) to an absolute URL."""
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            base_parsed = urlparse(self._base_url)
            if parsed.hostname != base_parsed.hostname:
                raise ValueError(
                    f"URL host mismatch: {parsed.hostname} != {base_parsed.hostname}"
                )
            return path
        return urljoin(self._base_url + "/", path.lstrip("/"))

    def _ensure_addressbook_home(self) -> None:
        """Discover the addressbook-home-set if not yet cached."""
        if self._addressbook_home is not None:
            return
        self._discover()

    def _discover(self) -> list[tuple[str, str]]:
        """Discover addressbooks via CardDAV well-known + PROPFIND sequence.

        Returns a list of (href, display_name) for each addressbook.
        Also caches ``_addressbook_home``.
        """
        # Step 1: Find principal URL via well-known redirect
        principal_url = self._discover_principal()

        # Step 2: Get addressbook-home-set from principal
        ab_home = self._discover_addressbook_home(principal_url)
        self._addressbook_home = ab_home

        # Step 3: List addressbooks under the home
        return self._list_addressbooks(ab_home)

    def _discover_principal(self) -> str:
        """PROPFIND /.well-known/carddav to find the principal URL."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:">'
            "<d:prop>"
            "<d:current-user-principal/>"
            "</d:prop>"
            "</d:propfind>"
        )
        url = f"{self._base_url}/.well-known/carddav"
        resp = self._client.request(
            "PROPFIND",
            url,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()

        tree = ET.fromstring(resp.text)
        # Look for current-user-principal/href
        for response_el in tree.findall(f"{{{DAV_NS}}}response"):
            propstat = response_el.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue
            cup = prop.find(f"{{{DAV_NS}}}current-user-principal")
            if cup is not None:
                href = _text(cup, f"{{{DAV_NS}}}href")
                if href:
                    return href

        # Fallback: use the final URL path after redirects
        return urlparse(str(resp.url)).path

    def _discover_addressbook_home(self, principal_url: str) -> str:
        """PROPFIND on principal to get addressbook-home-set."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
            "<d:prop>"
            "<card:addressbook-home-set/>"
            "</d:prop>"
            "</d:propfind>"
        )
        url = self._abs_url(principal_url)
        resp = self._client.request(
            "PROPFIND",
            url,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()

        tree = ET.fromstring(resp.text)
        for response_el in tree.findall(f"{{{DAV_NS}}}response"):
            propstat = response_el.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue
            home_set = prop.find(f"{{{CARD_NS}}}addressbook-home-set")
            if home_set is not None:
                href = _text(home_set, f"{{{DAV_NS}}}href")
                if href:
                    return href

        raise ValueError("CardDAV server did not provide addressbook-home-set")

    def _list_addressbooks(self, home_url: str) -> list[tuple[str, str]]:
        """PROPFIND Depth:1 on addressbook home to list addressbooks."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
            "<d:prop>"
            "<d:displayname/>"
            "<d:resourcetype/>"
            "<d:getetag/>"
            "</d:prop>"
            "</d:propfind>"
        )
        url = self._abs_url(home_url)
        resp = self._client.request(
            "PROPFIND",
            url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()

        addressbooks: list[tuple[str, str]] = []
        tree = ET.fromstring(resp.text)
        for response_el in tree.findall(f"{{{DAV_NS}}}response"):
            href = _text(response_el, f"{{{DAV_NS}}}href")
            if not href:
                continue

            propstat = response_el.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue

            # Check resourcetype contains <card:addressbook/>
            rt = prop.find(f"{{{DAV_NS}}}resourcetype")
            if rt is None:
                continue
            if rt.find(f"{{{CARD_NS}}}addressbook") is None:
                continue

            display_name = _text(prop, f"{{{DAV_NS}}}displayname") or href
            addressbooks.append((href, display_name))

        return addressbooks

    def _get_sync_token(self, addressbook_href: str) -> Optional[str]:
        """PROPFIND Depth:0 to get the current sync-token for an addressbook."""
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:">'
            "<d:prop>"
            "<d:sync-token/>"
            "</d:prop>"
            "</d:propfind>"
        )
        url = self._abs_url(addressbook_href)
        resp = self._client.request(
            "PROPFIND",
            url,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            content=body.encode(),
        )
        if resp.status_code >= 400:
            return None

        tree = ET.fromstring(resp.text)
        for response_el in tree.findall(f"{{{DAV_NS}}}response"):
            propstat = response_el.find(f"{{{DAV_NS}}}propstat")
            if propstat is None:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue
            token = _text(prop, f"{{{DAV_NS}}}sync-token")
            if token:
                return token
        return None


# -- vCard <-> SyncItem translation --------------------------------------------


def _text(parent: ET.Element, tag: str) -> Optional[str]:
    """Extract text from a child element, or None if missing."""
    el = parent.find(tag)
    if el is not None and el.text:
        return el.text.strip()
    return None


def _vcard_label(param_type_values: list[str]) -> str:
    """Map vCard TYPE parameter values to a canonical label."""
    lowered = {v.lower() for v in param_type_values}
    if "work" in lowered:
        return "work"
    if "home" in lowered:
        return "home"
    if "cell" in lowered:
        return "mobile"
    return "other"


def _get_type_params(vobj: Any) -> list[str]:
    """Extract TYPE parameter values from a vobject property."""
    params = getattr(vobj, "params", {})
    type_vals = params.get("TYPE", [])
    if isinstance(type_vals, str):
        return [type_vals]
    return list(type_vals)


def _vcard_to_sync_item(vcard_text: str, href: str, etag: str) -> SyncItem:
    """Parse a vCard string into a SyncItem."""
    card = vobject.readOne(vcard_text)
    fields: dict[str, Any] = {}

    # Full name
    if hasattr(card, "fn"):
        fields["full_name"] = card.fn.value

    # Structured name
    if hasattr(card, "n"):
        n = card.n.value
        if n.given:
            fields["given_name"] = n.given
        if n.family:
            fields["surname"] = n.family
        if hasattr(n, "additional") and n.additional:
            fields["middle_name"] = n.additional
        if hasattr(n, "prefix") and n.prefix:
            fields["prefix"] = n.prefix
        if hasattr(n, "suffix") and n.suffix:
            fields["suffix"] = n.suffix

    # Nickname
    if hasattr(card, "nickname"):
        fields["nickname"] = card.nickname.value

    # Birthday
    if hasattr(card, "bday"):
        bday_val = card.bday.value
        if hasattr(bday_val, "isoformat"):
            fields["birthday"] = bday_val.isoformat()[:10]  # just the date part
        else:
            fields["birthday"] = str(bday_val)[:10]

    # Emails
    emails: list[dict[str, str]] = []
    for email_prop in card.contents.get("email", []):
        addr = email_prop.value
        if not addr:
            continue
        label = _vcard_label(_get_type_params(email_prop))
        emails.append({"label": label, "value": addr})
    if emails:
        fields["emails"] = emails

    # Phones
    phones: list[dict[str, str]] = []
    for tel_prop in card.contents.get("tel", []):
        number = tel_prop.value
        if not number:
            continue
        label = _vcard_label(_get_type_params(tel_prop))
        phones.append({"label": label, "value": number})
    if phones:
        fields["phones"] = phones

    # Organization
    if hasattr(card, "org"):
        org_values = card.org.value
        if org_values:
            # vobject stores ORG as a list of strings
            org_name = org_values[0] if isinstance(org_values, list) else org_values
            if org_name:
                fields["organization"] = org_name
            # Department is the second ORG component
            if isinstance(org_values, list) and len(org_values) > 1:
                fields["department"] = org_values[1]

    # Website
    if hasattr(card, "url"):
        if card.url.value:
            fields["website"] = card.url.value

    # Job title
    if hasattr(card, "title"):
        if card.title.value:
            fields["job_title"] = card.title.value

    # Addresses
    addresses: list[dict[str, Optional[str]]] = []
    for adr_prop in card.contents.get("adr", []):
        adr = adr_prop.value
        label = _vcard_label(_get_type_params(adr_prop))
        addresses.append({
            "label": label,
            "street": adr.street or None,
            "city": adr.city or None,
            "state": adr.region or None,
            "postal_code": adr.code or None,
            "country": adr.country or None,
        })
    if addresses:
        fields["addresses"] = addresses

    # Notes
    if hasattr(card, "note"):
        if card.note.value:
            fields["notes"] = card.note.value

    # Photo
    if hasattr(card, "photo"):
        import base64
        photo_val = card.photo.value
        if isinstance(photo_val, bytes):
            fields["photo"] = base64.b64encode(photo_val).decode("ascii")
        elif isinstance(photo_val, str):
            # Might already be base64 or a URI
            if photo_val.startswith("data:"):
                parts = photo_val.split(",", 1)
                if len(parts) == 2:
                    fields["photo"] = parts[1]
            else:
                # Assume it's base64 already
                fields["photo"] = photo_val

        # Get media type
        params = card.photo.params if hasattr(card.photo, "params") else {}
        type_val = params.get("TYPE", params.get("MEDIATYPE", ["JPEG"]))
        if isinstance(type_val, list):
            type_val = type_val[0] if type_val else "JPEG"
        if "/" not in str(type_val):
            type_val = f"image/{type_val.lower()}"
        fields["photo_type"] = str(type_val)

    return SyncItem(
        provider_id=href,
        item_type=ItemType.CONTACT,
        fields=fields,
        fingerprint=etag,
    )


def _sync_item_to_vcard(item: SyncItem, uid: Optional[str] = None) -> str:
    """Build a vCard string from a SyncItem."""
    card = vobject.vCard()
    fields = item.fields

    # UID — reuse existing or generate
    if uid is None:
        # Try to extract UID from the provider_id (href) filename
        href = item.provider_id or ""
        basename = href.rstrip("/").rsplit("/", 1)[-1]
        if basename.endswith(".vcf"):
            uid = basename[:-4]
        else:
            uid = str(uuid.uuid4())
    card.add("uid").value = uid

    # Full name
    fn_value = fields.get("full_name", "")
    if not fn_value:
        # Synthesise from parts
        parts = [fields.get("given_name", ""), fields.get("surname", "")]
        fn_value = " ".join(p for p in parts if p) or "Unknown"
    card.add("fn").value = fn_value

    # Structured name
    card.add("n").value = vobject.vcard.Name(
        family=fields.get("surname") or "",
        given=fields.get("given_name") or "",
        additional=fields.get("middle_name") or "",
        prefix=fields.get("prefix") or "",
        suffix=fields.get("suffix") or "",
    )

    # Nickname
    if fields.get("nickname"):
        card.add("nickname").value = fields["nickname"]

    # Birthday
    if fields.get("birthday"):
        card.add("bday").value = fields["birthday"]

    # Emails
    for email_entry in fields.get("emails", []):
        e = card.add("email")
        e.value = email_entry["value"]
        label = email_entry.get("label", "other")
        e.type_param = label.upper()

    # Phones
    for phone_entry in fields.get("phones", []):
        t = card.add("tel")
        t.value = phone_entry["value"]
        label = phone_entry.get("label", "other")
        if label == "mobile":
            t.type_param = "CELL"
        else:
            t.type_param = label.upper()

    # Organization (with optional department as second value)
    org_val: list[str] = []
    if fields.get("organization"):
        org_val.append(fields["organization"])
    if fields.get("department"):
        if not org_val:
            org_val.append("")  # empty org name
        org_val.append(fields["department"])
    if org_val:
        card.add("org").value = org_val

    # Website
    if fields.get("website"):
        card.add("url").value = fields["website"]

    # Job title
    if fields.get("job_title") is not None:
        card.add("title").value = fields["job_title"]

    # Addresses
    for addr_entry in fields.get("addresses", []):
        a = card.add("adr")
        a.value = vobject.vcard.Address(
            street=addr_entry.get("street") or "",
            city=addr_entry.get("city") or "",
            region=addr_entry.get("state") or "",
            code=addr_entry.get("postal_code") or "",
            country=addr_entry.get("country") or "",
        )
        label = addr_entry.get("label", "other")
        a.type_param = label.upper()

    # Notes
    if fields.get("notes") is not None:
        card.add("note").value = fields["notes"]

    # Photo
    if fields.get("photo"):
        import base64
        photo_data = base64.b64decode(fields["photo"])
        photo_prop = card.add("photo")
        photo_prop.value = photo_data
        photo_prop.encoding_param = "b"
        media_type = fields.get("photo_type", "image/jpeg")
        # Extract just the subtype for TYPE param (e.g., "JPEG" from "image/jpeg")
        type_short = media_type.split("/")[-1].upper() if "/" in media_type else media_type.upper()
        photo_prop.type_param = type_short

    return card.serialize()
