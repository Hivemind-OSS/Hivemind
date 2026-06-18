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

from hive.domain.fusion import rrf_fuse
from hive.domain.lifecycle import (
    ESTABLISHED, PROVISIONAL, _cosine, is_servable,
)
from hive.domain.models import (
    CONFIDENT, Episode, RecallAssoc, RecallDraft, RecallHit, RecallResult, Scored,
)
from hive.domain.ports import (
    CoAccess, EmbeddingProvider, EpisodeReader, ExposureLedger, LexicalIndex,
    QuarantineReader, VectorIndex,
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
    is byte-identical to the reference. `suppress` iff `entropy_norm > h_frac_max`.
    Empty ⇒ `(False, 0.0, 0.0)`. Any internal failure ⇒ fail-closed
    `(True, 1.0, 0.0)` — a gate that cannot decide MUST abstain, never fabricate.
    """

    def __init__(self, h_frac_max: float, beta: float) -> None:
        if not math.isfinite(h_frac_max):
            raise ValueError("h_frac_max must be finite")
        if not (math.isfinite(beta) and beta > 0.0):
            # a non-positive β inverts/flattens the mass, breaking the abstain
            # decision — fail fast at construction (B1).
            raise ValueError("beta must be finite and > 0")
        self.h_frac_max = float(h_frac_max)
        self.beta = float(beta)
        self._recall = None              # set by from_recall() to the frozen config object

    @classmethod
    def from_recall(cls, recall, beta: float) -> "NormalizedEntropyGate":
        """Construct the gate BY IDENTITY from the single frozen recall-config object
        (CONFIG_DRIFT killed structurally — M11). The floor is read off
        ``recall.H_frac_max`` and the object itself is retained at ``self._recall`` so a
        future second gate cannot fork the float. ``recall`` is duck-typed (any frozen
        object exposing ``H_frac_max``) — the domain stays unaware of ``app.Config``.
        """
        gate = cls(float(recall.H_frac_max), beta)
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
            n_eff = sum(1 for p in mass if p > 0.0)
            if n_eff <= 1:
                entropy_norm = 0.0            # ln(1) guard — no div-by-zero / NaN
            else:
                h = -math.fsum(p * math.log(p) for p in mass if p > 0.0)
                entropy_norm = h / math.log(n_eff)
            entropy_norm = max(0.0, min(1.0, entropy_norm))   # [0,1] clamp
            suppress = entropy_norm > self.h_frac_max
            return (suppress, entropy_norm, top_margin)
        except Exception:                     # noqa: BLE001 — fail-closed by contract
            _log.warning("entropy gate internal failure (n=%s) → fail-closed SUPPRESS",
                         len(sims) if hasattr(sims, "__len__") else "?")
            return (True, 1.0, 0.0)


# ── CV3 version shadowing: a pure post-resolve dedup (default OFF) ─────────────
# Trust rank for the winner pick. Anything outside the two servable states ranks
# lowest (the resolve belt should have dropped it; rank-defensive, not load-bearing).
_TRUST_RANK = {ESTABLISHED: 1, PROVISIONAL: 0}

# ── associative recall (co-access edges) — mechanism CONSTANTS, not config ─────
# Only ONE knob per phase ships (recall.co_access / recall.associations); these three
# are mechanism defaults no operator has evidence to tune. Greppable, one-place-changeable.
_ASSOC_FANOUT = 8        # top-N served hits paired into co-access edges (bounds the I7 upserts to C(8,2)=28, independent of recall_top_n)
_ASSOC_MIN_WEIGHT = 2.0  # Phase-2 edge-surface floor: a single co-occurrence is noise
_ASSOC_TOP_N = 5         # Phase-2 associations-channel cap


def shadow_filter(episodes: Sequence[Episode], *,
                  shadow_tau: float) -> list[Episode]:
    """Within ONE confident shortlist: of any pair with pairwise
    cosine(value_i, value_j) >= shadow_tau, only the WINNER serves — higher
    trust rank (established > provisional: a newer unverified capture must not
    hide a human-vouched row); tie → newer ``ts``; tie → lower ``episode_id``
    (stable). Cosines use the value vectors already on the resolved carrier;
    an absent / non-finite / zero-norm vector is UNDECIDABLE — it never shadows
    and is never shadowed (fail-open = serve both, the status quo). Survivors
    keep input order. Pure, total.  // O(k²·d), k ≤ recall_top_n."""
    if len(episodes) < 2:
        return list(episodes)
    order = sorted(range(len(episodes)),
                   key=lambda i: (-_TRUST_RANK.get(episodes[i].trust, -1),
                                  -int(episodes[i].ts), int(episodes[i].id)))
    kept_idx: list[int] = []
    for i in order:                       # winners first: a kept row shadows losers
        vec = episodes[i].value
        is_shadowed = False
        if vec is not None:
            for j in kept_idx:
                kv = episodes[j].value
                if kv is None:
                    continue
                c = _cosine(vec, kv)      # None = undecidable ⇒ never shadows
                if c is not None and c >= shadow_tau:
                    is_shadowed = True
                    break
        if not is_shadowed:
            kept_idx.append(i)
    keep = set(kept_idx)
    return [ep for i, ep in enumerate(episodes) if i in keep]


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
        lexical_index: Optional[LexicalIndex] = None,
        hybrid_enabled: bool = False,
        shadow_enabled: bool = False,
        shadow_tau: float = 0.95,
        draft_reader: Optional[QuarantineReader] = None,
        drafts_enabled: bool = False,
        draft_tau: float = 0.6,
        quarantine_ttl_s: int = 0,
        co_access: Optional[CoAccess] = None,
        co_access_enabled: bool = False,
        associations_enabled: bool = False,
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
        # the lexical (BM25) channel, fused by RRF inside the CONFIDENT branch only.
        # OFF by default and byte-inert when off: the dense shortlist is used as-is
        # and the port is never called.
        self.lexical_index = lexical_index
        self.hybrid_enabled = bool(hybrid_enabled)
        # CV3 serve-time version shadowing: OFF by default and byte-inert when
        # off (golden-tested). The filter is the LAST transform before the
        # surfacer, so it dedups what is actually served regardless of which
        # upstream channels (dense / RRF / rerank) produced the shortlist.
        self.shadow_enabled = bool(shadow_enabled)
        self.shadow_tau = float(shadow_tau)
        # the self-quarantine resurfacing channel: a seat's OWN unpromoted
        # quarantined captures, returned as labeled drafts on a field STRICTLY
        # separate from `hits`. OFF by default and byte-inert when off (the
        # draft_reader is never touched). It only READS (quarantined_candidates +
        # get_episode) — never records exposure / sets trust / promotes, so the
        # quarantine TTL clock and the demand accounting are untouched.
        self.draft_reader = draft_reader
        self.drafts_enabled = bool(drafts_enabled)
        self.draft_tau = float(draft_tau)
        self.quarantine_ttl_s = int(quarantine_ttl_s)
        # associative recall (co-access edges), two independently-gated phases over
        # ONE CoAccess port + ONE table. WRITE accrual (co_access_enabled): post-
        # exposure on CONFIDENT, upsert pairwise edges among the top _ASSOC_FANOUT
        # served hits — bounded, fail-open, byte-inert OFF. READ surfacing
        # (associations_enabled): the served hits' 1-hop neighbors on the SEPARATE
        # `associations` channel — READ-ONLY (no exposure / no trust mutation; the
        # exposure-resurrection belt-ordering rule), fail-open, () OFF or non-CONFIDENT.
        self.co_access = co_access
        self.co_access_enabled = bool(co_access_enabled)
        self.associations_enabled = bool(associations_enabled)

    def recall(self, query: str, *, agent_id: str) -> RecallResult:
        trace_id = uuid.uuid4().hex

        # encode → search (fail-closed: embedder/index/authority failure ⇒ EMPTY).
        value_q = None
        drafts: tuple[RecallDraft, ...] = ()   # self-quarantine side-channel (off ⇒ ())
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
            # resurface the seat's OWN quarantined drafts off the SAME query vector,
            # BEFORE the empty-index short-circuit so a cold store (only quarantined
            # rows exist yet) still carries them. Computed once; rides every return.
            drafts = self._resurface_drafts(value_q, agent_id)
            if self.index.size() == 0:
                _log.debug("empty index → EMPTY_NO_DATA (agent_id=%s)", agent_id)
                self._note_non_answer(query, value_q, agent_id, "no_match")
                return RecallResult.empty(trace_id, drafts)
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
            return RecallResult.empty(trace_id, drafts)

        # abstain gate (self-fail-closed to SUPPRESS). One-way: suppress returns here.
        suppress, entropy_norm, top_margin = self.gate.evaluate(sims)
        if suppress:
            _log.info("ABSTAIN trace=%s h_norm=%.4f top_margin=%.4f n_cands=%d",
                      trace_id, entropy_norm, top_margin, len(sims))
            self._note_non_answer(query, value_q, agent_id, "abstained")
            return RecallResult.abstain(trace_id, entropy_norm, top_margin, drafts)

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

            # hybrid (off by default): fuse the lexical (BM25) ranking into the
            # shortlist. The lexical call sits INSIDE the confident branch — the
            # gate decided on the dense distribution alone, before any lexical I/O
            # (never-hallucinate unchanged) — and ANY lexical/fusion fault degrades
            # to the dense shortlist, never to EMPTY (the gate already ruled the
            # dense field confident).
            shortlist = dense_ids[:self.recall_top_n]
            if self.hybrid_enabled and self.lexical_index is not None:
                try:
                    lex_ids = [int(eid) for eid, _s in self.lexical_index.search_text(
                        query, self.recall_top_n)]
                    shortlist = rrf_fuse([dense_ids, lex_ids])[:self.recall_top_n]
                except Exception as exc:           # noqa: BLE001 — degrade to dense
                    _log.warning("lexical channel failure (trace=%s): %r — "
                                 "dense-only", trace_id, exc)

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

            if not resolved:
                _log.error("gate passed but 0 candidates resolved → EMPTY_NO_DATA "
                           "(fail-closed) trace=%s", trace_id)
                self._note_non_answer(query, value_q, agent_id, "no_match")
                return RecallResult.empty(trace_id, drafts)

            # CV3 shadowing — at RESOLVE, BEFORE margins/surfacing/exposure: a
            # shadowed row's liveness is never refreshed by the query that hid
            # it (the lapse-resurrection ordering rule, applied forward). The
            # winner always survives, so a non-empty resolved set stays
            # non-empty. A filter fault serves UNFILTERED (status quo, fail-
            # open) — never EMPTY: the gate already ruled this field confident.
            if self.shadow_enabled and len(resolved) > 1:
                try:
                    kept = {e.id for e in shadow_filter(
                        [ep for _s, _m, ep in resolved],
                        shadow_tau=self.shadow_tau)}
                    resolved = [r for r in resolved if r[2].id in kept]
                except Exception as exc:           # noqa: BLE001 — serve both
                    _log.warning("shadow filter failure (trace=%s): %r — "
                                 "serving unfiltered", trace_id, exc)

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
                          polarity=by_eid[s.episode_id][1].polarity)
                for s in scored)
        except Exception as exc:                       # noqa: BLE001 — fail closed
            _log.error("recall surface failure (agent_id=%s): %r → EMPTY_NO_DATA",
                       agent_id, exc)
            self._note_non_answer(query, value_q, agent_id, "no_match")
            return RecallResult.empty(trace_id, drafts)

        # exposure: WHO was served WHAT, with the per-hit margins, refreshing the
        # served rows' liveness — recorded post-resolve in surfaced order.
        self._note_exposure(
            trace_id, [(s.episode_id, by_eid[s.episode_id][0]) for s in scored],
            agent_id)
        # associative recall, post-exposure on the SERVED set (same eids the ledger
        # has): accrue co-access edges (WRITE), then surface their neighbors on the
        # separate `associations` channel (READ). Both guarded + fail-open: a fault in
        # either degrades silently and never touches the trusted return.
        served_eids = [s.episode_id for s in scored]
        self._record_co_access(served_eids, now_belt)
        assoc = self._associations(served_eids, now_belt)
        _log.debug("CONFIDENT trace=%s n_hits=%d top_sim=%.4f",
                   trace_id, len(hits), hits[0].sim)
        return RecallResult(CONFIDENT, trace_id, hits, entropy_norm, top_margin,
                            drafts, associations=assoc)

    # ── self-quarantine resurfacing (READ-ONLY; fail-open) ────────────────────
    def _resurface_drafts(self, value_q, agent_id: str) -> tuple[RecallDraft, ...]:
        """The asking seat's OWN live quarantined captures, scored against the live
        query vector and returned as labeled drafts. READ-ONLY by design — no
        exposure, no trust mutation, no promotion — so the quarantine TTL clock and
        the demand/promotion accounting are untouched (recording exposure here would
        refresh liveness and start survival credit, the learning-suppression this
        must avoid). Seat-scoped to ``agent_id``: you only ever see your own drafts,
        so this can neither pollute a teammate nor manufacture cross-seat demand.
        Wrapped fail-open — ANY fault degrades to () and never breaks recall.
        // O(|live-quarantined|·d); the TTL-bounded set is small."""
        if not self.drafts_enabled or self.draft_reader is None:
            return ()
        try:
            now = int(self.clock_now())
            scored: list[tuple[int, float, int]] = []      # (eid, cosine, ts)
            for eid, vec, writer, ts, _la in self.draft_reader.quarantined_candidates(
                    now=now, quarantine_ttl_s=self.quarantine_ttl_s):
                if writer != agent_id:                     # seat-scoping (the safety model)
                    continue
                c = _cosine(value_q, vec)                  # None ⇒ undecidable ⇒ drop
                if c is None or c < self.draft_tau:        # relevance floor
                    continue
                scored.append((int(eid), float(c), int(ts)))
            # strongest first; newer (higher ts) breaks a cosine tie
            scored.sort(key=lambda t: (-t[1], -t[2]))
            out: list[RecallDraft] = []
            for eid, c, ts in scored[:self.recall_top_n]:
                ep = self.reader.get_episode(eid)          # text resolution only
                if ep is None:
                    continue
                out.append(RecallDraft(episode_id=eid, text=ep.text, sim=c, ts=ts))
            return tuple(out)
        except Exception as exc:                           # noqa: BLE001 — never break recall
            _log.warning("self-quarantine resurface failed (agent=%s): %r — no drafts",
                         agent_id, exc)
            return ()

    # ── associative recall: co-access edge accrual (WRITE) + neighbor read ────
    def _record_co_access(self, eids: Sequence[int], now: int) -> None:
        """Accrue co-access edges among the top ``_ASSOC_FANOUT`` served hits (a
        constant, NOT recall_top_n — this bounds the I7 upsert fan-out to ≤ C(8,2)).
        Same data the exposure ledger already has. Guarded by ``co_access_enabled``
        and wrapped fail-open — a write fault never breaks recall (sibling of
        ``_note_exposure``). // O(_ASSOC_FANOUT²) pairs in ONE adapter tx."""
        if not self.co_access_enabled or self.co_access is None:
            return
        try:
            top = list(eids)[:_ASSOC_FANOUT]
            self.co_access.record_co_access(top, ts=int(now))
        except Exception as exc:                           # noqa: BLE001 — fail open
            _log.warning("co-access record failed: %r — recall unaffected", exc)

    def _associations(self, hit_eids: Sequence[int],
                      now: int) -> tuple[RecallAssoc, ...]:
        """The co-accessed neighbors of the SERVED hits, on the separate
        ``associations`` channel — *related*, never *answers*. Union the 1-hop
        neighbors (weight ≥ ``_ASSOC_MIN_WEIGHT``) over ``hit_eids``, drop ids already
        in ``hit_eids``, dedup keeping MAX weight, sort weight-desc, take
        ``_ASSOC_TOP_N``, resolve text, and DROP any that is missing or no longer
        ``is_servable`` (a neighbor may have decayed — a co-access edge never
        resurrects a row past its lifecycle). READ-ONLY: records NO exposure and NO
        trust mutation (refreshing a neighbor's liveness on a read that merely
        *mentions* it would resurrect a row the lifecycle should drop — the
        belt-ordering invariant). Guarded + ONE try/except → () (never breaks recall).
        // O(Σ deg(hit) + k log k)."""
        if not self.associations_enabled or self.co_access is None:
            return ()
        try:
            hit_set = {int(e) for e in hit_eids}
            best: dict[int, float] = {}                    # neighbor_id → max weight
            for eid in hit_set:
                for nid, w in self.co_access.co_access_neighbors(
                        eid, min_weight=_ASSOC_MIN_WEIGHT):
                    nid = int(nid)
                    if nid in hit_set:                     # already a trusted hit
                        continue
                    if w > best.get(nid, float("-inf")):
                        best[nid] = float(w)
            ranked = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
            out: list[RecallAssoc] = []
            for nid, w in ranked:
                if len(out) >= _ASSOC_TOP_N:
                    break
                ep = self.reader.get_episode(nid)
                if ep is None or not is_servable(
                        status=ep.status, trust=ep.trust,
                        last_active_ts=ep.last_active_ts, now=int(now),
                        provisional_ttl_s=self.provisional_ttl_s):
                    continue                               # decayed/missing ⇒ never resurrect
                out.append(RecallAssoc(episode_id=nid, text=ep.text, weight=w,
                                       trust=ep.trust, ts=ep.ts))
            return tuple(out)
        except Exception as exc:                           # noqa: BLE001 — never break recall
            _log.warning("associations read failed: %r — no associations", exc)
            return ()

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

