"""evidence.py: verdict / reason -> epistemic tag (machine-checked | unverified).

Comb-Drift's node facts are deterministic AST/structural facts, so a *decided*
verdict is machine-checked and every *uncertainty* is unverified (abstain) —
precision over coverage. The mapper is the single owner of the verdict->tag
policy so a receipt never hand-rolls it. Pinned exhaustively over the closed
Verdict set and REASON_* set, with a fail-closed default.
"""

from __future__ import annotations

import pytest

from hive.combdrift.evidence import tag_for_reason, tag_for_verdict
from hive.combdrift.types import (
    REASON_FILE_MISSING,
    REASON_FINGERPRINT_VERSION_MISMATCH,
    REASON_NO_ANCHORS,
    REASON_NO_SYMBOL_REQUESTED,
    REASON_OK,
    REASON_PARSE_ERROR,
    REASON_PATH_OUTSIDE_REPO,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_INDIRECT,
    REASON_SYMBOL_MISSING,
    REASON_UNSUPPORTED_LANGUAGE,
)


def test_verdict_tags():
    assert tag_for_verdict("current") == "machine-checked"
    assert tag_for_verdict("stale") == "machine-checked"
    assert tag_for_verdict("unverifiable") == "unverified"


@pytest.mark.parametrize(
    "reason,tag",
    [
        (REASON_OK, "machine-checked"),
        (REASON_FILE_MISSING, "machine-checked"),
        (REASON_SYMBOL_MISSING, "machine-checked"),
        (REASON_NO_SYMBOL_REQUESTED, "machine-checked"),
        (REASON_SIGNATURE_CHANGED, "machine-checked"),
        (REASON_PATH_OUTSIDE_REPO, "unverified"),
        (REASON_PARSE_ERROR, "unverified"),
        (REASON_NO_ANCHORS, "unverified"),
        (REASON_UNSUPPORTED_LANGUAGE, "unverified"),
        (REASON_SYMBOL_INDIRECT, "unverified"),
        (REASON_FINGERPRINT_VERSION_MISMATCH, "unverified"),
    ],
)
def test_reason_tags_exhaustive(reason: str, tag: str):
    assert tag_for_reason(reason) == tag


def test_reason_tag_ignores_detail_suffix():
    # Reasons carry an optional ": <detail>" suffix; tags match by prefix.
    assert tag_for_reason("symbol_missing: refresh") == "machine-checked"
    assert tag_for_reason("signature_changed: a -> b") == "machine-checked"
    assert tag_for_reason("symbol_indirect: reexport") == "unverified"
    assert (
        tag_for_reason("fingerprint_version_mismatch: ambiguous_overload")
        == "unverified"
    )


def test_symbol_missing_and_indirect_do_not_collide():
    # Both start with "symbol_" but tag oppositely — the mapper keys on the full code.
    assert tag_for_reason("symbol_missing") == "machine-checked"
    assert tag_for_reason("symbol_indirect") == "unverified"


def test_unknown_reason_is_unverified():
    # Fail-closed: anything not provably mechanical abstains.
    assert tag_for_reason("some_future_reason") == "unverified"
    assert tag_for_reason("") == "unverified"
