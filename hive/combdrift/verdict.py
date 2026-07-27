"""The single owner of the current/stale/unverifiable classification policy.

Precedence (one source of truth): unverifiable dominates stale dominates
current. A record with no anchors is unverifiable. An anchor whose file/symbol
is missing makes the record stale; an anchor whose path escapes the repo, whose
file will not parse, or whose call shape was never compared (no recorded
fingerprint) makes the record unverifiable — `current` is reserved for a claim
actually measured.
"""

from __future__ import annotations

from hive.combdrift.resolution import resolve_anchor
from hive.combdrift.types import (
    REASON_FINGERPRINT_VERSION_MISMATCH,
    REASON_NO_FINGERPRINT,
    REASON_PARSE_ERROR,
    REASON_PATH_OUTSIDE_REPO,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_INDIRECT,
    REASON_UNSUPPORTED_LANGUAGE,
    AnchorResult,
    Record,
    RecordVerdict,
    RunSummary,
    Verdict,
)

# Anchor reason-code prefixes that make the whole record unverifiable. Membership
# here is also what keeps a reason OUT of the stale tier, so it is the set the
# server-side wire-mapping ratchet reads the stale-classifiable reasons against.
_UNVERIFIABLE_PREFIXES = (
    REASON_PATH_OUTSIDE_REPO,
    REASON_PARSE_ERROR,
    REASON_UNSUPPORTED_LANGUAGE,
    REASON_SYMBOL_INDIRECT,
    REASON_FINGERPRINT_VERSION_MISMATCH,
    # // marker: removing this member is a mutation — an uncompared call shape
    # would fall back to `current` and serve as `fresh` (BUG-071).
    REASON_NO_FINGERPRINT,
)


def _anchor_makes_unverifiable(result: AnchorResult) -> bool:
    return result.reason.startswith(_UNVERIFIABLE_PREFIXES)


def _anchor_makes_stale(result: AnchorResult) -> bool:
    # A resolved-but-drifted symbol (signature_changed) is found=True yet stale;
    # everything else stale is an outright missing file/symbol (found=False).
    return (not result.found) or result.reason.startswith(REASON_SIGNATURE_CHANGED)


def verify_record(repo_root: str, record: Record) -> RecordVerdict:
    """Resolve every anchor in input order and classify the record."""
    results = tuple(resolve_anchor(repo_root, anchor) for anchor in record.anchors)
    verdict = _classify(results)
    return RecordVerdict(id=record.id, verdict=verdict, anchors=results)


def verify_records(
    repo_root: str, records: list[Record]
) -> tuple[list[RecordVerdict], RunSummary]:
    """Verify a batch in input order; return verdicts and aggregate counts.

    len(verdicts) == len(records); the summary buckets sum to len(records).
    """
    verdicts = [verify_record(repo_root, record) for record in records]
    summary = RunSummary(
        current=sum(1 for v in verdicts if v.verdict == "current"),
        stale=sum(1 for v in verdicts if v.verdict == "stale"),
        unverifiable=sum(1 for v in verdicts if v.verdict == "unverifiable"),
    )
    return verdicts, summary


def _classify(results: tuple[AnchorResult, ...]) -> Verdict:
    if not results:
        return "unverifiable"
    if any(_anchor_makes_unverifiable(r) for r in results):
        return "unverifiable"
    if any(_anchor_makes_stale(r) for r in results):
        return "stale"
    return "current"
