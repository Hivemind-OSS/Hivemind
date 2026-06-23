"""M04 — the read half of the pure domain core: the never-hallucinate
enforcement point.

`RecallPipeline.recall(query, *, agent_id) -> RecallResult` hides the whole
encode → dense-cosine search → absolute-relevance abstain flow behind one narrow
surface. It owns NO I/O: it composes injected ports (`EmbeddingProvider`,
`VectorIndex`, `EpisodeReader`) and the pure `AbsoluteRelevanceGate` stage. The
dense+gate order is served as-is — there is no utility rerank (reinforcement is
demand/exposure-only).

PURE: stdlib `math` + `uuid` only. The purity gate (tests/test_purity.py) forbids
sqlite3 | torch | subprocess | os | git | time here — so the whole read path is
fake-testable in milliseconds.

The central invariant — **abstain-no-resurrect** — is made STRUCTURAL: once the
gate suppresses, `recall()` returns before the resolve/exposure steps, and the
frozen `RecallResult.__post_init__` makes a non-CONFIDENT-result-carrying-hits
state unconstructable. There is no code path that can repopulate `hits`.
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from hive.domain.conflict import (
    ConflictItem, _cosine, detect_conflicts, suppression_targets,
)
from hive.domain.lifecycle import is_servable
from hive.domain.models import (
    CONFIDENT, Episode, RecallHit, RecallResult, Scored,
)
from hive.domain.ports import (
    EmbeddingProvider, EpisodeReader, ExposureLedger, VectorIndex,
)
from hive.domain.secret_scan import REDACT, REFUSE

_log = logging.getLogger("hive.recall")


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
        self._recall = None              # set by from_recall() to the frozen config object

    @classmethod
    def from_recall(cls, recall) -> "AbsoluteRelevanceGate":
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
            for s in sims:                       # non-finite guard FIRST (Law 1)
                if not math.isfinite(s):
                    raise ValueError("non-finite sim is undecidable — fail closed")
            if not sims:
                return GateVerdict(True, -1.0, 0)
            top_cos = max(-1.0, min(1.0, max(sims)))   # clamp float-error drift into [-1, 1]
            n_relevant = sum(1 for s in sims if s >= self.tau_serve)
            return GateVerdict(n_relevant < self.k_min, top_cos, n_relevant)
        except Exception:                        # noqa: BLE001 — fail-closed by contract
            _log.warning("relevance gate internal failure (n=%s) → fail-closed SUPPRESS",
                         len(sims) if hasattr(sims, "__len__") else "?")
            return GateVerdict(True, -1.0, 0)


def select_served(items: Sequence[tuple[Scored, Episode]], *, dup_tau: float,
                  top_n: int) -> list[tuple[Scored, Episode]]:
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
    citems = [ConflictItem(episode_id=ep.id, vector=ep.value, polarity=ep.polarity,
                           anchor=ep.anchor, ts=ep.ts, trust=ep.trust)
              for _s, ep in items]
    drop = suppression_targets(
        detect_conflicts(citems, tau=dup_tau, top_n=len(citems)),
        {it.episode_id: it.trust for it in citems})
    kept: list[tuple[Scored, Episode]] = []
    for s, ep in items:                          # items already sim-desc
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
    carry hits.  // recall(): O(N·d) search + O(N log N) sort, N = approved count.
    """

    def __init__(
        self, *, embedder: EmbeddingProvider, index: VectorIndex,
        gate: AbsoluteRelevanceGate,
        reader: EpisodeReader,
        recall_top_n: int, ledger: ExposureLedger, clock_now: Callable[[], int],
        scanner, provisional_ttl_s: int, lifecycle=None,
        autonomy_enabled: bool = True,
        overscan: int = 3,
        select: bool = True,
        dup_tau: float = 0.80,
        conflict_enabled: bool = False,
        conflict_top_n: int = 10,
    ) -> None:
        self.embedder = embedder
        self.index = index
        self.gate = gate
        self.reader = reader
        self.recall_top_n = int(recall_top_n)
        # the recall side-channel (REQUIRED, never silently absent): exposure on
        # CONFIDENT, a recorded miss on every non-answer. Writes are fail-open for
        # the read — a ledger fault never breaks recall — and fully inert when
        # autonomy is disabled (byte-stable read path).
        self.ledger = ledger
        self.clock_now = clock_now
        self.scanner = scanner            # SecretScanner: the miss-text secret floor
        # the pipeline-level servability belt: a non-servable row is dropped at
        # RESOLVE, before it can be surfaced or exposed — exposure refreshes
        # liveness, so exposing a TTL-lapsed row would resurrect it. The mcp
        # handler re-checks per hit as redundancy; both read the ONE is_servable.
        self.provisional_ttl_s = int(provisional_ttl_s)
        self.lifecycle = lifecycle        # LifecycleService or None: on_miss trigger
        self.autonomy_enabled = bool(autonomy_enabled)
        # post-gate decorrelated selection (default ON ⇒ off-path select=False is byte-identical
        # naive truncation). When on, an OVERSCAN pool (recall_top_n*overscan) is resolved and
        # select_served picks the served set by trust-dominance (drop a strictly-lower-trust
        # near-dup twin) THEN MMR-decorrelation (collapse cosine echoes) — never touching the
        # abstain decision, never retiring a row (resolution stays human). dup_tau is the near-dup
        # cosine floor (single-owned by ConflictConfig.tau).
        self.overscan = int(overscan)
        self.select = bool(select)
        self.dup_tau = float(dup_tau)
        # recall-time conflict carrier: detect near-dup/contradiction pairs over the PRE-select
        # resolved field (so a near-dup the decorrelated serve dropped is still surfaced for human
        # resolution). ids-only on RecallResult.conflicts; fail-OPEN side-channel (Law 6). OFF ⇒
        # never computed. Shares dup_tau (=ConflictConfig.tau) as the near-dup floor.
        self.conflict_enabled = bool(conflict_enabled)
        self.conflict_top_n = int(conflict_top_n)

    def recall(self, query: str, *, agent_id: str) -> RecallResult:
        trace_id = uuid.uuid4().hex

        # encode → search (fail-closed: embedder/index/authority failure ⇒ EMPTY).
        value_q = None
        try:
            if not self.index.is_authoritative():
                _log.error("non-authoritative index rejected (never-flip guard) "
                           "→ EMPTY_NO_DATA (agent_id=%s)", agent_id)
                self._note_non_answer(query, None, agent_id, "no_match")
                return RecallResult.empty(trace_id)
            # encode BEFORE the empty-index short-circuit: a cold store's misses
            # must still carry their query vector or demand can never accumulate
            # and no quarantined capture would ever promote (cold-start deadlock).
            value_q = self.embedder.encode(query)
            if self.index.size() == 0:
                _log.debug("empty index → EMPTY_NO_DATA (agent_id=%s)", agent_id)
                self._note_non_answer(query, value_q, agent_id, "no_match")
                return RecallResult.empty(trace_id)
            # search the FULL approved set so the gate sees the whole distribution;
            # recall_top_n only truncates hits, NEVER the abstain decision.
            candidates = self.index.search(value_q, self.index.size())
            # coerce INSIDE the try: a contract-violating adapter row (NULL cosine,
            # wrong arity) must fail-closed to EMPTY, never raise (AUDIT #D).
            sims = [float(sim) for _eid, sim in candidates]
        except Exception as exc:                       # noqa: BLE001 — fail closed
            _log.error("recall encode/search failure (agent_id=%s): %r "
                       "→ EMPTY_NO_DATA", agent_id, exc)
            self._note_non_answer(query, value_q, agent_id, "no_match")
            return RecallResult.empty(trace_id)

        # abstain gate (self-fail-closed to SUPPRESS). One-way: suppress returns here.
        verdict = self.gate.evaluate(sims)
        if verdict.suppress:
            _log.info("ABSTAIN trace=%s top_cos=%.4f n_relevant=%d n_cands=%d",
                      trace_id, verdict.top_cos, verdict.n_relevant, len(sims))
            self._note_non_answer(query, value_q, agent_id, "abstained")
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
            sim_by_eid = {dense_ids[i]: float(candidates[i][1])
                          for i in range(len(dense_ids))}

            # overscan the resolve pool when selecting (recall_top_n*overscan), so a query
            # whose top dense ranks are unservable can still backfill real servable hits from
            # deeper in the field; with select OFF the pool is exactly recall_top_n (byte-
            # identical naive truncation — no backfill).
            pool = self.recall_top_n * self.overscan if self.select else self.recall_top_n
            shortlist = dense_ids[:pool]
            # resolve the shortlist to full episodes (weight + text + trust labels). A hit
            # that cannot resolve OR is no longer servable is dropped HERE — before it can
            # be surfaced or exposed (a TTL-lapsed row in the stale warm index must never
            # get its liveness refreshed by the very read that should have refused it).
            now_belt = int(self.clock_now())
            resolved: list[tuple[Scored, Episode]] = []   # (scored, ep), dense order
            for eid in shortlist:
                ep: Optional[Episode] = self.reader.get_episode(eid)
                if ep is None or not is_servable(
                        status=ep.status, trust=ep.trust,
                        last_active_ts=ep.last_active_ts, now=now_belt,
                        provisional_ttl_s=self.provisional_ttl_s):
                    _log.warning("resolve drop (missing/unservable) eid=%s trace=%s",
                                 eid, trace_id)
                    continue
                resolved.append((Scored(eid, float(ep.weight), sim_by_eid[eid]), ep))

            # decorrelated selection (§8.4 dedup/shadow slot, default ON): trust-dominance drop
            # THEN MMR over the servable-resolved pool, capped at recall_top_n. Runs AFTER the
            # gate (never touches the abstain decision) and BEFORE the empty-check + exposure
            # below, so a dropped row is never surfaced or liveness-refreshed (belt-ordering). It
            # only REMOVES — no resurrection. A detector fault raises into this block's enclosing
            # try ⇒ EMPTY_NO_DATA (fail-closed). OFF ⇒ byte-identical naive truncation.
            if self.select:
                selected = select_served(resolved, dup_tau=self.dup_tau,
                                         top_n=self.recall_top_n)
            else:
                selected = resolved

            if not selected:
                _log.error("gate passed but 0 candidates resolved → EMPTY_NO_DATA "
                           "(fail-closed) trace=%s", trace_id)
                self._note_non_answer(query, value_q, agent_id, "no_match")
                return RecallResult.empty(trace_id)

            # recall-time conflict carrier: detect over the PRE-select resolved field so a near-dup
            # the decorrelated serve dropped is still surfaced for human resolution. FAIL-OPEN
            # (its own try, NOT the surface try): a carrier fault degrades to () — a side-channel
            # must never break the read (Law 6). OFF / <2 servable rows ⇒ ().
            conflicts: tuple = ()
            if self.conflict_enabled and len(resolved) >= 2:
                try:
                    citems = [ConflictItem(episode_id=ep.id, vector=ep.value,
                                           polarity=ep.polarity, anchor=ep.anchor,
                                           ts=ep.ts, trust=ep.trust)
                              for _s, ep in resolved]
                    conflicts = detect_conflicts(citems, tau=self.dup_tau,
                                                 top_n=self.conflict_top_n)
                except Exception as exc:               # noqa: BLE001 — fail-open side-channel
                    _log.warning("recall conflict carrier failed (trace=%s): %r — read unaffected",
                                 trace_id, exc)
                    conflicts = ()

            scored = [s for s, _ep in selected]
            # reinforcement is demand/exposure-ONLY by design — there is no utility rerank:
            # the dense+gate order is served as-is. Every served hit carries its trust label
            # + creation ts so the consumer can discount provisional content and order
            # coexisting versions.
            hits = tuple(
                RecallHit(s.episode_id, ep.text, s.sim, trust=ep.trust, ts=ep.ts,
                          polarity=ep.polarity, kind=ep.kind, anchor=ep.anchor)
                for s, ep in selected)
        except Exception as exc:                       # noqa: BLE001 — fail closed
            _log.error("recall surface failure (agent_id=%s): %r → EMPTY_NO_DATA",
                       agent_id, exc)
            self._note_non_answer(query, value_q, agent_id, "no_match")
            return RecallResult.empty(trace_id)

        # exposure: WHO was served WHAT, with the raw cosine weight, refreshing the served
        # rows' liveness — recorded post-resolve in surfaced order.
        self._note_exposure(
            trace_id, [(s.episode_id, s.sim) for s in scored], agent_id)
        _log.debug("CONFIDENT trace=%s n_hits=%d top_sim=%.4f",
                   trace_id, len(hits), hits[0].sim)
        return RecallResult(CONFIDENT, trace_id, hits, verdict.top_cos, conflicts=conflicts)

    # ── the recall side-channel (exposure + demand), fail-open for the read ───
    def _note_exposure(self, trace_id: str, items: list, agent_id: str) -> None:
        """Persist the served set (liveness refresh rides the same tx in the
        adapter). A fault is logged and swallowed — never breaks the read. Inert
        when autonomy is disabled (no new rows on the read path)."""
        if not self.autonomy_enabled:
            return
        try:
            self.ledger.record_exposure(trace_id, items, agent_id=agent_id,
                                        ts=int(self.clock_now()))
        except Exception as exc:                       # noqa: BLE001 — fail open
            _log.warning("exposure record failed (trace=%s): %r — recall unaffected",
                         trace_id, exc)

    def _note_non_answer(self, query: str, vector, agent_id: str,
                         miss_type: str) -> None:
        """Record the non-answer (the demand signal), secret-scanned BEFORE
        persistence: REFUSE ⇒ no content survives (empty text, no vector,
        type='secret_refused' — counts in telemetry, can never drive promotion);
        REDACT ⇒ masked text + a vector re-encoded FROM the masked text (never the
        raw query's). Then the synchronous on_miss promotion trigger (the miss is
        recorded FIRST so the trigger's window includes it). Fail-open; inert when
        autonomy is disabled."""
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
            self.ledger.record_miss(text, vbytes, agent_id, mtype,
                                    ts=int(self.clock_now()))
            if self.lifecycle is not None and vec is not None:
                self.lifecycle.on_miss(vec, agent_id)
        except Exception as exc:                       # noqa: BLE001 — fail open
            _log.warning("miss record failed (agent=%s): %r — recall unaffected",
                         agent_id, exc)

