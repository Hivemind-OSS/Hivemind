"""The ports (Protocols) the pure domain depends on. Every I/O and every swap
axis is an adapter behind one of these. runtime_checkable so fakes can be
isinstance-checked in tests. Method bodies are ``...`` — contracts only.

Episode-CRUD/admission methods live on the concrete store adapter (SqliteEpisodeStore);
this port pins the swap-seam contract only (the swap seam does not move as the adapter grows).
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

import numpy as np  # permitted in domain (not in the forbidden I/O set)

from hive.domain.models import (
    Episode,
)

if TYPE_CHECKING:
    # Typing-only forward ref; the concrete class lives in secret_scan.py. The guard
    # keeps ports.py import-cycle-free at runtime.
    from hive.domain.secret_scan import ScanVerdict


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
class SettledWinReader(Protocol):
    """The settled-win read the suspect-consensus martingale clause drives: for the requested
    episode ids, the subset carrying >= 1 settled-win evidence audit — ``outcome_helped``
    (self-reported via ``hive_outcome``) OR ``outcome_verified_helped`` (SHA-bound census
    corroboration), the UNION form. A SEPARATE narrow port (not a widening of an existing
    one) so existing narrow fakes stay conformant; the SqliteEpisodeStore already satisfies
    it (no adapter change). An id with no win audit is simply ABSENT — the caller
    under-claims (no settled win read for it)."""

    def settled_wins(self, episode_ids: "Sequence[int]") -> "set[int]": ...


@runtime_checkable
class LastVerificationReader(Protocol):
    """The verification-recency read the recall boundary enriches hits with: for each
    requested episode id, the NEWEST ``verify_current``/``verify_stale`` ledger row as
    ``(ts, head_sha, state)``, ``state ∈ {"current","stale"}`` derived from the kind.
    A SEPARATE narrow port (not a widening of an existing one) so existing narrow fakes
    stay conformant; the SqliteEpisodeStore satisfies it with one read method. A
    never-verified id is simply ABSENT (under-claim — the boundary then emits no key);
    the kernel never judges churn — the stamp is carried, the edge decides.
    ``own_refs`` (a DEFAULTED kwarg — the one conscious, minimal refinement of this
    port; omitted/empty keeps today's unscoped read, so every legacy caller compiles
    unchanged) maps an episode to the line(s) IT declared, and scopes that id's read
    to rows measured on them, so the rider never reports a memory stale on a line it
    never claimed; a legacy ref-less row always counts (the absence rule)."""

    def last_verification(
        self,
        episode_ids: Sequence[int],
        *,
        own_refs: Optional[Mapping[int, frozenset[str]]] = None,
    ) -> dict[int, tuple[int, str, str]]: ...


@runtime_checkable
class AnchoredEpisodeReader(Protocol):
    """The change→episode join's candidate read: ``(id, repo, anchor, polarity)`` rows —
    one per non-empty anchor binding of an approved episode (v3 ``episode_anchors``; an
    episode may carry several rows) — ALL trust states (mirrors ``hive_outcome``'s
    known-id rule: evidence on a deprecated row is honest ledger history). ``repo`` is
    the binding's partition — the census ingest filters rows to the receipt's repo, so
    one repo's change can never join another repo's memory. ``polarity`` is read for
    the verified-outcome classification only — the join itself stays anchor-driven.
    A SEPARATE narrow port (not a widening) so existing narrow fakes stay conformant;
    the SqliteEpisodeStore satisfies it with one read method."""

    def anchored_episodes(self) -> list[tuple[int, str, str, str]]: ...


@runtime_checkable
class RepoScopeReader(Protocol):
    """The servable-field partition map (v3 §3.6): every SERVABLE episode id →
    ``(repo scope, anchor pairs)`` — the read recall's partition filter and the
    lifecycle's partition-scoped competitor search consume. ``repo scope`` is the
    frozenset union of the episode's anchor repos and declared scope-only
    memberships (empty = a GENERAL memory, which matches every partition); ``anchor
    pairs`` are its ``(repo, anchor)`` code bindings. Servability is decided by the
    ONE ``lifecycle.is_servable`` rule, so this map and the vector index can never
    disagree about the field. A SEPARATE narrow port (not a widening of an existing
    one) so existing narrow fakes stay conformant; the SqliteEpisodeStore satisfies
    it with one read method."""

    def servable_scopes(
        self,
        *,
        now: int,
        provisional_ttl_s: int,
    ) -> dict[int, tuple[frozenset[str], tuple[tuple[str, str], ...]]]: ...


@runtime_checkable
class ChangeEvidenceAppender(Protocol):
    """The change-outcome feed's durable write seam: batch-append
    ``(episode_id, kind, actor, ts, payload)`` evidence rows in ONE transaction (an
    atomic receipt — a fault on any row rolls back all; never a partial receipt) with
    content-keyed idempotency (an exact ``(episode_id, kind, payload)`` duplicate is
    skipped, so a CI re-ingest cannot corrupt the ledger). Returns
    ``(inserted row ids, skipped count)``. Append-only: implementors never UPDATE or
    DELETE evidence rows."""

    def append_evidence(
        self, rows: Sequence[tuple[int, str, str, int, str]]
    ) -> tuple[list[int], int]: ...


@runtime_checkable
class RangeLedger(Protocol):
    """The census feed's durable range-dedupe seam: has this EXACT
    ``(repo, base_sha, head_sha, phase)`` range already been ingested, and record that it
    now has. Exact-key equality only — NO subsumption/overlap reasoning (a legacy A..B +
    B..C alongside a sync A..C are three distinct keys that all land; transition noise is
    tolerated, never interpreted). ``repo`` "" is the legacy receipt identity (pre-repo-key
    receipts dedupe among themselves). ``record_ingested_range`` is idempotent-bool: True
    iff a NEW row was written; a repeat of the same key is a no-op False (the first write
    stands). A SEPARATE narrow port (not a widening) so existing narrow fakes stay
    conformant; the SqliteEpisodeStore satisfies it with one table."""

    def already_ingested_range(
        self, repo: str, base_sha: str, head_sha: str, phase: str
    ) -> bool: ...
    def record_ingested_range(
        self, repo: str, base_sha: str, head_sha: str, phase: str, ts: int
    ) -> bool: ...


@runtime_checkable
class ConflictFlagStore(Protocol):
    """The ONE new narrow port: the durable write seam for an advisory conflict flag. The
    ConflictFlagService depends on this (+ ``EpisodeReader`` for id-exists + ``SecretScanner``
    for the resolution scan) — NOT a widening of an existing port, so the narrow fakes stay
    conformant. Idempotent on the canonical ``(a_id, b_id, kind)``; returns whether a NEW row
    was written (a re-flag of the same pair+kind is a no-op ``False``). The caller owns
    canonicalization (a_id<b_id) and the secret scan; this is the durable write only."""

    def record_conflict_flag(
        self,
        *,
        kind: str,
        a_id: int,
        b_id: int,
        winner_id: Optional[int],
        resolution: str,
        proposed_by: str,
        ts: int,
    ) -> bool: ...


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
    (secret-refused misses persist no content). ``repos_json`` is the miss's repo
    scope as a serialized JSON array (v3 §3.6) — a DEFAULTED kwarg whose '' reading
    is the GLOBAL miss, so every scope-less caller under-claims into
    matches-everything, never a crash. Implemented by the episode store; the recall
    pipeline depends only on this port."""

    def record_exposure(
        self,
        trace_id: str,
        items: Sequence[tuple[int, float]],
        *,
        agent_id: str,
        ts: int,
    ) -> None: ...
    def record_miss(
        self,
        query_text: str,
        query_vector: Optional[bytes],
        agent_id: str,
        miss_type: str,
        *,
        ts: int,
        repos_json: str = "",
    ) -> None: ...


@runtime_checkable
class SecretScanner(Protocol):
    """Deterministic credential scan run BEFORE staging (refuse/redact) — the
    no-secret-in-any-layer floor."""

    def scan(self, text: str) -> "ScanVerdict": ...
