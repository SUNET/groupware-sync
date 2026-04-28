"""Photo fetch errors in GraphContactAdapter.get_items must surface in logs.

Audit issue #23: the previous code swallowed every photo-fetch exception
silently, which meant transport errors, auth failures, and programming
bugs in the photo path were invisible in production.

Contract pinned here:
* HTTP 200 → photo fields populated (control case).
* HTTP 404 → no log line; "no photo" is the common case.
* Other HTTP status → WARNING with the status code.
* Transport / parse exception → WARNING with traceback (exc_info).
"""
from __future__ import annotations

import logging

import httpx
import pytest

from groupware_sync_contacts.adapters.graph_adapter import (
    GRAPH_BASE,
    GraphContactAdapter,
)


def _adapter_with_handler(handler) -> GraphContactAdapter:
    a = GraphContactAdapter("token")
    a._client = httpx.Client(
        base_url=GRAPH_BASE,
        headers={"Authorization": "Bearer token"},
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return a


_CONTACT_JSON = {
    "id": "cid-1",
    "displayName": "Alice",
    "lastModifiedDateTime": "2026-01-01T00:00:00Z",
}


def _route(path_predicates):
    """Build an httpx MockTransport handler from {match_substring: response}."""
    def handler(request: httpx.Request) -> httpx.Response:
        for sub, resp in path_predicates.items():
            if sub in str(request.url):
                return resp
        return httpx.Response(404, json={"error": "no route"}, request=request)
    return handler


@pytest.fixture
def caplog_warn(caplog):
    caplog.set_level(
        logging.WARNING,
        logger="groupware_sync_contacts.adapters.graph_adapter",
    )
    return caplog


def test_photo_404_does_not_log_warning(caplog_warn):
    handler = _route({
        "/photo/$value": httpx.Response(404, request=httpx.Request("GET", "x")),
        "/contacts/cid-1": httpx.Response(200, json=_CONTACT_JSON,
                                          request=httpx.Request("GET", "x")),
    })
    adapter = _adapter_with_handler(handler)
    items = adapter.get_items("folder", ["cid-1"])
    assert len(items) == 1
    assert "photo" not in items[0].fields
    photo_warnings = [r for r in caplog_warn.records if "photo" in r.getMessage()]
    assert photo_warnings == []


def test_photo_500_logs_warning_with_status(caplog_warn):
    handler = _route({
        "/photo/$value": httpx.Response(500, request=httpx.Request("GET", "x")),
        "/contacts/cid-1": httpx.Response(200, json=_CONTACT_JSON,
                                          request=httpx.Request("GET", "x")),
    })
    adapter = _adapter_with_handler(handler)
    items = adapter.get_items("folder", ["cid-1"])
    assert len(items) == 1
    photo_warnings = [r for r in caplog_warn.records if "photo" in r.getMessage()]
    assert len(photo_warnings) == 1
    msg = photo_warnings[0].getMessage()
    assert "cid-1" in msg
    assert "500" in msg


def test_photo_transport_error_logs_warning_with_traceback(caplog_warn):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/photo/$value" in str(request.url):
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=_CONTACT_JSON, request=request)

    adapter = _adapter_with_handler(handler)
    items = adapter.get_items("folder", ["cid-1"])
    # Contact is still returned even though photo fetch blew up.
    assert len(items) == 1
    photo_warnings = [r for r in caplog_warn.records if "photo" in r.getMessage()]
    assert len(photo_warnings) == 1
    rec = photo_warnings[0]
    assert "cid-1" in rec.getMessage()
    # exc_info=True must be honoured so the traceback is in the record.
    assert rec.exc_info is not None


def test_photo_200_populates_fields(caplog_warn):
    handler = _route({
        "/photo/$value": httpx.Response(
            200, content=b"\xff\xd8\xff",
            headers={"content-type": "image/jpeg"},
            request=httpx.Request("GET", "x"),
        ),
        "/contacts/cid-1": httpx.Response(200, json=_CONTACT_JSON,
                                          request=httpx.Request("GET", "x")),
    })
    adapter = _adapter_with_handler(handler)
    items = adapter.get_items("folder", ["cid-1"])
    assert items[0].fields["photo_type"] == "image/jpeg"
    assert items[0].fields["photo"]  # base64 string, non-empty
    photo_warnings = [r for r in caplog_warn.records if "photo" in r.getMessage()]
    assert photo_warnings == []
