"""The ports (Protocols) the pure domain depends on. Every I/O and every swap
axis is an adapter behind one of these. runtime_checkable so fakes can be
isinstance-checked in tests. Method bodies are ``...`` — contracts only.

Episode-CRUD/admission methods live on the concrete store adapter (SqliteEpisodeStore);
this port pins the swap-seam contract only (the swap seam does not move as the adapter grows).
"""
from __future__ import annotations

from typing import (
    Iterable, Optional, Protocol, Sequence, runtime_checkable,
)

import numpy as np  # permitted in domain (not in the forbidden I/O set)

from hive.domain.models import (
    Episode,
)


@runtime_checkable
class Clock(Protocol):
    """Injected time source (so the domain never imports ``time``)."""
    def now(self) -> int: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """text → value[d] (embed → L2-normalize). The SINGLE encode chain used by both capture
    and recall [D2]. ``d`` is the model's NATIVE dim (the stored-vector dim) — emitted
    unchanged, with no projection or dimension reduction."""
    d: int
    def encode(self, text: str) -> "np.ndarray": ...
    def encode_batch(self, texts: Sequence[str]) -> "np.ndarray": ...


@runtime_checkable
class VectorIndex(Protocol):
    """Approved-only dense search. Exhaustive is AUTHORITATIVE; a non-authoritative
    backend must declare itself so recall can refuse it [B3/D3]."""
    def search(self, query: "np.ndarray", k: int) -> list[tuple[int, float]]: ...
    def is_authoritative(self) -> bool: ...
    def size(self) -> int: ...


@runtime_checkable
class MutableVectorIndex(VectorIndex, Protocol):
    """The write side: a derived, rebuildable cache over the durable store."""
    def add(self, episode_id: int, value: "np.ndarray") -> None: ...
    def remove(self, episode_id: int) -> None: ...
    def rebuild_from_store(self, rows: Iterable[tuple[int, np.ndarray]]) -> None: ...


@runtime_checkable
class EpisodeReader(Protocol):
    """Narrow read seam the RecallPipeline uses to resolve a search hit (eid, sim)
    to its full candidate — the ``weight`` (surfacer base multiplier) and ``text``
    (the surfaced memory). A subset of the store adapter's surface (the SqliteEpisodeStore's
    ``get_episode`` already satisfies it); the pipeline never sees the schema."""
    def get_episode(self, episode_id: int) -> "Optional[Episode]": ...


@runtime_checkable
class QuarantineReader(Protocol):
    """The live (non-decayed) quarantined-row scan the self-quarantine resurfacing
    channel reads — the SAME method the promotion scan drives, returning
    ``(id, value, proposed_by, ts, last_active_ts)`` per row. A SEPARATE narrow port
    (not a widening of ``EpisodeReader``) so existing narrow fakes stay conformant;
    the SqliteEpisodeStore already satisfies it (no adapter change)."""
    def quarantined_candidates(
        self, *, now: int, quarantine_ttl_s: int
    ) -> list[tuple[int, "np.ndarray", str, int, int]]: ...


@runtime_checkable
class PromotionProvenanceReader(Protocol):
    """The newest-``promote``-audit demand-independence read the suspect-consensus worklist
    drives: for each requested episode id, the ``(rho_bar, n_eff, k)`` the promotion stamped
    (the effective-independence of the demand that promoted it). A SEPARATE narrow port (not a
    widening of an existing one) so existing narrow fakes stay conformant; the SqliteEpisodeStore
    already satisfies it (no adapter change). Pre-stamp promotion rows (no ``demand_independence``)
    and ids absent from any ``promote`` audit are simply OMITTED — the caller under-claims for
    them (a missing stamp is never read as a suspect signal)."""
    def promotion_provenance(
        self, episode_ids: Sequence[int]
    ) -> dict[int, tuple[float, float, int]]: ...


@runtime_checkable
class ConflictFlagStore(Protocol):
    """The ONE new narrow port: the durable write seam for an advisory conflict flag. The
    ConflictFlagService depends on this (+ ``EpisodeReader`` for id-exists + ``SecretScanner``
    for the resolution scan) — NOT a widening of an existing port, so the narrow fakes stay
    conformant. Idempotent on the canonical ``(a_id, b_id, kind)``; returns whether a NEW row
    was written (a re-flag of the same pair+kind is a no-op ``False``). The caller owns
    canonicalization (a_id<b_id) and the secret scan; this is the durable write only."""
    def record_conflict_flag(self, *, kind: str, a_id: int, b_id: int,
                             winner_id: Optional[int], resolution: str,
                             proposed_by: str, ts: int) -> bool: ...


@runtime_checkable
class MetaStore(Protocol):
    """The durable meta key-value WRITE seam (watermark / link records / readiness markers).
    The single-writer transaction lane is ``sqlite_db.tx()``, used directly by each mutating
    store method; meta reads go through raw SQL at the healthcheck boundary. Episode CRUD /
    admission live on the concrete store adapter."""
    def meta_set(self, key: str, value: str) -> None: ...


@runtime_checkable
class ExposureLedger(Protocol):
    """The recall side-channel writer: WHO was served WHAT (exposure, refreshing the
    served rows' liveness clocks in the same tx) and which queries got NO answer
    (misses — the demand signal). ``query_vector`` is raw float32 bytes or None
    (secret-refused misses persist no content). Implemented by the episode store;
    the recall pipeline depends only on this port."""
    def record_exposure(self, trace_id: str, items: Sequence[tuple[int, float]],
                        *, agent_id: str, ts: int) -> None: ...
    def record_miss(self, query_text: str, query_vector: Optional[bytes],
                    agent_id: str, miss_type: str, *, ts: int) -> None: ...


@runtime_checkable
class SecretScanner(Protocol):
    """Deterministic credential scan run BEFORE staging (refuse/redact) — the
    no-secret-in-any-layer floor."""
    def scan(self, text: str) -> "ScanVerdictLike": ...


# ── structural type aliases (forward refs to carriers defined in sibling modules)
# Typing-only names; the concrete classes live in models.py / secret_scan.py. Using
# strings keeps ports.py free of import cycles.
ScanVerdictLike = "hive.domain.secret_scan.ScanVerdict"
