"""M04 — the read half of the pure domain core: the never-hallucinate
enforcement point.

`RecallPipeline.recall(query, *, agent_id) -> RecallResult` hides the whole
encode → dense-cosine search → normalized-entropy abstain flow behind one narrow
surface. It owns NO I/O: it composes injected ports (`EmbeddingProvider`,
`VectorIndex`, `EpisodeReader`) and the pure `NormalizedEntropyGate` stage. The
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
from typing import Callable, Optional, Sequence

from hive.domain.conflict import ConflictItem, detect_conflicts, suppression_targets
from hive.domain.lifecycle import is_servable
from hive.domain.models import (
    CONFIDENT, Episode, RecallHit, RecallResult, Scored,
)
from hive.domain.ports import (
    EmbeddingProvider, EpisodeReader, ExposureLedger, VectorIndex,
)
from hive.domain.secret_scan import REDACT, REFUSE

_log = logging.getLogger("hive.recall")


# ── softmax-mass transform (shared by the gate AND the per-hit margins) ───────
# Owning this in ONE helper is what makes D1's "the same softmax masses the gate
# computes" structural rather than a convention the two call sites must agree on.
def _softmax_mass_from_sims(sims: Sequence[float], beta: float) -> list[float]:
    """mass = softmax(beta·sim), max-shifted for numerical stability.  // O(n) time.

    raw_i = exp(beta·sim_i − beta·max_j sim_j); p_i = raw_i / Σ raw.
    FALLBACK-1 (ported, gate_bundle.py:59): a non-finite raw_i (overflow/NaN) is
        floored to 0.0 before summing.
    FALLBACK-2 (ported verbatim, gate_bundle.py:61-66): total <= 0.0 ⇒ uniform
        1/n — a zero-information set reads as maximally uncertain (→ suppress),
        never a spurious peak. Empty ⇒ [].
    """
    n = len(sims)
    if n == 0:
        return []
    # A non-finite INPUT sim is UNDECIDABLE — refuse rather than silently floor it to
    # 0.0 (which would fabricate a confident verdict). The gate's except turns this
    # into a fail-closed SUPPRESS; the honesty contract holds for non-finite floats,
    # not just for raises (AUDIT wf_e5fdbb3c-5f1 #A).
    if any(not math.isfinite(s) for s in sims):
        raise ValueError("non-finite sim is undecidable — fail closed")
    m = max(sims)
    raw: list[float] = []
    for s in sims:
        x = math.exp(beta * s - beta * m)
        if not math.isfinite(x):       # FALLBACK-1
            x = 0.0
        raw.append(x)
    total = math.fsum(raw)
    if total <= 0.0:                   # FALLBACK-2
        return [1.0 / n] * n
    return [r / total for r in raw]


def _recall_margins(masses: Sequence[float]) -> list[float]:
    """Per-hit credit-split weight [D1]. For score-descending masses (rank i):
    margin_i = mass_i − mass_{i+1} for i < N−1 ; margin_{N−1} = mass_{N−1} − 0
    (the last returned hit's "next" mass is 0 ⇒ equals its own mass; never < 0).
    The gate's `top_margin` is exactly the i=0 case.  // O(n) time."""
    n = len(masses)
    if n == 0:
        return []
    out = [masses[i] - masses[i + 1] for i in range(n - 1)]
    out.append(masses[n - 1])  # tail: next mass is 0
    return out


class NormalizedEntropyGate:
    """C4 PORT+EXTEND: abstain iff the recall candidate set is too uncertain.

    `evaluate(sims) -> (suppress, entropy_norm, top_margin)`. The sims→mass step
    is `softmax(beta·sim)` (new code, β in the constructor); everything downstream
    is byte-identical to the reference. `suppress` iff `entropy_norm > h_frac_max`
    OR `top1 < tau_top1` (the top-1 score-gap floor — an additive abstention, inert
    at the `tau_top1=0.0` default since masses ∈ [0,1]). Empty ⇒ `(False, 0.0, 0.0)`.
    Any internal failure ⇒ fail-closed `(True, 1.0, 0.0)` — a gate that cannot decide
    MUST abstain, never fabricate.
    """

    def __init__(self, h_frac_max: float, beta: float, tau_top1: float = 0.0) -> None:
        if not math.isfinite(h_frac_max):
            raise ValueError("h_frac_max must be finite")
        if not (math.isfinite(beta) and beta > 0.0):
            # a non-positive β inverts/flattens the mass, breaking the abstain
            # decision — fail fast at construction (B1).
            raise ValueError("beta must be finite and > 0")
        if not (math.isfinite(tau_top1) and tau_top1 >= 0.0):
            # the top-1 floor is only ever an additive abstention; a non-finite or
            # negative threshold is undecidable — fail fast at construction.
            raise ValueError("tau_top1 must be finite and >= 0")
        self.h_frac_max = float(h_frac_max)
        self.beta = float(beta)
        self.tau_top1 = float(tau_top1)
        self._recall = None              # set by from_recall() to the frozen config object

    @classmethod
    def from_recall(cls, recall, beta: float) -> "NormalizedEntropyGate":
        """Construct the gate BY IDENTITY from the single frozen recall-config object
        (CONFIG_DRIFT killed structurally — M11). The floor is read off
        ``recall.H_frac_max`` (+ the additive ``tau_top1`` top-1 floor, getattr-defaulted so
        the duck-typed contract survives an older config) and the object itself is retained at
        ``self._recall`` so a future second gate cannot fork the float. ``recall`` is duck-typed
        (any frozen object exposing ``H_frac_max``) — the domain stays unaware of ``app.Config``.
        """
        gate = cls(float(recall.H_frac_max), beta,
                   float(getattr(recall, "tau_top1", 0.0)))
        gate._recall = recall
        return gate

    def evaluate(self, sims: Sequence[float]) -> tuple[bool, float, float]:
        try:
            n = len(sims)
            if n == 0:
                return (False, 0.0, 0.0)
            mass = _softmax_mass_from_sims(sims, self.beta)
            mass_sorted = sorted(mass, reverse=True)
            top_margin = mass_sorted[0] - (mass_sorted[1] if n > 1 else 0.0)
            top1 = mass_sorted[0]
            n_eff = sum(1 for p in mass if p > 0.0)
            if n_eff <= 1:
                entropy_norm = 0.0            # ln(1) guard — no div-by-zero / NaN
            else:
                h = -math.fsum(p * math.log(p) for p in mass if p > 0.0)
                entropy_norm = h / math.log(n_eff)
            entropy_norm = max(0.0, min(1.0, entropy_norm))   # [0,1] clamp
            # the floor is the SAME try as the entropy decision: a non-finite sim raises in
            # _softmax_mass_from_sims above (before mass_sorted), so the except still returns
            # the fail-closed SUPPRESS — Law 1 holds. At tau_top1=0.0 the OR term is inert.
            suppress = (entropy_norm > self.h_frac_max) or (top1 < self.tau_top1)
            return (suppress, entropy_norm, top_margin)
        except Exception:                     # noqa: BLE001 — fail-closed by contract
            _log.warning("entropy gate internal failure (n=%s) → fail-closed SUPPRESS",
                         len(sims) if hasattr(sims, "__len__") else "?")
            return (True, 1.0, 0.0)


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
        gate: NormalizedEntropyGate,
        reader: EpisodeReader,
        recall_top_n: int, ledger: ExposureLedger, clock_now: Callable[[], int],
        scanner, provisional_ttl_s: int, lifecycle=None,
        autonomy_enabled: bool = True,
        suppress_conflicts: bool = False,
        conflict_tau: float = 0.80,
        conflict_classifier=None,
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
        # serve-time conflict suppression (post-gate, OFF by default ⇒ byte-inert): when on,
        # the already-confident shortlist is pruned of the strictly-lower-trust member of any
        # detected near-dup/contradiction pair, so a low-trust poison co-served with a vouched
        # fact is dropped WITHOUT touching the abstain decision and WITHOUT retiring the row
        # (resolution stays human). conflict_tau is the near-dup cosine floor (single-owned by
        # ConflictConfig.tau); conflict_classifier is the Phase-2 semantic seam, unused while None.
        self.suppress_conflicts = bool(suppress_conflicts)
        self.conflict_tau = float(conflict_tau)
        self.conflict_classifier = conflict_classifier

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
        suppress, entropy_norm, top_margin = self.gate.evaluate(sims)
        if suppress:
            _log.info("ABSTAIN trace=%s h_norm=%.4f top_margin=%.4f n_cands=%d",
                      trace_id, entropy_norm, top_margin, len(sims))
            self._note_non_answer(query, value_q, agent_id, "abstained")
            return RecallResult.abstain(trace_id, entropy_norm, top_margin)

        # ── CONFIDENT path ────────────────────────────────────────────────────
        # The whole surface step (resolve → margins → surface) fails
        # closed to EMPTY_NO_DATA on ANY internal raise — no collaborator may throw
        # into the caller (AUDIT #C/#D). The masses are the SAME ones the gate
        # computed (shared helper); per-hit margins are taken over the RETURNED hit
        # set so the LAST returned hit's "next" mass is 0 ⇒ its own mass (D1), even
        # under recall_top_n truncation (AUDIT #B).
        try:
            full_masses = _softmax_mass_from_sims(sims, self.gate.beta)

            # the gate's masses + the honest dense cosine, keyed by eid — the ONLY
            # mass/sim source for the resolve step. The dense search spans the full
            # servable index, so every servable id has an entry; a fused id WITHOUT
            # one (dense-cache divergence) is dropped fail-closed at resolve.
            dense_ids = [int(eid) for eid, _sim in candidates]
            mass_by_eid = {dense_ids[i]: full_masses[i] for i in range(len(dense_ids))}
            sim_by_eid = {dense_ids[i]: float(candidates[i][1])
                          for i in range(len(dense_ids))}

            shortlist = dense_ids[:self.recall_top_n]
            # resolve the shortlist to full episodes (weight + text + trust labels),
            # keeping each hit's full-set mass. A hit that cannot resolve OR is no
            # longer servable is dropped HERE — before it can be surfaced or exposed
            # (a TTL-lapsed row in the stale warm index must never get its liveness
            # refreshed by the very read that should have refused it).
            now_belt = int(self.clock_now())
            resolved: list[tuple[Scored, float, Episode]] = []   # (scored, full_mass, ep)
            for eid in shortlist:
                mass = mass_by_eid.get(eid)
                if mass is None:
                    _log.warning("resolve drop (no dense mass) eid=%s trace=%s",
                                 eid, trace_id)
                    continue
                ep: Optional[Episode] = self.reader.get_episode(eid)
                if ep is None or not is_servable(
                        status=ep.status, trust=ep.trust,
                        last_active_ts=ep.last_active_ts, now=now_belt,
                        provisional_ttl_s=self.provisional_ttl_s):
                    _log.warning("resolve drop (missing/unservable) eid=%s trace=%s",
                                 eid, trace_id)
                    continue
                resolved.append(
                    (Scored(eid, float(ep.weight), sim_by_eid[eid]), mass, ep))

            # post-gate conflict suppression (§8.4 dedup/shadow slot): prune the
            # strictly-lower-trust member of any near-dup/contradiction pair among the resolved
            # set. Runs AFTER the gate (never touches the abstain decision) and BEFORE the
            # empty-check + exposure below, so a pruned row is never surfaced or liveness-
            # refreshed (belt-ordering). It only REMOVES — no resurrection. A detector/rule
            # fault raises into this block's enclosing try ⇒ EMPTY_NO_DATA (fail-closed). OFF
            # ⇒ byte-inert (no detector call). conflict_classifier is reserved for Phase 2.
            if self.suppress_conflicts and len(resolved) >= 2:
                citems = [ConflictItem(episode_id=ep.id, vector=ep.value,
                                       polarity=ep.polarity, anchor=ep.anchor,
                                       ts=ep.ts, trust=ep.trust)
                          for _s, _m, ep in resolved]
                drop = suppression_targets(
                    detect_conflicts(citems, tau=self.conflict_tau, top_n=len(citems)),
                    {it.episode_id: it.trust for it in citems})
                if drop:
                    resolved = [r for r in resolved if r[2].id not in drop]

            if not resolved:
                _log.error("gate passed but 0 candidates resolved → EMPTY_NO_DATA "
                           "(fail-closed) trace=%s", trace_id)
                self._note_non_answer(query, value_q, agent_id, "no_match")
                return RecallResult.empty(trace_id)

            # D1 margins over the RETURNED set, taken in MASS-DESCENDING order: the
            # shortlist may be fusion-reordered (non-monotone masses), and a gap over
            # non-monotone masses could go negative. Stable-sort positions by mass
            # desc, take consecutive gaps, map back by position — byte-identical to
            # the dense-order computation when the shortlist is dense-ordered, and
            # every margin is non-negative by construction under fusion. The last
            # (lowest-mass) hit's next mass is 0 ⇒ its own mass.
            order_desc = sorted(range(len(resolved)), key=lambda j: resolved[j][1],
                                reverse=True)
            gaps = _recall_margins([resolved[j][1] for j in order_desc])
            hit_margins = [0.0] * len(resolved)
            for pos, j in enumerate(order_desc):
                hit_margins[j] = gaps[pos]
            by_eid = {s.episode_id: (hit_margins[j], ep)
                      for j, (s, _fm, ep) in enumerate(resolved)}
            scored = [s for s, _fm, _e in resolved]

            # reinforcement is demand/exposure-ONLY by design — there is no utility rerank:
            # the dense+gate order is served as-is. Every served hit carries its trust label
            # + creation ts so the consumer can discount provisional content and order
            # coexisting versions.
            hits = tuple(
                RecallHit(s.episode_id, by_eid[s.episode_id][1].text, s.sim,
                          trust=by_eid[s.episode_id][1].trust,
                          ts=by_eid[s.episode_id][1].ts,
                          polarity=by_eid[s.episode_id][1].polarity,
                          kind=by_eid[s.episode_id][1].kind,
                          anchor=by_eid[s.episode_id][1].anchor)
                for s in scored)
        except Exception as exc:                       # noqa: BLE001 — fail closed
            _log.error("recall surface failure (agent_id=%s): %r → EMPTY_NO_DATA",
                       agent_id, exc)
            self._note_non_answer(query, value_q, agent_id, "no_match")
            return RecallResult.empty(trace_id)

        # exposure: WHO was served WHAT, with the per-hit margins, refreshing the
        # served rows' liveness — recorded post-resolve in surfaced order.
        self._note_exposure(
            trace_id, [(s.episode_id, by_eid[s.episode_id][0]) for s in scored],
            agent_id)
        _log.debug("CONFIDENT trace=%s n_hits=%d top_sim=%.4f",
                   trace_id, len(hits), hits[0].sim)
        return RecallResult(CONFIDENT, trace_id, hits, entropy_norm, top_margin)

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

