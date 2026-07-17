"""The mechanical memory-lifecycle core: trust states, the ONE servability
predicate, and lazy TTL decay.

A captured memory is born ``quarantined`` (embedded but structurally unservable);
measured demand promotes it to ``provisional`` (served WITH its label); a human
``hive_write`` lands ``established``; supersession or TTL decay retires rows to
``deprecated``. ``is_servable`` is the single source every serving layer
re-evaluates (store scan predicate, index membership sync, per-hit recall belt);
``decayed`` is the lazy death rule the sweep materializes.

Promotion is MECHANICAL DEMAND, the only path out of quarantine in this build:
a quarantined memory matching enough recall misses, from at least one identity
other than its writer (the structural anti-gaming clause — the writer cannot
manufacture both supply and demand), with no servable row already answering the
need (the near-dup competitor veto), auto-promotes to provisional. Promotion
means "demanded, unique, and not self-demanded" — NOT "true"; the guards against
wrongness are the trust label on every served hit, cheap human supersession, and
decay.

PURE: stdlib + numpy only. The purity gate (tests/test_purity.py) forbids
sqlite3 | torch | subprocess | os | git | time imports anywhere in hive/domain/.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import numpy as np  # permitted in domain (not in the forbidden I/O set)

from hive.domain.anomaly import cluster_anomaly
from hive.domain.evidence_kinds import EK_OUTCOME_VERIFIED_HELPED, EK_PROMOTE

_log = logging.getLogger("hive.lifecycle")

QUARANTINED, PROVISIONAL, ESTABLISHED, DEPRECATED = (
    "quarantined", "provisional", "established", "deprecated")
TRUST_STATES = (QUARANTINED, PROVISIONAL, ESTABLISHED, DEPRECATED)


@dataclass(frozen=True, slots=True)
class MissRow:
    """One recorded recall miss inside the demand window — the carrier the store
    returns and the demand rule consumes. ``agent_id`` is the asker's authenticated
    token label (the anti-gaming key); ``vector`` is the query embedding (re-encoded
    from the REDACTED text when the scanner masked; never present for refused)."""
    vector: "np.ndarray"
    agent_id: str
    ts: int


def is_servable(*, status: str, trust: str, last_active_ts: int,
                now: int, provisional_ttl_s: int) -> bool:
    """THE single servability rule. Used by the store's servable scan (index
    rebuild), the promotion/demotion index sync, and the mcp_server recall belt.

    servable ≡ status='approved' AND (
        trust='established'
        OR (trust='provisional' AND last_active_ts > now − provisional_ttl_s))

    The provisional freshness compare is STRICT — at exactly the TTL boundary the
    row is no longer served (fail-closed). An unknown trust label is NOT servable.
    Pure, total, never raises.  // O(1)."""
    if status != "approved":
        return False
    if trust == ESTABLISHED:
        return True
    if trust == PROVISIONAL:
        return last_active_ts > now - provisional_ttl_s
    return False


def decayed(*, trust: str, last_active_ts: int, created_ts: int, now: int,
            quarantine_ttl_s: int, provisional_ttl_s: int) -> bool:
    """Lazy death rule (the sweep materializes what this reads):

    quarantined  — dead iff ``now − max(created_ts, last_active_ts) > quarantine_ttl``
                   (any touch refreshes the clock).
    provisional  — dead iff ``now − last_active_ts > provisional_ttl`` (exposure
                   refreshes it; promotion stamps it, so a fresh promotion is never
                   instantly dead).
    established / deprecated — never (supersession is established's only retirement).

    Both compares STRICT, mirroring ``is_servable``: at exactly the boundary a
    provisional row is unservable-but-not-yet-swept (a benign one-tick gap in the
    fail-closed direction). Pure, total, never raises.  // O(1)."""
    if trust == QUARANTINED:
        return now - max(created_ts, last_active_ts) > quarantine_ttl_s
    if trust == PROVISIONAL:
        return now - last_active_ts > provisional_ttl_s
    return False


# ── demand-promotion: the ONLY path out of quarantine ──────────────────────────

def _cosine(a, b) -> Optional[float]:
    """True cosine in float64, or None when UNDECIDABLE — non-finite components,
    shape mismatch, or a zero norm. None is the fail-closed signal: an undecidable
    similarity must never count as demand.  // O(d)."""
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    if va.shape != vb.shape:
        return None
    if not (np.all(np.isfinite(va)) and np.all(np.isfinite(vb))):
        return None
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na <= 0.0 or nb <= 0.0:
        return None
    return float(np.dot(va, vb) / (na * nb))


def _demand_independence(matched: Sequence["MissRow"]) -> tuple[float, float]:
    """(rho_bar, n_eff) — a decorrelated-vote measure over the matched-miss QUERY vectors.

    Counting identities is not measuring independence: many SESSIONS can ask near-identical
    questions (correlated demand). ``rho_bar`` = mean pairwise cosine of the matched vectors
    (the average correlation among the asks); ``D_eff = 1 + (k-1)*max(0, rho_bar)`` is the
    design effect of correlated votes; ``n_eff = k / D_eff`` is the effective number of
    INDEPENDENT votes — k identical asks ⇒ n_eff≈1, k orthogonal asks ⇒ n_eff≈k. k<2 or no
    decidable pair ⇒ ``(0.0, float(k))`` (under-claim — not flagged). Undecidable pairs are
    skipped. PURE, total; returns BUILTIN floats (never np.float64 — json.dumps would
    TypeError). rho_bar is clamped into [-1, 1]; the design effect floors rho_bar at 0 so
    n_eff can never exceed k.  // O(k^2 d)."""
    k = len(matched)
    if k < 2:
        return (0.0, float(k))
    cosines: list[float] = []
    for i in range(k):
        for j in range(i + 1, k):
            c = _cosine(matched[i].vector, matched[j].vector)
            if c is None:                          # undecidable pair ⇒ skipped, never counted
                continue
            cosines.append(max(-1.0, min(1.0, c)))
    if not cosines:
        return (0.0, float(k))
    rho_bar = max(-1.0, min(1.0, float(sum(cosines) / len(cosines))))
    d_eff = 1.0 + (k - 1) * max(0.0, rho_bar)      # max(0,·) ⇒ n_eff <= k by construction
    return (rho_bar, float(k) / d_eff)


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """The machine-readable verdict on one quarantined candidate — its fields go
    verbatim into the ``promote`` audit payload. ``reason`` names the FIRST failed
    clause in rule order (insufficient_demand → self_demand → competitor_answers),
    ``non_finite`` for undecidable inputs, ``demand`` on promotion. ``rho_bar`` / ``n_eff``
    are the decorrelated-demand stamp (computed on the promote path only, value-INDEPENDENT
    of the verdict — they record HOW independent the demand was, never gate it); both keep the
    under-claiming 0.0 on every non-promote path."""
    promote: bool
    n_misses: int                 # matched misses inside the window
    n_other_identities: int       # matched misses from identities ≠ the writer
    competitor_sim: float         # top-1 cosine against the SERVABLE index
    reason: str
    rho_bar: float = 0.0          # mean pairwise cosine of the matched-miss vectors
    n_eff: float = 0.0            # effective independent votes (k / design-effect)


class DemandRule:
    """promote IFF, over the window misses:
        matched = [m for m in misses if cos(m.vector, candidate) >= demand_tau]
        len(matched) >= demand_m                                  (enough demand)
        AND any(m.agent_id != candidate_writer for m in matched)  (anti-gaming:
            demand consisting solely of the writer's own misses never promotes)
        AND competitor_top_sim < competitor_tau                   (a servable row
            this close already answers it — near-dup pile-up prevention)
    Pure, total, never raises; ANY non-finite input ⇒ promote=False (fail-closed,
    NaN/inf in any position).  // O(|misses|·d).

    The identity-diversity clause is the SOLE anti-gaming rule (solo and team share it):
    a lone agent cannot manufacture both supply and demand, but ≥2 agent-sessions can —
    independent of how many humans direct them. There is no elapsed-span bypass."""

    def __init__(self, *, demand_m: int, demand_tau: float,
                 competitor_tau: float) -> None:
        if int(demand_m) < 1:
            raise ValueError(f"demand_m must be >= 1 (got {demand_m})")
        for name, v in (("demand_tau", demand_tau), ("competitor_tau", competitor_tau)):
            if not (math.isfinite(v) and 0.0 < v <= 1.0):
                raise ValueError(f"{name} must be finite in (0, 1] (got {v})")
        self.demand_m = int(demand_m)
        self.demand_tau = float(demand_tau)
        self.competitor_tau = float(competitor_tau)

    def decide(self, *, candidate_vec: "np.ndarray", candidate_writer: str,
               misses: Sequence[MissRow],
               competitor_top_sim: float) -> PromotionDecision:
        try:
            comp = float(competitor_top_sim)
            if not math.isfinite(comp):
                return PromotionDecision(False, 0, 0, 0.0, "non_finite")
            matched: list[MissRow] = []
            for m in misses:
                c = _cosine(m.vector, candidate_vec)
                if c is None:                      # undecidable input ⇒ fail closed
                    return PromotionDecision(False, 0, 0, comp, "non_finite")
                if c >= self.demand_tau:
                    matched.append(m)
            n_other = sum(1 for m in matched if m.agent_id != candidate_writer)
            if len(matched) < self.demand_m:
                return PromotionDecision(False, len(matched), n_other, comp,
                                         "insufficient_demand")
            if n_other == 0:                       # the always-on anti-gaming floor (SOLE rule)
                return PromotionDecision(False, len(matched), 0, comp, "self_demand")
            if comp >= self.competitor_tau:
                return PromotionDecision(False, len(matched), n_other, comp,
                                         "competitor_answers")
            # promote path ONLY (after every gating clause has had its chance to return) —
            # so the measure is value-INDEPENDENT of the verdict (Law 3): it records HOW
            # independent the demand was, it never decides whether to promote.
            rho_bar, n_eff = _demand_independence(matched)
            return PromotionDecision(True, len(matched), n_other, comp, "demand",
                                     rho_bar=rho_bar, n_eff=n_eff)
        except Exception:                          # noqa: BLE001 — total by contract
            _log.warning("demand rule internal failure → fail-closed no-promote",
                         exc_info=True)
            return PromotionDecision(False, 0, 0, 0.0, "error")


def verified_win_decision(*, has_verified_win: bool, competitor_top_sim: float,
                          competitor_tau: float) -> PromotionDecision:
    """The verified-win rung's pure verdict — the SECOND mechanical path out of
    quarantine, reachable only when the operator wires a verified reader (default
    OFF). Promote IFF a SHA-bound ``outcome_verified_helped`` audit exists AND no
    servable competitor already answers (the veto is retained, inclusive at the
    threshold — DemandRule parity). Demand and identity-diversity are NOT required:
    ``evidence_events`` is server-written only and the verified row's writer is the
    operator-run census ingest, so a memory's writer structurally cannot manufacture
    the corroboration. Non-finite competitor sim ⇒ fail-closed. Pure, total, never
    raises.  // O(1)."""
    try:
        comp = float(competitor_top_sim)
    except (TypeError, ValueError):
        return PromotionDecision(False, 0, 0, 0.0, "non_finite")
    if not math.isfinite(comp):
        return PromotionDecision(False, 0, 0, 0.0, "non_finite")
    if not has_verified_win:
        return PromotionDecision(False, 0, 0, comp, "no_verified_win")
    if comp >= competitor_tau:
        return PromotionDecision(False, 0, 0, comp, "competitor_answers")
    return PromotionDecision(True, 0, 0, comp, "verified_win")


class LifecycleService:
    """The promotion/decay driver, composed from duck-typed store + index handles,
    the pure DemandRule, and an injected clock. Trigger sites are SYNCHRONOUS at
    the two moments demand can change — a capture arriving (check it against the
    existing miss window) and a miss arriving (scan live quarantined candidates).
    Every entry point is fail-open for its CALLER (a promotion fault must never
    break the capture/recall that triggered it) and inert when disabled.

    ``verified_reader`` (default ``None``) wires the verified-win rung: when the
    demand rule does NOT promote a candidate, a SHA-bound verified win promotes it
    instead (competitor veto retained). ``None`` makes the rung UNREACHABLE — the
    read is never consulted, every envelope byte-identical (the opt-OUT form —
    the shipped config default is ON: ``HIVE_AUTONOMY__VERIFIED_PROMOTION``)."""

    def __init__(self, *, store, index, rule: DemandRule, now: Callable[[], int],
                 demand_window_s: int, quarantine_ttl_s: int, provisional_ttl_s: int,
                 enabled: bool = True, anomaly_tau: float = 0.95,
                 anomaly_min_cluster: int = 5, verified_reader=None) -> None:
        self._store = store
        self._index = index
        self._rule = rule
        self._now = now
        self._window_s = int(demand_window_s)
        self._q_ttl = int(quarantine_ttl_s)
        self._p_ttl = int(provisional_ttl_s)
        self.enabled = bool(enabled)
        # promotion-time embedding-cluster anomaly flag thresholds (detection-only, advisory)
        self._anomaly_tau = float(anomaly_tau)
        self._anomaly_min_cluster = int(anomaly_min_cluster)
        self._verified_reader = verified_reader      # None ⇒ the rung is unreachable

    # ── trigger: a capture arrived — does existing demand already want it? ────
    def on_capture(self, episode_id: int) -> Optional[PromotionDecision]:
        if not self.enabled:
            return None
        try:
            t = self._now()
            cands = {c[0]: c for c in self._store.quarantined_candidates(
                now=t, quarantine_ttl_s=self._q_ttl)}
            cand = cands.get(int(episode_id))
            if cand is None:                       # unknown / not quarantined / decayed
                return None
            _eid, vec, writer, _ts, _la = cand
            neighbors = [c[1] for k, c in cands.items() if k != int(episode_id)]
            return self._evaluate(int(episode_id), vec, writer, t,
                                  misses=self._window(t), neighbor_vecs=neighbors)
        except Exception:                          # noqa: BLE001 — never break a capture
            _log.warning("lifecycle.on_capture_failed episode_id=%s", episode_id,
                         exc_info=True)
            return None

    # ── trigger: a miss arrived — is any live candidate now demanded enough? ──
    # The caller records the miss FIRST; the window read here must include it.
    def on_miss(self, vector: "np.ndarray",
                agent_id: str) -> list[tuple[int, PromotionDecision]]:
        if not self.enabled:
            return []
        try:
            t = self._now()
            misses = self._window(t)
            cands = list(self._store.quarantined_candidates(
                now=t, quarantine_ttl_s=self._q_ttl))
            out: list[tuple[int, PromotionDecision]] = []
            for eid, vec, writer, _ts, _la in cands:
                pre = _cosine(vector, vec)
                if pre is None or pre < self._rule.demand_tau:
                    continue                       # this miss is not about this candidate
                # competitor sim is re-searched PER candidate, so a promotion earlier
                # in this same scan vetoes its own near-duplicates. The OTHER co-quarantined
                # vectors are the anomaly detector's neighborhood (the flood signature).
                neighbors = [c[1] for c in cands if int(c[0]) != int(eid)]
                out.append((int(eid), self._evaluate(int(eid), vec, writer, t,
                                                     misses=misses,
                                                     neighbor_vecs=neighbors)))
            return out
        except Exception:                          # noqa: BLE001 — never break a recall
            _log.warning("lifecycle.on_miss_failed agent_id=%s", agent_id, exc_info=True)
            return []

    def sweep(self, now: Optional[int] = None) -> dict:
        """Materialize lazy decay (boot + post-capture piggyback): TTL-lapsed
        quarantined/provisional rows flip to deprecated. Inert when disabled;
        a sweep fault is logged, never raised. This is the ONLY mechanical
        sweep-time trust change — ``established`` is reachable only via a human
        ``hive_write(approved_by=…)``, never mechanically."""
        if not self.enabled:
            return {}
        try:
            t = self._now() if now is None else int(now)
            return dict(self._store.sweep_decayed(now=t, q_ttl_s=self._q_ttl,
                                                  p_ttl_s=self._p_ttl))
        except Exception:                          # noqa: BLE001
            _log.warning("lifecycle.sweep_failed", exc_info=True)
            return {}

    # ── internals ──────────────────────────────────────────────────────────────
    def _window(self, t: int) -> list[MissRow]:
        return self._store.misses_window(t - self._window_s)

    def _competitor_top_sim(self, vec: "np.ndarray") -> float:
        hits = self._index.search(vec, 1)
        return float(hits[0][1]) if hits else -1.0   # empty index ⇒ no competitor

    def _evaluate(self, episode_id: int, vec: "np.ndarray", writer: str, t: int,
                  *, misses: Sequence[MissRow],
                  neighbor_vecs: Sequence = ()) -> PromotionDecision:
        d = self._rule.decide(candidate_vec=vec, candidate_writer=writer,
                              misses=misses,
                              competitor_top_sim=self._competitor_top_sim(vec))
        if d.promote and self._store.set_trust(episode_id, PROVISIONAL, now=t):
            # embedding-cluster anomaly flag (detection-only, advisory): the candidate's
            # compactness against its co-quarantined neighbors — the MINJA/AgentPoison flood
            # signature. Computed AFTER the promote decision and stamped into the durable
            # audit; it NEVER blocks promotion (O7 — resolution stays the human rail).
            anomaly = cluster_anomaly(vec, neighbor_vecs, tau=self._anomaly_tau,
                                      min_cluster=self._anomaly_min_cluster)
            self._store.insert_audit(episode_id, EK_PROMOTE, "server", t, json.dumps({
                "rule": "demand", "n_misses": d.n_misses,
                "demand_independence": {"rho_bar": d.rho_bar, "n_eff": d.n_eff,
                                        "k": d.n_misses},
                "cluster_anomaly": anomaly,
                "n_other_identities": d.n_other_identities,
                "competitor_sim": d.competitor_sim, "reason": d.reason}))
            _log.info("lifecycle.promoted episode_id=%s n_misses=%d n_other=%d anomaly=%s",
                      episode_id, d.n_misses, d.n_other_identities, anomaly)
            return d
        # the verified-win rung: consulted ONLY when the demand rule did not promote,
        # and only when a reader is wired (None ⇒ unreachable — the opt-OUT form).
        if d.promote or self._verified_reader is None:
            return d
        has_win = int(episode_id) in self._verified_reader.verified_wins(
            [int(episode_id)])
        if not has_win:
            return d
        v = verified_win_decision(has_verified_win=True,
                                  competitor_top_sim=self._competitor_top_sim(vec),
                                  competitor_tau=self._rule.competitor_tau)
        if v.promote and self._store.set_trust(episode_id, PROVISIONAL, now=t):
            self._store.insert_audit(episode_id, EK_PROMOTE, "server", t, json.dumps({
                "rule": "verified_win",
                "evidence_kind": EK_OUTCOME_VERIFIED_HELPED,
                "competitor_sim": v.competitor_sim, "reason": v.reason}))
            _log.info("lifecycle.promoted_verified episode_id=%s competitor_sim=%.4f",
                      episode_id, v.competitor_sim)
            return v
        return d                                     # richer demand diagnostics on no-op
