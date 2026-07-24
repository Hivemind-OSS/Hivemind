"""The single owner of the verdict/reason -> epistemic-tag policy (pure, total).

Comb-Drift's facts are deterministic AST/structural facts, so a *decided* verdict
is `machine-checked` and every *uncertainty* is `unverified` (the verifier
abstained) — precision over coverage. A receipt tags its node lines through here
rather than hand-rolling the mapping, so the policy lives in one place. Comb-Drift
emits only these two tags; it never produces `unverified-judgment` (that tag is
reserved for LLM-touched lines, and nothing here is LLM-judged).
"""

from __future__ import annotations

from typing import Literal

from hive.combdrift.types import (
    REASON_FILE_MISSING,
    REASON_NO_SYMBOL_REQUESTED,
    REASON_OK,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_MISSING,
    Verdict,
)

EvidenceTag = Literal["machine-checked", "unverified"]

# Reason codes that name a provable mechanical fact: a name bound (ok), a file or
# symbol provably absent from a cleanly-parsed tree (file_missing / symbol_missing),
# a confirmed call-break (signature_changed), or a confirmed file presence
# (no_symbol_requested). Every other reason is an abstention -> unverified. Matched
# by prefix because a reason may carry a ": <detail>" suffix.
_MACHINE_CHECKED_REASONS = (
    REASON_OK,
    REASON_FILE_MISSING,
    REASON_SYMBOL_MISSING,
    REASON_NO_SYMBOL_REQUESTED,
    REASON_SIGNATURE_CHANGED,
)


def tag_for_verdict(verdict: Verdict) -> EvidenceTag:
    """current / stale -> machine-checked; unverifiable -> unverified (abstain)."""
    return "unverified" if verdict == "unverifiable" else "machine-checked"


def tag_for_reason(reason: str) -> EvidenceTag:
    """Tag a machine-stable reason code; fail-closed to unverified for the rest."""
    return (
        "machine-checked"
        if reason.startswith(_MACHINE_CHECKED_REASONS)
        else "unverified"
    )
