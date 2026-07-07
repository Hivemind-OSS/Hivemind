"""B2: scoring over metrics_ir. Fixtures are hand-worked so each metric is pinned to a known
value, and the abstain-as-miss vs answered-only split (the review's correction) is exercised."""
from __future__ import annotations

import pytest

from hive.research.bench.backends import RecallObs
from hive.research.bench.scoring import (
    paired_delta_ci, score_abstention, score_retrieval,
)


def _obs(ranked, *, conf=1.0, abst=False):
    return RecallObs(query="q", ranked_ids=tuple(ranked),
                     top_score=1.0 if ranked else 0.0, confidence=conf, abstained=abst)


# ── retrieval: hit@k / mrr / coverage / answered-recall on a worked example ─────
def test_score_retrieval_hand_worked():
    # Q1 answered: rank c at 3 of (a,b,c,d,e,f); gold {c,x}. recall@5=1/2, hit@5=1, mrr=1/3.
    q1 = (_obs(["a", "b", "c", "d", "e", "f"]), {"c", "x"})
    # Q2 abstained (gate fired): empty ranked, gold {y} → hit=0, recall=0, mrr=0, NOT answered.
    q2 = (_obs([], abst=True), {"y"})
    s = score_retrieval([q1, q2], ks=(5, 10))

    assert s.n == 2 and s.n_answered == 1
    assert s.coverage == 0.5
    # operating point: abstain counts as a miss → mean over BOTH answerable questions
    assert s.hit_at_k[5] == pytest.approx(0.5)        # (1 + 0) / 2
    assert s.mrr == pytest.approx((1 / 3 + 0.0) / 2)  # (1/3 + 0) / 2
    # quality WHEN answering: only Q1 counts
    assert s.recall_at_k_answered[5] == pytest.approx(0.5)   # 1 of 2 gold retrieved
    # per-question vectors preserve order for paired CI
    assert s.hit_at_k_per_q[5] == (1.0, 0.0)
    assert s.mrr_per_q == pytest.approx((1 / 3, 0.0))


def test_score_retrieval_skips_unanswerable_and_raises_when_none():
    # all-empty-relevant (unanswerable only) ⇒ nothing to score ⇒ refuse, not a fake 0
    with pytest.raises(ValueError):
        score_retrieval([(_obs(["a"]), set()), (_obs([], abst=True), set())])


def test_hit_at_10_catches_a_deeper_gold_that_hit_at_5_misses():
    # gold at rank 7 → hit@5 = 0 but hit@10 = 1 (the k window is honored)
    ranked = [f"r{i}" for i in range(10)]
    q = (_obs(ranked), {"r6"})
    s = score_retrieval([q], ks=(5, 10))
    assert s.hit_at_k[5] == 0.0 and s.hit_at_k[10] == 1.0


# ── abstention AUROC: native confidence separates answerable from unanswerable ──
def test_score_abstention_perfect_and_inverted():
    scored = [
        (_obs(["a"], conf=0.9), False), (_obs(["b"], conf=0.8), False),  # answerable, confident
        (_obs([], conf=0.2, abst=True), True), (_obs([], conf=0.1, abst=True), True),  # unanswerable, unsure
    ]
    assert score_abstention(scored) == pytest.approx(1.0)               # perfect separation
    inverted = [(o, not u) for o, u in scored]
    assert score_abstention(inverted) == pytest.approx(0.0)             # labels flipped


def test_score_abstention_raises_without_both_classes():
    with pytest.raises(ValueError):
        score_abstention([(_obs(["a"], conf=0.9), False)])             # no unanswerable class


# ── paired bootstrap CI: the ship gate ─────────────────────────────────────────
def test_paired_delta_ci_significant_when_a_strictly_better():
    point, lo, hi = paired_delta_ci([1.0, 1.0, 1.0, 1.0], [0.0, 0.0, 0.0, 0.0])
    assert point == pytest.approx(1.0) and lo > 0.0                    # ships (improvement)


def test_paired_delta_ci_not_significant_when_equal():
    point, lo, hi = paired_delta_ci([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
    assert point == pytest.approx(0.0) and lo == 0.0 and hi == 0.0     # CI does not exclude 0


def test_paired_delta_ci_length_mismatch_raises():
    with pytest.raises(ValueError):
        paired_delta_ci([1.0, 2.0], [1.0])
