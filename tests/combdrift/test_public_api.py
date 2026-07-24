"""The receipt-facing change-path surface is importable from the top-level package.

The change path (verify_change + the pairwise compare, the structured delta, the
SHA stamp, and the evidence-tag mapper) is the surface a receipt builder consumes;
it must be reachable as `from combdrift import ...` alongside the original memory
path, which stays exported unchanged.
"""

from __future__ import annotations

import hive.combdrift as combdrift

_CHANGE_PATH = [
    "verify_change",
    "SymbolChange",
    "ChangeVerdict",
    "compare",
    "FingerprintDelta",
    "VerifierVersion",
    "verifier_version",
    "tag_for_verdict",
    "tag_for_reason",
    "EvidenceTag",
]

_MEMORY_PATH = [
    "verify_records",
    "verify_record",
    "resolve_anchor",
    "fingerprint_anchor",
    "Anchor",
    "AnchorResult",
    "Record",
    "RecordVerdict",
    "RunSummary",
    "Verdict",
]


def test_change_path_surface_exported():
    for name in _CHANGE_PATH:
        assert name in combdrift.__all__, f"{name} missing from __all__"
        assert hasattr(combdrift, name), f"{name} not importable from combdrift"


def test_memory_path_surface_unchanged():
    for name in _MEMORY_PATH:
        assert name in combdrift.__all__, f"{name} missing from __all__"
        assert hasattr(combdrift, name), f"{name} not importable from combdrift"


def test_top_level_imports_resolve():
    from hive.combdrift import (  # noqa: F401
        ChangeVerdict,
        FingerprintDelta,
        SymbolChange,
        VerifierVersion,
        compare,
        tag_for_reason,
        tag_for_verdict,
        verifier_version,
        verify_change,
    )
