"""M04 — the read half of the pure domain core: the never-hallucinate
enforcement point.

`RecallPipeline.recall(query, *, agent_id, agent_ctx) -> RecallResult` hides the
whole encode → dense-cosine search → normalized-entropy abstain → utility-surface
→ trace + exposure-ledger flow behind one narrow surface. It owns NO I/O: it
composes injected ports (`EmbeddingProvider`, `VectorIndex`, `ExposureLedger`,
`EpisodeReader`, `UtilityStore`) and two pure stage objects
(`NormalizedEntropyGate`, `UtilitySurfacer`).

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

from hive.domain.models import (
    CONFIDENT, AgentContext, Episode, RecallHit, RecallResult, Scored,
)
from hive.domain.ports import (
    EmbeddingProvider, EpisodeReader, ExposureLedger, UtilityStore, VectorIndex,
)
from hive.domain.surfacer import UtilitySurfacer

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
        (CONFIG_DRIFT killed structurally — M11 §2.3). The floor is read off
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


# ── query → family (A2): the SOLE owner of the live query's family_scope ──────
def _resolve_query_family(ctx: AgentContext) -> str:
    """Byte-identical grammar to the producer's link-time family_scope (§11):
    "<remote>|<lang>|<workflow>".  // O(1). Empty axes collapse to "*"/"general"
    so an unscoped query selects the cross-repo aggregate slice that nothing
    credits ⇒ utility_map returns {} ⇒ surfacer is identity (safe degradation)."""
    return f"{ctx.repo_remote or '*'}|{ctx.language or '*'}|{ctx.workflow or 'general'}"


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
        gate: NormalizedEntropyGate, surfacer: UtilitySurfacer,
        ledger: ExposureLedger, reader: EpisodeReader, utility_store: UtilityStore,
        recall_top_n: int, now: Callable[[], int],
    ) -> None:
        self.embedder = embedder
        self.index = index
        self.gate = gate
        self.surfacer = surfacer
        self.ledger = ledger
        self.reader = reader
        self.utility_store = utility_store
        self.recall_top_n = int(recall_top_n)
        self.now = now

    def recall(self, query: str, *, agent_id: str,
               agent_ctx: AgentContext) -> RecallResult:
        trace_id = uuid.uuid4().hex
        fam = _resolve_query_family(agent_ctx)

        # encode → search (fail-closed: embedder/index/authority failure ⇒ EMPTY).
        try:
            if not self.index.is_authoritative():
                _log.error("non-authoritative index rejected (never-flip guard) "
                           "→ EMPTY_NO_DATA (agent_id=%s)", agent_id)
                return RecallResult.empty(trace_id)
            if self.index.size() == 0:
                _log.debug("empty index → EMPTY_NO_DATA (agent_id=%s)", agent_id)
                return RecallResult.empty(trace_id)
            value_q = self.embedder.encode(query)
            # search the FULL approved set so the gate sees the whole distribution;
            # recall_top_n only truncates hits, NEVER the abstain decision (§4.2).
            candidates = self.index.search(value_q, self.index.size())
            # coerce INSIDE the try: a contract-violating adapter row (NULL cosine,
            # wrong arity) must fail-closed to EMPTY, never raise (AUDIT #D).
            sims = [float(sim) for _eid, sim in candidates]
        except Exception as exc:                       # noqa: BLE001 — fail closed
            _log.error("recall encode/search failure (agent_id=%s): %r "
                       "→ EMPTY_NO_DATA", agent_id, exc)
            return RecallResult.empty(trace_id)

        # abstain gate (self-fail-closed to SUPPRESS). One-way: suppress returns here.
        suppress, entropy_norm, top_margin = self.gate.evaluate(sims)
        if suppress:
            _log.info("ABSTAIN trace=%s h_norm=%.4f top_margin=%.4f n_cands=%d",
                      trace_id, entropy_norm, top_margin, len(sims))
            return RecallResult.abstain(trace_id, entropy_norm, top_margin)

        # ── CONFIDENT path ────────────────────────────────────────────────────
        # The whole surface step (resolve → margins → surface → exposure) fails
        # closed to EMPTY_NO_DATA on ANY internal raise — no collaborator may throw
        # into the caller (AUDIT #C/#D). The masses are the SAME ones the gate
        # computed (shared helper); per-hit margins are taken over the RETURNED hit
        # set so the LAST returned hit's "next" mass is 0 ⇒ its own mass (D1), even
        # under recall_top_n truncation (AUDIT #B).
        try:
            full_masses = _softmax_mass_from_sims(sims, self.gate.beta)
            utility_map = self._utility_map(fam)

            # resolve the top_n candidates to full episodes (weight + text), keeping
            # each hit's full-set mass. A hit that cannot resolve is dropped (logged).
            resolved: list[tuple[Scored, float, str]] = []   # (scored, full_mass, text)
            for rank, (eid, sim) in enumerate(candidates[:self.recall_top_n]):
                ep: Optional[Episode] = self.reader.get_episode(int(eid))
                if ep is None:
                    _log.warning("resolve miss eid=%s (dropped) trace=%s", eid, trace_id)
                    continue
                resolved.append(
                    (Scored(int(eid), float(ep.weight), float(sim)),
                     full_masses[rank], ep.text))

            if not resolved:
                _log.error("gate passed but 0 candidates resolved → EMPTY_NO_DATA "
                           "(fail-closed) trace=%s", trace_id)
                return RecallResult.empty(trace_id)

            # D1 margins over the RETURNED set (score order): margin_j = mass_j −
            # mass_{j+1}; the last returned hit's next mass is 0 ⇒ its own mass.
            hit_margins = _recall_margins([fm for _s, fm, _t in resolved])
            by_eid = {s.episode_id: (hit_margins[j], text)
                      for j, (s, _fm, text) in enumerate(resolved)}
            scored = [s for s, _fm, _t in resolved]

            ordered = self.surfacer.order(scored, utility_map, family_scope=fam)
            hits = tuple(RecallHit(s.episode_id, by_eid[s.episode_id][1], s.sim)
                         for s in ordered)
            rows = [(s.episode_id, by_eid[s.episode_id][0]) for s in ordered]
        except Exception as exc:                       # noqa: BLE001 — fail closed
            _log.error("recall surface failure (agent_id=%s): %r → EMPTY_NO_DATA",
                       agent_id, exc)
            return RecallResult.empty(trace_id)

        # move-#6 exposure capture — fire-and-forget: a ledger failure logs WARN and
        # the recall result is STILL returned (telemetry never endangers the hot path).
        try:
            self.ledger.record_exposure(trace_id, rows, self.now())
        except Exception as exc:                       # noqa: BLE001
            _log.warning("ledger.record_exposure dropped (trace=%s, n_rows=%d): %r "
                         "— recall preserved", trace_id, len(rows), exc)

        _log.debug("CONFIDENT trace=%s n_hits=%d top_sim=%.4f",
                   trace_id, len(hits), hits[0].sim)
        return RecallResult(CONFIDENT, trace_id, hits, entropy_norm, top_margin)

    def _utility_map(self, fam: str) -> dict:
        """utility_map for the live family; a store failure degrades to identity ({})."""
        try:
            return self.utility_store.utility_map(family_scope=fam, confident_only=True)
        except Exception as exc:                       # noqa: BLE001
            _log.warning("utility_map failure (fam=%s): %r → identity", fam, exc)
            return {}
