"""ExhaustiveCosineIndex — the AUTHORITATIVE default vector backend [B3].

Exact signed-cosine kNN over a stacked (N,d) float32 matrix. There is NO ANN
branch and NO approx_threshold field, so the silent-ANN-fallback trap (recall silently → 0 once N
crosses a threshold) is structurally impossible: growing N can never flip the path.

The in-RAM index is a best-effort WARM CACHE; `status='approved'` in SQLite is the
durable truth, and `rebuild_from_store(scan_approved())` on boot / post-migration is
the divergence-recovery guarantee (NOT in-transaction atomicity).
"""

from __future__ import annotations

import threading
from typing import Iterable

import numpy as np


class ExhaustiveCosineIndex:
    def __init__(self, dim: int) -> None:
        self.dim = int(dim)
        self._ids: list[int] = []
        self._pos: dict[int, int] = {}
        self._mat = np.zeros((0, self.dim), dtype=np.float32)
        self._lock = threading.Lock()

    def is_authoritative(self) -> bool:
        return True

    def size(self) -> int:
        return len(self._ids)

    # ── write side (warm cache) ───────────────────────────────────────────────
    def sync_approved(self, episode_id: int, value: np.ndarray) -> None:
        v = self._check_vec(value)
        with self._lock:
            if episode_id in self._pos:
                self._mat[self._pos[episode_id]] = v
                return
            self._pos[episode_id] = len(self._ids)
            self._ids.append(int(episode_id))
            self._mat = (
                np.vstack([self._mat, v[None, :]])
                if self._mat.size
                else v[None, :].copy()
            )

    add = sync_approved  # MutableVectorIndex alias

    def remove(self, episode_id: int) -> None:
        with self._lock:
            i = self._pos.pop(episode_id, None)
            if i is None:
                return
            last = len(self._ids) - 1
            if i != last:  # swap-remove
                self._mat[i] = self._mat[last]
                moved = self._ids[last]
                self._ids[i] = moved
                self._pos[moved] = i
            self._ids.pop()
            self._mat = self._mat[:last]

    drop = remove

    def rebuild_from_store(self, rows: Iterable[tuple[int, np.ndarray]]) -> None:
        """Divergence recovery [B3]: discard the cache and rebuild from the store's
        APPROVED rows only (the caller passes scan_approved())."""
        with self._lock:
            self._ids, self._pos = [], {}
            self._mat = np.zeros((0, self.dim), dtype=np.float32)
        for eid, value in rows:
            self.sync_approved(eid, value)

    # ── read side (exact, signed, authoritative) ──────────────────────────────
    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        if k <= 0 or not self._ids:
            return []
        q = self._check_vec(query)
        with self._lock:
            mat, ids = self._mat, list(self._ids)
        sims = mat @ q  # signed cosine (rows + query unit-norm)
        n = len(ids)
        kk = min(k, n)
        if kk < n:
            top = np.argpartition(-sims, kk - 1)[:kk]  # unordered top-k
            top = top[np.argsort(-sims[top])]  # MUST re-sort the slice
        else:
            top = np.argsort(-sims)
        return [(ids[i], float(sims[i])) for i in top]

    def _check_vec(self, value: np.ndarray) -> np.ndarray:
        v = np.asarray(value, dtype=np.float32)
        if v.shape != (self.dim,):
            raise ValueError(f"vector shape {v.shape} != ({self.dim},)")
        if not np.all(np.isfinite(v)):
            raise ValueError(
                "non-finite vector (NaN/inf) rejected at the index boundary"
            )
        return v.copy()  # copy-on-ingest


def build_index(backend: str, dim: int) -> "ExhaustiveCosineIndex":
    """Fail-fast factory. The swap point for the index backend [D3]."""
    backends = {"exhaustive": ExhaustiveCosineIndex}
    if backend not in backends:
        raise ValueError(
            f"unknown index backend {backend!r}; valid: {sorted(backends)}"
        )
    return backends[backend](dim)
