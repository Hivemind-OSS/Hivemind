"""The `meta` grammar gate — ``normalize_meta`` is the ONE boundary that shapes the
opaque metadata map into its canonical serialized carrier form. The kernel never
parses the result again (carried-not-interpreted, like ``anchor``/``kind``): the
grammar is enforced HERE or nowhere, so every clause gets a truth-table row.

Contract under test:
  - absent forms (None / "" / {}) normalize to "" (the column default — no carrier);
  - a well-formed map serializes canonically (key-sorted, tight separators) so the
    same map always yields the same bytes (dedup/idempotency-safe);
  - every grammar violation raises ``BadMeta`` naming the violation — loud refusal,
    never a silent drop or a partial normalize;
  - total over ``object``: isinstance-driven, never indexes/raises anything else.
"""
from __future__ import annotations

import json

import pytest

from hive.domain.meta import (
    BadMeta, META_MAX_KEYS, META_MAX_SERIALIZED, META_MAX_VALUE_LEN, normalize_meta,
)


# ── absent forms → "" (no carrier) ────────────────────────────────────────────
def test_absent_forms_normalize_to_empty_string():
    assert normalize_meta(None) == ""
    assert normalize_meta("") == ""
    assert normalize_meta({}) == ""


# ── the good path: canonical, byte-stable serialization ──────────────────────
def test_good_map_serializes_canonically_key_sorted_and_tight():
    m = {"zzz/last": "v2", "combdrift/fp": "combdrift-fp/1:abc"}
    out = normalize_meta(m)
    assert out == '{"combdrift/fp":"combdrift-fp/1:abc","zzz/last":"v2"}'
    # canonical == json with sorted keys + tight separators, and byte-stable
    assert out == json.dumps(m, sort_keys=True, separators=(",", ":"))
    reordered = {"combdrift/fp": "combdrift-fp/1:abc", "zzz/last": "v2"}
    assert normalize_meta(reordered) == out


def test_key_grammar_accepts_namespaced_lowercase_forms():
    for key in ("a/b", "combdrift/fp", "tool-x/attr.name", "t0/a_b-c.d", "9/9"):
        assert normalize_meta({key: "v"}) == json.dumps(
            {key: "v"}, sort_keys=True, separators=(",", ":"))


# ── violations: each raises BadMeta naming the violation ─────────────────────
@pytest.mark.parametrize("bad_key", [
    "noslash",            # no namespace separator
    "Upper/case",         # uppercase in tool
    "tool/Case",          # uppercase in attr
    "/leading",           # empty tool
    "tool/",              # empty attr
    "-tool/attr",         # tool starts with punctuation
    "tool/.attr",         # attr starts with punctuation
    "to ol/attr",         # whitespace
    "a/b/c",              # a second separator
])
def test_bad_keys_raise_badmeta_naming_the_key(bad_key):
    with pytest.raises(BadMeta, match="key"):
        normalize_meta({bad_key: "v"})


def test_non_string_key_raises_badmeta():
    with pytest.raises(BadMeta, match="key"):
        normalize_meta({1: "v"})


def test_non_string_value_raises_badmeta():
    with pytest.raises(BadMeta, match="str"):
        normalize_meta({"a/b": 1})
    with pytest.raises(BadMeta, match="str"):
        normalize_meta({"a/b": None})
    with pytest.raises(BadMeta, match="str"):
        normalize_meta({"a/b": ["x"]})


def test_oversize_value_raises_badmeta():
    assert normalize_meta({"a/b": "x" * META_MAX_VALUE_LEN})  # at the cap: fine
    with pytest.raises(BadMeta, match="value"):
        normalize_meta({"a/b": "x" * (META_MAX_VALUE_LEN + 1)})


def test_too_many_keys_raises_badmeta():
    ok = {f"t/k{i:02d}": "v" for i in range(META_MAX_KEYS)}
    assert normalize_meta(ok)                                # at the cap: fine
    over = {f"t/k{i:02d}": "v" for i in range(META_MAX_KEYS + 1)}
    with pytest.raises(BadMeta, match="keys"):
        normalize_meta(over)


def test_oversize_serialized_raises_badmeta():
    # each entry is within the per-value cap, but the whole map exceeds the
    # serialized budget — the total cap must bind independently.
    big = {f"t/k{i:02d}": "x" * META_MAX_VALUE_LEN for i in range(12)}
    assert len(json.dumps(big, sort_keys=True,
                          separators=(",", ":")).encode()) > META_MAX_SERIALIZED
    with pytest.raises(BadMeta, match="serialized"):
        normalize_meta(big)


@pytest.mark.parametrize("raw", [["x"], 5, 1.5, True, "not-empty", object()])
def test_non_dict_raw_raises_badmeta(raw):
    with pytest.raises(BadMeta):
        normalize_meta(raw)
