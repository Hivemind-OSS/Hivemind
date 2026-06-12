"""P1.10 — M10 [C1] keystone: the control-arm decision + the two BUILD-NEW
statistics (_non_saturating, _within_family_transfer) + the four-arm ranking seam.

Locked assertions for the keystone block.
"""
from __future__ import annotations

import numpy as np
import pytest

from hive.research.eval_membrane import HashEmbedder, _EvalService
from hive.research.keystone import (
    KeystoneSpec, _non_saturating, _rank_candidates, _within_family_transfer,
    run_keystone_eval,
)


# ── deterministic accrual / transfer fixtures ─────────────────────────────────
def _accrual(bases, *, seed, spread):
    rng = np.random.default_rng(seed)
    pts = []
    for binc, base in enumerate(bases):
        for _ in range(6):
            pts.append((binc + rng.uniform(0, 0.9), base + rng.uniform(-spread, spread)))
    return tuple(pts)


# rising: clear upward slope, tight spread ⇒ β1_lo > 0 ⇒ non_saturating
_RISING = _accrual([0.20, 0.35, 0.50, 0.65, 0.80], seed=1, spread=0.03)
# saturating: plateau with a SLIGHT positive point-slope but wide spread ⇒ β1_lo ≤ 0
# (β1_hat > 0). This is the fixture the mutation (β1_lo>0 → β1_hat>0) flips.
_SATURATING = _accrual([0.48, 0.49, 0.50, 0.51, 0.52], seed=2, spread=0.25)

_XFER_PRESENT = (1, 1, 1, 0, 1, 1, 1, 1, 0, 1)   # credited posterior PRESENT lifts recall
_XFER_ABLATED = (0, 1, 0, 0, 1, 0, 0, 1, 0, 0)   # posterior-ABLATED control
_NOXFER_PRESENT = (1, 0, 1, 0, 1, 0, 1, 0)       # present ≈ ablated ⇒ no transfer
_NOXFER_ABLATED = (1, 0, 1, 0, 0, 1, 1, 0)


def _spec(**kw) -> KeystoneSpec:
    """A base WINNING spec; override one field to isolate a single failure mode."""
    base = dict(
        utility_recall=(1,) * 10,                       # utility finds gold every task
        recency_recall=(1, 0, 0, 1, 0, 0, 1, 0, 0, 0),  # recency 3/10
        frequency_recall=(0, 1, 0, 0, 1, 0, 0, 1, 0, 0),  # frequency 3/10
        accrual_points=_RISING,
        transfer_present=_XFER_PRESENT,
        transfer_ablated=_XFER_ABLATED,
        settled=40, distinct_memories=15, clawback_traced=True,
    )
    base.update(kw)
    return KeystoneSpec(**base)


# ── the keystone decision ─────────────────────────────────────────────────────────
def test_keystone_win_requires_beating_recency_AND_frequency():
    win = run_keystone_eval(_spec(), seed=0)
    assert win.beats_recency and win.beats_frequency
    assert win.win is True and win.killed is False and win.inconclusive is False
    # frequency ties utility ⇒ beats_frequency False ⇒ no win (the AND is load-bearing)
    tie = run_keystone_eval(_spec(frequency_recall=(1,) * 10), seed=0)
    assert tie.beats_frequency is False and tie.win is False


def test_keystone_non_saturating_required():
    rising = run_keystone_eval(_spec(accrual_points=_RISING), seed=0)
    assert rising.non_saturating is True and rising.killed is False
    saturating = run_keystone_eval(_spec(accrual_points=_SATURATING), seed=0)
    assert saturating.non_saturating is False and saturating.win is False


def test_keystone_within_family_transfer_required():
    xfer = run_keystone_eval(_spec(), seed=0)
    assert xfer.within_family_transfer is True and xfer.win is True
    no_xfer = run_keystone_eval(
        _spec(transfer_present=_NOXFER_PRESENT, transfer_ablated=_NOXFER_ABLATED), seed=0)
    assert no_xfer.within_family_transfer is False and no_xfer.win is False


def test_keystone_kill_criterion_fires():
    # recency matches utility on every task ⇒ utility does NOT beat recency ⇒ killed
    killed = run_keystone_eval(_spec(recency_recall=(1,) * 10), seed=0)
    assert killed.beats_recency is False
    assert killed.killed is True and killed.win is False
    assert killed.inconclusive is False          # a real negative, not inconclusive


def test_keystone_sparse_credit_is_inconclusive_not_killed():
    # too few settled outcomes ⇒ inconclusive, NEVER killed — even though recency ties.
    sparse = run_keystone_eval(_spec(recency_recall=(1,) * 10, settled=5), seed=0)
    assert sparse.inconclusive is True
    assert sparse.killed is False and sparse.win is False
    # too few distinct credited memories is the other readiness floor
    few = run_keystone_eval(_spec(distinct_memories=3), seed=0)
    assert few.inconclusive is True and few.killed is False


def test_keystone_lift_must_trace_to_clawback():
    # everything else passes, but the lift does NOT trace to an ungameable clawback
    untraced = run_keystone_eval(_spec(clawback_traced=False), seed=0)
    assert untraced.beats_recency and untraced.beats_frequency
    assert untraced.non_saturating and untraced.within_family_transfer
    assert untraced.win is False                 # clawback-trace is required for a win
    assert run_keystone_eval(_spec(clawback_traced=True), seed=0).win is True


# ── the two BUILD-NEW statistics (direct) ─────────────────────────────────────
def test_non_saturating_rising_is_significant():
    flag, (hat, lo, hi), nb = _non_saturating(_RISING, seed=0)
    assert flag is True and lo > 0.0 and nb == 5


def test_non_saturating_plateau_is_not_significant():
    flag, (hat, lo, hi), nb = _non_saturating(_SATURATING, seed=0)
    assert flag is False and lo <= 0.0 and hat > 0.0   # hat>0 but CI straddles 0


def test_non_saturating_under_three_bins_is_inconclusive():
    # x clusters into only 2 of the 5 equal-width bins ⇒ < 3 non-empty bins
    pts = [(0.0, 0.1), (0.001, 0.2), (1.0, 0.5), (1.001, 0.6)]
    flag, _ci, nb = _non_saturating(pts, seed=0)
    assert flag is None and nb < 3                 # inconclusive (None ≠ False)


def test_non_saturating_singleton_bins_are_inconclusive_not_significant():
    # AUDIT HIGH-2: one point per bin ⇒ each bin's bootstrap mean is FROZEN, so the
    # slope CI would collapse to zero width and `lo>0` would degrade into `hat>0` — a
    # fake-significant verdict. Singleton bins (<2 points) are dropped ⇒ < 3 counted
    # ⇒ None (honest inconclusive), NOT a confident rising slope.
    singleton = [(0.0, 0.50), (1.0, 0.5001), (2.0, 0.5002), (3.0, 0.5003), (4.0, 0.5004)]
    flag, _ci, nb = _non_saturating(singleton, n_boot=2000, seed=0)
    assert flag is None and nb < 3
    # a fat noisy middle bin flanked by singleton endpoints is ALSO inconclusive (the
    # endpoints that lever the slope carry no resampling variance)
    mid = [(0.0, 0.1)] + [(2.0 + 0.01 * j, 0.3) for j in range(20)] + [(4.0, 0.9)]
    flag2, _ci2, _nb2 = _non_saturating(mid, n_boot=2000, seed=0)
    assert flag2 is None


def test_within_family_transfer_direction():
    pos, _ci = _within_family_transfer(_XFER_PRESENT, _XFER_ABLATED, seed=0)
    assert pos is True
    # the inverse direction (ablated − present) would NOT be significant here
    neg, _ci2 = _within_family_transfer(_XFER_ABLATED, _XFER_PRESENT, seed=0)
    assert neg is False


def test_within_family_transfer_len_mismatch_raises():
    with pytest.raises(ValueError):
        _within_family_transfer([1, 0, 1], [1, 0], seed=0)


def test_keystone_mismatched_arm_lengths_raise():
    # AUDIT HIGH-1: zip would SILENTLY truncate the longer arm and fabricate a paired
    # comparison out of unpaired tasks — a recency-wins run could come back win=True.
    # Refuse instead. (utility 3 tasks vs recency 10 tasks where recency actually wins.)
    with pytest.raises(ValueError, match=r"equal length|arm"):
        run_keystone_eval(_spec(utility_recall=(1, 1, 1),
                                recency_recall=(0, 0, 0, 1, 1, 1, 1, 1, 1, 1)), seed=0)
    with pytest.raises(ValueError, match=r"frequency"):
        run_keystone_eval(_spec(frequency_recall=(0, 1, 0)), seed=0)
    with pytest.raises(ValueError, match=r"utility_off"):
        run_keystone_eval(_spec(utility_off_recall=(1, 0, 1)), seed=0)


# ── the four-arm ranking seam ─────────────────────────────────────────────────
def test_rank_candidates_modes():
    cands = [(1, 0.9), (2, 0.5)]                   # sim-desc: eid1 better match
    posteriors = {2: 0.9}                          # eid2 credited high
    weight_map = {1: 1.0, 2: 1.0}
    ts_map = {1: 0, 2: 1}                          # eid2 newer
    freq_map = {1: 1, 2: 5}                        # eid2 more frequent
    kw = dict(posteriors=posteriors, ts_map=ts_map, freq_map=freq_map,
              weight_map=weight_map)
    # utility promotes the credited eid2 above the better-matching eid1
    assert _rank_candidates("utility", cands, **kw) == [2, 1]
    # utility_off ablates utility ⇒ weight tie ⇒ sim order (eid1 first) holds
    assert _rank_candidates("utility_off", cands, **kw) == [1, 2]
    assert _rank_candidates("recency", cands, **kw) == [2, 1]
    assert _rank_candidates("frequency", cands, **kw) == [2, 1]


def test_rank_candidates_unknown_mode_raises():
    with pytest.raises(ValueError):
        _rank_candidates("bogus", [(1, 0.5)], posteriors={}, ts_map={},
                         freq_map={}, weight_map={})


def test_keystone_arms_rank_over_hermetic_store():
    # the arm machinery wired to the REAL ports: utility promotes a credited memory
    # over a better-matching uncredited one; utility_off keeps the sim order.
    d = 64
    svc = _EvalService(embedder=HashEmbedder(d=d), d=d, h_frac_max=1.0, beta=16.0,
                       recall_top_n=8)
    e1 = svc.write("alpha bravo charlie delta")    # 3/3 query tokens ⇒ higher sim
    e2 = svc.write("alpha bravo golf hotel")        # 2/3 query tokens ⇒ lower sim
    res = svc.recall("alpha bravo charlie")
    cands = [(h.episode_id, h.sim) for h in res.hits]
    assert {e for e, _ in cands} == {e1, e2}
    kw = dict(posteriors={e2: 0.9}, ts_map={e1: 0, e2: 1}, freq_map={e1: 1, e2: 5},
              weight_map={e1: 1.0, e2: 1.0})
    assert _rank_candidates("utility", cands, **kw)[0] == e2       # credited promoted
    assert _rank_candidates("utility_off", cands, **kw)[0] == e1   # sim order holds
