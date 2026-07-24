"""Comb-Drift — the node-level verification reflex (existence + interface drift).

Two faces share one parse/resolve/fingerprint core:

- the memory path — verify_records / verify_record / resolve_anchor /
  fingerprint_anchor — re-checks code-anchored memory records against a repo;
- the change path — verify_change over a commit's touched symbols, plus the
  pairwise drift `compare`, the structured `FingerprintDelta`, the
  `VerifierVersion` SHA stamp, and the `tag_for_*` evidence mapper — the
  receipt-facing surface.

The CLI (memory path) is a second surface, as the `comb-drift` console script or
`from combdrift.cli import main`.

A first-party server subpackage of the one `hive` dist — the call-shape /
change-fingerprint engine. Fails conservative: presence it cannot positively
resolve routes to `unverifiable`, never a false `stale`.
"""

from __future__ import annotations

from hive.combdrift.change import ChangeVerdict, SymbolChange, verify_change
from hive.combdrift.evidence import EvidenceTag, tag_for_reason, tag_for_verdict
from hive.combdrift.fingerprint import compare
from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
from hive.combdrift.types import (
    Anchor,
    AnchorResult,
    FingerprintDelta,
    Record,
    RecordVerdict,
    RunSummary,
    Verdict,
)
from hive.combdrift.verdict import verify_record, verify_records
from hive.combdrift.version import VerifierVersion, verifier_version

__all__ = [
    # data contract
    "Anchor",
    "AnchorResult",
    "FingerprintDelta",
    "Record",
    "RecordVerdict",
    "RunSummary",
    "Verdict",
    # memory path
    "fingerprint_anchor",
    "resolve_anchor",
    "verify_record",
    "verify_records",
    # change path (receipt-facing)
    "ChangeVerdict",
    "SymbolChange",
    "verify_change",
    "compare",
    "VerifierVersion",
    "verifier_version",
    # evidence tagging
    "EvidenceTag",
    "tag_for_reason",
    "tag_for_verdict",
]
