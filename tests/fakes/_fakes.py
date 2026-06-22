"""In-memory conforming fakes for the pure-domain test suite. Each satisfies its
runtime_checkable Protocol; behavior is the minimum the slice needs (richer
behavior is added as later chunks require it). NO sqlite/git/torch/wall-clock."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

import numpy as np

from hive.domain.kinds import DEFAULT_KIND
from hive.domain.models import Episode, content_hash
from hive.domain.secret_scan import REFUSE, ScanVerdict
from hive.domain.secret_scan import scan as _scan


class FakeClock:
    def __init__(self, t: int = 0) -> None:
        self._t = int(t)

    def now(self) -> int:
        return self._t

    def advance(self, dt: int) -> None:
        self._t += int(dt)

    def set(self, t: int) -> None:
        self._t = int(t)


class FakeProvider:
    """Deterministic hash-based unit vectors — similar text → similar vectors."""
    def __init__(self, d: int = 256) -> None:
        self.d = int(d)

    def _vec(self, text: str) -> np.ndarray:
        h = hashlib.sha256(text.encode()).digest()
        rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
        v = rng.standard_normal(self.d).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def encode(self, text: str) -> np.ndarray:
        return self._vec(text)

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts], axis=0)


class FakeWarmProvider:
    """A deterministic hash-based ``EmbeddingProvider`` that ALSO satisfies the M12 warm
    contract (``.loaded`` / ``.load()`` / ``.name``) — for fast container-WIRING tests that
    must not pay the real model load. Hash-per-text means only IDENTICAL text matches (semantic
    recall quality is proven against the real embedder in tests/acceptance/*)."""
    name = "fake-warm"

    def __init__(self, d: int = 256, loaded: bool = False) -> None:
        self.d = int(d)
        self.loaded = bool(loaded)

    def load(self) -> "FakeWarmProvider":
        self.loaded = True
        return self

    def _vec(self, text: str) -> np.ndarray:
        h = hashlib.sha256(text.encode()).digest()
        rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
        v = rng.standard_normal(self.d).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def encode(self, text: str) -> np.ndarray:
        if not self.loaded:
            self.load()
        return self._vec(text)

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        if not self.loaded:
            self.load()
        rows = list(texts)
        if not rows:
            return np.zeros((0, self.d), dtype=np.float32)
        return np.stack([self._vec(t) for t in rows], axis=0)


class FakeClusterProvider:
    """An ``EmbeddingProvider`` whose vectors CLUSTER by a ``cid=<n>`` tag in the text: two
    DISTINCT texts sharing a cid are near-duplicate (cosine≈1), different cids are
    near-orthogonal. The controllable near-dup substrate the conflict-detection tests need —
    the hash-based ``FakeProvider`` gives orthogonal vectors for ANY distinct text, so it
    cannot model paraphrase. A text with no ``cid=`` tag falls back to a hash vector (its own
    singleton cluster). Also satisfies the warm contract (``loaded``/``load``/``name``)."""
    name = "fake-cluster"

    def __init__(self, d: int = 64, eps: float = 0.02,
                 loaded: bool = True) -> None:
        self.d = int(d)
        self._eps = float(eps)
        self.loaded = bool(loaded)

    def load(self) -> "FakeClusterProvider":
        self.loaded = True
        return self

    def _hashvec(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(
            int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big"))
        v = rng.standard_normal(self.d).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def _vec(self, text: str) -> np.ndarray:
        import re
        m = re.search(r"cid=(\d+)", text)
        if m is None:
            return self._hashvec(text)
        base = np.zeros(self.d, dtype=np.float32)
        base[int(m.group(1)) % self.d] = 1.0          # one-hot cluster direction
        v = base + self._eps * self._hashvec(text)     # tiny per-text perturbation
        n = float(np.linalg.norm(v))
        return (v / n).astype(np.float32)

    def encode(self, text: str) -> np.ndarray:
        return self._vec(text)

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        rows = list(texts)
        if not rows:
            return np.zeros((0, self.d), dtype=np.float32)
        return np.stack([self._vec(t) for t in rows], axis=0)


class FakeIndex:
    """Exhaustive cosine over an in-memory {eid: unit-vec}. Authoritative."""
    def __init__(self) -> None:
        self._rows: dict[int, np.ndarray] = {}

    def add(self, episode_id: int, value: np.ndarray) -> None:
        self._rows[int(episode_id)] = np.asarray(value, dtype=np.float32)

    def remove(self, episode_id: int) -> None:
        self._rows.pop(int(episode_id), None)

    def rebuild_from_store(self, store) -> None:  # noqa: ANN001 (test fake)
        self._rows.clear()

    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        q = np.asarray(query, dtype=np.float32)
        scored = [(eid, float(np.dot(q, v))) for eid, v in self._rows.items()]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]

    def is_authoritative(self) -> bool:
        return True

    def size(self) -> int:
        return len(self._rows)


def make_episode(episode_id: int, text: str, weight: float = 1.0,
                 *, status: str = "approved", trust: Optional[str] = None,
                 last_active_ts: int = 0, ts: int = 0,
                 value: Optional[np.ndarray] = None,
                 kind: str = DEFAULT_KIND, anchor: str = "") -> Episode:
    """A valid (self-asserting) Episode for resolve-seam tests — content_hash binds
    text. An approved episode defaults to trust='established' (mirroring the real
    store, where the human-vouched flip stamps established); pass ``trust`` to model
    provisional/quarantined rows. ``value`` defaults to None (margin/surface tests
    read only text + weight + labels) but can be supplied — the real store's
    get_episode always populates it."""
    approved = status == "approved"
    if trust is None:
        trust = "established" if approved else "quarantined"
    return Episode(
        id=int(episode_id), tenant_id="default", text=text, weight=float(weight),
        ts=int(ts), source="test", tags="", content_hash=content_hash(text),
        status=status, proposed_by="tester",
        approved_by="approver" if approved else None,
        approved_ts=0 if approved else None, version=0,
        trust=trust, last_active_ts=int(last_active_ts),
        kind=kind, anchor=anchor,
        value=None if value is None else np.asarray(value, dtype=np.float32),
    )


class FakeEpisodeReader:
    """EpisodeReader: resolves eid → Episode(text, weight). Unknown eid → None."""
    def __init__(self) -> None:
        self._by_id: dict[int, Episode] = {}

    def add(self, episode_id: int, text: str, weight: float = 1.0,
            **episode_kwargs) -> Episode:
        ep = make_episode(episode_id, text, weight, **episode_kwargs)
        self._by_id[int(episode_id)] = ep
        return ep

    def get_episode(self, episode_id: int) -> Optional[Episode]:
        return self._by_id.get(int(episode_id))


class FakeQuarantineReader:
    """QuarantineReader: the live-quarantined candidate scan the self-quarantine
    resurfacing channel reads. Rows are ``(id, value, proposed_by, ts,
    last_active_ts)`` — the SAME tuple the promotion scan consumes. ``calls`` counts
    scans so an off-inert test can assert the channel NEVER touched it."""
    def __init__(self) -> None:
        self._rows: list[tuple[int, np.ndarray, str, int, int]] = []
        self.calls = 0

    def add(self, episode_id: int, value: np.ndarray, *, writer: str,
            ts: int = 0, last_active_ts: int = 0) -> None:
        self._rows.append((int(episode_id), np.asarray(value, dtype=np.float32),
                           str(writer), int(ts), int(last_active_ts)))

    def quarantined_candidates(self, *, now: int, quarantine_ttl_s: int):
        self.calls += 1
        return list(self._rows)


class FakeStore:
    """EpisodeStore slice (transaction + meta kv), in-memory. The exposure /
    task_outcomes ledger methods were removed with the producer subsystem."""
    def __init__(self) -> None:
        self._meta: dict[str, str] = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield  # fakes are single-threaded in-memory; no real tx needed

    def meta_get(self, key: str) -> Optional[str]:
        return self._meta.get(key)

    def meta_set(self, key: str, value: str) -> None:
        self._meta[key] = value


class FakeConflictFlagStore:
    """ConflictFlagStore: records advisory flags in-memory, idempotent on the canonical
    (a_id, b_id, kind). ``open_conflict_flags`` mirrors the durable read. There is NO
    supersede/set_trust here BY CONSTRUCTION — the ConflictFlagService holds only this
    write seam, so it structurally cannot retire anything (advisory only)."""
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record_conflict_flag(self, *, kind: str, a_id: int, b_id: int,
                             winner_id: Optional[int], resolution: str,
                             proposed_by: str, ts: int) -> bool:
        key = (int(a_id), int(b_id), kind)
        if any((r["a_id"], r["b_id"], r["kind"]) == key for r in self.rows):
            return False
        self.rows.append({"kind": kind, "a_id": int(a_id), "b_id": int(b_id),
                          "winner_id": winner_id, "resolution": resolution,
                          "proposed_by": proposed_by, "ts": int(ts), "status": "open"})
        return True

    def open_conflict_flags(self) -> list[dict]:
        return [dict(r) for r in self.rows if r["status"] == "open"]


class FakeScanner:
    """SecretScanner wrapping the real deterministic scan. ``mode`` selects the
    secret disposition: REFUSE (default, fail-closed) or REDACT (mask-and-stage)."""
    def __init__(self, mode: str = REFUSE) -> None:
        self._mode = mode

    def scan(self, text: str) -> ScanVerdict:
        return _scan(text, mode=self._mode)


class FakeLedger:
    """ExposureLedger: in-memory recording of exposures + misses (the recall
    side-channel the pipeline writes)."""
    def __init__(self) -> None:
        self.exposures: list[dict] = []
        self.misses: list[dict] = []

    def record_exposure(self, trace_id: str, items, *, agent_id: str, ts: int) -> None:
        self.exposures.append({"trace_id": trace_id,
                               "items": [(int(e), float(m)) for e, m in items],
                               "agent_id": agent_id, "ts": int(ts)})

    def record_miss(self, query_text: str, query_vector, agent_id: str,
                    miss_type: str, *, ts: int) -> None:
        self.misses.append({"query_text": query_text, "query_vector": query_vector,
                            "agent_id": agent_id, "miss_type": miss_type, "ts": int(ts)})
