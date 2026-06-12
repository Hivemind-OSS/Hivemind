"""M10 — pure information-retrieval metrics + BUILD-NEW significance machinery.

The load-bearing arithmetic of the eval membrane. The six metric functions are a
verbatim PORT of the reference ``research/metrics_ir.py`` (contracts already
correct: dedup-before-truncate, ``k<=0`` raises, empty-relevant→0.0). The two
significance helpers (``abstention_auroc``, ``bootstrap_ci``) are BUILD-NEW — the
honest-abstention AUROC gate and the CI-significance ship rule have NO scorer anywhere
in the reference tree (grep-verified). Dev-time only — the runtime never imports
``hive.research`` (P0.0 AST fence).

Conventions (made explicit, not silently degenerate — this is precisely the
ERROR_MASKING failure class the bug registry warns about: a metric that always
returns a defined-looking number even when its inputs are nonsense hides bugs):

- ``retrieved`` is rank-ordered, best first. The full ranked list is
  order-preserving-**deduplicated first, then cut at k** (standard IR): a leading
  duplicate never displaces a later distinct relevant item out of the top-k window.
- ``k <= 0`` is a programming error and **raises** ``ValueError`` (a 0.0 here would
  mask a caller bug — the lesson of BUG-001/002/003).
- ``recall_at_k`` over an empty ``relevant`` set returns 0.0 and the harness skips
  such questions (documented, not silent).
- AUROC / bootstrap **degenerate inputs RAISE** — an untestable metric is refused,
  not masked with a 0.5/0.0 default.

Complexity per function noted inline.
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np


# ── pure IR metrics (PORT-AS-IS from research/metrics_ir.py) ──────────────────

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


def precision_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the top-``k`` ranks that are relevant. Denominator is ``k`` (the
    cutoff), not ``len(retrieved)`` — a short result list is correctly penalized.
    // O(k) time, O(R) space."""
    if k <= 0:
        raise ValueError(f"precision_at_k requires k > 0, got {k}")
    if not relevant:
        return 0.0
    hits = sum(1 for item in _topk_unique(retrieved, k) if item in relevant)
    return hits / k


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


def ndcg_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Normalized DCG at ``k`` with binary relevance.

    DCG = Σ_{i=1..k} rel_i / log2(i + 1), rel_i ∈ {0,1} over the deduped top-k.
    IDCG = the DCG of the ideal ranking (min(k, |relevant|) ones up front).
    nDCG = DCG / IDCG; IDCG == 0 → 0.0. // O(k) time."""
    if k <= 0:
        raise ValueError(f"ndcg_at_k requires k > 0, got {k}")
    if not relevant:
        return 0.0
    topk = _topk_unique(retrieved, k)
    dcg = 0.0
    for i, item in enumerate(topk, start=1):
        if item in relevant:
            dcg += 1.0 / math.log2(i + 1)
    ideal_hits = min(k, len(relevant))
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def jaccard_at_k(a: Sequence[str], b: Sequence[str], k: int) -> float:
    """Jaccard similarity of the two top-``k`` rank *sets* (order-insensitive).

    |A∩B| / |A∪B| over the deduped top-k windows. Two empty windows → 1.0
    (identical); one empty, one not → 0.0. The stability metric capture→replay
    reports across a retrieval change. // O(k) time."""
    if k <= 0:
        raise ValueError(f"jaccard_at_k requires k > 0, got {k}")
    sa = set(_topk_unique(a, k))
    sb = set(_topk_unique(b, k))
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


# ── BUILD-NEW: significance machinery (no scorer exists in the reference) ──────

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
            f"len(is_miss)={len(is_miss)}")
    # A NaN score is the ONE input class where the rank-sum diverges from the
    # brute-force P(hit>miss): every comparison against NaN is False, so the sort
    # mis-places it and the `==` tie test never groups it — it would be assigned a
    # finite rank and return a clean, WRONG float. NaN is undefined ⇒ refuse, never
    # mask (use isnan, NOT `not isfinite`, so legitimate ±inf scores still work —
    # inf is orderable and produces the correct AUROC).
    if any(math.isnan(s) for s in scores):
        raise ValueError(
            "abstention_auroc requires non-NaN scores; NaN is undefined "
            "(refused, not masked)")
    n_miss = sum(1 for m in is_miss if m)
    n_hit = n - n_miss
    if n_hit == 0 or n_miss == 0:
        raise ValueError(
            f"abstention_auroc is undefined without BOTH classes: "
            f"hits={n_hit}, misses={n_miss} (degenerate — refused, not masked)")
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


def bootstrap_ci(
    deltas: Sequence[float], *, n_boot: int = 10_000, alpha: float = 0.05,
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
