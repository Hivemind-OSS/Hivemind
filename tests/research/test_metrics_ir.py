"""P1.10 — M10 pure metrics (PORT) + BUILD-NEW significance (abstention_auroc,
bootstrap_ci). Store-free, hash-speed.

Locked assertions: docs/05-BUILD-PLAN.md §P1.10(a) and docs/03-modules/M10-eval.md §2.2.
"""
from __future__ import annotations

import pytest

from hive.research.metrics_ir import (
    abstention_auroc, bootstrap_ci, jaccard_at_k, mrr, ndcg_at_k,
    precision_at_k, recall_at_k,
)


# ── pure IR metrics (ported contracts) ────────────────────────────────────────
def test_recall_at_k_basic():
    assert recall_at_k(["a", "b", "c"], {"a", "z"}, 5) == pytest.approx(0.5)
    assert recall_at_k(["a", "b", "c"], {"a", "b"}, 2) == pytest.approx(1.0)
    assert recall_at_k(["x", "y"], {"a"}, 5) == 0.0


def test_precision_at_k_denominator_is_k():
    # only one retrieved, k=5 ⇒ short list penalized (denominator is k, not len)
    assert precision_at_k(["a"], {"a"}, 5) == pytest.approx(0.2)
    assert precision_at_k(["a", "b"], {"a", "b"}, 2) == pytest.approx(1.0)


def test_metrics_reject_nonpositive_k():
    for fn in (precision_at_k, recall_at_k, ndcg_at_k):
        with pytest.raises(ValueError):
            fn(["a"], {"a"}, 0)
        with pytest.raises(ValueError):
            fn(["a"], {"a"}, -1)
    with pytest.raises(ValueError):
        jaccard_at_k(["a"], ["a"], 0)


def test_metrics_empty_relevant_is_zero():
    assert recall_at_k(["a", "b"], set(), 5) == 0.0
    assert precision_at_k(["a", "b"], set(), 5) == 0.0
    assert ndcg_at_k(["a", "b"], set(), 5) == 0.0
    assert mrr(["a", "b"], set()) == 0.0


def test_dedup_before_truncate():
    # a leading duplicate must NOT push the distinct relevant item out of top-2
    # naive truncate-then-dedup of ["a","a","b"] @2 → {"a"} (loses b); correct → {"a","b"}
    assert recall_at_k(["a", "a", "b"], {"a", "b"}, 2) == pytest.approx(1.0)
    assert jaccard_at_k(["a", "a", "b"], ["a", "b"], 2) == pytest.approx(1.0)


def test_mrr_first_relevant_rank():
    assert mrr(["x", "y", "a"], {"a"}) == pytest.approx(1.0 / 3.0)
    assert mrr(["a"], {"a"}) == pytest.approx(1.0)
    assert mrr(["x", "y"], {"a"}) == 0.0


def test_ndcg_monotone_in_rank():
    top = ndcg_at_k(["a", "x", "y"], {"a"}, 3)
    low = ndcg_at_k(["x", "y", "a"], {"a"}, 3)
    assert top == pytest.approx(1.0)         # gold at rank 1 ⇒ perfect
    assert 0.0 < low < top                   # gold deeper ⇒ discounted


def test_jaccard_both_empty_is_one():
    assert jaccard_at_k([], [], 5) == 1.0
    assert jaccard_at_k(["a"], [], 5) == 0.0


# ── BUILD-NEW: abstention_auroc ───────────────────────────────────────────────
def test_auroc_perfect_separation_is_one():
    # hits get HIGH confidence, misses LOW ⇒ AUROC == 1.0
    scores = [0.9, 0.8, 0.2, 0.1]
    is_miss = [False, False, True, True]
    assert abstention_auroc(scores, is_miss) == pytest.approx(1.0)


def test_auroc_inverted_is_zero():
    # misses get HIGHER confidence than hits ⇒ perfectly inverted ⇒ 0.0
    scores = [0.1, 0.2, 0.8, 0.9]
    is_miss = [False, False, True, True]
    assert abstention_auroc(scores, is_miss) == pytest.approx(0.0)


def test_auroc_ties_are_half():
    # all-equal scores ⇒ no separating power ⇒ 0.5 (ties resolved at 0.5)
    scores = [0.5, 0.5, 0.5, 0.5]
    is_miss = [False, True, False, True]
    assert abstention_auroc(scores, is_miss) == pytest.approx(0.5)


def test_auroc_degenerate_raises():
    with pytest.raises(ValueError):
        abstention_auroc([0.1, 0.2], [False, False])   # all hits
    with pytest.raises(ValueError):
        abstention_auroc([0.1, 0.2], [True, True])     # all misses


def test_auroc_len_mismatch_raises():
    with pytest.raises(ValueError):
        abstention_auroc([0.1, 0.2, 0.3], [True, False])


def test_auroc_nan_score_raises():
    # AUDIT MED-5: a NaN score breaks the sort + tie-detection and would return a
    # clean WRONG float (brute-force AUROC differs); NaN is undefined ⇒ refuse, never
    # mask. ±inf is orderable and must STILL work (use isnan, not `not isfinite`).
    nan = float("nan")
    with pytest.raises(ValueError):
        abstention_auroc([5.0, nan], [True, False])        # NaN in a hit position
    with pytest.raises(ValueError):
        abstention_auroc([1.0, nan, 2.0], [False, True, False])  # NaN in a miss position
    # a legitimate +inf confidence still scores (inf is orderable)
    assert abstention_auroc([float("inf"), 0.1], [False, True]) == pytest.approx(1.0)


def test_auroc_target_band_on_fixture():
    # a realistic (imperfect) confidence split lands AUROC in the §6.1 #2 band.
    # 6 hits + 4 misses that overlap heavily in the middle ⇒ 18/24 concordant = 0.75.
    scores = [0.92, 0.85, 0.80, 0.74, 0.60, 0.40,   # hits (a low straggler at 0.40)
              0.78, 0.70, 0.55, 0.35]               # misses (high stragglers 0.78/0.70)
    is_miss = [False, False, False, False, False, False,
               True, True, True, True]
    auroc = abstention_auroc(scores, is_miss)
    assert 0.70 <= auroc <= 0.84


# ── BUILD-NEW: bootstrap_ci ───────────────────────────────────────────────────
def test_bootstrap_ci_significant_improvement():
    # a clearly-positive paired delta ⇒ lo > 0 (CI-significant improvement)
    deltas = [0.3, 0.25, 0.4, 0.35, 0.28, 0.31, 0.27, 0.33]
    point, lo, hi = bootstrap_ci(deltas, seed=0)
    assert point > 0 and lo > 0


def test_bootstrap_ci_noise_not_significant():
    # symmetric noise around zero ⇒ CI straddles 0 (neither lo>0 nor hi<0)
    deltas = [0.2, -0.2, 0.1, -0.1, 0.15, -0.15, 0.05, -0.05]
    point, lo, hi = bootstrap_ci(deltas, seed=0)
    assert lo <= 0.0 <= hi          # not CI-significant in either direction


def test_bootstrap_ci_deterministic_seed():
    deltas = [0.1, -0.05, 0.2, 0.0, 0.15, -0.1, 0.3, 0.05]
    assert bootstrap_ci(deltas, seed=7) == bootstrap_ci(deltas, seed=7)
    # a different seed perturbs the resample (very likely a different interval)
    assert bootstrap_ci(deltas, seed=7) != bootstrap_ci(deltas, seed=8)


def test_bootstrap_ci_empty_raises():
    with pytest.raises(ValueError):
        bootstrap_ci([])
