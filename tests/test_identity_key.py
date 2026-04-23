"""Unit tests for compute_identity_key."""
from groupware_sync.models import compute_identity_key


def test_returns_none_for_empty_fields():
    assert compute_identity_key({}, ["uid"]) is None
    assert compute_identity_key({"uid": None}, ["uid"]) is None
    assert compute_identity_key({"uid": ""}, ["uid"]) is None


def test_stable_hash_for_same_input():
    a = compute_identity_key({"uid": "abc-123"}, ["uid"])
    b = compute_identity_key({"uid": "abc-123"}, ["uid"])
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_case_insensitive_canonicalization():
    a = compute_identity_key({"uid": "ABC-123"}, ["uid"])
    b = compute_identity_key({"uid": "abc-123"}, ["uid"])
    assert a == b


def test_whitespace_stripped():
    a = compute_identity_key({"uid": "  abc  "}, ["uid"])
    b = compute_identity_key({"uid": "abc"}, ["uid"])
    assert a == b


def test_different_values_produce_different_hashes():
    a = compute_identity_key({"uid": "abc"}, ["uid"])
    b = compute_identity_key({"uid": "abd"}, ["uid"])
    assert a != b


def test_list_values_normalized():
    # emails-like: list of dicts with 'value' key
    a = compute_identity_key(
        {"emails": [{"type": "work", "value": "X@Y.Z"}, {"type": "home", "value": "a@b.c"}]},
        ["emails"],
    )
    b = compute_identity_key(
        {"emails": [{"value": "a@b.c"}, {"value": "x@y.z"}]},
        ["emails"],
    )
    # Order-independent and case-insensitive
    assert a == b


def test_multiple_identity_fields_combined():
    a = compute_identity_key({"uid": "u1", "email": "e1"}, ["uid", "email"])
    b = compute_identity_key({"email": "e1", "uid": "u1"}, ["uid", "email"])
    assert a == b


def test_unicode_nfc_nfd_equivalence():
    import unicodedata
    pre = "café"  # pre-composed
    decomposed = unicodedata.normalize("NFD", pre)
    assert pre != decomposed  # sanity: different byte sequences
    a = compute_identity_key({"uid": pre}, ["uid"])
    b = compute_identity_key({"uid": decomposed}, ["uid"])
    assert a == b


def test_dict_value_none_does_not_produce_spurious_key():
    """Dict entries shaped {"value": None} must normalize to empty,
    not to the string "None" — otherwise compute_identity_key would
    return a non-None hash for missing data and accidentally pair
    unrelated records."""
    assert compute_identity_key({"emails": [{"value": None}]}, ["emails"]) is None
    assert compute_identity_key({"uid": {"value": None}}, ["uid"]) is None
    # Sanity: mixed list with one real value still produces a key
    a = compute_identity_key(
        {"emails": [{"value": None}, {"value": "a@b.c"}]},
        ["emails"],
    )
    b = compute_identity_key({"emails": [{"value": "a@b.c"}]}, ["emails"])
    assert a == b


def test_casefold_handles_german_eszett():
    a = compute_identity_key({"uid": "straße"}, ["uid"])
    b = compute_identity_key({"uid": "STRASSE"}, ["uid"])
    assert a == b


def test_sync_node_has_identity_key_field():
    from groupware_sync.models import NodeType, SyncNode
    leaf = SyncNode("n", "n", NodeType.LEAF, fingerprint="fp", identity_key="abc")
    assert leaf.identity_key == "abc"

    leaf_no_key = SyncNode("n", "n", NodeType.LEAF, fingerprint="fp")
    assert leaf_no_key.identity_key is None
