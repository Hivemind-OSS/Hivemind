"""The §3.2 retirement-evidence gate — the ONE owner of "may this memory be retired?".

Retirement (``hive_prune`` / ``hive_supersede`` / the ``hive_write(replaces=)`` rider)
is agent-CALLED but machine-GATED: the server itself searches for a qualifying machine
signal on the target and refuses to retire without one (an unqualified attempt is a
benign no-op at the boundary, never an error). ``retirement_evidence`` is that search's
pure verdict over feeds the boundary assembles:

  1. **drift**    — a materialized drift verdict at the memory's OWN line reading
                    ``anchor_missing`` / ``anchor_changed`` / ``blast_radius_changed``
                    (anchored memories only — a general memory reads drift as n/a).
                    This is the SOLE drift feed: the census ``verify_*`` ledger twin
                    was retired with the census verification channel, so exactly one
                    oracle — git, at the memory's own baseline — answers "did this
                    anchor move?" both here and on the served hit.
  2. **outcome**  — a server-written ``outcome_verified_hurt`` row; OR an agent-reported
                    ``outcome_hurt`` whose recorded actor ≠ the RETIRING caller (the
                    DemandRule identity-diversity clause applied to retirement: a
                    ``hive_outcome(hurt=) + hive_prune`` two-call self-authorized
                    destruction is structurally blocked).
  3. **supersede near-dup winner** — a boundary-thresholded ``cos(loser, winner)``:
                    the successor demonstrably answers the same need. Supersede ONLY.
                    A merely-detected near-dup PAIR is deliberately not a signal:
                    near-duplication is the expected steady state of a fleet store,
                    and polarity is caller-supplied, so a pair feed would let any
                    caller manufacture the evidence for destroying any memory by
                    writing a paraphrase of it.

Advisory ``hive_flag`` rows NEVER qualify (agent-asserted, not machine evidence) —
they are conflict_flags rows, not evidence rows, and no feed here carries them; a
foreign kind smuggled into ``evidence_rows`` is simply ignored.

Pure, TOTAL, fail-closed: any undecidable input — a malformed row, an unreadable
pair, an internal fault — under-claims (that clause stays unsatisfied) and a whole-
gate fault returns *ineligible*; the gate can refuse a retirement it should have
allowed, never allow one it should have refused. The boundary counterpart (Law 7):
the gate CALL in the mcp_server handlers is the named mutation surface — deleting it
lets a healthy, evidence-less target be retired, which reds CT-7's noop-on-healthy
tests (tests/contract/test_retirement_gate.py).

PURE: stdlib only. The purity gate (tests/test_purity.py) forbids
sqlite3 | torch | subprocess | os | git | time imports anywhere in hive/domain/.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Optional

from hive.domain.evidence_kinds import EK_OUTCOME_HURT, EK_OUTCOME_VERIFIED_HURT

if TYPE_CHECKING:
    # Typing-only: the carrier class lives in models.py; retirement stays leaf-level
    # at runtime (evidence_kinds only).
    from hive.domain.models import Episode

# The §3.4 wire drift verdicts that mean the anchor MOVED — ONE tier, two policies:
# it qualifies a retirement here, and it is what ``drift.branch_route_verdict`` may
# soften to ``branch_scoped`` for an off-line consumer. A member that means "moved"
# must do both or neither, so both policies read THIS object rather than a second
# copy. fresh/branch_scoped/unverifiable are never in it: unverifiable is the
# fail-safe unknown, and unknown never retires.
QUALIFYING_DRIFT = frozenset(
    {"anchor_missing", "anchor_changed", "blast_radius_changed"}
)

# The stamped-signal vocabulary (audit ``signals`` entries). CT-7 asserts these by
# SUBSTRING — 'drift' / 'hurt' / 'near_dup' — the exact spellings are owned here.
SIG_DRIFT_PREFIX = "drift:"  # + the qualifying wire verdict
SIG_VERIFIED_HURT = "outcome_verified_hurt"
SIG_HURT_OTHER_IDENTITY = "outcome_hurt_other_identity"
SIG_WINNER_NEAR_DUP = "winner_near_dup"


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """One ledger row of the gate feed — the minimal projection of an
    ``evidence_events`` row the clauses read: the kind (vocabulary of
    ``evidence_kinds``), the recorded actor (the identity-diversity key for
    agent-reported hurt), and the row timestamp. Nothing is read out of the
    payload: no surviving clause is decided by one, so the domain parses none."""

    kind: str
    actor: str
    ts: int


@dataclass(frozen=True, slots=True)
class Eligibility:
    """The gate's verdict: ``eligible`` iff at least one clause held; ``signals``
    names EVERY satisfied clause in clause order — stamped verbatim into the
    retirement audit so the ledger records exactly WHICH machine signal(s)
    authorized the retirement."""

    eligible: bool
    signals: tuple[str, ...]


_INELIGIBLE = Eligibility(False, ())


def _row_fields(row: object) -> Optional[tuple[str, str, int]]:
    """(kind, actor, ts) from an EvidenceRow, an attr-shaped object, or a sequence
    of THREE OR MORE elements (anything past the third is ignored, so a caller
    still projecting a wider row cannot crash the gate); None for anything
    undecidable (skipped — under-claim)."""
    try:
        if isinstance(row, EvidenceRow):
            return row.kind, row.actor, int(row.ts)
        if hasattr(row, "kind"):
            return (
                str(row.kind),
                str(getattr(row, "actor", "")),
                int(getattr(row, "ts", 0)),
            )
        fields: Any = list(row)  # type: ignore[call-overload]  # sequence form
        kind, actor, ts = fields[0], fields[1], fields[2]
        return str(kind), str(actor), int(ts)
    except Exception:  # noqa: BLE001 — undecidable row ⇒ skipped
        return None


def retirement_evidence(
    *,
    episode: "Episode",
    caller_identity: str,
    drift_verdicts: Optional[Iterable[str]],
    evidence_rows: Optional[Iterable[object]],
    winner_cosine: Optional[float] = None,
) -> Eligibility:
    """Does at least one qualifying machine signal exist for ``episode``?

    Feeds (assembled by the boundary; every feed already fail-open there — a
    reader FAULT must be treated by the caller as undecidable ⇒ ineligible,
    never a retire):
      - ``episode`` — the target's carrier (``anchors`` decides whether the
        drift-cache clause applies).
      - ``caller_identity`` — the RETIRING caller (never the hurt reporter).
      - ``drift_verdicts`` — the newest materialized §3.4 wire verdicts for the
        target's anchors at each repo's OWN line's tip (strings; order free).
      - ``evidence_rows`` — the target's ledger rows (``EvidenceRow`` or
        (kind, actor, ts) shapes).
      - ``winner_cosine`` — supersede only: the measured cos(loser, winner),
        passed by the boundary ONLY when it already cleared ``conflict.tau``
        (the config threshold lives at the boundary — Law 4); validated here for
        finiteness/range only. None everywhere else.

    Returns ``Eligibility(eligible, signals)`` with every satisfied clause named
    in clause order. Pure, total, fail-closed: undecidable ⇒ ineligible."""
    try:
        signals: list[str] = []

        # ── clause 1: materialized drift at the memory's OWN line (anchored only —
        # a general/scope-only memory reads drift as n/a, §3.2 clause 4).
        # marker: this is now the SOLE drift feed (the verify_stale ledger twin was
        # retired with the census verification channel), so both halves of it are
        # load-bearing. Dropping the ``episode.anchors`` guard reds
        # tests/domain/test_retirement.py::test_drift_is_na_for_a_general_memory —
        # a scope-only memory would start retiring on another memory's verdict.
        # Admitting a verdict outside QUALIFYING_DRIFT reds
        # tests/domain/test_retirement.py::
        # test_non_qualifying_or_malformed_drift_never_qualifies and its boundary twin
        # tests/mcp/test_retirement_gate_boundary.py::test_fresh_drift_does_not_qualify
        # — `fresh` and `unverifiable` would authorize destroying a healthy memory.
        if tuple(getattr(episode, "anchors", ())):
            seen: list[str] = []
            for verdict in drift_verdicts or ():
                if isinstance(verdict, str) and verdict in QUALIFYING_DRIFT:
                    sig = SIG_DRIFT_PREFIX + verdict
                    if sig not in seen:
                        seen.append(sig)
            signals.extend(seen)

        parsed: list[tuple[str, str, int]] = []
        for row in evidence_rows or ():
            fields = _row_fields(row)
            if fields is None:
                continue  # malformed row ⇒ under-claim
            parsed.append(fields)

        # ── clause 2: outcome-hurt — server-written verified hurt (any actor),
        # or agent-reported hurt from an identity OTHER than the retiring caller
        # (the identity-diversity clause: two-call self-destruction blocked).
        if any(kind == EK_OUTCOME_VERIFIED_HURT for kind, _a, _t in parsed):
            signals.append(SIG_VERIFIED_HURT)
        if any(
            kind == EK_OUTCOME_HURT and actor != str(caller_identity)
            for kind, actor, _t in parsed
        ):
            signals.append(SIG_HURT_OTHER_IDENTITY)

        # ── clause 3: the supersede near-dup WINNER — a boundary-thresholded
        # cos(loser, winner). marker: re-admitting a detected conflict-PAIR feed
        # here reds tests/contract/test_minimal_hardening_e2e.py::
        # test_a_cold_stranger_cannot_prune_a_near_duplicated_memory — near
        # duplication is the expected steady state of a fleet store, so a pair feed
        # opens the gate on most of the corpus, to any caller.
        if winner_cosine is not None:
            try:
                c = float(winner_cosine)
            except (TypeError, ValueError):
                c = float("nan")  # undecidable ⇒ not a signal
            if math.isfinite(c) and -1.0 <= c <= 1.0:
                signals.append(SIG_WINNER_NEAR_DUP)

        return Eligibility(bool(signals), tuple(signals))
    except Exception:  # noqa: BLE001 — total by contract: undecidable ⇒ ineligible
        return _INELIGIBLE
