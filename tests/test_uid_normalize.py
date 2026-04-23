"""Unit tests for normalize_outlook_goid — the pure function that strips
Outlook's vCal-Uid wrapper from a UID so cross-provider identity pairing
collides correctly."""
from __future__ import annotations

from groupware_sync.models import compute_identity_key
from groupware_sync_calendar.uid_normalize import normalize_outlook_goid

# -- Fixture builders -----------------------------------------------------------

OUTLOOK_GOID_PREFIX_HEX = "040000008200E00074C5B7101A82E008"
# "vCal-Uid\x01\x00\x00\x00" — the inner-UID marker inside a wrapped GOID.
VCAL_UID_MARKER_HEX = "7643616C2D55696401000000"


def _wrap_inner_uid(inner: str) -> str:
    """Build a realistic hex string that looks like a Stalwart-returned
    wrapped-GOID: prefix + 28 bytes of filler + vCal-Uid marker + inner UID
    + trailing null byte."""
    filler = "00" * 28  # version/flags/date/reserved/size — not inspected
    inner_hex = inner.encode("ascii").hex().upper()
    null_hex = "00"
    return OUTLOOK_GOID_PREFIX_HEX + filler + VCAL_UID_MARKER_HEX + inner_hex + null_hex


# -- Falsy / None handling ------------------------------------------------------

def test_none_returned_as_is():
    assert normalize_outlook_goid(None) is None


def test_empty_string_returned_as_is():
    assert normalize_outlook_goid("") == ""


# -- Non-GOID UIDs --------------------------------------------------------------

def test_random_uuid_returned_unchanged():
    uid = "5f8d1b2a-1234-4321-8abc-deadbeef0001"
    assert normalize_outlook_goid(uid) == uid


def test_google_style_uid_returned_unchanged():
    uid = "abc123xyz@google.com"
    assert normalize_outlook_goid(uid) == uid


def test_caldav_slug_returned_unchanged():
    uid = "meeting-2026-04-23-lunch"
    assert normalize_outlook_goid(uid) == uid


# -- Bare GOID (no vCal-Uid marker) ---------------------------------------------

def test_bare_goid_without_marker_returned_unchanged():
    """A GOID-prefixed string with no vCal-Uid marker — e.g. an Outlook-native
    event's iCalUId from Graph — must pass through untouched."""
    bare = OUTLOOK_GOID_PREFIX_HEX + "00" * 40
    assert normalize_outlook_goid(bare) == bare


# -- Wrapped GOID (happy path) --------------------------------------------------

def test_wrapped_goid_yields_inner_uid():
    inner = "inner-uid-12345"
    wrapped = _wrap_inner_uid(inner)
    assert normalize_outlook_goid(wrapped) == inner


def test_wrapped_goid_is_case_insensitive_on_prefix():
    """Stalwart may return the GOID prefix in lowercase; our detection must
    not be case-sensitive on the hex prefix."""
    inner = "inner-abc"
    wrapped = _wrap_inner_uid(inner).lower()
    assert normalize_outlook_goid(wrapped) == inner


# -- Malformed inputs -----------------------------------------------------------

def test_prefix_present_but_no_marker_returned_unchanged():
    """Some edge GOID blobs might lack the vCal-Uid marker. Graceful fallback
    — return the raw UID rather than raising."""
    malformed = OUTLOOK_GOID_PREFIX_HEX + "DEADBEEF" * 10
    assert normalize_outlook_goid(malformed) == malformed


def test_non_hex_input_returned_unchanged():
    """Defence against totally non-GOID strings that happen to start with
    the prefix characters — the hex decode should fail gracefully."""
    # Prefix present but non-hex content after it
    garbage = OUTLOOK_GOID_PREFIX_HEX + "ZZZZZZZZZZZZZZZZ"
    assert normalize_outlook_goid(garbage) == garbage


# -- The core acceptance: identity-key convergence ------------------------------

def test_graph_and_stalwart_forms_produce_same_identity_key():
    """The acceptance criterion for issue #4: Graph's bare inner UID and
    Stalwart's wrapped form of the same event must hash to the same
    identity_key after normalisation."""
    inner = "040000008200E00074C5B7101A82E00807000000ABCDEF0123456789"
    graph_side = inner
    stalwart_side = _wrap_inner_uid(inner)

    graph_key = compute_identity_key(
        {"uid": normalize_outlook_goid(graph_side)}, ["uid"]
    )
    stalwart_key = compute_identity_key(
        {"uid": normalize_outlook_goid(stalwart_side)}, ["uid"]
    )

    assert graph_key is not None
    assert graph_key == stalwart_key
