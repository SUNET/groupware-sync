"""UID normalisation for cross-provider calendar identity pairing.

Outlook events carry a GlobalObjectId (GOID) binary blob as their iCalendar
UID. Microsoft Graph returns the bare inner UID (the iCalendar UID with
which the event was originally created or imported). Stalwart's JMAP
CalendarEvent/get returns the full GOID hex, which embeds the inner UID
inside a ``vCal-Uid\\x01\\x00\\x00\\x00<inner>\\x00`` wrapper.

``compute_identity_key`` hashes raw strings, so these two representations
of the same event produce different identity keys and fail to pair. This
module provides one pure function that strips the wrapper when present,
returning the inner UID, so both sides hash to the same key.

See issue #4 and
docs:2026-04-23-post-pr2-stalwart-quirks-design.md for background.
"""
from __future__ import annotations

# Outlook GlobalObjectId fixed prefix (16 bytes, hex encoded).
_OUTLOOK_GOID_PREFIX_HEX = "040000008200E00074C5B7101A82E008"

# Byte sequence that marks the start of an embedded iCalendar UID inside
# a GOID data section. "vCal-Uid" + 0x01 0x00 0x00 0x00.
_VCAL_UID_MARKER = b"vCal-Uid\x01\x00\x00\x00"


def normalize_outlook_goid(uid: str | None) -> str | None:
    """If *uid* is an Outlook-wrapped GOID, return the embedded inner UID;
    otherwise return *uid* unchanged.

    Safe fallback: any parsing failure (non-hex content, missing marker,
    decode error) returns the original input untouched. Callers can always
    feed the result straight into ``compute_identity_key``.
    """
    if not uid:
        return uid

    if not uid.upper().startswith(_OUTLOOK_GOID_PREFIX_HEX):
        return uid

    try:
        raw = bytes.fromhex(uid)
    except ValueError:
        return uid

    idx = raw.find(_VCAL_UID_MARKER)
    if idx == -1:
        return uid

    inner_bytes = raw[idx + len(_VCAL_UID_MARKER):]
    # The inner UID is ASCII and terminated by a trailing null byte.
    # Strip every trailing null defensively.
    inner_bytes = inner_bytes.rstrip(b"\x00")
    if not inner_bytes:
        return uid

    try:
        return inner_bytes.decode("ascii")
    except UnicodeDecodeError:
        return uid
