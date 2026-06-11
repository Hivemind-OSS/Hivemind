"""channel_eval — the dense-vs-hybrid arm comparison whose CI alone can justify
flipping recall.hybrid.

The corpus is engineered so one query is a dense-miss / lexical-hit (an exact
error identifier buried under token-overlap distractors) and the rest are exact
ties — so the per-query delta vector is [1, 0, 0, 0]: a POSITIVE point estimate
whose bootstrap lo is 0. The ship rule (lo > 0, AC5) must refuse it; a
point-estimate rule would ship it.
"""
from __future__ import annotations

import pytest

from hive.domain.models import content_hash
from hive.research.channel_eval import ChannelEvalResult, run_channel_eval

GOLD = "ERR_FLUX_077 thermal runaway in coolant loop"
DISTRACTORS = [
    f"how to fix the failing stage in the pipeline build number {w}"
    for w in ("one", "two", "three", "four", "five", "six")
]
CORPUS = [GOLD] + DISTRACTORS
# shares ONE rare identifier with the gold and six common tokens with every
# distractor ⇒ dense ranks the gold below top-5; BM25's IDF puts it lexical #1
WIN_Q = "how to fix ERR_FLUX_077 in the pipeline"

# one hybrid win + three exact ties (query == a distractor's own text)
MIXED = [(WIN_Q, {content_hash(GOLD)})] + [
    (d, {content_hash(d)}) for d in DISTRACTORS[:3]
]


def _run(labeled, **kw):
    kw.setdefault("k", 5)
    kw.setdefault("n_boot", 2000)
    kw.setdefault("seed", 0)
    kw.setdefault("store_fixture", CORPUS)
    return run_channel_eval(labeled, **kw)


# ── AC5: recommend ONLY on CI lo > 0, never on a bare positive point ──────────
def test_recommend_only_on_ci_lo_gt_0():
    r = _run(MIXED)
    point, lo, _hi = r.hybrid_vs_dense_ci
    assert point > 0.0                      # the bare point estimate IS positive…
    assert lo <= 0.0                        # …but the CI does not exclude zero…
    assert r.recommend_hybrid is False      # …so the instrument must refuse (AC5)


def test_recommends_on_uniformly_positive_delta():
    # positive control: every query is a hybrid win ⇒ lo > 0 ⇒ recommend fires
    r = _run([(WIN_Q, {content_hash(GOLD)})] * 4)
    assert r.hybrid_vs_dense_ci[1] > 0.0
    assert r.recommend_hybrid is True


# ── the arms report the channel difference the deltas are built from ──────────
def test_arms_report_hybrid_lift():
    r = _run(MIXED)
    assert isinstance(r, ChannelEvalResult)
    assert set(r.arms) == {"dense", "hybrid"}
    # the dense arm misses the buried gold; the hybrid arm surfaces it
    assert r.arms["hybrid"]["recall@k"] > r.arms["dense"]["recall@k"]
    for arm in r.arms.values():
        assert 0.0 <= arm["recall@k"] <= 1.0
        assert 0.0 <= arm["ndcg@k"] <= 1.0


def test_deterministic_for_fixed_seed():
    a, b = _run(MIXED), _run(MIXED)
    assert a.hybrid_vs_dense_ci == b.hybrid_vs_dense_ci
    assert a.arms == b.arms and a.recommend_hybrid == b.recommend_hybrid


def test_empty_labeled_refused():
    # zero signal is refused, not masked (bootstrap_ci raises on empty deltas)
    with pytest.raises(ValueError):
        _run([])
