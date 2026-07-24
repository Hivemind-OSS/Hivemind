"""Information-retrieval & selective-prediction evaluation metrics.

General-purpose scorers for evaluating any retrieval / ranking system and any system
that can abstain ("answer or not"):

- ``recall_at_k`` — retrieval quality: of the items that SHOULD be retrieved, how many
  land in the top-k distinct ranks.
- ``mrr`` — rank quality: reciprocal rank of the first relevant item (order matters).
- ``abstention_auroc`` — abstention-signal quality: does a confidence score separate the
  queries that have a real answer from the ones a system should decline, threshold-free.
- ``bootstrap_ci`` — the ship gate for paired A/B deltas: a percentile bootstrap CI on a
  per-query delta vector; a difference is CI-significant iff the interval excludes 0.

Design rule carried over verbatim from where these were first written: degenerate inputs
(k<=0, empty relevant set, single-class AUROC, NaN scores, length mismatch, empty delta
vector) RAISE rather than return a defined-looking default — an untestable number is
refused, never masked, so a caller bug surfaces instead of hiding behind a plausible
0.0/0.5. The IR/AUROC scorers are stdlib-only; ``bootstrap_ci`` needs numpy.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np


# ── pure IR metrics ───────────────────────────────────────────────────────────


def _dedup(retrieved: Sequence[str]) -> list[str]:
    """Order-preserving de-duplication of the FULL ranked list. Each id is kept
    at its first (best) rank. // O(R) time/space."""
    seen: set[str] = set()
    out: list[str] = []
    for item in retrieved:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _topk_unique(retrieved: Sequence[str], k: int) -> list[str]:
    """The first ``k`` *distinct* ranks: de-duplicate the whole ranked list, THEN
    cut at k (dedup-before-truncate, standard IR). A duplicate at rank i therefore
    never pushes a distinct relevant item out of the top-k window. // O(R)."""
    return _dedup(retrieved)[:k]


def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of all relevant items in the top-``k`` ranks. Denominator is
    ``len(relevant)``. Empty ``relevant`` → 0.0 (undefined; caller should skip).
    // O(k) time, O(R) space."""
    if k <= 0:
        raise ValueError(f"recall_at_k requires k > 0, got {k}")
    if not relevant:
        return 0.0
    hits = sum(1 for item in _topk_unique(retrieved, k) if item in relevant)
    return hits / len(relevant)


def mrr(retrieved: Sequence[str], relevant: set[str]) -> float:
    """Reciprocal rank of the *first* relevant item (1-indexed); 0.0 if none are
    relevant. Scans the full ranked list (no cutoff). // O(R) time."""
    if not relevant:
        return 0.0
    seen: set[str] = set()
    rank = 0
    for item in retrieved:
        if item in seen:
            continue
        seen.add(item)
        rank += 1
        if item in relevant:
            return 1.0 / rank
    return 0.0


# ── selective-prediction (abstention) metric ──────────────────────────────────


def abstention_auroc(scores: Sequence[float], is_miss: Sequence[bool]) -> float:
    """AUROC of the abstention confidence at separating top-k HITS from MISSES,
    via the Mann-Whitney rank-sum identity = P(score(hit) > score(miss)) with
    ties → 0.5. Maps the honest-abstention gate (target ≈0.77).

    ``scores`` is the gate's confidence proxy where HIGHER = more confident = LESS
    likely to abstain (the natural choice is ``1 - H/ln(N_eff)``). ``is_miss[i]``
    is the event the gate *should* abstain on (gold not in top-k). A well-calibrated
    gate gives misses LOWER confidence than hits ⇒ AUROC → 1.0; a perfectly
    inverted gate ⇒ 0.0; no separation ⇒ 0.5.

    **Degenerate inputs RAISE** ``ValueError`` (all-hit, all-miss, or length
    mismatch) — a 0.5 default would mask an untestable metric, the exact
    ERROR_MASKING class this module forbids. // O(n log n) time (average-rank sort),
    O(n) space.
    """
    n = len(scores)
    if n != len(is_miss):
        raise ValueError(
            f"abstention_auroc length mismatch: len(scores)={n} != "
            f"len(is_miss)={len(is_miss)}"
        )
    # A NaN score is the ONE input class where the rank-sum diverges from the
    # brute-force P(hit>miss): every comparison against NaN is False, so the sort
    # mis-places it and the `==` tie test never groups it — it would be assigned a
    # finite rank and return a clean, WRONG float. NaN is undefined ⇒ refuse, never
    # mask (use isnan, NOT `not isfinite`, so legitimate ±inf scores still work —
    # inf is orderable and produces the correct AUROC).
    if any(math.isnan(s) for s in scores):
        raise ValueError(
            "abstention_auroc requires non-NaN scores; NaN is undefined "
            "(refused, not masked)"
        )
    n_miss = sum(1 for m in is_miss if m)
    n_hit = n - n_miss
    if n_hit == 0 or n_miss == 0:
        raise ValueError(
            f"abstention_auroc is undefined without BOTH classes: "
            f"hits={n_hit}, misses={n_miss} (degenerate — refused, not masked)"
        )
    # Average ranks of the scores in ascending order (proper Mann-Whitney tie
    # handling). 1-indexed; tied scores share the mean of their rank block.
    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for t in range(i, j + 1):
            ranks[order[t]] = avg_rank
        i = j + 1
    sum_ranks_hit = math.fsum(ranks[idx] for idx in range(n) if not is_miss[idx])
    # U_hit counts (hit > miss) pairs (+0.5 per tie); AUROC = U_hit / (n_hit·n_miss).
    u_hit = sum_ranks_hit - n_hit * (n_hit + 1) / 2.0
    return u_hit / (n_hit * n_miss)


# ── paired-delta significance (the ship gate) ─────────────────────────────────


def bootstrap_ci(
    deltas: Sequence[float],
    *,
    n_boot: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Percentile bootstrap CI on a paired per-query delta vector. Returns
    ``(point, lo, hi)`` where ``point`` is the observed mean and ``[lo, hi]`` is
    the central ``1-alpha`` percentile interval of the bootstrap means.

    The ship rule reads this as **CI-significant iff ``lo > 0`` (improvement)
    or ``hi < 0`` (regression)** — a change ships only on a CI-significant delta,
    never a bare point estimate. **Deterministic** for a fixed ``seed``; **empty
    ``deltas`` RAISE** ``ValueError`` (an undefined CI is refused, not masked).
    // O(n_boot · n) time, O(n_boot + n) space.
    """
    arr = np.asarray(list(deltas), dtype=np.float64)
    if arr.size == 0:
        raise ValueError("bootstrap_ci requires ≥1 delta (empty is undefined)")
    if not (0.0 < alpha < 1.0):
        raise ValueError(f"alpha must be in (0,1), got {alpha}")
    rng = np.random.default_rng(seed)
    # resample indices with replacement: (n_boot, n) → per-resample means
    idx = rng.integers(0, arr.size, size=(int(n_boot), arr.size))
    boot_means = arr[idx].mean(axis=1)
    lo = float(np.percentile(boot_means, 100.0 * (alpha / 2.0)))
    hi = float(np.percentile(boot_means, 100.0 * (1.0 - alpha / 2.0)))
    return (float(arr.mean()), lo, hi)
