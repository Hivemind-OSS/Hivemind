"""P1.6 — M08 PredictionBiasMonitor (guardrail-3 / §12 Phase-2 readiness instrument, A6).

The phantom guardrail made real: a PURE domain object (clock injected, no SQL/git)
that measures the mean signed gap between what the ranker PREDICTED (the Beta posterior
mean it would rank by) and what REALITY DELIVERED (the settled reward) over a window.
A large positive divergence ⇒ the ranker over-predicts utility relative to reality —
"the ranker is stale, the codebase moved underneath it."

GROUNDED DEVIATIONS from docs/08-RESOLUTIONS.md (flagged, both reconcile internal
contradictions in the source contract):
  GD-1  The resolution's "wins=9,losses=1 → predicted ≈0.9" is the prior-FREE ratio
        9/10. The deployed predictor is the Beta(1,1) prior-folded UtilityPosterior.mean()
        the ranker actually ranks by (= (1+wins)/(2+wins+losses)). To land predicted at
        EXACTLY 0.9 under that same predictor — so the monitor measures the real ranker,
        not a phantom raw-ratio one — the stale-ranker test credits Beta(9,1) (wins=8,
        losses=0 ⇒ mean = 9/10 = 0.9). The pinned assertion `divergence ≈ 0.9 (±1e-6)`
        is honored literally.
  GD-2  The pinned signature `divergence(family_scope, window_s)` carries no settled
        param, while the prose says "the settled outcomes the producer tick passes in."
        Reconciled: the monitor READS settled outcomes from its injected `store` (the
        single-DB object that co-locates posteriors + task_outcomes) — "passed in" =
        "persisted to the shared store the monitor reads." One method
        `settled_exposures_since(family, since_ts)` is added to the UtilityStore port;
        the divergence signature stays byte-identical.
"""
from __future__ import annotations

from hive.domain.attribution import CreditDelta, PredictionBiasMonitor
from hive.domain.models import UtilityPosterior
from tests.fakes import FakeClock, FakeUtilityStore

FAM = "r|python|bugfix"
OTHER = "r|rust|dep-upgrade"
NOW = 1_000_000
WEEK = 7 * 24 * 3600           # utility.prediction_bias_window_s default (604800)


def _high_mean_store(eid: int = 1) -> FakeUtilityStore:
    """A family whose prior-folded posterior mean is EXACTLY 0.9 (Beta(9,1)) — GD-1.
    Credit 8 wins / 0 losses ⇒ mean = (1+8)/(1+8+1+0) = 9/10 = 0.9."""
    store = FakeUtilityStore()
    store.apply_credit([CreditDelta(eid, FAM, d_wins=8.0, d_losses=0.0)])
    assert abs(store.posterior(eid, FAM).mean() - 0.9) < 1e-12   # the predictor is 0.9
    return store


# ── ★ the pinned guardrail test (A6 / B4e) ───────────────────────────────────────
def test_divergence_flags_stale_ranker():
    """High posterior mean (0.9) but 10 in-window settled outcomes ALL clawed back
    (reward_sign=−1 ⇒ realized=0) ⟹ divergence ≈ 0.9 (±1e-6): a large positive gap
    flags the stale ranker."""
    store = _high_mean_store(eid=1)
    for i in range(10):                      # 10 distinct settled clawbacks in-window
        store.plant_settled(FAM, reward_sign=-1, exposed=(1,), settle_ts=NOW - i)
    mon = PredictionBiasMonitor(store, clock=FakeClock(NOW))
    div = mon.divergence(FAM, WEEK)
    assert abs(div - 0.9) < 1e-6             # predicted 0.9 − realized 0 = 0.9


def test_divergence_empty_window_is_zero():
    """No settled outcomes IN the window ⟹ 0.0 (no confident-empty divergence). The
    outcomes exist but settled BEFORE the window floor ⟹ the window filter excludes
    them, exercising the floor (stronger than an empty store)."""
    store = _high_mean_store(eid=1)
    for i in range(10):                      # all settled 8 days ago — outside a 7-day window
        store.plant_settled(FAM, reward_sign=-1, exposed=(1,), settle_ts=NOW - WEEK - 1 - i)
    mon = PredictionBiasMonitor(store, clock=FakeClock(NOW))
    assert mon.divergence(FAM, WEEK) == 0.0


def test_divergence_truly_empty_store_is_zero():
    """An utterly empty store ⟹ 0.0 (division-by-zero is structurally avoided)."""
    mon = PredictionBiasMonitor(FakeUtilityStore(), clock=FakeClock(NOW))
    assert mon.divergence(FAM, WEEK) == 0.0


def test_divergence_aligned_ranker_near_zero():
    """Predicted rate (0.9) MATCHES the realized rate (9 of 10 settled positive) ⟹
    |divergence| ≈ 0: the ranker tracks reality. 9 pairs gap=(0.9−1)=−0.1, 1 pair
    gap=(0.9−0)=+0.9 ⇒ Σ=0 ⇒ mean 0."""
    store = _high_mean_store(eid=1)
    for i in range(9):
        store.plant_settled(FAM, reward_sign=+1, exposed=(1,), settle_ts=NOW - i)   # realized 1
    store.plant_settled(FAM, reward_sign=-1, exposed=(1,), settle_ts=NOW - 9)        # realized 0
    mon = PredictionBiasMonitor(store, clock=FakeClock(NOW))
    assert abs(mon.divergence(FAM, WEEK)) < 1e-9


# ── belts (ultracode: strengthen beyond the 3 pinned cases) ───────────────────────
def test_divergence_sign_underprediction_is_negative():
    """Low posterior mean but reality keeps delivering positives ⟹ divergence < 0
    (ranker UNDER-predicts). Fresh posterior (mean 0.5) vs 4 settled_pos (realized 1)
    ⟹ each gap = 0.5 − 1 = −0.5 ⟹ divergence = −0.5."""
    store = FakeUtilityStore()              # eid 7 never credited ⇒ mean 0.5
    for i in range(4):
        store.plant_settled(FAM, reward_sign=+1, exposed=(7,), settle_ts=NOW - i)
    mon = PredictionBiasMonitor(store, clock=FakeClock(NOW))
    assert abs(mon.divergence(FAM, WEEK) - (-0.5)) < 1e-9


def test_divergence_averages_over_all_exposed_eids():
    """A single settled outcome that exposed TWO eids contributes BOTH (predicted−realized)
    pairs — divergence is the mean over (outcome, exposed-eid) PAIRS, not over outcomes."""
    store = FakeUtilityStore()
    store.apply_credit([CreditDelta(1, FAM, d_wins=8.0, d_losses=0.0)])   # mean 0.9
    store.apply_credit([CreditDelta(2, FAM, d_wins=0.0, d_losses=0.0)])   # mean 0.5 (fresh)
    store.plant_settled(FAM, reward_sign=-1, exposed=(1, 2), settle_ts=NOW)   # realized 0
    mon = PredictionBiasMonitor(store, clock=FakeClock(NOW))
    # pairs: (0.9−0)=0.9 and (0.5−0)=0.5 ⟹ mean = 0.7
    assert abs(mon.divergence(FAM, WEEK) - 0.7) < 1e-9


def test_divergence_isolates_by_family():
    """Settled outcomes in a DIFFERENT family never bleed into this family's divergence
    (cross-family isolation — the reader filters by family_scope)."""
    store = _high_mean_store(eid=1)
    # noise in OTHER family: high mean, all clawed back — would be 0.9 if it leaked.
    store.apply_credit([CreditDelta(5, OTHER, d_wins=8.0, d_losses=0.0)])
    for i in range(10):
        store.plant_settled(OTHER, reward_sign=-1, exposed=(5,), settle_ts=NOW - i)
    mon = PredictionBiasMonitor(store, clock=FakeClock(NOW))
    assert mon.divergence(FAM, WEEK) == 0.0          # FAM has no settled outcomes → 0.0


def test_divergence_window_boundary_is_inclusive():
    """An outcome settled EXACTLY at the window floor (now − window_s) is INCLUDED;
    one settled a tick earlier is excluded."""
    store = _high_mean_store(eid=1)
    store.plant_settled(FAM, reward_sign=-1, exposed=(1,), settle_ts=NOW - WEEK)        # == floor: in
    store.plant_settled(FAM, reward_sign=-1, exposed=(1,), settle_ts=NOW - WEEK - 1)    # < floor: out
    mon = PredictionBiasMonitor(store, clock=FakeClock(NOW))
    div = mon.divergence(FAM, WEEK)
    assert abs(div - 0.9) < 1e-9                      # exactly one pair counted (the boundary one)


def test_divergence_realized_mapping_settled_pos_is_one():
    """reward_sign>0 maps to realized=1 (not the raw reward magnitude). A perfectly
    calibrated certain ranker (mean→1) over settled_pos ⟹ ~0 gap."""
    store = FakeUtilityStore()
    store.apply_credit([CreditDelta(1, FAM, d_wins=998.0, d_losses=0.0)])   # mean → ~0.999
    store.plant_settled(FAM, reward_sign=+1, exposed=(1,), settle_ts=NOW)
    mon = PredictionBiasMonitor(store, clock=FakeClock(NOW))
    div = mon.divergence(FAM, WEEK)
    assert abs(div - (-0.001)) < 1e-9                 # predicted 0.999 − realized 1 = −0.001


def test_settled_exposure_rejects_zero_sign():
    """The carrier enforces ±1 (a CI-green ~0 event is not a settled credit signal)."""
    import pytest
    from hive.domain.models import SettledExposure
    with pytest.raises(ValueError):
        SettledExposure(family_scope=FAM, reward_sign=0, exposed=(1,), settle_ts=NOW)
