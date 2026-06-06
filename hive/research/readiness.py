"""P2.0 — the pre-registered §8 Phase-2 readiness gate (credit-density probe).

Phase 2 (the §6.6 keystone A/B, P2.1) must NOT run underpowered. Before the
experiment is allowed to render a keep/kill verdict, two pre-registered floors
(fixed on the corpus BEFORE the run, docs §8) must both hold:

  * **settled-outcome volume**  ``settled ≥ N_settled``  (ProducerTick.settled
    aggregated over the Phase-1 accrual window).
  * **distinct credited memories** ``distinct(episode_id with wins+losses>0) ≥
    M_memories`` in the chosen family (``utility`` rows, isolation-excluded).

Below either floor the verdict is **inconclusive, NOT killed** — sparsity is read
as "widen the family / extend the window," never as a negative (§6.6 / §12). This
module does NOT re-implement that decision: it computes the two real counters and
hands them to ``KeystoneSpec`` so ``run_keystone_eval``'s existing pre-gate (the
P1.10-pinned ``inconclusive`` branch) fires. The probe is the *measurement*; the
keystone is the *gate*.

Dev-time only — the P0.0 AST fence keeps ``domain``/``adapters``/``app`` from
importing ``hive.research``; this reads adapters, never the reverse.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Protocol

from hive.domain.models import ProducerTick

log = logging.getLogger("hive.research.readiness")

# ── pre-registered §8 thresholds (fixed BEFORE the run; docs/05-BUILD-PLAN.md §8) ──
# The recommended family is the only machine-verifiable, high-credit-density one.
PROD_FAMILY = "fix-failing-CI"
PROD_N_SETTLED_MIN = 200            # settled-outcome volume floor for fix-failing-CI
PROD_M_MEMORIES_MIN = 30           # distinct credited-memory floor in that family


class _UtilityConn(Protocol):
    """The slice of SqliteUtilityStore the probe reads — its shared connection."""
    conn: "object"


@dataclass(frozen=True)
class ReadinessReport:
    """The frozen credit-density measurement + the pre-gate decision it implies.

    ``ready`` is the AND of both floors; a not-ready report is the §8 signal to
    widen the family / extend the window — it is NEVER a negative on move #6."""
    family_scope: str
    settled: int
    distinct_memories: int
    n_settled_min: int
    m_memories_min: int
    ready: bool
    reason: str

    def blocks_keystone(self) -> bool:
        """True iff this report should hold the keystone in ``inconclusive`` (the
        same condition ``run_keystone_eval`` re-derives from the counts it is fed)."""
        return not self.ready


def aggregate_settled(ticks: Iterable[ProducerTick]) -> int:
    """Total clawback-settled outcome volume = Σ ``ProducerTick.settled`` over the
    Phase-1 accrual window (the §8-named source counter). // O(n) time, O(1) space."""
    return sum(int(t.settled) for t in ticks)


def count_distinct_credited(util_store: _UtilityConn, family_scope: str) -> int:
    """Distinct episodes that carry REAL credit in ``family_scope``: ``wins+losses>0``
    (a memory the producer has actually credited or clawed back — not a bare,
    never-settled row) AND ``isolation=0`` (held-out control memories are never
    credited and must never count toward density — guardrail-2). A missing
    ``utility`` table raises loud (a probe that silently returns 0 on a wiring fault
    would read as 'sparse ⇒ inconclusive', masking the fault — the M08 phantom).
    // O(R) over the family's utility rows (single indexed scan), O(1) space."""
    row = util_store.conn.execute(
        "SELECT COUNT(*) FROM utility "
        "WHERE family_scope = ? AND (wins + losses) > 0 AND isolation = 0",
        (family_scope,),
    ).fetchone()
    return int(row[0])


def readiness_probe(
    ticks: Iterable[ProducerTick],
    util_store: _UtilityConn,
    *,
    family_scope: str = PROD_FAMILY,
    n_settled_min: int = PROD_N_SETTLED_MIN,
    m_memories_min: int = PROD_M_MEMORIES_MIN,
) -> ReadinessReport:
    """Measure the two §8 credit-density floors against the real Phase-1 accrual and
    decide readiness. ``ready`` requires BOTH floors (the AND is load-bearing —
    enough settled volume on too few memories, or vice-versa, is not powered). The
    decision is logged at the boundary so an operator can see exactly which floor
    blocks. // O(n + R) time."""
    settled = aggregate_settled(ticks)
    distinct = count_distinct_credited(util_store, family_scope)
    settled_ok = settled >= n_settled_min
    memories_ok = distinct >= m_memories_min
    ready = settled_ok and memories_ok

    if ready:
        reason = (f"ready: settled={settled}≥{n_settled_min} and "
                  f"distinct_memories={distinct}≥{m_memories_min} in {family_scope!r}")
        log.info("readiness.ready family=%s settled=%d distinct_memories=%d",
                 family_scope, settled, distinct)
    else:
        shortfalls = []
        if not settled_ok:
            shortfalls.append(f"settled={settled}<{n_settled_min}")
        if not memories_ok:
            shortfalls.append(f"distinct_memories={distinct}<{m_memories_min}")
        reason = ("inconclusive (NOT a negative — widen family / extend window): "
                  + ", ".join(shortfalls) + f" in {family_scope!r}")
        # WARN, not error: under-powered is a recoverable "keep accruing" state, the
        # §6.6 "inconclusive ≠ negative" contract — the keystone is simply not yet
        # reachable, NOT a kill of move #6.
        log.warning("readiness.not_ready family=%s settled=%d (min %d) "
                    "distinct_memories=%d (min %d) — keystone held inconclusive",
                    family_scope, settled, n_settled_min, distinct, m_memories_min)

    return ReadinessReport(
        family_scope=family_scope, settled=settled, distinct_memories=distinct,
        n_settled_min=n_settled_min, m_memories_min=m_memories_min,
        ready=ready, reason=reason)
