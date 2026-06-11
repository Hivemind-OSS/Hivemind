"""Pure Reciprocal Rank Fusion — the only fusion math in the tree.

RRF is rank-based by construction: it consumes positions, never raw scores, so a
dense cosine list and a BM25 list fuse without any cross-channel score
calibration. ``k=60`` is the canonical zero-tune constant.

PURE: stdlib only. The purity gate (tests/test_purity.py) forbids I/O imports
anywhere in hive/domain/ and auto-covers this module.
"""
from __future__ import annotations

from typing import Sequence


def rrf_fuse(rankings: "Sequence[Sequence[int]]", *, k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion over N rank-ordered id lists.

    score(id) = Σ_r 1/(k + rank_r(id)), rank 0-indexed; absent from a ranking ⇒
    contributes 0. Returns ids fused-score-descending; exact ties keep first-seen
    order (stable, deterministic). Duplicate ids within one ranking: the first
    occurrence counts. Empty input ⇒ [].  // O(Σ|rankings|) time + space."""
    scores: dict[int, float] = {}
    first_seen: dict[int, int] = {}
    for ranking in rankings:
        counted: set[int] = set()
        for rank, eid in enumerate(ranking):
            eid = int(eid)
            if eid in counted:
                continue
            counted.add(eid)
            scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank)
            if eid not in first_seen:
                first_seen[eid] = len(first_seen)
    return sorted(scores, key=lambda e: (-scores[e], first_seen[e]))
