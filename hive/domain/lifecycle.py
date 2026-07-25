"""The mechanical memory-lifecycle core: trust states, the ONE servability
predicate, and lazy TTL decay.

A captured memory is born ``quarantined`` (embedded but structurally unservable);
measured demand promotes it to ``provisional`` (served WITH its label); a
``hive_write`` lands ``provisional`` directly (serve-as-provisional-then-heal);
``established`` is reachable ONLY through ``promote_established`` — a SHA-bound
``outcome_verified_helped`` row from the canonical line (there is no human vouch);
supersession or TTL decay retires rows to ``deprecated``. ``is_servable`` is the
single source every serving layer re-evaluates (store scan predicate, index
membership sync, per-hit recall belt); ``decayed`` is the lazy death rule the
sweep materializes.

Promotion out of quarantine is MECHANICAL DEMAND: a quarantined memory matching
enough PARTITION-COMPATIBLE recall misses (the §3.6 scope clause — a miss scoped
to another repo never counts) FROM IDENTITIES OTHER THAN ITS WRITER (THEORY §9
#6 — the writer's own misses are subtracted from the count entirely, not merely
required to share the tally with one other ask; the writer cannot manufacture
both supply and demand), with no partition-compatible servable row already
answering the need (the near-dup competitor veto, partition-scoped), auto-promotes
to provisional. Promotion means "demanded, unique, and not self-demanded" — NOT
"true"; the guards against wrongness are the trust label on every served hit,
machine-gated retirement, and decay.

PURE: stdlib + numpy only. The purity gate (tests/test_purity.py) forbids
sqlite3 | torch | subprocess | os | git | time imports anywhere in hive/domain/.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Callable,
    Collection,
    Final,
    Optional,
    Protocol,
    Sequence,
)

import numpy as np  # permitted in domain (not in the forbidden I/O set)

from hive.domain.anomaly import cluster_anomaly
from hive.domain.evidence_kinds import EK_OUTCOME_VERIFIED_HELPED, EK_PROMOTE

if TYPE_CHECKING:
    # Typing-only: runtime imports here would close cycles (ports -> models ->
    # lifecycle; models -> lifecycle).
    from hive.domain.models import Episode
    from hive.domain.ports import VectorIndex

_log = logging.getLogger("hive.lifecycle")

QUARANTINED: Final = "quarantined"
PROVISIONAL: Final = "provisional"
ESTABLISHED: Final = "established"
DEPRECATED: Final = "deprecated"
TRUST_STATES: Final = (QUARANTINED, PROVISIONAL, ESTABLISHED, DEPRECATED)


@dataclass(frozen=True, slots=True)
class MissRow:
    """One recorded recall miss inside the demand window — the carrier the store
    returns and the demand rule consumes. ``agent_id`` is the asker's authenticated
    token label (the anti-gaming key); ``vector`` is the query embedding (re-encoded
    from the REDACTED text when the scanner masked; never present for refused);
    ``repos`` is the query's repo scope (v3 §3.6) — ``()`` = a GLOBAL miss, which
    matches every candidate scope."""

    vector: "np.ndarray"
    agent_id: str
    ts: int
    repos: tuple[str, ...] = ()


def scope_matches(a: Collection[str], b: Collection[str]) -> bool:
    """THE §3.6 partition-compatibility clause, in one owner:

        S matches R  ≡  S == [] or R == [] or S ∩ R != ∅

    (an empty scope is GLOBAL/GENERAL and matches everything; otherwise the two
    scopes must share at least one repo). Symmetric, pure, total.  // O(|a|+|b|)."""
    return not a or not b or bool(frozenset(a) & frozenset(b))


def is_servable(
    *, status: str, trust: str, last_active_ts: int, now: int, provisional_ttl_s: int
) -> bool:
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


def decayed(
    *,
    trust: str,
    last_active_ts: int,
    created_ts: int,
    now: int,
    quarantine_ttl_s: int,
    provisional_ttl_s: int,
) -> bool:
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


def _cosine(a: "np.ndarray", b: "np.ndarray") -> Optional[float]:
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
            if c is None:  # undecidable pair ⇒ skipped, never counted
                continue
            cosines.append(max(-1.0, min(1.0, c)))
    if not cosines:
        return (0.0, float(k))
    rho_bar = max(-1.0, min(1.0, float(sum(cosines) / len(cosines))))
    d_eff = 1.0 + (k - 1) * max(0.0, rho_bar)  # max(0,·) ⇒ n_eff <= k by construction
    return (rho_bar, float(k) / d_eff)


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """The machine-readable verdict on one quarantined candidate — its fields go
    verbatim into the ``promote`` audit payload. ``reason`` names the failed clause:
    the single demand-count gate (``n_other >= demand_m``) diagnoses itself as
    ``self_demand`` (matched misses exist but every one is the writer's own) or
    ``insufficient_demand`` (some non-writer misses matched, just not enough of
    them), then ``competitor_answers``; ``non_finite`` for undecidable inputs,
    ``demand`` on promotion. ``rho_bar`` / ``n_eff`` are the decorrelated-demand
    stamp (computed on the promote path only, value-INDEPENDENT of the verdict —
    they record HOW independent the demand was, never gate it); both keep the
    under-claiming 0.0 on every non-promote path."""

    promote: bool
    n_misses: int  # matched misses inside the window
    n_other_identities: int  # matched misses from identities ≠ the writer
    competitor_sim: float  # top-1 cosine against the SERVABLE index
    reason: str
    rho_bar: float = 0.0  # mean pairwise cosine of the matched-miss vectors
    n_eff: float = 0.0  # effective independent votes (k / design-effect)


class DemandRule:
    """promote IFF, over the window misses:
        matched = [m for m in misses
                   if scope_matches(m.repos, candidate_repos)      (§3.6 scope clause,
                       BEFORE the cosine clause — an out-of-partition miss is skipped,
                       never measured)
                   and cos(m.vector, candidate) >= demand_tau]
        n_other = count(m in matched if m.agent_id != candidate_writer)
        n_other >= demand_m                                       (enough demand FROM
            IDENTITIES OTHER THAN THE WRITER — THEORY §9 #6: the writer's own
            matched misses are counted in ``n_misses`` for the audit but subtracted
            out of the bar itself, so the writer cannot manufacture both supply and
            demand by asking after its own capture)
        AND competitor_top_sim < competitor_tau                   (a servable row
            this close already answers it — near-dup pile-up prevention; the
            caller supplies a PARTITION-COMPATIBLE top sim)
    Pure, total, never raises; ANY non-finite input among the SCOPE-MATCHED
    misses ⇒ promote=False (fail-closed, NaN/inf in any position).  // O(|misses|·d).

    The identity-diversity clause is the SOLE anti-gaming rule (solo and team share it):
    a lone agent cannot manufacture both supply and demand, but ≥2 agent-sessions can —
    independent of how many humans direct them. There is no elapsed-span bypass."""

    def __init__(
        self, *, demand_m: int, demand_tau: float, competitor_tau: float
    ) -> None:
        if int(demand_m) < 1:
            raise ValueError(f"demand_m must be >= 1 (got {demand_m})")
        for name, v in (("demand_tau", demand_tau), ("competitor_tau", competitor_tau)):
            if not (math.isfinite(v) and 0.0 < v <= 1.0):
                raise ValueError(f"{name} must be finite in (0, 1] (got {v})")
        self.demand_m = int(demand_m)
        self.demand_tau = float(demand_tau)
        self.competitor_tau = float(competitor_tau)

    def decide(
        self,
        *,
        candidate_vec: "np.ndarray",
        candidate_writer: str,
        misses: Sequence[MissRow],
        competitor_top_sim: float,
        candidate_repos: Sequence[str],
    ) -> PromotionDecision:
        try:
            comp = float(competitor_top_sim)
            if not math.isfinite(comp):
                return PromotionDecision(False, 0, 0, 0.0, "non_finite")
            cand_scope = tuple(candidate_repos)
            matched: list[MissRow] = []
            for m in misses:
                # marker: the §3.6 scope clause, BEFORE the cosine clause — a miss
                # scoped OUTSIDE the candidate's partition never counts as demand.
                # Dropping this guard is the named mutation: CT-2's cross-repo test
                # (tests/contract/test_demand_repo_scoped.py::
                # test_other_repo_scoped_misses_never_promote) and its unit twin
                # (tests/domain/test_demand_rule.py::
                # test_other_repo_scoped_misses_never_count) red.
                if not scope_matches(m.repos, cand_scope):
                    continue
                c = _cosine(m.vector, candidate_vec)
                if c is None:  # undecidable input ⇒ fail closed
                    return PromotionDecision(False, 0, 0, comp, "non_finite")
                if c >= self.demand_tau:
                    matched.append(m)
            n_other = sum(1 for m in matched if m.agent_id != candidate_writer)
            if n_other < self.demand_m:
                # the writer's own misses are not demand (THEORY §9 #6). Both diagnoses stay
                # live: no other identity at all vs. not enough of them.
                reason = (
                    "self_demand"
                    if (matched and n_other == 0)
                    else "insufficient_demand"
                )
                return PromotionDecision(False, len(matched), n_other, comp, reason)
            if comp >= self.competitor_tau:
                return PromotionDecision(
                    False, len(matched), n_other, comp, "competitor_answers"
                )
            # promote path ONLY (after every gating clause has had its chance to return) —
            # so the measure is value-INDEPENDENT of the verdict (Law 3): it records HOW
            # independent the demand was, it never decides whether to promote.
            rho_bar, n_eff = _demand_independence(matched)
            return PromotionDecision(
                True,
                len(matched),
                n_other,
                comp,
                "demand",
                rho_bar=rho_bar,
                n_eff=n_eff,
            )
        except Exception:  # noqa: BLE001 — total by contract
            _log.warning(
                "demand rule internal failure → fail-closed no-promote", exc_info=True
            )
            return PromotionDecision(False, 0, 0, 0.0, "error")


def verified_win_decision(
    *, has_verified_win: bool, competitor_top_sim: float, competitor_tau: float
) -> PromotionDecision:
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


class _LifecycleStore(Protocol):
    """The store surface the lifecycle service actually consumes (typing-only;
    the concrete SqliteEpisodeStore satisfies it structurally). NOT a swap-seam
    port — episode-CRUD deliberately stays off ports.py (see ports docstring)."""

    def quarantined_candidates(
        self, *, now: int, quarantine_ttl_s: int
    ) -> list[tuple[int, "np.ndarray", str, int, int]]: ...
    def sweep_decayed(
        self, *, now: int, q_ttl_s: int, p_ttl_s: int
    ) -> dict[str, int]: ...
    def provisional_ids(self) -> list[int]: ...
    def set_trust(self, episode_id: int, new_trust: str, *, now: int) -> bool: ...
    def insert_audit(
        self, episode_id: int, kind: str, actor: str, ts: int, payload: str
    ) -> int: ...
    def misses_window(self, since_ts: int) -> list[MissRow]: ...
    def get_episode(self, episode_id: int) -> "Optional[Episode]": ...
    def servable_scopes(
        self, *, now: int, provisional_ttl_s: int
    ) -> dict[int, tuple[frozenset[str], tuple[tuple[str, str], ...]]]: ...


class _VerifiedWinReader(Protocol):
    """The SHA-bound verified-win read the established rung consumes (typing-only;
    the SqliteEpisodeStore satisfies it structurally)."""

    def verified_wins(self, episode_ids: Sequence[int]) -> set[int]: ...


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

    def __init__(
        self,
        *,
        store: "_LifecycleStore",
        index: "VectorIndex",
        rule: DemandRule,
        now: Callable[[], int],
        demand_window_s: int,
        quarantine_ttl_s: int,
        provisional_ttl_s: int,
        enabled: bool = True,
        anomaly_tau: float = 0.95,
        anomaly_min_cluster: int = 5,
        verified_reader: Optional["_VerifiedWinReader"] = None,
    ) -> None:
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
        self._verified_reader = verified_reader  # None ⇒ the rung is unreachable

    # ── trigger: a capture arrived — does existing demand already want it? ────
    def on_capture(self, episode_id: int) -> Optional[PromotionDecision]:
        if not self.enabled:
            return None
        try:
            t = self._now()
            cands = {
                c[0]: c
                for c in self._store.quarantined_candidates(
                    now=t, quarantine_ttl_s=self._q_ttl
                )
            }
            cand = cands.get(int(episode_id))
            if cand is None:  # unknown / not quarantined / decayed
                return None
            _eid, vec, writer, _ts, _la = cand
            repos = self._candidate_repos(int(episode_id))
            if repos is None:  # scope undecidable ⇒ fail-closed skip
                return None
            neighbors = [c[1] for k, c in cands.items() if k != int(episode_id)]
            return self._evaluate(
                int(episode_id),
                vec,
                writer,
                t,
                misses=self._window(t),
                neighbor_vecs=neighbors,
                candidate_repos=repos,
            )
        except Exception:  # noqa: BLE001 — never break a capture
            _log.warning(
                "lifecycle.on_capture_failed episode_id=%s", episode_id, exc_info=True
            )
            return None

    # ── trigger: a miss arrived — is any live candidate now demanded enough? ──
    # The caller records the miss FIRST; the window read here must include it.
    def on_miss(
        self, vector: "np.ndarray", agent_id: str
    ) -> list[tuple[int, PromotionDecision]]:
        if not self.enabled:
            return []
        try:
            t = self._now()
            misses = self._window(t)
            cands = list(
                self._store.quarantined_candidates(now=t, quarantine_ttl_s=self._q_ttl)
            )
            out: list[tuple[int, PromotionDecision]] = []
            for eid, vec, writer, _ts, _la in cands:
                pre = _cosine(vector, vec)
                if pre is None or pre < self._rule.demand_tau:
                    continue  # this miss is not about this candidate
                repos = self._candidate_repos(int(eid))
                if repos is None:  # scope undecidable ⇒ fail-closed skip
                    continue
                # competitor sim is re-searched PER candidate, so a promotion earlier
                # in this same scan vetoes its own near-duplicates. The OTHER co-quarantined
                # vectors are the anomaly detector's neighborhood (the flood signature).
                neighbors = [c[1] for c in cands if int(c[0]) != int(eid)]
                out.append(
                    (
                        int(eid),
                        self._evaluate(
                            int(eid),
                            vec,
                            writer,
                            t,
                            misses=misses,
                            neighbor_vecs=neighbors,
                            candidate_repos=repos,
                        ),
                    )
                )
            return out
        except Exception:  # noqa: BLE001 — never break a recall
            _log.warning(
                "lifecycle.on_miss_failed agent_id=%s", agent_id, exc_info=True
            )
            return []

    def sweep(self, now: Optional[int] = None) -> dict[str, int]:
        """Materialize lazy decay (boot + post-capture piggyback): TTL-lapsed
        quarantined/provisional rows flip to deprecated. Inert when disabled;
        a sweep fault is logged, never raised. Decay is the only trust change
        here — ``established`` is reachable only via ``promote_established``
        (canonical-line outcome verification), never by the decay sweep."""
        if not self.enabled:
            return {}
        try:
            t = self._now() if now is None else int(now)
            return dict(
                self._store.sweep_decayed(
                    now=t, q_ttl_s=self._q_ttl, p_ttl_s=self._p_ttl
                )
            )
        except Exception:  # noqa: BLE001
            _log.warning("lifecycle.sweep_failed", exc_info=True)
            return {}

    def promote_established(self) -> list[int]:
        """The provisional→established rung (v3 §3.1) — the ONLY path to the top
        tier: every PROVISIONAL episode carrying >= 1 SHA-bound
        ``outcome_verified_helped`` row (the server-written, non-forgeable census
        artifact) flips to ``established`` with an audit row. Driven post-ingest
        by the sync loop. Reads the PROVISIONAL set only (a quarantined row with
        a win goes via the provisional rung first). Inert when disabled or when
        no verified reader is wired; fail-open for its caller (a fault returns
        the ids already promoted, never raises). Returns the promoted ids."""
        if not self.enabled or self._verified_reader is None:
            return []
        promoted: list[int] = []
        try:
            t = self._now()
            ids = [int(e) for e in self._store.provisional_ids()]
            if not ids:
                return []
            # marker: the establish fuel is verified_wins — the server-written
            # ``outcome_verified_helped`` read — ONLY. Reading settled_wins here
            # (which unions the self-reported ``outcome_helped``) is the named
            # mutation: CT-8's self-report test (tests/contract/
            # test_established_rung.py::test_self_reported_help_never_establishes)
            # and its unit twin (tests/domain/test_demand_rule.py::
            # test_promote_established_ignores_settled_wins) red.
            wins = self._verified_reader.verified_wins(ids)
            for eid in ids:
                if eid not in wins:
                    continue
                if self._store.set_trust(eid, ESTABLISHED, now=t):
                    self._store.insert_audit(
                        eid,
                        EK_PROMOTE,
                        "server",
                        t,
                        json.dumps(
                            {
                                "rule": "verified_established",
                                "evidence_kind": EK_OUTCOME_VERIFIED_HELPED,
                            }
                        ),
                    )
                    promoted.append(eid)
                    _log.info("lifecycle.established episode_id=%s", eid)
            return promoted
        except Exception:  # noqa: BLE001 — fail-open sweep
            _log.warning("lifecycle.promote_established_failed", exc_info=True)
            return promoted

    # ── internals ──────────────────────────────────────────────────────────────
    def _window(self, t: int) -> list[MissRow]:
        return self._store.misses_window(t - self._window_s)

    def _candidate_repos(self, episode_id: int) -> Optional[tuple[str, ...]]:
        """The candidate's repo scope, read from its Episode carrier. ``None``
        (row vanished / read fault) is the fail-closed signal: a candidate whose
        partition cannot be determined is skipped this pass, never promoted."""
        try:
            ep = self._store.get_episode(int(episode_id))
        except Exception:  # noqa: BLE001 — undecidable scope
            _log.warning(
                "lifecycle.candidate_repos_failed episode_id=%s",
                episode_id,
                exc_info=True,
            )
            return None
        if ep is None:
            return None
        return tuple(ep.repos)

    def _competitor_top_sim(self, vec: "np.ndarray", scope: Sequence[str]) -> float:
        """The PARTITION-SCOPED competitor read (§3.6): walk the full servable
        index descending and return the sim of the FIRST partition-compatible
        row — a near-dup in ANOTHER repo never vetoes. A general candidate
        (empty scope) competes with everything (top-1). An indexed row whose
        scope cannot be resolved is treated as general (compatible) — the
        fail-closed direction for PROMOTION (an unknown row may veto, never
        wave a duplicate through)."""
        hits = self._index.search(vec, max(1, int(self._index.size())))
        if not hits:
            return -1.0  # empty index ⇒ no competitor
        cand_scope = tuple(scope)
        if not cand_scope:
            return float(hits[0][1])  # general: everything competes
        scopes = self._store.servable_scopes(
            now=self._now(), provisional_ttl_s=self._p_ttl
        )
        for eid, sim in hits:  # walk desc, first compatible eid
            entry = scopes.get(int(eid))
            repos = tuple(entry[0]) if entry is not None else ()
            if scope_matches(repos, cand_scope):
                return float(sim)
        return -1.0  # no partition-compatible competitor

    def _evaluate(
        self,
        episode_id: int,
        vec: "np.ndarray",
        writer: str,
        t: int,
        *,
        misses: Sequence[MissRow],
        neighbor_vecs: Sequence["np.ndarray"] = (),
        candidate_repos: Sequence[str] = (),
    ) -> PromotionDecision:
        d = self._rule.decide(
            candidate_vec=vec,
            candidate_writer=writer,
            misses=misses,
            competitor_top_sim=self._competitor_top_sim(vec, candidate_repos),
            candidate_repos=candidate_repos,
        )
        if d.promote and self._store.set_trust(episode_id, PROVISIONAL, now=t):
            # embedding-cluster anomaly flag (detection-only, advisory): the candidate's
            # compactness against its co-quarantined neighbors — the MINJA/AgentPoison flood
            # signature. Computed AFTER the promote decision and stamped into the durable
            # audit; it NEVER blocks promotion (O7 — resolution stays the human rail).
            anomaly = cluster_anomaly(
                vec,
                neighbor_vecs,
                tau=self._anomaly_tau,
                min_cluster=self._anomaly_min_cluster,
            )
            self._store.insert_audit(
                episode_id,
                EK_PROMOTE,
                "server",
                t,
                json.dumps(
                    {
                        "rule": "demand",
                        "n_misses": d.n_misses,
                        "demand_independence": {
                            "rho_bar": d.rho_bar,
                            "n_eff": d.n_eff,
                            "k": d.n_misses,
                        },
                        "cluster_anomaly": anomaly,
                        "n_other_identities": d.n_other_identities,
                        "competitor_sim": d.competitor_sim,
                        "reason": d.reason,
                    }
                ),
            )
            _log.info(
                "lifecycle.promoted episode_id=%s n_misses=%d n_other=%d anomaly=%s",
                episode_id,
                d.n_misses,
                d.n_other_identities,
                anomaly,
            )
            return d
        # the verified-win rung: consulted ONLY when the demand rule did not promote,
        # and only when a reader is wired (None ⇒ unreachable — the opt-OUT form).
        if d.promote or self._verified_reader is None:
            return d
        has_win = int(episode_id) in self._verified_reader.verified_wins(
            [int(episode_id)]
        )
        if not has_win:
            return d
        v = verified_win_decision(
            has_verified_win=True,
            competitor_top_sim=self._competitor_top_sim(vec, candidate_repos),
            competitor_tau=self._rule.competitor_tau,
        )
        if v.promote and self._store.set_trust(episode_id, PROVISIONAL, now=t):
            self._store.insert_audit(
                episode_id,
                EK_PROMOTE,
                "server",
                t,
                json.dumps(
                    {
                        "rule": "verified_win",
                        "evidence_kind": EK_OUTCOME_VERIFIED_HELPED,
                        "competitor_sim": v.competitor_sim,
                        "reason": v.reason,
                    }
                ),
            )
            _log.info(
                "lifecycle.promoted_verified episode_id=%s competitor_sim=%.4f",
                episode_id,
                v.competitor_sim,
            )
            return v
        return d  # richer demand diagnostics on no-op
