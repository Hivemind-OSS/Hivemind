"""M04 — the read half of the pure domain core: the never-hallucinate
enforcement point.

`RecallPipeline.recall(query, *, agent_id, scope) -> RecallResult` hides the
whole encode → dense-cosine search → repo-scope filter → absolute-relevance
abstain flow behind one narrow surface. It owns NO I/O: it composes injected
ports (`EmbeddingProvider`, `VectorIndex`, the `ScopedEpisodeReader`
resolve+partition read) and the pure `AbsoluteRelevanceGate` stage. The
dense+gate order is served as-is — there is no utility rerank (reinforcement is
demand/exposure-only).

Repo partitioning (v3 §3.6, decision D6): ONE shared exhaustive index
(id→vector, unchanged — Law 5 cache purity); the partition lives in the
CANDIDATE LIST. A non-global `RecallScope` filters the searched field through
the `servable_scopes()` partition map BETWEEN search and gate, so the gate
evaluates only the scoped sims — the absolute-relevance gate has no
distribution-shape dependence, so `tau_serve` is partition-safe by
construction. The global path (scope None/empty) never reads the map: the
legacy read stays byte-stable.

PURE: stdlib `math` + `uuid` + `json` only. The purity gate
(tests/test_purity.py) forbids sqlite3 | torch | subprocess | os | git | time
here — so the whole read path is fake-testable in milliseconds.

The central invariant — **abstain-no-resurrect** — is made STRUCTURAL: once the
gate suppresses, `recall()` returns before the resolve/exposure steps, and the
frozen `RecallResult.__post_init__` makes a non-CONFIDENT-result-carrying-hits
state unconstructable. There is no code path that can repopulate `hits`.
"""

from __future__ import annotations

import json
import logging
import math
import uuid

import numpy as np  # permitted in domain (compute carrier, not I/O)
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Sequence, runtime_checkable

from hive.domain.conflict import (
    ConflictItem,
    ConflictNote,
    _cosine,
    detect_conflicts,
    suppression_targets,
)
from hive.domain.lifecycle import LifecycleService, is_servable, scope_matches
from hive.domain.models import (
    CONFIDENT,
    Episode,
    RecallHit,
    RecallResult,
    Scored,
)
from hive.domain.ports import (
    EmbeddingProvider,
    EpisodeReader,
    ExposureLedger,
    RepoScopeReader,
    SecretScanner,
    VectorIndex,
)
from hive.domain.secret_scan import REDACT, REFUSE

_log = logging.getLogger("hive.recall")


@dataclass(frozen=True, slots=True)
class RecallScope:
    """The recall partition ask (v3 §3.3), parsed and validated at the boundary
    BEFORE it reaches the pipeline (unknown-repo refusal is the boundary's job —
    the pipeline assumes a valid scope). ``repos`` are ``(repo, ref)`` pairs:
    ``ref`` "" = the repo's canonical line, and the ref names the drift tip
    ONLY (a boundary/drift concern, carried-not-interpreted here) — the
    partition filter matches by repo NAME alone. Empty ``repos`` = GLOBAL.
    ``anchor_prefix`` "" = no anchor narrowing. Frozen + self-asserting: a
    malformed scope is unconstructable."""

    repos: tuple[tuple[str, str], ...]
    anchor_prefix: str

    def __post_init__(self) -> None:
        for pair in self.repos:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not pair[0]
                or not isinstance(pair[1], str)
            ):
                raise ValueError(
                    "RecallScope.repos entries must be (repo, ref) string "
                    f"pairs with a non-empty repo name (got {pair!r})"
                )
        if not isinstance(self.anchor_prefix, str):
            raise ValueError("RecallScope.anchor_prefix must be a str")

    @property
    def repo_names(self) -> frozenset[str]:
        """The partition-name set the filter matches on (refs are drift-only)."""
        return frozenset(repo for repo, _ref in self.repos)

    @property
    def is_global(self) -> bool:
        """True iff this scope filters nothing (no repos, no anchor prefix) —
        the identity filter, equivalent to passing no scope at all."""
        return not self.repos and not self.anchor_prefix


@runtime_checkable
class ScopedEpisodeReader(EpisodeReader, RepoScopeReader, Protocol):
    """The pipeline's ONE store dependency: eid→Episode resolve
    (``EpisodeReader``) + the servable partition map (``RepoScopeReader``).
    Composed HERE, at the consumer, so each port in ports.py stays narrow —
    the real store satisfies both with the same object."""


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """Frozen, self-asserting gate output. ``suppress`` ⇔ the field lacks absolute
    relevance. ``top_cos`` = max absolute cosine over the FULL servable field
    (clamped into [-1, 1]; -1.0 on an empty field). ``n_relevant`` = count of sims
    clearing ``tau_serve`` (>= 0). An inconsistent verdict is UNCONSTRUCTABLE."""

    suppress: bool
    top_cos: float
    n_relevant: int

    def __post_init__(self) -> None:
        if not (math.isfinite(self.top_cos) and -1.0 <= self.top_cos <= 1.0):
            raise ValueError(f"top_cos must be finite in [-1, 1] (got {self.top_cos})")
        if self.n_relevant < 0:
            raise ValueError(f"n_relevant must be >= 0 (got {self.n_relevant})")


class _RecallConfigLike(Protocol):
    """The duck-typed frozen recall-config contract ``from_recall`` reads
    (``tau_serve`` required; ``k_min`` getattr-defaulted) — the domain stays
    unaware of ``app.Config``."""

    @property
    def tau_serve(self) -> float: ...


class AbsoluteRelevanceGate:
    """Abstain unless the field carries absolute relevance. PURE (math only); fail-closed.

    ``suppress`` iff ``n_relevant < k_min``, where ``n_relevant = |{s in sims : s >=
    tau_serve}|`` on EXACT unit-norm vectors. A flat-but-all-relevant field SERVES (many
    clear the floor); a flat-but-weak field ABSTAINS (none clear it) — shift-invariance is
    gone by construction. A peaked-but-absolutely-weak field also ABSTAINS (top cos <
    tau_serve) — the old entropy gate wrongly SERVED it. Empty / non-finite / any internal
    failure → fail-closed SUPPRESS (Law 1; the new gate has no softmax to raise on a
    non-finite input, so the explicit guard below is load-bearing). PRECONDITION: the
    producer emits exact unit-norm vectors (BUG-008), so an absolute-cosine floor is
    meaningful.
    """

    def __init__(self, tau_serve: float, k_min: int = 1) -> None:
        if not (math.isfinite(tau_serve) and 0.0 < tau_serve <= 1.0):
            # the absolute-cosine serve floor; an out-of-range floor is undecidable — fail
            # fast at construction (B1). 1.0 ⇒ only an exact-match field ever serves.
            raise ValueError("tau_serve must be finite in (0, 1]")
        k_min = int(k_min)
        if k_min < 1:
            raise ValueError("k_min must be an int >= 1")
        self.tau_serve = float(tau_serve)
        self.k_min = k_min
        # set by from_recall() to the frozen config object
        self._recall: Optional[_RecallConfigLike] = None

    @classmethod
    def from_recall(cls, recall: "_RecallConfigLike") -> "AbsoluteRelevanceGate":
        """Construct the gate BY IDENTITY from the single frozen recall-config object
        (CONFIG_DRIFT killed structurally — M11). ``tau_serve`` and the ``k_min`` floor are
        read off that one object (``k_min`` getattr-defaulted so the duck-typed contract
        survives an older config) and the object itself is retained at ``self._recall`` so a
        future second gate cannot fork the floor. ``recall`` is duck-typed (any frozen object
        exposing ``tau_serve``) — the domain stays unaware of ``app.Config``."""
        gate = cls(float(recall.tau_serve), int(getattr(recall, "k_min", 1)))
        gate._recall = recall
        return gate

    def evaluate(self, sims: Sequence[float]) -> GateVerdict:
        try:
            for s in sims:  # non-finite guard FIRST (Law 1)
                if not math.isfinite(s):
                    raise ValueError("non-finite sim is undecidable — fail closed")
            if not sims:
                return GateVerdict(True, -1.0, 0)
            top_cos = max(
                -1.0, min(1.0, max(sims))
            )  # clamp float-error drift into [-1, 1]
            n_relevant = sum(1 for s in sims if s >= self.tau_serve)
            return GateVerdict(n_relevant < self.k_min, top_cos, n_relevant)
        except Exception:  # noqa: BLE001 — fail-closed by contract
            _log.warning(
                "relevance gate internal failure (n=%s) → fail-closed SUPPRESS",
                len(sims) if hasattr(sims, "__len__") else "?",
            )
            return GateVerdict(True, -1.0, 0)


def _conflict_anchor(ep: Episode) -> str:
    """Project the v3 multi-anchor episode onto ``ConflictItem``'s single
    advisory ``anchor`` label: the FIRST binding's anchor (the store's canonical
    (repo, anchor) projection order — deterministic), "" when unanchored. The
    label only ever feeds the shared-anchor note on a detected pair; the
    conflict scan itself is vector-driven, so this projection is advisory-only."""
    return ep.anchors[0].anchor if ep.anchors else ""


def _scope_filter(
    candidates: Sequence[tuple[int, float]],
    scopes: dict[int, tuple[frozenset[str], tuple[tuple[str, str], ...]]],
    scope: RecallScope,
) -> list[tuple[int, float]]:
    """The v3 §3.6 partition filter over the searched field (D6 — between
    search and gate). Keep an eid iff:
      1. it is SERVABLE — present in the map. In ``servable_scopes`` absence
         only ever means non-servable (a general memory reads an explicit
         ``(frozenset(), ())``), so a warm-but-lapsed index row is dropped
         here, pre-gate (fail-closed — it could never be surfaced anyway);
      2. its repo scope matches the query's — the ONE ``scope_matches`` owner:
         a general memory (empty scope) is ALWAYS in; a global query (empty
         name set) keeps every partition; otherwise the scopes must intersect;
      3. the ``anchor_prefix`` clause: a prefix narrows the ANCHORED population
         only — an unanchored (general / scope-only) memory is KEPT; an
         anchored one needs ≥1 anchor (in any of its repos) under the prefix.
    Pure, order-preserving, REMOVE-only.  // O(candidates · anchors)."""
    names = scope.repo_names
    prefix = scope.anchor_prefix
    kept: list[tuple[int, float]] = []
    for eid, sim in candidates:
        entry = scopes.get(int(eid))
        if entry is None:
            continue  # absent ⇒ non-servable ⇒ never a candidate
        repos, anchor_pairs = entry
        if not scope_matches(repos, names):
            continue  # out-of-partition (S∩R=∅, both non-empty)
        if (
            prefix
            and anchor_pairs
            and not any(anchor.startswith(prefix) for _repo, anchor in anchor_pairs)
        ):
            continue  # anchored, but every anchor outside the prefix
        kept.append((eid, sim))
    return kept


def select_served(
    items: Sequence[tuple[Scored, Episode]], *, dup_tau: float, top_n: int
) -> list[tuple[Scored, Episode]]:
    """Choose the served set from a sim-DESCENDING overscan pool. REMOVE-only, two reasons,
    then cap at ``top_n``:
      1. trust-dominated near-dup (the folded-in conflict.suppress): of a near-dup pair
         (cosine >= dup_tau) with STRICT trust dominance, drop the lower-trust member. This
         runs FIRST and is what makes the MMR pass safe — pure greedy-by-sim would keep a
         higher-cosine LOWER-trust cosine-twin poison over a lower-cosine higher-trust truth.
         Reuses detect_conflicts + suppression_targets (no new cosine code).
      2. MMR decorrelation: greedy over the survivors in sim-desc order; drop a candidate
         whose cosine to an ALREADY-KEPT one is >= dup_tau (keep the unique representative,
         remove the echoes). The same-trust echoes that #1 cannot decide collapse to the
         higher-sim member here.
    PURE (conflict._cosine), TOTAL, never raises; an undecidable cosine is treated as
    NOT-a-near-dup (keep both — safe for a REMOVE-only filter). Drops only — never reorders
    beyond the input sim order, never resurrects, never elevates trust.  // O(n^2 d), n small.
    PRECONDITION: episodes carry exact unit-norm vectors (BUG-008)."""
    citems = [
        ConflictItem(
            episode_id=ep.id,
            vector=ep.value,
            polarity=ep.polarity,
            anchor=_conflict_anchor(ep),
            ts=ep.ts,
            trust=ep.trust,
        )
        for _s, ep in items
    ]
    drop = suppression_targets(
        detect_conflicts(citems, tau=dup_tau, top_n=len(citems)),
        {it.episode_id: it.trust for it in citems},
    )
    kept: list[tuple[Scored, Episode]] = []
    for s, ep in items:  # items already sim-desc
        if ep.id in drop:
            continue
        if any((_cosine(ep.value, kep.value) or -1.0) >= dup_tau for _ks, kep in kept):
            continue
        kept.append((s, ep))
    return kept[:top_n]


class RecallPipeline:
    """The one deep module that answers-or-abstains. Composes injected ports +
    the two pure stages. NEVER raises into the caller: any internal failure is
    logged and degrades to EMPTY_NO_DATA (fail closed — return nothing, never an
    un-vetted hit). abstain-no-resurrect is structural: the suppress branch returns
    before the resolve/exposure steps, and a non-CONFIDENT RecallResult cannot
    carry hits. The v3 repo partition (D6) is applied BETWEEN search and gate:
    one shared index, a non-global scope filters the candidate list through the
    reader's ``servable_scopes`` map, and the gate sees only the scoped sims.
    ``recall_top_n`` is a CAP, not a target: the served set is the ``>= tau_serve``
    prefix, capped at ``recall_top_n`` — the floor decides WHETHER to answer and
    also WHICH rows may ride, so one relevant candidate cannot carry padding.
    // recall(): O(N·d) search + O(N log N) sort, N = approved count.
    """

    def __init__(
        self,
        *,
        embedder: EmbeddingProvider,
        index: VectorIndex,
        gate: AbsoluteRelevanceGate,
        reader: ScopedEpisodeReader,
        recall_top_n: int,
        ledger: ExposureLedger,
        clock_now: Callable[[], int],
        scanner: SecretScanner,
        provisional_ttl_s: int,
        lifecycle: Optional[LifecycleService] = None,
        autonomy_enabled: bool = True,
        overscan: int = 3,
        dup_tau: float = 0.80,
        conflict_enabled: bool = False,
        conflict_top_n: int = 10,
    ) -> None:
        self.embedder = embedder
        self.index = index
        self.gate = gate
        # the one store read seam: eid→Episode resolve + the servable partition
        # map (ScopedEpisodeReader). The map is read ONLY under a non-global scope.
        self.reader = reader
        self.recall_top_n = int(recall_top_n)
        # the recall side-channel (REQUIRED, never silently absent): exposure on
        # CONFIDENT, a recorded miss on every non-answer. Writes are fail-open for
        # the read — a ledger fault never breaks recall — and fully inert when
        # autonomy is disabled (byte-stable read path).
        self.ledger = ledger
        self.clock_now = clock_now
        self.scanner = scanner  # SecretScanner: the miss-text secret floor
        # the pipeline-level servability belt: a non-servable row is dropped at
        # RESOLVE, before it can be surfaced or exposed — exposure refreshes
        # liveness, so exposing a TTL-lapsed row would resurrect it. The mcp
        # handler re-checks per hit as redundancy; both read the ONE is_servable.
        self.provisional_ttl_s = int(provisional_ttl_s)
        self.lifecycle = lifecycle  # LifecycleService or None: on_miss trigger
        self.autonomy_enabled = bool(autonomy_enabled)
        # post-gate decorrelated selection (UNCONDITIONAL — always on, no knob). Over an OVERSCAN
        # pool (recall_top_n*overscan) select_served picks the served set by trust-dominance (drop
        # a strictly-lower-trust near-dup twin) THEN MMR-decorrelation (collapse cosine echoes) —
        # never touching the abstain decision, never retiring a row (resolution stays human).
        # dup_tau is the near-dup cosine floor (single-owned by ConflictConfig.tau).
        self.overscan = int(overscan)
        self.dup_tau = float(dup_tau)
        # recall-time conflict carrier: detect near-dup/contradiction pairs over the PRE-select
        # resolved field (so a near-dup the decorrelated serve dropped is still surfaced for human
        # resolution). ids-only on RecallResult.conflicts. It runs AFTER select_served over the
        # SAME resolved field select_served already fed to detect_conflicts, so its detector call
        # cannot fault when reached — select_served is the SINGLE owner of the detect_conflicts /
        # vector-fault decision and fail-CLOSES the read first (no separate fail-open guard). OFF ⇒
        # never computed. Shares dup_tau (=ConflictConfig.tau) as the near-dup floor.
        self.conflict_enabled = bool(conflict_enabled)
        self.conflict_top_n = int(conflict_top_n)

    def recall(
        self, query: str, *, agent_id: str, scope: Optional[RecallScope] = None
    ) -> RecallResult:
        """Answer-or-abstain over the servable field. ``scope`` (v3 §3.3) is the
        already-parsed partition ask: ``None`` — the omitted-repos default — or a
        global ``RecallScope`` searches EVERYTHING (the legacy read, byte-stable,
        no partition-map read); a non-global scope filters the candidate list
        between search and gate (D6), so the served set AND the abstain decision
        are both computed over the repos∩scope ∪ general partition, and every
        miss is recorded WITH the query's repo scope."""
        trace_id = uuid.uuid4().hex

        # encode → search (fail-closed: embedder/index/authority failure ⇒ EMPTY).
        value_q = None
        try:
            if not self.index.is_authoritative():
                _log.error(
                    "non-authoritative index rejected (never-flip guard) "
                    "→ EMPTY_NO_DATA (agent_id=%s)",
                    agent_id,
                )
                self._note_non_answer(query, None, agent_id, "no_match", scope)
                return RecallResult.empty(trace_id)
            # encode BEFORE the empty-index short-circuit: a cold store's misses
            # must still carry their query vector or demand can never accumulate
            # and no quarantined capture would ever promote (cold-start deadlock).
            value_q = self.embedder.encode(query)
            if self.index.size() == 0:
                _log.debug("empty index → EMPTY_NO_DATA (agent_id=%s)", agent_id)
                self._note_non_answer(query, value_q, agent_id, "no_match", scope)
                return RecallResult.empty(trace_id)
            # search the FULL shared index (one exhaustive id→vector cache — D6);
            # recall_top_n only truncates hits, NEVER the abstain decision.
            candidates = self.index.search(value_q, self.index.size())
            # v3 §3.6: the repo partition lives in the CANDIDATE LIST, never the
            # index — a non-global scope filters the searched field through the
            # servable partition map, BETWEEN search and gate. A map fault raises
            # into this try ⇒ fail-closed EMPTY (never an out-of-scope serve).
            if scope is not None and not scope.is_global:
                scopes = self.reader.servable_scopes(
                    now=int(self.clock_now()), provisional_ttl_s=self.provisional_ttl_s
                )
                candidates = _scope_filter(candidates, scopes, scope)
            # marker: the gate evaluates the SCOPE-FILTERED sims ONLY (the
            # absolute-relevance gate has no distribution-shape dependence, so
            # tau_serve is preserved under partitioning — D6). Gating on the
            # UNFILTERED sims is the named mutation: CT-3's tau-preservation test
            # (tests/contract/test_recall_partition.py::
            # test_scoped_weak_field_abstains_tau_preserved) and its unit twin
            # (tests/domain/test_recall_pipeline.py::
            # test_scoped_weak_field_abstains_tau_preserved) red.
            # coerce INSIDE the try: a contract-violating adapter row (NULL cosine,
            # wrong arity) must fail-closed to EMPTY, never raise (AUDIT #D).
            sims = [float(sim) for _eid, sim in candidates]
        except Exception as exc:  # noqa: BLE001 — fail closed
            _log.error(
                "recall encode/search failure (agent_id=%s): %r → EMPTY_NO_DATA",
                agent_id,
                exc,
            )
            self._note_non_answer(query, value_q, agent_id, "no_match", scope)
            return RecallResult.empty(trace_id)

        # abstain gate (self-fail-closed to SUPPRESS). One-way: suppress returns
        # here — an empty scoped partition lands here too (0 sims ⇒ suppress).
        verdict = self.gate.evaluate(sims)
        if verdict.suppress:
            _log.info(
                "ABSTAIN trace=%s top_cos=%.4f n_relevant=%d n_cands=%d",
                trace_id,
                verdict.top_cos,
                verdict.n_relevant,
                len(sims),
            )
            self._note_non_answer(query, value_q, agent_id, "abstained", scope)
            return RecallResult.abstain(trace_id, verdict.top_cos)

        # ── CONFIDENT path ────────────────────────────────────────────────────
        # The whole surface step (resolve → surface) fails closed to EMPTY_NO_DATA on
        # ANY internal raise — no collaborator may throw into the caller (AUDIT #C/#D).
        # The per-hit exposure weight is the raw dense cosine ``hit.sim`` (reinforcement
        # is demand/exposure-only — there is no utility rerank; the dense+gate order is
        # served as-is).
        try:
            # the honest dense cosine keyed by eid — the dense search spans the full
            # servable index, so every shortlisted id has an entry.
            dense_ids = [int(eid) for eid, _sim in candidates]
            sim_by_eid = {
                dense_ids[i]: float(candidates[i][1]) for i in range(len(dense_ids))
            }

            # overscan the resolve pool (recall_top_n*overscan), so a query whose top dense ranks
            # are unservable can still backfill real servable hits from deeper in the field before
            # select_served decorrelates and caps at recall_top_n.
            pool = self.recall_top_n * self.overscan
            # marker: dropping this filter (or moving it below select_served /
            # _note_exposure) reds tests/contract/test_minimal_hardening_e2e.py::
            # test_every_served_hit_clears_tau_serve and ::test_a_sub_tau_row_is_never_
            # exposed — the gate's floor is a per-hit filter as well as a call-level
            # decision, and a row the floor rejects must never have its liveness
            # refreshed by the read that rejected it. The floor is read BY IDENTITY off
            # the gate the pipeline already holds, so there is no second copy to drift.
            shortlist = [
                eid
                for eid in dense_ids[:pool]
                if sim_by_eid[eid] >= self.gate.tau_serve
            ]
            # resolve the shortlist to full episodes (weight + text + trust labels). A hit
            # that cannot resolve OR is no longer servable is dropped HERE — before it can
            # be surfaced or exposed (a TTL-lapsed row in the stale warm index must never
            # get its liveness refreshed by the very read that should have refused it).
            now_belt = int(self.clock_now())
            resolved: list[tuple[Scored, Episode]] = []  # (scored, ep), dense order
            for eid in shortlist:
                ep: Optional[Episode] = self.reader.get_episode(eid)
                if ep is None or not is_servable(
                    status=ep.status,
                    trust=ep.trust,
                    last_active_ts=ep.last_active_ts,
                    now=now_belt,
                    provisional_ttl_s=self.provisional_ttl_s,
                ):
                    _log.warning(
                        "resolve drop (missing/unservable) eid=%s trace=%s",
                        eid,
                        trace_id,
                    )
                    continue
                resolved.append((Scored(eid, float(ep.weight), sim_by_eid[eid]), ep))

            # decorrelated selection (§8.4 dedup/shadow slot, UNCONDITIONAL): trust-dominance drop
            # THEN MMR over the servable-resolved pool, capped at recall_top_n. Runs AFTER the
            # gate (never touches the abstain decision) and BEFORE the empty-check + exposure
            # below, so a dropped row is never surfaced or liveness-refreshed (belt-ordering). It
            # only REMOVES — no resurrection. A detector fault raises into this block's enclosing
            # try ⇒ EMPTY_NO_DATA (fail-closed): select_served is the SINGLE owner of the
            # detect_conflicts-fault decision for the whole read.
            selected = select_served(
                resolved, dup_tau=self.dup_tau, top_n=self.recall_top_n
            )

            if not selected:
                _log.error(
                    "gate passed but 0 candidates resolved → EMPTY_NO_DATA "
                    "(fail-closed) trace=%s",
                    trace_id,
                )
                self._note_non_answer(query, value_q, agent_id, "no_match", scope)
                return RecallResult.empty(trace_id)

            # recall-time conflict carrier: detect over the PRE-select resolved field so a near-dup
            # the decorrelated serve dropped is still surfaced for human resolution. It runs over
            # the SAME resolved field select_served just fed to detect_conflicts, so this call
            # cannot fault when reached — no separate fail-open guard: a detector/vector fault was
            # already owned (fail-closed → EMPTY) by select_served above. Inside the surface try.
            # OFF / <2 servable rows ⇒ ().
            conflicts: tuple[ConflictNote, ...] = ()
            if self.conflict_enabled and len(resolved) >= 2:
                citems = [
                    ConflictItem(
                        episode_id=ep.id,
                        vector=ep.value,
                        polarity=ep.polarity,
                        anchor=_conflict_anchor(ep),
                        ts=ep.ts,
                        trust=ep.trust,
                    )
                    for _s, ep in resolved
                ]
                conflicts = detect_conflicts(
                    citems, tau=self.dup_tau, top_n=self.conflict_top_n
                )

            scored = [s for s, _ep in selected]
            # reinforcement is demand/exposure-ONLY by design — there is no utility rerank:
            # the dense+gate order is served as-is. Every served hit carries its trust label
            # + creation ts (to discount provisional content and order coexisting versions)
            # and its repo partition + code bindings (v3 — repos/anchors ride every hit).
            hits = tuple(
                RecallHit(
                    s.episode_id,
                    ep.text,
                    s.sim,
                    trust=ep.trust,
                    ts=ep.ts,
                    polarity=ep.polarity,
                    kind=ep.kind,
                    repos=ep.repos,
                    anchors=ep.anchors,
                    meta=ep.meta,
                )
                for s, ep in selected
            )
        except Exception as exc:  # noqa: BLE001 — fail closed
            _log.error(
                "recall surface failure (agent_id=%s): %r → EMPTY_NO_DATA",
                agent_id,
                exc,
            )
            self._note_non_answer(query, value_q, agent_id, "no_match", scope)
            return RecallResult.empty(trace_id)

        # exposure: WHO was served WHAT, with the raw cosine weight, refreshing the served
        # rows' liveness — recorded post-resolve in surfaced order.
        self._note_exposure(trace_id, [(s.episode_id, s.sim) for s in scored], agent_id)
        _log.debug(
            "CONFIDENT trace=%s n_hits=%d top_sim=%.4f",
            trace_id,
            len(hits),
            hits[0].sim,
        )
        return RecallResult(
            CONFIDENT, trace_id, hits, verdict.top_cos, conflicts=conflicts
        )

    # ── the recall side-channel (exposure + demand), fail-open for the read ───
    def _note_exposure(
        self, trace_id: str, items: Sequence[tuple[int, float]], agent_id: str
    ) -> None:
        """Persist the served set (liveness refresh rides the same tx in the
        adapter). A fault is logged and swallowed — never breaks the read. Inert
        when autonomy is disabled (no new rows on the read path)."""
        if not self.autonomy_enabled:
            return
        try:
            self.ledger.record_exposure(
                trace_id, items, agent_id=agent_id, ts=int(self.clock_now())
            )
        except Exception as exc:  # noqa: BLE001 — fail open
            _log.warning(
                "exposure record failed (trace=%s): %r — recall unaffected",
                trace_id,
                exc,
            )

    def _note_non_answer(
        self,
        query: str,
        vector: Optional[np.ndarray],
        agent_id: str,
        miss_type: str,
        scope: Optional[RecallScope] = None,
    ) -> None:
        """Record the non-answer (the demand signal), secret-scanned BEFORE
        persistence: REFUSE ⇒ no content survives (empty text, no vector,
        type='secret_refused' — counts in telemetry, can never drive promotion);
        REDACT ⇒ masked text + a vector re-encoded FROM the masked text (never the
        raw query's). The miss carries the query's REPO scope (v3 §3.6) as a
        sorted, deduplicated JSON name array — "" = a GLOBAL miss (scope-less or
        repo-empty; the drift refs and ``anchor_prefix`` never ride:
        ``MissRow.repos`` is repo names only) — so demand accumulates inside the
        partition that asked. Then the synchronous on_miss promotion trigger (the
        miss is recorded FIRST so the trigger's window includes it). Fail-open;
        inert when autonomy is disabled."""
        if not self.autonomy_enabled:
            return
        try:
            text, vec, mtype = query, vector, miss_type
            verdict = self.scanner.scan(query)
            if verdict.action == REFUSE:
                text, vec, mtype = "", None, "secret_refused"
            elif verdict.action == REDACT:
                text = verdict.redacted_text or ""
                vec = self.embedder.encode(text)
            vbytes = vec.tobytes() if vec is not None else None
            repos_json = ""
            if scope is not None and scope.repos:
                repos_json = json.dumps(sorted(scope.repo_names), separators=(",", ":"))
            self.ledger.record_miss(
                text,
                vbytes,
                agent_id,
                mtype,
                ts=int(self.clock_now()),
                repos_json=repos_json,
            )
            if self.lifecycle is not None and vec is not None:
                self.lifecycle.on_miss(vec, agent_id)
        except Exception as exc:  # noqa: BLE001 — fail open
            _log.warning(
                "miss record failed (agent=%s): %r — recall unaffected", agent_id, exc
            )
