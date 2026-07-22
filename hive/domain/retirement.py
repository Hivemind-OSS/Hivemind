"""The §3.2 retirement-evidence gate — the ONE owner of "may this memory be retired?".

Retirement (``hive_prune`` / ``hive_supersede`` / the ``hive_write(replaces=)`` rider)
is agent-CALLED but machine-GATED: the server itself searches for a qualifying machine
signal on the target and refuses to retire without one (an unqualified attempt is a
benign no-op at the boundary, never an error). ``retirement_evidence`` is that search's
pure verdict over feeds the boundary assembles:

  1. **drift**    — a materialized drift verdict at the repo's canonical tip reading
                    ``anchor_missing`` / ``anchor_changed`` / ``blast_radius_changed``
                    (anchored memories only — a general memory reads drift as n/a);
                    OR a ``verify_stale`` ledger row strictly newer than every
                    ``verify_current`` (the ledger form of the same fact — reachable
                    for any memory the census verified, anchored or not).
  2. **outcome**  — a server-written ``outcome_verified_hurt`` row; OR an agent-reported
                    ``outcome_hurt`` whose recorded actor ≠ the RETIRING caller (the
                    DemandRule identity-diversity clause applied to retirement: a
                    ``hive_outcome(hurt=) + hive_prune`` two-call self-authorized
                    destruction is structurally blocked).
  3. **contradiction** — a mechanical near-dup contradiction/redundancy detected at
                    gate time between the target and a co-servable row; for a
                    supersede, a boundary-thresholded ``cos(loser, winner)`` (the
                    successor demonstrably answers the same need).

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
from typing import Optional

from hive.domain.evidence_kinds import (
    EK_OUTCOME_HURT, EK_OUTCOME_VERIFIED_HURT, EK_VERIFY_CURRENT, EK_VERIFY_STALE,
)

# The §3.4 wire drift verdicts that prove the anchor MOVED (the qualifying subset —
# fresh/branch_scoped/unverifiable never justify retirement; unverifiable is the
# fail-safe unknown, and unknown never retires).
_QUALIFYING_DRIFT = frozenset({"anchor_missing", "anchor_changed",
                               "blast_radius_changed"})

# The stamped-signal vocabulary (audit ``signals`` entries). CT-7 asserts these by
# SUBSTRING — 'drift' / 'stale' / 'hurt' / 'contradiction' — the exact spellings are
# owned here.
SIG_DRIFT_PREFIX = "drift:"                       # + the qualifying wire verdict
SIG_VERIFY_STALE = "verify_stale"
SIG_VERIFIED_HURT = "outcome_verified_hurt"
SIG_HURT_OTHER_IDENTITY = "outcome_hurt_other_identity"
SIG_CONTRADICTION = "contradiction"
SIG_WINNER_NEAR_DUP = "winner_near_dup"


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """One ledger row of the gate feed — the minimal projection of an
    ``evidence_events`` row the clauses read: the kind (vocabulary of
    ``evidence_kinds``), the recorded actor (the identity-diversity key for
    agent-reported hurt), and the row timestamp (the stale-vs-current recency
    order)."""
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
    """(kind, actor, ts) from an EvidenceRow, an attr-shaped object, or a
    3-sequence; None for anything undecidable (skipped — under-claim)."""
    try:
        if isinstance(row, EvidenceRow):
            return row.kind, row.actor, int(row.ts)
        if hasattr(row, "kind"):
            return (str(row.kind), str(getattr(row, "actor", "")),
                    int(getattr(row, "ts", 0)))
        kind, actor, ts = row  # type: ignore[misc]  # 3-sequence form
        return str(kind), str(actor), int(ts)
    except Exception:  # noqa: BLE001 — undecidable row ⇒ skipped
        return None


def _involves(pair: object, episode_id: int) -> bool:
    """True iff a detected conflict pair names the target — read from ``a_id``/
    ``b_id`` attrs (ConflictNote shape) or a 2+-sequence of ids; anything
    undecidable is False (a malformed pair never fabricates a contradiction)."""
    try:
        a = getattr(pair, "a_id", None)
        b = getattr(pair, "b_id", None)
        if a is None and b is None:
            a, b = pair[0], pair[1]  # type: ignore[index]
        return int(a) == episode_id or int(b) == episode_id
    except Exception:  # noqa: BLE001 — undecidable pair ⇒ no conflict
        return False


def retirement_evidence(*, episode, caller_identity, drift_verdicts,
                        evidence_rows, conflict_pairs,
                        winner_cosine=None) -> Eligibility:
    """Does at least one qualifying machine signal exist for ``episode``?

    Feeds (assembled by the boundary; every feed already fail-open there — a
    reader FAULT must be treated by the caller as undecidable ⇒ ineligible,
    never a retire):
      - ``episode`` — the target's carrier (``anchors`` decides whether the
        drift-cache clause applies; ``id`` keys the pair check).
      - ``caller_identity`` — the RETIRING caller (never the hurt reporter).
      - ``drift_verdicts`` — the newest materialized §3.4 wire verdicts for the
        target's anchors at each repo's canonical tip (strings; order free).
      - ``evidence_rows`` — the target's ledger rows (``EvidenceRow`` or
        (kind, actor, ts) shapes).
      - ``conflict_pairs`` — mechanical detector output (``ConflictNote``-shaped)
        for the co-servable field; NEVER advisory conflict_flags rows.
      - ``winner_cosine`` — supersede only: the measured cos(loser, winner),
        passed by the boundary ONLY when it already cleared ``conflict.tau``
        (the config threshold lives at the boundary — Law 4); validated here for
        finiteness/range only. None everywhere else.

    Returns ``Eligibility(eligible, signals)`` with every satisfied clause named
    in clause order. Pure, total, fail-closed: undecidable ⇒ ineligible."""
    try:
        eid = int(episode.id)
        signals: list[str] = []

        # ── clause 1a: materialized drift at the canonical tip (anchored only —
        # a general/scope-only memory reads drift as n/a, §3.2 clause 4).
        if tuple(getattr(episode, "anchors", ())):
            seen: list[str] = []
            for verdict in drift_verdicts or ():
                if isinstance(verdict, str) and verdict in _QUALIFYING_DRIFT:
                    sig = SIG_DRIFT_PREFIX + verdict
                    if sig not in seen:
                        seen.append(sig)
            signals.extend(seen)

        # ── clause 1b: the ledger form — the newest verify_stale strictly newer
        # than every verify_current (a tie or a newer current disqualifies:
        # fail-closed, the re-verification wins).
        stale_ts: Optional[int] = None
        current_ts: Optional[int] = None
        parsed: list[tuple[str, str, int]] = []
        for row in evidence_rows or ():
            fields = _row_fields(row)
            if fields is None:
                continue                            # malformed row ⇒ under-claim
            parsed.append(fields)
            kind, _actor, ts = fields
            if kind == EK_VERIFY_STALE:
                stale_ts = ts if stale_ts is None else max(stale_ts, ts)
            elif kind == EK_VERIFY_CURRENT:
                current_ts = ts if current_ts is None else max(current_ts, ts)
        if stale_ts is not None and (current_ts is None or stale_ts > current_ts):
            signals.append(SIG_VERIFY_STALE)

        # ── clause 2: outcome-hurt — server-written verified hurt (any actor),
        # or agent-reported hurt from an identity OTHER than the retiring caller
        # (the identity-diversity clause: two-call self-destruction blocked).
        if any(kind == EK_OUTCOME_VERIFIED_HURT for kind, _a, _t in parsed):
            signals.append(SIG_VERIFIED_HURT)
        if any(kind == EK_OUTCOME_HURT and actor != str(caller_identity)
               for kind, actor, _t in parsed):
            signals.append(SIG_HURT_OTHER_IDENTITY)

        # ── clause 3: mechanical contradiction — a detector pair naming the
        # target, or (supersede) the boundary-thresholded near-dup winner.
        if any(_involves(pair, eid) for pair in conflict_pairs or ()):
            signals.append(SIG_CONTRADICTION)
        if winner_cosine is not None:
            try:
                c = float(winner_cosine)
            except (TypeError, ValueError):
                c = float("nan")                    # undecidable ⇒ not a signal
            if math.isfinite(c) and -1.0 <= c <= 1.0:
                signals.append(SIG_WINNER_NEAR_DUP)

        return Eligibility(bool(signals), tuple(signals))
    except Exception:  # noqa: BLE001 — total by contract: undecidable ⇒ ineligible
        return _INELIGIBLE
