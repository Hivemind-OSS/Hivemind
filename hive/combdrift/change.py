"""Change-oriented front-end: verify a commit's touched symbols, not a memory.

The receipt verifies a *change* — "which touched symbol changed call-shape, and
is the drift breaking?" — whereas the record path verifies a stored memory's
anchors. This module maps a diff's touched (path, symbol) pairs to per-symbol
existence + drift facts by minting the call shape at a base worktree and a head
worktree and feeding the existing directional compare. It re-derives nothing:
existence comes from `resolve_anchor`, tokens from `fingerprint_anchor`, drift
from `fingerprint.compare`, and the roll-up from `verdict._classify` (the single
precedence owner). Comb-Drift stays git-blind — the caller materializes the two
worktrees and passes the SHAs — so the no-network/deterministic posture holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hive.combdrift.fingerprint import compare
from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
from hive.combdrift.types import (
    REASON_FILE_MISSING,
    REASON_FINGERPRINT_VERSION_MISMATCH,
    REASON_OK,
    REASON_SYMBOL_INDIRECT,
    REASON_SYMBOL_MISSING,
    Anchor,
    AnchorResult,
    Verdict,
)
from hive.combdrift.verdict import _classify
from hive.combdrift.version import VerifierVersion, verifier_version

# unchanged | additive | breaking : a persisting symbol's call-shape relationship;
# removed : present at base, provably gone at head (a deletion);
# indeterminate : the drift could not be decided (abstain).
Drift = Literal["unchanged", "additive", "breaking", "removed", "indeterminate"]

_DELETION_REASONS = (REASON_SYMBOL_MISSING, REASON_FILE_MISSING)


@dataclass(frozen=True, slots=True)
class SymbolChange:
    path: str  # repo-relative, head-tree path
    symbol: str  # top-level name or "Class.method"
    existed_before: bool  # resolvable at base
    exists_after: bool  # resolvable at head
    drift: Drift
    old_fingerprint: str | None  # base call-shape token (None if not a single callable)
    new_fingerprint: str | None  # head call-shape token (None if not a single callable)
    reason: str  # machine-stable code (reuses REASON_* spellings)


@dataclass(frozen=True, slots=True)
class ChangeVerdict:
    verifier_version: VerifierVersion
    base_sha: str
    head_sha: str
    symbols: tuple[SymbolChange, ...]
    # Roll-up over symbols reusing verdict precedence (unverifiable > stale >
    # current): a breaking drift or a deletion is stale; an undecidable symbol is
    # unverifiable and dominates; a change that verifies nothing is unverifiable.
    verdict: Verdict


def verify_change(
    base_tree: str,
    head_tree: str,
    touched: tuple[tuple[str, str | None], ...],
    *,
    base_sha: str,
    head_sha: str,
) -> ChangeVerdict:
    """Classify each touched symbol's existence + drift between two worktrees.

    `base_tree`/`head_tree` are checked-out worktree roots the caller provides
    (Comb-Drift does no git); each is used as the repo root for resolution, so
    path-containment and the read-only/parse-only posture are unchanged. Touched
    entries with a None symbol are file-level and produce no SymbolChange in v1.
    """
    changes: list[SymbolChange] = []
    classify_inputs: list[AnchorResult] = []
    for path, symbol in touched:
        if symbol is None:
            continue
        anchor = Anchor(path=path, symbol=symbol)
        base_res = resolve_anchor(base_tree, anchor)
        old_token = fingerprint_anchor(base_tree, anchor)
        new_token = fingerprint_anchor(head_tree, anchor)
        # Resolve head against the base token so the kept directional compare
        # decides breaking-vs-additive through the single fingerprint oracle.
        head_res = resolve_anchor(
            head_tree, Anchor(path=path, symbol=symbol, fingerprint=old_token)
        )
        change, classify = _classify_symbol(
            path, symbol, base_res, head_res, old_token, new_token
        )
        changes.append(change)
        classify_inputs.append(classify)

    return ChangeVerdict(
        verifier_version=verifier_version(base_sha=base_sha, head_sha=head_sha),
        base_sha=base_sha,
        head_sha=head_sha,
        symbols=tuple(changes),
        verdict=_classify(tuple(classify_inputs)),
    )


def _classify_symbol(
    path: str,
    symbol: str,
    base_res: AnchorResult,
    head_res: AnchorResult,
    old_token: str | None,
    new_token: str | None,
) -> tuple[SymbolChange, AnchorResult]:
    """Decide one symbol's drift + the AnchorResult fed to the precedence roll-up.

    The returned AnchorResult's reason is chosen so verdict._classify routes it
    correctly without forking the precedence: a deletion / breaking drift -> stale,
    an undecidable case -> unverifiable, an intact or new symbol -> current.
    """
    existed_before = base_res.found
    exists_after = head_res.found

    drift: Drift
    if exists_after:
        if not existed_before:
            # Added in this change; a new symbol breaks no previously-valid call.
            drift, reason = "additive", REASON_OK
        elif old_token is not None and new_token is not None:
            # The drift case: both shapes minted -> the kept compare decides, and
            # head_res (resolved vs old_token) carries the agreeing reason.
            drift, reason = compare(old_token, new_token), head_res.reason
        else:
            # Present both sides but not a single callable at one (overload /
            # indirect): the shape cannot be compared -> abstain.
            drift, reason = "indeterminate", REASON_FINGERPRINT_VERSION_MISMATCH
    else:
        if existed_before and head_res.reason.startswith(_DELETION_REASONS):
            # Present at base, provably absent at head -> a deletion (D1).
            drift, reason = "removed", head_res.reason
        elif not existed_before:
            # D2: resolvable at neither tree -> a missing-at-head is NOT a
            # regression of a symbol that never existed. Abstain, never stale.
            drift, reason = "indeterminate", REASON_SYMBOL_INDIRECT
        else:
            # Present at base, but head is undecidable (parse_error / unsupported /
            # indirect / outside-repo): cannot confirm a deletion -> abstain.
            drift, reason = "indeterminate", head_res.reason

    change = SymbolChange(
        path=path,
        symbol=symbol,
        existed_before=existed_before,
        exists_after=exists_after,
        drift=drift,
        old_fingerprint=old_token,
        new_fingerprint=new_token,
        reason=reason,
    )
    classify = AnchorResult(
        path=path,
        symbol=symbol,
        found=exists_after,
        location=head_res.location,
        reason=reason,
    )
    return change, classify
