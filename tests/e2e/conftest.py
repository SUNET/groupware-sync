"""Shared fixtures for end-to-end sync tests.

Requires a running Radicale instance (via docker-compose) on localhost:15232.
Tests are marked with ``e2e`` so they can be selected or skipped:
    pytest -m e2e          # run only e2e tests
    pytest -m "not e2e"    # skip e2e tests
"""
from __future__ import annotations

import os
import tempfile

import httpx
import pytest

from groupware_sync_contacts.adapters.carddav_adapter import CardDavContactAdapter
from groupware_sync.state.db import make_session_factory

RADICALE_URL = os.environ.get("RADICALE_URL", "http://localhost:15232")
ALICE_USER = "alice"
ALICE_PASS = "testpass"
BOB_USER = "bob"
BOB_PASS = "testpass"


def _radicale_reachable() -> bool:
    """Return True if the Radicale server responds."""
    try:
        r = httpx.get(RADICALE_URL, timeout=3)
        return r.status_code < 500
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


radicale_available = pytest.mark.skipif(
    not _radicale_reachable(),
    reason="Radicale not reachable at " + RADICALE_URL,
)


def _ensure_addressbook(base_url: str, user: str, password: str, book_name: str) -> str:
    """Create an addressbook for *user* if it does not already exist.

    Returns the collection href (e.g. ``/alice/book_name/``).
    """
    href = f"/{user}/{book_name}/"
    url = f"{base_url}{href}"
    client = httpx.Client(
        auth=httpx.BasicAuth(user, password),
        timeout=10,
        follow_redirects=True,
    )
    try:
        # Check if it exists
        resp = client.request(
            "PROPFIND",
            url,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            content=(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:propfind xmlns:d="DAV:"><d:prop>'
                "<d:resourcetype/>"
                "</d:prop></d:propfind>"
            ).encode(),
        )
        if resp.status_code < 400:
            return href

        # Create it via MKCOL
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:mkcol xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
            "<d:set><d:prop>"
            "<d:resourcetype><d:collection/><card:addressbook/></d:resourcetype>"
            f"<d:displayname>{book_name}</d:displayname>"
            "</d:prop></d:set>"
            "</d:mkcol>"
        )
        resp = client.request(
            "MKCOL",
            url,
            headers={"Content-Type": "application/xml"},
            content=body.encode(),
        )
        resp.raise_for_status()
        return href
    finally:
        client.close()


def _delete_all_contacts(base_url: str, user: str, password: str, book_href: str) -> None:
    """Remove every contact from the given addressbook."""
    url = f"{base_url}{book_href}"
    client = httpx.Client(
        auth=httpx.BasicAuth(user, password),
        timeout=10,
        follow_redirects=True,
    )
    try:
        resp = client.request(
            "PROPFIND",
            url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:propfind xmlns:d="DAV:"><d:prop>'
                "<d:resourcetype/>"
                "</d:prop></d:propfind>"
            ).encode(),
        )
        if resp.status_code >= 400:
            return

        import xml.etree.ElementTree as ET
        DAV = "DAV:"
        tree = ET.fromstring(resp.text)
        for response_el in tree.findall(f"{{{DAV}}}response"):
            href_el = response_el.find(f"{{{DAV}}}href")
            if href_el is None or href_el.text is None:
                continue
            href = href_el.text.strip()
            # Skip the collection itself
            propstat = response_el.find(f"{{{DAV}}}propstat")
            if propstat is not None:
                prop = propstat.find(f"{{{DAV}}}prop")
                if prop is not None:
                    rt = prop.find(f"{{{DAV}}}resourcetype")
                    if rt is not None and rt.find(f"{{{DAV}}}collection") is not None:
                        continue
            # Delete this contact
            del_url = f"{base_url}{href}"
            client.request("DELETE", del_url)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Session-scoped: adapter instances (one per user)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def alice_book_href() -> str:
    """Ensure alice has an addressbook and return its href."""
    return _ensure_addressbook(RADICALE_URL, ALICE_USER, ALICE_PASS, "contacts")


@pytest.fixture(scope="session")
def bob_book_href() -> str:
    """Ensure bob has an addressbook and return its href."""
    return _ensure_addressbook(RADICALE_URL, BOB_USER, BOB_PASS, "contacts")


@pytest.fixture(scope="session")
def alice_adapter(alice_book_href: str) -> CardDavContactAdapter:
    """CardDavContactAdapter for alice on local Radicale."""
    adapter = CardDavContactAdapter(RADICALE_URL, ALICE_USER, ALICE_PASS)
    yield adapter  # type: ignore[misc]
    adapter.close()


@pytest.fixture(scope="session")
def bob_adapter(bob_book_href: str) -> CardDavContactAdapter:
    """CardDavContactAdapter for bob on local Radicale."""
    adapter = CardDavContactAdapter(RADICALE_URL, BOB_USER, BOB_PASS)
    yield adapter  # type: ignore[misc]
    adapter.close()


# ---------------------------------------------------------------------------
# Function-scoped: fresh state DB + clean addressbooks for each test
# ---------------------------------------------------------------------------

@pytest.fixture
def state_session():
    """Fresh in-memory SQLite state DB for each test."""
    sf = make_session_factory("sqlite://")
    session = sf()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_addressbooks(alice_book_href: str, bob_book_href: str) -> None:
    """Remove all contacts from both addressbooks before each test."""
    _delete_all_contacts(RADICALE_URL, ALICE_USER, ALICE_PASS, alice_book_href)
    _delete_all_contacts(RADICALE_URL, BOB_USER, BOB_PASS, bob_book_href)
