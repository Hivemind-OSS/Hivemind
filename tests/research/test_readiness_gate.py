"""P2.0 — the pre-registered Phase-2 readiness gate (credit-density probe).

Locks the readiness-gate contract: below either floor the keystone is held
``inconclusive`` (NOT ``killed``) — sparsity is read as "widen / extend," never as a
negative on move #6. The probe is the measurement; ``run_keystone_eval`` is the gate.

Also pins the HONEST current-state run: there is no hivemind production accrual yet
(no /data store has ever run a producer tick), so the real verdict is inconclusive —
the gate working exactly as designed ("do not run the keystone underpowered").
"""
from __future__ import annotations

from hive.adapters.sqlite_db import connect
from hive.adapters.utility_store_sqlite import SqliteUtilityStore
from hive.domain.attribution import CreditDelta
from hive.research.keystone import KeystoneSpec, run_keystone_eval
from hive.research.readiness import (
    PROD_M_MEMORIES_MIN, PROD_N_SETTLED_MIN, ReadinessReport, aggregate_settled,
    count_distinct_credited, readiness_probe,
)

_FAMILY = "fix-failing-CI"


# ── seeding helpers (the production path: real :memory: store + apply_credit) ──
def _store() -> SqliteUtilityStore:
    return SqliteUtilityStore(connect(":memory:"))


def _credit(store: SqliteUtilityStore, eid: int, *, family: str = _FAMILY,
            dw: float = 1.0, dl: float = 0.0) -> None:
    store.apply_credit([CreditDelta(episode_id=eid, family_scope=family,
                                    d_wins=dw, d_losses=dl)])


def _ticks(*settled_counts: int) -> list[int]:
    """Per-tick settled counts. The producer that emitted ProducerTick objects is gone;
    readiness_probe / aggregate_settled now take the raw int counts directly."""
    return list(settled_counts)


def _spec_from(report: ReadinessReport, **arm_overrides) -> KeystoneSpec:
    """A would-be-KILL spec (recency ties utility) carrying the probe's real counts +
    the production thresholds. If the readiness floors are unmet, the keystone MUST
    still report inconclusive — proving the readiness pre-gate PRECEDES the kill."""
    base = dict(
        utility_recall=(1,) * 10,
        recency_recall=(1,) * 10,                       # recency ties ⇒ would be a KILL
        frequency_recall=(0, 1, 0, 0, 1, 0, 0, 1, 0, 0),
        accrual_points=tuple((i, 0.2 + 0.1 * i) for i in range(5) for _ in range(6)),
        transfer_present=(1,) * 10, transfer_ablated=(0,) * 10,
        settled=report.settled, distinct_memories=report.distinct_memories,
        clawback_traced=True,
        n_settled_min=report.n_settled_min, m_memories_min=report.m_memories_min,
    )
    base.update(arm_overrides)
    return KeystoneSpec(**base)


# ── the named build-plan test: blocked until credit density ───────────────────
def test_phase2_blocked_until_credit_density():
    store = _store()
    # a handful of credited memories + a trickle of settled outcomes — far below the
    # production credit-density floors (200 settled / 30 distinct for fix-failing-CI).
    for eid in range(5):
        _credit(store, eid)
    report = readiness_probe(_ticks(10, 12, 8), store)            # 30 settled, 5 distinct
    assert report.ready is False
    assert report.settled == 30 and report.distinct_memories == 5
    assert report.n_settled_min == PROD_N_SETTLED_MIN
    assert report.m_memories_min == PROD_M_MEMORIES_MIN

    # feeding the real counts into the keystone holds it INCONCLUSIVE — and NOT killed,
    # even though recency ties utility (which, powered, would be a pre-committed kill).
    verdict = run_keystone_eval(_spec_from(report), seed=0)
    assert verdict.inconclusive is True
    assert verdict.killed is False and verdict.win is False


def test_settled_floor_alone_blocks():
    # enough distinct memories, but too few settled outcomes ⇒ still not ready.
    store = _store()
    for eid in range(PROD_M_MEMORIES_MIN + 5):
        _credit(store, eid)
    report = readiness_probe(_ticks(50), store)                  # 50 < 200
    assert report.distinct_memories >= PROD_M_MEMORIES_MIN
    assert report.ready is False and "settled=50<200" in report.reason
    assert run_keystone_eval(_spec_from(report), seed=0).inconclusive is True


def test_memories_floor_alone_blocks():
    # plenty of settled volume, but credit concentrated on too few memories ⇒ not ready.
    store = _store()
    for eid in range(PROD_M_MEMORIES_MIN - 1):                   # 29 < 30 distinct
        _credit(store, eid)
    report = readiness_probe(_ticks(PROD_N_SETTLED_MIN + 100), store)
    assert report.settled >= PROD_N_SETTLED_MIN
    assert report.ready is False and "distinct_memories=29<30" in report.reason
    assert run_keystone_eval(_spec_from(report), seed=0).inconclusive is True


def test_requires_both_floors_AND():
    # the AND is load-bearing: neither floor alone makes it ready (mutation M-B: AND→OR
    # would let either single-floor case pass).
    store = _store()
    for eid in range(PROD_M_MEMORIES_MIN + 1):
        _credit(store, eid)
    only_memories = readiness_probe(_ticks(10), store)
    assert only_memories.ready is False                          # settled short
    only_settled = readiness_probe(_ticks(PROD_N_SETTLED_MIN), _store())
    assert only_settled.ready is False                          # memories short (0)


def test_sufficient_density_lifts_the_gate():
    # both credit-density floors met ⇒ ready, AND the keystone is NO LONGER inconclusive — it now
    # renders a real verdict (here a KILL, because recency ties utility on the arms).
    store = _store()
    for eid in range(PROD_M_MEMORIES_MIN + 2):
        _credit(store, eid)
    report = readiness_probe(_ticks(PROD_N_SETTLED_MIN, 25), store)
    assert report.ready is True and "ready:" in report.reason
    verdict = run_keystone_eval(_spec_from(report), seed=0)
    assert verdict.inconclusive is False                        # readiness no longer blocks
    assert verdict.killed is True                               # recency tie ⇒ real negative


# ── the distinct-credited counter (the probe's core measurement) ──────────────
def test_count_distinct_credited_excludes_uncredited_and_isolation():
    store = _store()
    _credit(store, 1)                                            # credited ✓
    _credit(store, 2, dl=1.0)                                    # credited (a loss) ✓
    store.apply_credit([CreditDelta(3, _FAMILY, 0.0, 0.0)])      # row exists, wins+losses==0 ✗
    _credit(store, 4)
    store.mark_isolation(4, _FAMILY)                             # credited BUT held-out ✗
    _credit(store, 5, family="other-family")                    # different family ✗
    assert count_distinct_credited(store, _FAMILY) == 2         # only eids 1 and 2


def test_isolation_row_with_credit_is_not_counted():
    # guardrail-2 (mutation M-C: dropping the isolation=0 filter would count this).
    store = _store()
    _credit(store, 7)
    store.mark_isolation(7, _FAMILY)
    assert count_distinct_credited(store, _FAMILY) == 0


def test_aggregate_settled_sums_ticks():
    assert aggregate_settled(_ticks(3, 0, 5, 1)) == 9
    assert aggregate_settled([]) == 0


# ── the HONEST current-state run (no production accrual exists yet) ───────────
def test_current_accrual_is_inconclusive_no_production_stream():
    """The real Phase-1 accrual right now: zero producer ticks, an empty utility
    table (no /data store has ever run). The probe reads the truth — not ready — and
    the keystone is therefore unreachable. This is the readiness gate holding shut by design,
    NOT a negative on move #6: the remedy is to accrue in production, never to fake it."""
    empty = _store()
    report = readiness_probe([], empty)                         # the genuine current state
    assert report.settled == 0 and report.distinct_memories == 0
    assert report.ready is False
    assert report.blocks_keystone() is True
    assert "settled=0<200" in report.reason
    # and the keystone, fed the true zeros, is inconclusive — never killed.
    verdict = run_keystone_eval(_spec_from(report), seed=0)
    assert verdict.inconclusive is True and verdict.killed is False
