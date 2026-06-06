"""The ports (Protocols) the pure domain depends on. Every I/O and every swap
axis is an adapter behind one of these. runtime_checkable so fakes can be
isinstance-checked in tests. Method bodies are ``...`` — contracts only.

Slice scope (Phase 0): the methods the trace↔outcome join touches are pinned
here; episode-CRUD/admission methods are ADDED to EpisodeStore in Phase 1
(the Protocol grows; the swap seam does not move).
"""
from __future__ import annotations

from typing import (
    Mapping, Optional, Protocol, Sequence, runtime_checkable,
)

import numpy as np  # permitted in domain (not in the forbidden I/O set)

from hive.domain.models import (
    Episode, SettledExposure, UtilityPosterior,
)


@runtime_checkable
class Clock(Protocol):
    """Injected time source (so the domain never imports ``time``)."""
    def now(self) -> int: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """text → value[d] (embed → PCA head → L2-normalize). The SINGLE encode chain
    used by both capture and recall [D2]. ``d`` is the projected dim (256);
    ``w_version`` versions the frozen projection head for re-embed migration."""
    d: int
    w_version: int
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
    def rebuild_from_store(self, store: "EpisodeStore") -> None: ...


@runtime_checkable
class EpisodeReader(Protocol):
    """Narrow read seam the RecallPipeline uses to resolve a search hit (eid, sim)
    to its full candidate — the ``weight`` (surfacer base multiplier) and ``text``
    (the surfaced memory). A subset of EpisodeStore (the SqliteEpisodeStore's
    ``get_episode`` already satisfies it); the pipeline never sees the schema."""
    def get_episode(self, episode_id: int) -> "Optional[Episode]": ...


@runtime_checkable
class EpisodeStore(Protocol):
    """The durable single-writer store. The trace↔outcome state-machine methods
    (exposure + task_outcomes) were removed with the producer; the dormant tables
    survive but nothing drives them. The slice contract is now the transaction lane
    + the meta kv; episode CRUD / admission live on the concrete adapter (Phase 1)."""
    def transaction(self): ...                              # contextmanager: the single-writer tick lane
    # meta kv (watermark / link records / readiness markers)
    def meta_get(self, key: str) -> Optional[str]: ...
    def meta_set(self, key: str, value: str) -> None: ...


@runtime_checkable
class UtilityStore(Protocol):
    """Beta-Bernoulli utility posterior, separate + versioned. NO weight setter
    and NO episodes handle — the loop never writes episode.weight [A3]."""
    def apply_credit(self, deltas: Sequence["CreditDeltaLike"]) -> None: ...
    def posterior(self, episode_id: int, family_scope: str) -> UtilityPosterior: ...
    def utility_map(
        self, family_scope: str, *, confident_only: bool = True
    ) -> dict[int, float]: ...
    def isolation_episode_ids(self) -> set[int]: ...
    def zero_utility_layer(self) -> None: ...
    # The settled stream the PredictionBiasMonitor (guardrail-3, A6) reads. In the
    # single-DB design the posteriors and task_outcomes are co-located, so the
    # producer tick "passes outcomes in" by persisting them here; the monitor reads
    # them back, windowed. settle_ts ≥ since_ts (settled rows are always ≤ now), O(k)
    # via idx_task_outcomes_settle in the SQLite adapter.
    def settled_exposures_since(
        self, family_scope: str, since_ts: int
    ) -> Sequence[SettledExposure]: ...


@runtime_checkable
class SecretScanner(Protocol):
    """Deterministic credential scan run BEFORE staging (refuse/redact) [§9]."""
    def scan(self, text: str) -> "ScanVerdictLike": ...


@runtime_checkable
class InstallPlanner(Protocol):
    """The M07 hive_init handshake the MCP surface drives. ``plan`` is phase-1
    (zero writes — renders the rules block, sources ``trailer_key`` single-origin
    from ``producer.stamp_trailer``); ``confirm`` is the phase-2 hash-verified link.
    M06 depends only on this port; the concrete InstallPlanner adapter is M07 [B10]."""
    def plan(self, repo_path: str, harness: str,
             rules_file: "Optional[str]" = None) -> "InstallPlanLike": ...
    def confirm(self, repo_path: str, confirm_hash: bytes,
                harness: str = "generic") -> dict: ...


# ── structural type aliases (forward refs to carriers defined in sibling modules)
# These are typing-only names; the concrete classes live in models.py /
# attribution.py. Using strings keeps ports.py free of import cycles.
CreditDeltaLike = "hive.domain.attribution.CreditDelta"
ScanVerdictLike = "hive.domain.secret_scan.ScanVerdict"
InstallPlanLike = "hive.domain.models.InstallPlan"
