"""CV5 — gate self-calibration: the replay labeler's temporal restriction, the
CI-only ship rule (a positive point estimate with lo<=0 NEVER recommends), arm
metrics, determinism, and the standing runtime⇸research fence."""
from __future__ import annotations

import numpy as np
import pytest

from hive.research.gate_eval import (
    GateEvalSpec, LabeledMiss, label_misses, run_gate_eval, spec_from_store,
)

D = 8
CURRENT = (0.1, 8.0, 0.0)   # the strict live gate: abstains every fixture field (floor inert)
LOOSE = (0.5, 8.0, 0.0)     # the candidate: serves every fixture field (floor inert)


def _rows(ts: int = 10):
    """Four orthonormal servable rows e1..e4 (ids 1..4), all created at ``ts``."""
    out = []
    for i in range(4):
        v = np.zeros(D, dtype=np.float32)
        v[i] = 1.0
        out.append((i + 1, v, ts))
    return out


def _miss(sims4, ts: int = 50) -> tuple[np.ndarray, int]:
    """A miss vector with EXACT prescribed cosines against the four rows: the
    residual mass goes to a fifth, row-free axis (off-corpus content), so the
    sims pattern is controlled independent of the vector being unit-norm."""
    v = np.zeros(D, dtype=np.float32)
    v[:4] = np.asarray(sims4, dtype=np.float32)
    rest = 1.0 - float(np.dot(v, v))
    assert rest >= 0.0, "sims pattern violates the unit-norm budget"
    v[4] = np.sqrt(rest)
    return v, ts


# fixture fields (beta=8): entropy_norm ≈ 0.14–0.25 — between the two arms'
# thresholds, so CURRENT (0.1) abstains all three and LOOSE (0.5) serves all.
FA1 = _miss([0.85, 0.30, 0.30, 0.30])    # top 0.85 ≥ 0.8 ⇒ false abstain, gold=1
FA2 = _miss([0.82, 0.32, 0.32, 0.32])    # false abstain, gold=1
TA1 = _miss([0.60, 0.15, 0.15, 0.15])    # top 0.60 < 0.8 ⇒ TRUE abstain
FA_MORE = [_miss([0.84, 0.30 + 0.005 * i, 0.30, 0.30]) for i in range(6)]


def _spec(misses, **kw) -> GateEvalSpec:
    return GateEvalSpec(servable=_rows(), misses=misses, current=CURRENT, **kw)


# ── the labeler ────────────────────────────────────────────────────────────────
def test_false_abstain_labeling_from_replay():
    labeled = label_misses(_spec([FA1, TA1]))
    assert [c.false_abstain for c in labeled] == [True, False]
    assert labeled[0].gold_id == 1 and labeled[1].gold_id is None
    assert labeled[0].sims == pytest.approx((0.85, 0.30, 0.30, 0.30), abs=1e-5)


def test_labeler_temporal_restriction_and_skip():
    # a row created AFTER the miss cannot retro-label it: the perfect answer at
    # ts=100 is invisible to a ts=50 miss, whose predating field stays weak
    late_perfect = (9, _miss([0.0] * 4)[0] * 0 + np.eye(D, dtype=np.float32)[4], 100)
    rows = _rows(ts=10) + [late_perfect]
    miss_near_late = _miss([0.1, 0.1, 0.1, 0.1], ts=50)     # ≈ e5 ⇒ sim 1.0 to late row
    labeled = label_misses(GateEvalSpec(servable=rows, misses=[miss_near_late],
                                        current=CURRENT))
    assert len(labeled) == 1 and labeled[0].false_abstain is False
    assert len(labeled[0].sims) == 4                        # the late row never entered
    # a miss predating EVERY row is skipped — the gate never had a field
    early = _miss([0.9, 0.2, 0.2, 0.2], ts=5)
    assert label_misses(_spec([early])) == []


# ── the ship rule ★ ────────────────────────────────────────────────────────────
def test_recommend_only_on_ci_lo_gt_0():
    # the candidate FIXES two false abstains but BREAKS one true abstain:
    # point delta = +1/3 > 0, yet the paired CI straddles 0 ⇒ recommend=False
    noisy = run_gate_eval(_spec([FA1, FA2, TA1]), sweep=[LOOSE],
                          n_boot=2000, seed=0)
    assert noisy.best == LOOSE
    point, lo, _hi = noisy.best_vs_current_ci
    assert point == pytest.approx(1 / 3)
    assert lo <= 0.0 and noisy.recommend is False           # ★ never the point estimate
    # eight clean fixes against the one break ⇒ lo > 0 ⇒ recommend=True
    clear = run_gate_eval(_spec([FA1, FA2, *FA_MORE, TA1]), sweep=[LOOSE],
                          n_boot=2000, seed=0)
    assert clear.best == LOOSE
    assert clear.best_vs_current_ci[1] > 0.0 and clear.recommend is True


def test_best_equals_current_never_recommends():
    # sweeping only worse arms: best stays current; the self-delta CI is 0-width
    worse = (0.001, 8.0, 0.0)                               # abstains even harder
    out = run_gate_eval(_spec([FA1, FA2, *FA_MORE, TA1]), sweep=[worse],
                        n_boot=500, seed=0)
    assert out.best == CURRENT
    assert out.best_vs_current_ci == (0.0, 0.0, 0.0)
    assert out.recommend is False


def test_gate_eval_sweeps_tau_top1():
    # the third sweep dimension is live: a tau_top1=0.99 arm (entropy threshold relaxed to
    # 0.5) floors every fixture's top-1 mass (all in 0.92–0.96 < 0.99) → abstains the FAs it
    # should have served, so it never beats CURRENT. Proves the 3-tuple arm is unpacked and
    # the floor reaches the gate inside the sweep.
    tau_arm = (0.5, 8.0, 0.99)
    out = run_gate_eval(_spec([FA1, FA2, *FA_MORE, TA1]), sweep=[tau_arm],
                        n_boot=500, seed=0)
    assert set(out.arms) == {tau_arm, CURRENT}
    assert out.arms[tau_arm]["false_abstain_rate"] == 1.0   # the floor abstains every FA
    assert out.best == CURRENT                              # the over-flooring arm never wins
    assert out.recommend is False


# ── arm metrics + hygiene ──────────────────────────────────────────────────────
def test_arm_metrics_shapes_and_values():
    out = run_gate_eval(_spec([FA1, FA2, TA1]), sweep=[LOOSE],
                        n_boot=500, seed=0)
    assert set(out.arms) == {LOOSE, CURRENT}
    strict, loose = out.arms[CURRENT], out.arms[LOOSE]
    assert strict["false_abstain_rate"] == 1.0              # abstains every FA
    assert loose["false_abstain_rate"] == 0.0
    assert loose["recall_at_k"] == 1.0                      # the gold row surfaces
    assert strict["correct_rate"] == pytest.approx(1 / 3)
    assert loose["correct_rate"] == pytest.approx(2 / 3)
    # the confidence proxy separates: FA fields are more peaked than the TA one
    assert strict["auroc"] == 1.0 and loose["auroc"] == 1.0


def test_deterministic_and_degenerate_refused():
    a = run_gate_eval(_spec([FA1, FA2, TA1]), sweep=[LOOSE], n_boot=500, seed=0)
    b = run_gate_eval(_spec([FA1, FA2, TA1]), sweep=[LOOSE], n_boot=500, seed=0)
    assert a == b                                           # fixed-seed determinism
    with pytest.raises(ValueError):
        run_gate_eval(_spec([]), sweep=[LOOSE])             # nothing labelable
    with pytest.raises(ValueError):
        run_gate_eval(_spec([FA1, FA2]), sweep=[LOOSE])     # one-class: refused


def test_spec_from_store_reads_real_rows():
    from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
    from hive.adapters.sqlite_db import connect
    from hive.adapters.store_sqlite import SqliteEpisodeStore
    s = SqliteEpisodeStore(connect(":memory:"), index=ExhaustiveCosineIndex(D))
    vec = np.eye(D, dtype=np.float32)[0]
    eid, _ = s.stage(text="row", weight=1.0, source="t", tags="",
                     proposed_by="w", ts=10)
    s.complete(eid, vec, expected_version=0, trust="established",
               approver="h", approved_ts=10, last_active_ts=10)
    s.record_miss("q", vec.tobytes(), "a", "abstained", ts=50)
    spec = spec_from_store(s, now=100, provisional_ttl_s=10**6,
                           current=CURRENT, window_s=10**6)
    assert [(e, ts) for e, _v, ts in spec.servable] == [(eid, 10)]
    assert len(spec.misses) == 1 and spec.misses[0][1] == 50
    labeled = label_misses(spec)
    assert labeled and labeled[0].false_abstain is True     # the row predated it


def test_runtime_never_imports_research():
    # the standing AST fence (tests/test_purity.py) automatically covers the
    # new module — pinned here as the plan's named test row
    from tests.test_purity import test_research_not_imported_by_runtime
    test_research_not_imported_by_runtime()
