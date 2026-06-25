"""S3 — the Pareto scorers + the one paired-CI ship gate (PURE, fixture-tested).

Every scorer is graded against the EXTERNAL gold (the substrate's labeled regimes, confirmed
by executed-test task outcomes) — NEVER the store's own deprecation verdict. The circularity
firewall is structural: ``scoring`` imports ``metrics_ir`` only and must NOT import the
diagnostic carrier module (``diagnostics``), so the gold scorer cannot read the store's "is it
retired" verdict even by accident. A test pins that non-import.

The Pareto set is never collapsed to a scalar (raw store-size is gameable by pruning
everything); the headline is the conjunction ``clean_win`` over Δ(ON−OFF) CIs, each axis read
with its own direction.
"""
from __future__ import annotations

import ast
import math
import pathlib

import pytest

from hive.research.selfmaint import scoring
from hive.research.selfmaint.scoring import (
    bad_serve_rate, clean_win, density_curve, paired_delta_ci, prune_precision,
    prune_recall, task_success, verdict,
)


# ── density: retained-valuable ÷ total-servable, per step ────────────────────────────
def test_density_curve_is_valuable_over_total_per_step():
    servable = [{"v1"}, {"v1", "j1"}, {"v1", "j1", "j2"}, {"v1"}]
    out = density_curve(servable, {"v1", "v2"})
    assert out[0] == 1.0
    assert out[1] == 0.5
    assert math.isclose(out[2], 1 / 3)
    assert out[3] == 1.0


def test_density_curve_empty_step_is_zero_not_div0():
    # an empty store retains no value ⇒ 0.0 (documented), never a 0/0 crash.
    assert density_curve([set()], {"v1"}) == [0.0]


# ── bad-serve rate: fraction of TASKS that served a labeled-bad fact ─────────────────
def test_bad_serve_rate_counts_tasks_with_a_bad_hit():
    served = [{"v1"}, {"b1", "v1"}, set(), {"b2"}]
    assert bad_serve_rate(served, {"b1", "b2"}) == 0.5      # tasks 1 and 3 served a bad fact


def test_bad_serve_rate_zero_when_no_bad_served():
    assert bad_serve_rate([{"v1"}, {"v2"}], {"b1"}) == 0.0


def test_bad_serve_rate_empty_tasks_raises():
    with pytest.raises(ValueError):
        bad_serve_rate([], {"b1"})


# ── prune recall: retired ∩ externally-confirmed-bad ÷ confirmed-bad ─────────────────
def test_prune_recall_is_over_externally_confirmed_bad():
    # b3 is labeled-bad but NOT task-confirmed (never exercised) ⇒ excluded from the denominator;
    # recall is measured only over facts the executed tests proved harmful.
    out = prune_recall({"b1"}, {"b1", "b2", "b3"}, task_gold={"b1", "b2"})
    assert out == 0.5                                       # 1 of 2 confirmed-bad retired


def test_prune_recall_raises_when_nothing_externally_confirmed():
    with pytest.raises(ValueError):
        prune_recall({"b1"}, {"b1"}, task_gold=set())


# ── prune precision + false-prune (over-prune harm) ─────────────────────────────────
def test_prune_precision_and_false_prune_rate():
    # retired b1 (confirmed-bad ⇒ justified) and n1 (a keep ⇒ over-prune harm).
    precision, false_prune = prune_precision(
        {"b1", "n1"}, {"n1", "v1"}, task_gold={"b1"})
    assert precision == 0.5                                 # 1 of 2 retirements was justified
    assert false_prune == 0.5                               # 1 of 2 keep memories wrongly retired


def test_no_retirements_is_vacuously_precise_zero_false_prune():
    precision, false_prune = prune_precision(set(), {"n1", "v1"}, task_gold={"b1"})
    assert precision == 1.0 and false_prune == 0.0


def test_prune_everything_tanks_precision_and_false_prune():
    # the anti-gaming probe: retiring EVERYTHING maximises recall but tanks precision (most
    # retirements were not justified) and maxes false-prune (every keep is wrongly retired).
    retired = {"b1", "n1", "n2", "v1"}
    precision, false_prune = prune_precision(retired, {"n1", "n2", "v1"}, task_gold={"b1"})
    assert precision == 0.25                                # only b1 of 4 retirements justified
    assert false_prune == 1.0                              # all 3 keep memories wrongly retired


def test_prune_precision_empty_keep_raises():
    with pytest.raises(ValueError):
        prune_precision({"b1"}, set(), task_gold={"b1"})


# ── task success vector (the external executed-test gold) ────────────────────────────
def test_task_success_coerces_to_bools():
    assert task_success([True, False, True]) == [True, False, True]


def test_task_success_empty_raises():
    # AC7: the report refuses to emit on a missing success vector — the empty case is loud.
    with pytest.raises(ValueError):
        task_success([])


# ── the one ship gate: paired bootstrap CI on Δ(ON − OFF) ────────────────────────────
def test_paired_delta_ci_point_is_mean_delta():
    point, lo, hi = paired_delta_ci([1, 1, 1, 0], [0, 0, 0, 0], seed=0)
    assert math.isclose(point, 0.75)
    assert lo <= point <= hi


def test_paired_delta_ci_length_mismatch_raises():
    with pytest.raises(ValueError):
        paired_delta_ci([1, 0], [0], seed=0)


# ── verdict direction + the clean_win conjunction ───────────────────────────────────
def test_verdict_respects_direction():
    assert verdict((0.3, 0.1, 0.5), higher_better=True) == "improve"     # lo>0
    assert verdict((-0.3, -0.5, -0.1), higher_better=True) == "regress"  # hi<0
    assert verdict((0.0, -0.1, 0.1), higher_better=True) == "inconclusive"
    # a lower-is-better axis (bad-serve): improvement is Δ<0 (hi<0)
    assert verdict((-0.3, -0.5, -0.1), higher_better=False) == "improve"
    assert verdict((0.3, 0.1, 0.5), higher_better=False) == "regress"


def _clean_inputs():
    return dict(
        density_ci=(0.3, 0.1, 0.5),          # improve (higher better)
        bad_serve_ci=(-0.4, -0.6, -0.2),     # improve (lower better)
        prune_recall_ci=(0.5, 0.2, 0.8),     # improve
        precision_ci=(0.0, -0.1, 0.1),       # inconclusive — not a regression
        success_ci=(0.05, -0.02, 0.12))      # inconclusive — not a regression


def test_clean_win_true_when_all_axes_pass():
    v = clean_win(**_clean_inputs())
    assert v["clean_win"] is True
    assert v["density"] == "improve" and v["bad_serve"] == "improve"


def test_clean_win_false_on_success_regression():
    bad = _clean_inputs()
    bad["success_ci"] = (-0.3, -0.5, -0.1)                  # a real success regression
    v = clean_win(**bad)
    assert v["clean_win"] is False and v["success"] == "regress"


def test_clean_win_false_when_density_inconclusive():
    bad = _clean_inputs()
    bad["density_ci"] = (0.1, -0.1, 0.3)                    # density Δ CI includes 0
    assert clean_win(**bad)["clean_win"] is False


# ── the structural circularity firewall (§5 / AC6) ──────────────────────────────────
def test_scoring_does_not_import_the_diagnostic_carrier():
    # the gold scorer must be UNABLE to read the store's own "is it retired/served" verdict —
    # enforced structurally: scoring.py imports neither `diagnostics` nor any store/daemon carrier.
    src = pathlib.Path(scoring.__file__).read_text()
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            # also the bound NAMES — `from hive.research.selfmaint import diagnostics` binds
            # `diagnostics` while node.module is only the package, so the name must be checked too.
            for alias in node.names:
                imported.add(alias.name)
                imported.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    forbidden = {m for m in imported
                 if "diagnostics" in m or m.endswith(".daemon") or "store" in m}
    assert not forbidden, f"scoring must not import a store-verdict carrier: {forbidden}"
