"""P2.1 — the §6.6 four-arm keystone experiment DRIVER, run as a labeled DRY-RUN.

Locks docs/05-BUILD-PLAN.md §P2.1: run the four arms (utility / utility_off /
recency / frequency) over the accrued corpus and assert a DEFINITE win/killed +
lift_traces_to_clawback. Because no real clawback-settled accrual exists yet (P2.0's
gate reads inconclusive), this runs on a CONSTRUCTED corpus and is stamped a dry-run:
the verdict validates the MACHINERY, it is NEVER a product keep/kill.

Three things are proven: (1) the driver yields a definite verdict; (2) it DISCRIMINATES
— a utility-carries-signal corpus KEEPs, a utility-is-noise corpus KILLs (not rigged);
(3) every dry-run is structurally is_product_verdict=False, and P2.0's readiness gate
still precedes the verdict.
"""
from __future__ import annotations

from hive.research.experiment import (
    build_keep_corpus, build_kill_corpus, run_keystone_experiment,
)

_K = dict(settled=40, distinct_memories=15)            # above the keystone readiness floor


def _keep():
    st, tt = build_keep_corpus()
    return run_keystone_experiment(st, tt, seed=0, **_K)


def _kill():
    st, tt = build_kill_corpus()
    return run_keystone_experiment(st, tt, seed=0, **_K)


# ── the named build-plan test: a definite verdict ─────────────────────────────
def test_keystone_verdict_is_keep_or_kill():
    for report in (_keep(), _kill()):
        r = report.result
        assert r.inconclusive is False                 # readiness floor cleared
        assert (r.win or r.killed) and not (r.win and r.killed)   # exactly one verdict
        assert r.clawback_traced is True               # lift_traces_to_clawback recorded


# ── the harness DISCRIMINATES (not rigged to always keep) ─────────────────────
def test_dry_run_keep_when_utility_carries_signal():
    r = _keep().result
    assert r.win is True and r.killed is False and r.inconclusive is False
    assert r.beats_recency and r.beats_frequency
    assert r.non_saturating and r.within_family_transfer and r.clawback_traced


def test_dry_run_kill_when_utility_is_noise():
    r = _kill().result
    assert r.killed is True and r.win is False and r.inconclusive is False
    assert r.beats_recency is False                    # utility provides no lift over recency


# ── the arms are MEASURED through real recall + rerank (not hand-set) ──────────
def test_arms_measured_through_real_machinery():
    report = _keep()
    util = report.result.arms["utility"].per_task
    off = report.result.arms["utility_off"].per_task
    rec = report.result.arms["recency"].per_task
    # the credit lifts the gold (utility=1) where the same candidate set, ranked WITHOUT
    # utility, misses it (utility_off=recency=0) — proof the _rank_candidates seam is live.
    assert all(x == 1.0 for x in util)
    assert all(x == 0.0 for x in off) and all(x == 0.0 for x in rec)
    assert util != off                                 # the arm signal genuinely reorders


def test_accrual_curve_is_measured_not_pasted():
    report = _keep()
    pts = report.spec.accrual_points
    ys = [y for _x, y in pts]
    assert len(pts) == 12 + 1                           # one point per accrual step (+0)
    assert ys[0] == 0.0 and ys[-1] == 1.0              # rises from no-credit to all-credited
    assert all(b >= a for a, b in zip(ys, ys[1:]))     # monotone non-decreasing (a real sweep)


# ── the integrity lock: a dry-run is NEVER a product verdict ──────────────────
def test_dry_run_is_never_a_product_verdict():
    st, tt = build_keep_corpus()
    # even if a caller asks for product status on a SYNTHETIC provenance, it is refused.
    sneaky = run_keystone_experiment(st, tt, seed=0, is_product_verdict=True,
                                     provenance="DRY-RUN-SYNTHETIC", **_K)
    assert sneaky.is_product_verdict is False
    assert _keep().is_product_verdict is False
    # only a real PRODUCTION provenance may carry a product verdict.
    real = run_keystone_experiment(st, tt, seed=0, is_product_verdict=True,
                                   provenance="PRODUCTION", **_K)
    assert real.is_product_verdict is True


# ── P2.0's readiness gate still PRECEDES the verdict (composes with P2.1) ──────
def test_readiness_gate_still_blocks_under_powered_experiment():
    st, tt = build_keep_corpus()
    # the SAME KEEP corpus, but below the §8 credit-density floor ⇒ inconclusive, NOT a
    # verdict — the readiness pre-gate precedes the keep/kill even when utility wins.
    under = run_keystone_experiment(st, tt, seed=0, settled=5, distinct_memories=2)
    assert under.result.inconclusive is True
    assert under.result.win is False and under.result.killed is False
