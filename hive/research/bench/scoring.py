"""Scoring — a thin, honest layer over ``hive.research.metrics_ir``.

The review's correction is baked in here: a raw ``recall@k`` over all answerable questions
conflates *gate aggressiveness* (Hivemind abstains; mem0 never does) with *retrieval quality*.
So the HEADLINE triple separates them:

  - ``hit_at_k`` / ``mrr`` — over ALL answerable questions, where an abstain (empty ranked_ids)
    scores 0. This is the honest OPERATING POINT: a false abstention is a real cost and is
    counted as one. ``hit@k`` (any gold-source memory in the top-k) is also granularity-robust
    across backends, unlike ``recall@k`` whose denominator shifts with ingestion unit.
  - ``coverage`` — the answer-rate (fraction NOT abstained), so the abstain cost is visible.
  - ``recall_at_k_answered`` — recall@k conditioned on the backend actually answering: retrieval
    quality WHEN it commits to an answer. SECONDARY, never the headline.

``score_abstention`` reports each system's native abstention-signal separability (AUROC of
confidence vs the unanswerable label) — NOT a calibration claim (the confidence proxies differ
by backend, disclosed in ``RecallObs``). ``paired_delta_ci`` is the ship gate: a per-question
paired bootstrap CI; an arm difference ships only when the CI excludes 0.

Pure + deterministic (fixed bootstrap seed). Degenerate inputs RAISE via ``metrics_ir`` — an
untestable number is refused, never masked.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from hive.research.bench.backends import RecallObs
from hive.research.metrics_ir import (
    abstention_auroc, bootstrap_ci, mrr as _mrr, recall_at_k,
)


@dataclass(frozen=True)
class RetrievalScores:
    """Per-arm retrieval result. Aggregate means for reporting + per-question vectors so a
    second arm's vectors can be diffed under ``paired_delta_ci`` (same question order by
    construction). ``hit_at_k``/``mrr`` are over all ``n`` answerable questions (abstain → 0);
    ``recall_at_k_answered`` is over the ``n_answered`` non-abstained subset only."""
    ks: tuple[int, ...]
    n: int                                      # answerable questions scored
    n_answered: int                             # of those, how many did NOT abstain
    coverage: float                             # n_answered / n
    hit_at_k: dict[int, float]                  # mean, abstain counted as a miss
    mrr: float                                  # mean, abstain counted as 0
    recall_at_k_answered: dict[int, float]      # mean over answered-only (secondary)
    hit_at_k_per_q: dict[int, tuple[float, ...]]  # aligned per-question, for paired CI
    mrr_per_q: tuple[float, ...]


def _hit_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """1.0 iff any relevant id appears in the top-k (granularity-robust success@k). Reuses the
    one IR recall primitive so the dedup-before-truncate window is identical."""
    return 1.0 if recall_at_k(ranked, relevant, k) > 0.0 else 0.0


def score_retrieval(scored: Sequence[tuple[RecallObs, set[str]]], *,
                    ks: tuple[int, ...] = (5, 10)) -> RetrievalScores:
    """Score answerable questions. ``scored`` pairs each observation with its gold-relevant
    memory-id set (the harness builds it from ``source_id → session`` + the question's evidence).
    Questions with EMPTY relevant (unanswerable / no evidence) are skipped here — they belong to
    ``score_abstention``. An abstained observation has empty ``ranked_ids`` ⇒ hit/recall/MRR 0,
    so a false abstention is correctly penalized in the operating-point means."""
    answerable = [(obs, rel) for obs, rel in scored if rel]
    n = len(answerable)
    if n == 0:
        raise ValueError("score_retrieval needs ≥1 answerable question (non-empty relevant)")
    hit_per_q: dict[int, list[float]] = {k: [] for k in ks}
    mrr_per_q: list[float] = []
    recall_answered: dict[int, list[float]] = {k: [] for k in ks}
    n_answered = 0
    for obs, rel in answerable:
        ranked = obs.ranked_ids
        for k in ks:
            hit_per_q[k].append(_hit_at_k(ranked, rel, k))
        mrr_per_q.append(_mrr(ranked, rel))
        if not obs.abstained:
            n_answered += 1
            for k in ks:
                recall_answered[k].append(recall_at_k(ranked, rel, k))

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return RetrievalScores(
        ks=tuple(ks), n=n, n_answered=n_answered, coverage=n_answered / n,
        hit_at_k={k: _mean(hit_per_q[k]) for k in ks},
        mrr=_mean(mrr_per_q),
        recall_at_k_answered={k: _mean(recall_answered[k]) for k in ks},
        hit_at_k_per_q={k: tuple(hit_per_q[k]) for k in ks},
        mrr_per_q=tuple(mrr_per_q))


def score_abstention(scored: Sequence[tuple[RecallObs, bool]]) -> float:
    """AUROC of each system's native confidence at separating ANSWERABLE from UNANSWERABLE
    questions. ``scored`` pairs each observation with ``is_unanswerable`` (the event the system
    SHOULD abstain on). Higher confidence = more committed ⇒ a well-behaved system gives
    unanswerable questions lower confidence ⇒ AUROC → 1. RAISES (via ``abstention_auroc``) if a
    class is missing — the metric is undefined, never masked."""
    confidences = [obs.confidence for obs, _ in scored]
    is_miss = [bool(un) for _, un in scored]
    return abstention_auroc(confidences, is_miss)


def paired_delta_ci(a: Sequence[float], b: Sequence[float], *,
                    seed: int = 0) -> tuple[float, float, float]:
    """The ship gate: percentile bootstrap CI on the per-question delta ``a - b`` (same question
    order in both arms by construction). Returns ``(point, lo, hi)``; an improvement ships iff
    ``lo > 0`` (a regression iff ``hi < 0``). RAISES on a length mismatch or empty input."""
    if len(a) != len(b):
        raise ValueError(f"paired_delta_ci length mismatch: {len(a)} != {len(b)}")
    deltas = [float(ai) - float(bi) for ai, bi in zip(a, b)]
    return bootstrap_ci(deltas, seed=seed)
