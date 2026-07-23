"""DemandRule (the pure promotion verdict) + LifecycleService (the synchronous
trigger driver), store-independent via duck-typed fakes.

The rule's headline properties are STRUCTURAL anti-gaming — demand consisting
solely of the writer's own misses never promotes (the writer cannot manufacture
both supply and demand) — and, in v3, PARTITION honesty: the §3.6 scope clause
runs BEFORE the cosine clause, so a miss scoped to another repo never counts as
demand, and the competitor veto consults only partition-compatible servable rows.
Any non-finite input among the scope-matched misses fails CLOSED (no promotion,
no raise). The provisional→established rung (``promote_established``) is fueled
by the server-written verified-win read ONLY."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from hive.domain.evidence_kinds import EVIDENCE_KINDS
from hive.domain.lifecycle import (
    ESTABLISHED,
    PROVISIONAL,
    QUARANTINED,
    DemandRule,
    LifecycleService,
    MissRow,
    PromotionDecision,
    _demand_independence,
    scope_matches,
    verified_win_decision,
)
from tests.fakes import FakeIndex
from tests.fakes._fakes import make_episode

NOW = 10_000
WINDOW_S = 1_000
Q_TTL, P_TTL = 500, 900

CAND = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)  # the quarantined candidate


def _at_cos(c: float) -> np.ndarray:
    """A unit vector whose cosine to CAND is ≈ c (well clear of float noise)."""
    return np.array([c, math.sqrt(1.0 - c * c), 0.0, 0.0], dtype=np.float32)


def _miss(
    agent: str, *, cos: float = 0.9, ts: int = NOW - 10, repos: tuple[str, ...] = ()
) -> MissRow:
    return MissRow(vector=_at_cos(cos), agent_id=agent, ts=ts, repos=repos)


def _rule(m: int = 3, tau: float = 0.75, comp: float = 0.85) -> DemandRule:
    return DemandRule(demand_m=m, demand_tau=tau, competitor_tau=comp)


def _decide(
    rule: DemandRule,
    misses,
    *,
    writer: str = "writer-1",
    competitor: float = -1.0,
    candidate_repos: tuple[str, ...] = (),
) -> PromotionDecision:
    return rule.decide(
        candidate_vec=CAND,
        candidate_writer=writer,
        misses=misses,
        competitor_top_sim=competitor,
        candidate_repos=candidate_repos,
    )


# ── the rule ───────────────────────────────────────────────────────────────────
def test_rule_ctor_validates():
    with pytest.raises(ValueError):
        DemandRule(demand_m=0, demand_tau=0.75, competitor_tau=0.85)
    with pytest.raises(ValueError):
        DemandRule(demand_m=3, demand_tau=0.0, competitor_tau=0.85)  # tau must be >0
    with pytest.raises(ValueError):
        DemandRule(demand_m=3, demand_tau=1.5, competitor_tau=0.85)  # tau must be ≤1
    with pytest.raises(ValueError):
        DemandRule(demand_m=3, demand_tau=0.75, competitor_tau=float("nan"))


def test_promotes_at_m_distinct_demand():
    misses = [_miss("writer-1"), _miss("other-1"), _miss("other-2")]
    d = _decide(_rule(), misses)
    assert d.promote is True and d.reason == "demand"
    assert d.n_misses == 3 and d.n_other_identities == 2


def test_writer_only_demand_never_promotes():
    # the anti-gaming headline: m misses, ALL from the candidate's own writer
    misses = [_miss("writer-1"), _miss("writer-1"), _miss("writer-1")]
    d = _decide(_rule(), misses)
    assert d.promote is False and d.reason == "self_demand"
    assert d.n_misses == 3 and d.n_other_identities == 0


def test_competitor_vetoes():
    misses = [_miss("writer-1"), _miss("other-1"), _miss("other-2")]
    d = _decide(_rule(), misses, competitor=0.9)  # a servable row this close
    assert d.promote is False and d.reason == "competitor_answers"
    # the veto compare is INCLUSIVE: competitor_top_sim == competitor_tau vetoes
    d_eq = _decide(_rule(comp=0.85), misses, competitor=0.85)
    assert d_eq.promote is False and d_eq.reason == "competitor_answers"
    # just-below the veto threshold promotes
    d_ok = _decide(_rule(comp=0.85), misses, competitor=0.80)
    assert d_ok.promote is True


def test_tau_and_count_boundaries():
    # cosine below demand_tau does not count as demand…
    below = [
        _miss("writer-1", cos=0.5),
        _miss("other-1", cos=0.5),
        _miss("other-2", cos=0.5),
    ]
    d = _decide(_rule(tau=0.75), below)
    assert d.promote is False and d.reason == "insufficient_demand" and d.n_misses == 0
    # …and m−1 matched misses are insufficient even with identity diversity
    short = [_miss("writer-1"), _miss("other-1")]
    d2 = _decide(_rule(m=3), short)
    assert (
        d2.promote is False and d2.reason == "insufficient_demand" and d2.n_misses == 2
    )
    # clearly-above tau counts
    above = [_miss("a", cos=0.8), _miss("b", cos=0.8), _miss("c", cos=0.8)]
    assert _decide(_rule(tau=0.75), above, writer="a").promote is True


@pytest.mark.parametrize(
    "poison",
    [
        "candidate_nan",
        "candidate_inf",
        "miss_nan",
        "miss_inf",
        "competitor_nan",
        "competitor_inf",
        "candidate_zero_norm",
    ],
)
def test_nonfinite_fails_closed(poison: str):
    # NaN/inf in ANY position is undecidable ⇒ promote=False, reason machine-readable,
    # and the rule NEVER raises.
    misses = [_miss("writer-1"), _miss("other-1"), _miss("other-2")]
    cand, comp = CAND, -1.0
    if poison == "candidate_nan":
        cand = np.array([np.nan, 0, 0, 0], dtype=np.float32)
    elif poison == "candidate_inf":
        cand = np.array([np.inf, 0, 0, 0], dtype=np.float32)
    elif poison == "miss_nan":
        misses[1] = MissRow(
            vector=np.array([np.nan, 0, 0, 0], dtype=np.float32),
            agent_id="other-1",
            ts=NOW - 10,
        )
    elif poison == "miss_inf":
        misses[2] = MissRow(
            vector=np.array([np.inf, 0, 0, 0], dtype=np.float32),
            agent_id="other-2",
            ts=NOW - 10,
        )
    elif poison == "competitor_nan":
        comp = float("nan")
    elif poison == "competitor_inf":
        comp = float("inf")
    elif poison == "candidate_zero_norm":
        cand = np.zeros(4, dtype=np.float32)
    d = _rule().decide(
        candidate_vec=cand,
        candidate_writer="writer-1",
        misses=misses,
        competitor_top_sim=comp,
        candidate_repos=(),
    )
    assert d.promote is False and d.reason == "non_finite"


# ── the §3.6 scope clause (v3): partition compatibility BEFORE the cosine ──────
def test_scope_matches_algebra():
    # S matches R ≡ S==[] or R==[] or S∩R≠∅ — symmetric; the one owner.
    assert scope_matches((), ()) is True
    assert scope_matches((), ("alpha",)) is True  # global miss matches all
    assert scope_matches(("alpha",), ()) is True  # general candidate matched by all
    assert scope_matches(("alpha", "beta"), ("beta",)) is True
    assert scope_matches(("alpha",), ("beta",)) is False
    assert scope_matches(("alpha",), ("beta",)) == scope_matches(("beta",), ("alpha",))


def test_other_repo_scoped_misses_never_count():
    # THE named-mutation twin (CT-2 cross-repo): B-scoped misses toward an A-scoped
    # candidate are excluded by the scope clause — dropping the clause makes this
    # demand promote, which reds here.
    misses = [
        _miss("other-1", repos=("beta",)),
        _miss("other-2", repos=("beta",)),
        _miss("other-3", repos=("beta",)),
    ]
    d = _decide(_rule(), misses, candidate_repos=("alpha",))
    assert d.promote is False
    assert d.reason == "insufficient_demand" and d.n_misses == 0


def test_global_misses_count_for_any_candidate_scope():
    misses = [_miss("other-1"), _miss("other-2"), _miss("other-3")]  # repos=() = global
    d = _decide(_rule(), misses, candidate_repos=("alpha",))
    assert d.promote is True and d.n_misses == 3


def test_scoped_misses_count_for_a_general_candidate():
    misses = [
        _miss("other-1", repos=("beta",)),
        _miss("other-2", repos=("beta",)),
        _miss("other-3", repos=("beta",)),
    ]
    d = _decide(_rule(), misses, candidate_repos=())
    assert d.promote is True and d.n_misses == 3


def test_intersecting_scopes_count():
    misses = [
        _miss("other-1", repos=("alpha", "gamma")),
        _miss("other-2", repos=("alpha",)),
        _miss("other-3", repos=("alpha",)),
    ]
    d = _decide(_rule(), misses, candidate_repos=("alpha", "beta"))
    assert d.promote is True and d.n_misses == 3


def test_scope_clause_runs_before_the_cosine_clause():
    # order pin: an out-of-partition miss with a POISONED vector is skipped by the
    # scope clause BEFORE any cosine reads it — the decision stays decidable.
    # (Cosine-first would fail the whole decision closed as non_finite.)
    poisoned = MissRow(
        vector=np.array([np.nan, 0, 0, 0], dtype=np.float32),
        agent_id="other-9",
        ts=NOW - 10,
        repos=("beta",),
    )
    good = [_miss("writer-1"), _miss("other-1"), _miss("other-2")]
    d = _rule().decide(
        candidate_vec=CAND,
        candidate_writer="writer-1",
        misses=[poisoned, *good],
        competitor_top_sim=-1.0,
        candidate_repos=("alpha",),
    )
    assert d.promote is True and d.reason == "demand"


def test_miss_row_scope_defaults_global():
    assert MissRow(vector=CAND, agent_id="a", ts=0).repos == ()


# ── the service, against a duck-typed recording store ──────────────────────────
class _LifeStore:
    """The store slice the service drives, recording every call. ``set_trust``
    mirrors the real adapter's behavior of adding a promoted row to the index AND
    to the servable-scope read (the D6 ``servable_scopes`` map); ``get_episode``
    serves the candidate's repo scope off the v3 Episode carrier."""

    def __init__(
        self,
        candidates=(),
        misses=(),
        index: FakeIndex | None = None,
        repos_by_eid=None,
        servable_scopes_map=None,
    ):
        self.candidates = list(candidates)  # (eid, vec, proposed_by, ts, last_active)
        self.misses = list(misses)
        self.index = index
        self.repos_by_eid = {
            int(k): tuple(v) for k, v in dict(repos_by_eid or {}).items()
        }
        # eid → repo scope of SERVABLE rows (the partition-aware competitor read)
        self.servable = {
            int(k): tuple(v) for k, v in dict(servable_scopes_map or {}).items()
        }
        self._vec_by_eid = {c[0]: c[1] for c in self.candidates}
        self.calls: list[tuple] = []
        self.trust: dict[int, tuple[str, int]] = {}
        self.audits: list[tuple] = []

    def quarantined_candidates(self, *, now, quarantine_ttl_s):
        self.calls.append(("quarantined_candidates", now, quarantine_ttl_s))
        return list(self.candidates)

    def get_episode(self, episode_id):
        eid = int(episode_id)
        if eid not in self._vec_by_eid and eid not in self.repos_by_eid:
            return None
        return make_episode(
            eid,
            f"candidate-{eid}",
            status="approved",
            trust=QUARANTINED,
            repos=self.repos_by_eid.get(eid, ()),
        )

    def servable_scopes(self, *, now, provisional_ttl_s):
        self.calls.append(("servable_scopes", now, provisional_ttl_s))
        return {eid: (frozenset(repos), ()) for eid, repos in self.servable.items()}

    def misses_window(self, since_ts):
        self.calls.append(("misses_window", since_ts))
        return [m for m in self.misses if m.ts > since_ts]

    def set_trust(self, episode_id, new_trust, *, now):
        self.trust[episode_id] = (new_trust, now)
        if new_trust == PROVISIONAL:
            if self.index is not None:
                self.index.add(episode_id, self._vec_by_eid[episode_id])
            # the promoted row is now servable in ITS partition — the competitor
            # read must see it (self-veto of near-duplicates stays partition-true)
            self.servable[int(episode_id)] = self.repos_by_eid.get(int(episode_id), ())
        return True

    def insert_audit(self, episode_id, kind, actor, ts, payload):
        self.audits.append((episode_id, kind, actor, ts, payload))
        return len(self.audits)

    def sweep_decayed(self, *, now, q_ttl_s, p_ttl_s):
        self.calls.append(("sweep_decayed", now, q_ttl_s, p_ttl_s))
        return {QUARANTINED: 1, PROVISIONAL: 0}

    def provisional_ids(self):
        return []


def _service(
    store,
    *,
    enabled: bool = True,
    index: FakeIndex | None = None,
    anomaly_tau: float = 0.95,
    anomaly_min_cluster: int = 5,
):
    return LifecycleService(
        store=store,
        index=index if index is not None else (store.index or FakeIndex()),
        rule=_rule(),
        now=lambda: NOW,
        demand_window_s=WINDOW_S,
        quarantine_ttl_s=Q_TTL,
        provisional_ttl_s=P_TTL,
        enabled=enabled,
        anomaly_tau=anomaly_tau,
        anomaly_min_cluster=anomaly_min_cluster,
    )


def _demand() -> list[MissRow]:
    return [_miss("writer-1"), _miss("other-1"), _miss("other-2")]


def test_on_capture_promotes_against_staged_demand():
    idx = FakeIndex()
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_demand(),
        index=idx,
    )
    d = _service(st).on_capture(7)
    assert d is not None and d.promote is True
    assert st.trust[7] == (PROVISIONAL, NOW)
    # the audit payload round-trips the decision, machine-readable
    eid, kind, actor, ts, payload = st.audits[0]
    assert (eid, kind, actor, ts) == (7, "promote", "server", NOW)
    assert kind in EVIDENCE_KINDS  # the write site emits a registry member
    body = json.loads(payload)
    di = body.pop("demand_independence")  # the decorrelated-demand stamp (3b)
    assert (
        body.pop("cluster_anomaly") is False
    )  # the advisory anomaly flag (3c); no cluster here
    assert body == {
        "rule": "demand",
        "n_misses": 3,
        "n_other_identities": 2,
        "competitor_sim": -1.0,
        "reason": "demand",
    }
    # _demand() is three identical-vector asks ⇒ correlated ⇒ n_eff≈1 (rho_bar≈1), k=3
    assert di["k"] == 3
    assert di["rho_bar"] == pytest.approx(1.0, abs=1e-4)
    assert di["n_eff"] == pytest.approx(1.0, abs=1e-4)
    # the candidate scan + window read used the configured knobs
    assert ("quarantined_candidates", NOW, Q_TTL) in st.calls
    assert ("misses_window", NOW - WINDOW_S) in st.calls


def test_on_capture_writer_only_demand_no_promotion():
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=[_miss("writer-1"), _miss("writer-1"), _miss("writer-1")],
        index=FakeIndex(),
    )
    d = _service(st).on_capture(7)
    assert d is not None and d.promote is False and d.reason == "self_demand"
    assert st.trust == {} and st.audits == []


def test_on_capture_unknown_or_dead_candidate_is_noop():
    # the store's candidate scan already excludes TTL-dead rows; an eid absent from
    # it (dead, deprecated, or foreign) is never evaluated
    st = _LifeStore(candidates=[], misses=_demand(), index=FakeIndex())
    assert _service(st).on_capture(404) is None
    assert st.trust == {} and st.audits == []


def test_on_capture_scope_matched_demand_promotes_scoped_candidate():
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=[
            _miss("other-1", repos=("alpha",)),
            _miss("other-2", repos=("alpha",)),
            _miss("other-3", repos=("alpha",)),
        ],
        index=FakeIndex(),
        repos_by_eid={7: ("alpha",)},
    )
    d = _service(st).on_capture(7)
    assert d is not None and d.promote is True
    assert st.trust[7] == (PROVISIONAL, NOW)


def test_on_capture_other_repo_demand_never_promotes():
    # the service threads the candidate's Episode.repos into the scope clause
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=[
            _miss("other-1", repos=("beta",)),
            _miss("other-2", repos=("beta",)),
            _miss("other-3", repos=("beta",)),
        ],
        index=FakeIndex(),
        repos_by_eid={7: ("alpha",)},
    )
    d = _service(st).on_capture(7)
    assert d is not None and d.promote is False
    assert st.trust == {} and st.audits == []


def test_on_capture_vanished_candidate_row_is_skipped_fail_closed():
    # quarantined_candidates lists the eid but get_episode cannot resolve its
    # scope (row vanished) ⇒ the candidate is skipped this pass, never promoted.
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_demand(),
        index=FakeIndex(),
    )
    st.get_episode = lambda eid: None
    assert _service(st).on_capture(7) is None
    assert st.trust == {} and st.audits == []


def test_on_miss_scans_only_live_quarantine_and_prefilters():
    far = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)  # orthogonal to the miss
    st = _LifeStore(
        candidates=[
            (7, CAND, "writer-1", NOW - 5, NOW - 5),
            (9, far, "writer-2", NOW - 5, NOW - 5),
        ],
        misses=_demand(),
        index=FakeIndex(),
    )
    out = _service(st).on_miss(_at_cos(0.95), agent_id="other-9")
    assert [eid for eid, _d in out] == [7]  # 9 never evaluated
    assert ("quarantined_candidates", NOW, Q_TTL) in st.calls  # live-only source


def test_on_miss_promotion_vetoes_its_own_near_duplicate():
    # two near-duplicate candidates: the first promotes, lands in the servable
    # index, and becomes the competitor that vetoes the second — no pile-up.
    near = _at_cos(0.99)
    idx = FakeIndex()
    st = _LifeStore(
        candidates=[
            (7, CAND, "writer-1", NOW - 5, NOW - 5),
            (8, near, "writer-1", NOW - 5, NOW - 5),
        ],
        misses=_demand(),
        index=idx,
    )
    out = _service(st).on_miss(_at_cos(0.95), agent_id="other-9")
    by_eid = dict(out)
    assert by_eid[7].promote is True
    assert by_eid[8].promote is False and by_eid[8].reason == "competitor_answers"
    assert set(st.trust) == {7}


# ── the competitor veto is PARTITION-scoped (v3 §3.6) ──────────────────────────
def _scoped_demand(repo: str) -> list[MissRow]:
    return [
        _miss("other-1", repos=(repo,)),
        _miss("other-2", repos=(repo,)),
        _miss("other-3", repos=(repo,)),
    ]


def test_competitor_in_another_repo_does_not_veto():
    idx = FakeIndex()
    idx.add(99, CAND)  # a servable near-dup… in beta
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_scoped_demand("alpha"),
        index=idx,
        repos_by_eid={7: ("alpha",)},
        servable_scopes_map={99: ("beta",)},
    )
    out = _service(st).on_miss(_at_cos(0.95), agent_id="other-9")
    d = dict(out)[7]
    assert d.promote is True, "a near-dup competitor in ANOTHER repo must not veto"
    assert d.competitor_sim == -1.0  # no partition-compatible competitor
    assert st.trust[7] == (PROVISIONAL, NOW)


def test_competitor_in_partition_vetoes():
    idx = FakeIndex()
    idx.add(99, CAND)  # a servable near-dup IN alpha
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_scoped_demand("alpha"),
        index=idx,
        repos_by_eid={7: ("alpha",)},
        servable_scopes_map={99: ("alpha",)},
    )
    out = _service(st).on_miss(_at_cos(0.95), agent_id="other-9")
    d = dict(out)[7]
    assert d.promote is False and d.reason == "competitor_answers"
    assert st.trust == {}


def test_general_servable_competitor_vetoes_any_scoped_candidate():
    idx = FakeIndex()
    idx.add(99, CAND)  # a GENERAL servable near-dup
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_scoped_demand("alpha"),
        index=idx,
        repos_by_eid={7: ("alpha",)},
        servable_scopes_map={99: ()},
    )
    out = _service(st).on_miss(_at_cos(0.95), agent_id="other-9")
    assert dict(out)[7].reason == "competitor_answers"


def test_unresolvable_competitor_scope_is_treated_general_and_vetoes():
    # an indexed row absent from the scope read (index/store divergence) may veto,
    # never wave a duplicate through — the fail-closed direction for promotion.
    idx = FakeIndex()
    idx.add(99, CAND)  # indexed, but no scope entry at all
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_scoped_demand("alpha"),
        index=idx,
        repos_by_eid={7: ("alpha",)},
        servable_scopes_map={},
    )
    out = _service(st).on_miss(_at_cos(0.95), agent_id="other-9")
    assert dict(out)[7].reason == "competitor_answers"


def test_disabled_service_is_inert():
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_demand(),
        index=FakeIndex(),
    )
    svc = _service(st, enabled=False)
    assert svc.on_capture(7) is None
    assert svc.on_miss(CAND, agent_id="a") == []
    assert svc.sweep() == {}
    assert st.calls == [] and st.trust == {} and st.audits == []


def test_sweep_delegates_with_configured_ttls():
    st = _LifeStore(index=FakeIndex())
    out = _service(st).sweep()
    assert out == {QUARANTINED: 1, PROVISIONAL: 0}
    assert st.calls == [("sweep_decayed", NOW, Q_TTL, P_TTL)]


# ── the identity-diversity floor is the SOLE rule (no solo span bypass) ──────────
def test_writer_only_demand_over_any_span_never_promotes():
    # MODE-COLLAPSE: there is no elapsed-span bypass — writer-only demand spread over ANY
    # span is still self_demand (the always-on anti-gaming floor, now the SOLE rule).
    DAY = 86_400
    spread = [
        _miss("writer-1", ts=1000),
        _miss("writer-1", ts=1000 + DAY),
        _miss("writer-1", ts=1000 + 2 * DAY),
    ]
    d = _rule().decide(
        candidate_vec=CAND,
        candidate_writer="writer-1",
        misses=spread,
        competitor_top_sim=-1.0,
        candidate_repos=(),
    )
    assert d.promote is False and d.reason == "self_demand"


def test_demand_rule_ctor_rejects_solo_kwargs():
    # the solo_mode / solo_min_span_days params are deleted — passing them is a TypeError.
    with pytest.raises(TypeError):
        DemandRule(demand_m=3, demand_tau=0.75, competitor_tau=0.85, solo_mode=True)
    with pytest.raises(TypeError):
        DemandRule(
            demand_m=3, demand_tau=0.75, competitor_tau=0.85, solo_min_span_days=2
        )


def test_triggers_never_raise_into_caller():
    # a store fault inside a trigger is logged and swallowed — a promotion fault
    # must never break the capture/recall that triggered it
    class _Boom(_LifeStore):
        def quarantined_candidates(self, **kw):
            raise RuntimeError("store down")

        def sweep_decayed(self, **kw):
            raise RuntimeError("store down")

    svc = _service(_Boom(index=FakeIndex()))
    assert svc.on_capture(7) is None
    assert svc.on_miss(CAND, agent_id="a") == []
    assert svc.sweep() == {}


# ── 3c: the embedding-cluster anomaly flag is advisory — it NEVER blocks promotion ──
def test_anomaly_flag_is_stamped_without_blocking_promotion():
    # three near-identical co-quarantined candidates (a compact cluster) + real demand for
    # one of them: it PROMOTES (the flag never vetoes) and the audit records cluster_anomaly.
    idx = FakeIndex()
    cands = [(i, CAND, "writer-1", NOW - 5, NOW - 5) for i in (1, 2, 3)]
    st = _LifeStore(candidates=cands, misses=_demand(), index=idx)
    d = _service(st, anomaly_min_cluster=2).on_capture(
        1
    )  # 2 neighbors clear the quorum
    assert d is not None and d.promote is True  # the flag does NOT block promotion
    assert st.trust[1] == (PROVISIONAL, NOW)
    body = json.loads(st.audits[0][4])
    assert body["cluster_anomaly"] is True  # the compact cluster is flagged


def test_no_anomaly_flag_on_an_isolated_candidate():
    # a lone quarantined candidate has no neighbors ⇒ the flag stays quiet (precision-first)
    st = _LifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_demand(),
        index=FakeIndex(),
    )
    d = _service(st, anomaly_min_cluster=2).on_capture(7)
    assert d.promote is True
    assert json.loads(st.audits[0][4])["cluster_anomaly"] is False


# ── decorrelated demand (n_eff): counting identities ≠ measuring independence ───
def _missv(agent: str, vec, ts: int = NOW - 10) -> MissRow:
    return MissRow(vector=np.asarray(vec, dtype=np.float32), agent_id=agent, ts=ts)


def test_demand_independence_identical_vectors_collapse_to_one():
    # k near-IDENTICAL asks ⇒ rho_bar≈1 ⇒ n_eff≈1 (one effective independent vote)
    v = _at_cos(0.9)
    rho, n_eff = _demand_independence([_missv("a", v), _missv("b", v), _missv("c", v)])
    assert rho == pytest.approx(1.0, abs=1e-5)
    assert n_eff == pytest.approx(1.0, abs=1e-5)


def test_demand_independence_orthogonal_vectors_are_full_n_eff():
    # k ORTHOGONAL asks ⇒ rho_bar≈0 ⇒ n_eff≈k (fully independent demand)
    e = np.eye(4, dtype=np.float32)
    rho, n_eff = _demand_independence(
        [_missv("a", e[0]), _missv("b", e[1]), _missv("c", e[2])]
    )
    assert rho == pytest.approx(0.0, abs=1e-6)
    assert n_eff == pytest.approx(3.0, abs=1e-6)


def test_demand_independence_k_lt_2_under_claims():
    assert _demand_independence([]) == (0.0, 0.0)
    assert _demand_independence([_missv("a", _at_cos(0.9))]) == (0.0, 1.0)


def test_demand_independence_returns_json_serializable_builtin_floats():
    rho, n_eff = _demand_independence(
        [_missv("a", _at_cos(0.9)), _missv("b", _at_cos(0.8))]
    )
    assert type(rho) is float and type(n_eff) is float  # builtin, NOT np.float64
    json.dumps({"rho_bar": rho, "n_eff": n_eff})  # must not TypeError


def test_demand_independence_n_eff_never_exceeds_k():
    # anti-correlated asks (rho_bar<0) ⇒ the design effect floors rho at 0 ⇒ n_eff == k, never > k
    rho, n_eff = _demand_independence(
        [_missv("a", _at_cos(0.9)), _missv("b", _at_cos(-0.9))]
    )
    assert rho < 0.0 and n_eff == pytest.approx(2.0)


def test_demand_independence_skips_undecidable_pairs():
    # a wrong-shape (undecidable) vector pair is skipped, not counted as demand
    bad = np.array([1.0, 0.0], dtype=np.float32)  # shape mismatch with the 4-d asks
    rho, n_eff = _demand_independence([_missv("a", _at_cos(0.9)), _missv("b", bad)])
    assert (rho, n_eff) == (0.0, 2.0)  # no decidable pair ⇒ under-claim


def test_promote_stamps_demand_independence_non_promote_keeps_zero():
    # identical-vector demand promotes and carries n_eff≈1; a rejected case under-claims 0.0
    d = _decide(_rule(), _demand())
    assert d.promote is True
    assert d.rho_bar == pytest.approx(1.0, abs=1e-5) and d.n_eff == pytest.approx(
        1.0, abs=1e-5
    )
    rej = _decide(_rule(), [_miss("writer-1")])  # insufficient demand
    assert rej.promote is False and rej.rho_bar == 0.0 and rej.n_eff == 0.0


def test_n_eff_is_value_independent_of_the_verdict(monkeypatch):
    # ★ Law 3: the independence measure NEVER gates the verdict. Force absurd values — a
    # promoting case still promotes, a rejecting case still rejects.
    import hive.domain.lifecycle as lc

    monkeypatch.setattr(lc, "_demand_independence", lambda matched: (1.0, 0.0001))
    assert _decide(_rule(), _demand()).promote is True  # n_eff≈0 does not block
    monkeypatch.setattr(lc, "_demand_independence", lambda matched: (0.0, 9999.0))
    assert _decide(_rule(), _demand()).promote is True  # huge n_eff does not change it
    assert (
        _decide(_rule(), [_miss("writer-1")]).promote is False
    )  # gated paths untouched


# ── the verified-win rung: second mechanical path out of quarantine ─────────────


def test_verified_win_decision_truth_table():
    win = verified_win_decision(
        has_verified_win=True, competitor_top_sim=-1.0, competitor_tau=0.85
    )
    assert win.promote is True and win.reason == "verified_win"
    no_win = verified_win_decision(
        has_verified_win=False, competitor_top_sim=-1.0, competitor_tau=0.85
    )
    assert no_win.promote is False and no_win.reason == "no_verified_win"
    # the competitor veto is retained, inclusive at the threshold (DemandRule parity)
    veto = verified_win_decision(
        has_verified_win=True, competitor_top_sim=0.85, competitor_tau=0.85
    )
    assert veto.promote is False and veto.reason == "competitor_answers"
    below = verified_win_decision(
        has_verified_win=True, competitor_top_sim=0.80, competitor_tau=0.85
    )
    assert below.promote is True


def test_verified_win_decision_nonfinite_fails_closed():
    for bad in (float("nan"), float("inf"), float("-inf")):
        d = verified_win_decision(
            has_verified_win=True, competitor_top_sim=bad, competitor_tau=0.85
        )
        assert d.promote is False and d.reason == "non_finite"


class _VerifiedLifeStore(_LifeStore):
    """The _LifeStore plus the verified-wins read, recording every consult."""

    def __init__(self, *args, verified: set[int] | None = None, **kw):
        super().__init__(*args, **kw)
        self.verified = set(verified or ())
        self.verified_calls: list[list[int]] = []

    def verified_wins(self, episode_ids):
        self.verified_calls.append([int(e) for e in episode_ids])
        return {e for e in map(int, episode_ids) if e in self.verified}


def _thin_demand() -> list[MissRow]:
    return [_miss("other-1")]  # 1 < demand_m=3: demand rejects


def test_default_off_never_touches_the_verified_read():
    # no verified_reader wired (the default) ⇒ the rung is UNREACHABLE: the store's
    # verified read is never consulted even though a verified win exists.
    st = _VerifiedLifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_thin_demand(),
        index=FakeIndex(),
        verified={7},
    )
    out = _service(st).on_miss(_at_cos(0.95), agent_id="other-9")
    assert dict(out)[7].promote is False
    assert st.verified_calls == []  # the handle is never touched
    assert st.trust == {} and st.audits == []


def _verified_service(store, *, index=None):
    return LifecycleService(
        store=store,
        index=index if index is not None else (store.index or FakeIndex()),
        rule=_rule(),
        now=lambda: NOW,
        demand_window_s=WINDOW_S,
        quarantine_ttl_s=Q_TTL,
        provisional_ttl_s=P_TTL,
        verified_reader=store,
    )


def test_verified_win_promotes_when_demand_did_not():
    st = _VerifiedLifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_thin_demand(),
        index=FakeIndex(),
        verified={7},
    )
    out = _verified_service(st).on_miss(_at_cos(0.95), agent_id="other-9")
    d = dict(out)[7]
    assert d.promote is True and d.reason == "verified_win"
    assert st.trust[7] == (PROVISIONAL, NOW)
    assert st.verified_calls == [[7]]
    eid, kind, actor, ts, payload = st.audits[0]
    assert (eid, kind, actor, ts) == (7, "promote", "server", NOW)
    assert kind in EVIDENCE_KINDS
    body = json.loads(payload)
    assert body == {
        "rule": "verified_win",
        "evidence_kind": "outcome_verified_helped",
        "competitor_sim": -1.0,
        "reason": "verified_win",
    }


def test_no_verified_win_means_no_second_rung_promotion():
    st = _VerifiedLifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_thin_demand(),
        index=FakeIndex(),
        verified=set(),
    )
    out = _verified_service(st).on_miss(_at_cos(0.95), agent_id="other-9")
    assert dict(out)[7].promote is False
    assert st.trust == {} and st.audits == []


def test_verified_rung_keeps_the_competitor_veto():
    idx = FakeIndex()
    idx.add(99, CAND)  # a servable near-dup already answers
    st = _VerifiedLifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_thin_demand(),
        index=idx,
        verified={7},
    )
    out = _verified_service(st, index=idx).on_miss(_at_cos(0.95), agent_id="other-9")
    assert dict(out)[7].promote is False
    assert st.trust == {} and st.audits == []


def test_demand_promotion_never_consults_the_verified_read():
    # the rung runs ONLY when the demand rule did not promote — a demand-promoted
    # candidate keeps its demand audit and the verified read stays untouched.
    st = _VerifiedLifeStore(
        candidates=[(7, CAND, "writer-1", NOW - 5, NOW - 5)],
        misses=_demand(),
        index=FakeIndex(),
        verified={7},
    )
    out = _verified_service(st).on_miss(_at_cos(0.95), agent_id="other-9")
    assert dict(out)[7].reason == "demand"
    assert st.verified_calls == []
    assert json.loads(st.audits[0][4])["rule"] == "demand"


# ═══ promote_established: the provisional→established rung (v3 §3.1) ═════════════


class _EstStore:
    """The slice promote_established drives: the provisional-id read, the
    verified-win read (this fake doubles as the wired verified_reader), the
    SELF-REPORTED settled-win read (present so the named mutation — reading it
    instead — is executable and CAUGHT), the trust flip, and the audit write."""

    def __init__(self, provisional=(), verified=(), settled=()):
        self.provisional = [int(e) for e in provisional]
        self.verified = set(verified)
        self.settled = set(settled)
        self.trust: dict[int, tuple[str, int]] = {}
        self.audits: list[tuple] = []
        self.verified_calls: list[list[int]] = []
        self.settled_calls: list[list[int]] = []

    def provisional_ids(self):
        return list(self.provisional)

    def verified_wins(self, episode_ids):
        ids = [int(e) for e in episode_ids]
        self.verified_calls.append(ids)
        return {e for e in ids if e in self.verified}

    def settled_wins(self, episode_ids):
        # the UNION read (self-reported outcome_helped ∪ verified) — establish
        # fuel must NEVER come from here.
        ids = [int(e) for e in episode_ids]
        self.settled_calls.append(ids)
        return {e for e in ids if e in self.settled or e in self.verified}

    def set_trust(self, episode_id, new_trust, *, now):
        self.trust[int(episode_id)] = (new_trust, now)
        return True

    def insert_audit(self, episode_id, kind, actor, ts, payload):
        self.audits.append((episode_id, kind, actor, ts, payload))
        return len(self.audits)


def _est_service(store, *, reader, enabled: bool = True):
    return LifecycleService(
        store=store,
        index=FakeIndex(),
        rule=_rule(),
        now=lambda: NOW,
        demand_window_s=WINDOW_S,
        quarantine_ttl_s=Q_TTL,
        provisional_ttl_s=P_TTL,
        enabled=enabled,
        verified_reader=reader,
    )


def test_promote_established_flips_verified_provisionals_with_audit():
    st = _EstStore(provisional=[7, 8], verified={7})
    out = _est_service(st, reader=st).promote_established()
    assert out == [7]
    assert st.trust[7] == (ESTABLISHED, NOW)
    assert 8 not in st.trust  # no win ⇒ untouched
    eid, kind, actor, ts, payload = st.audits[0]
    assert (eid, kind, actor, ts) == (7, "promote", "server", NOW)
    assert kind in EVIDENCE_KINDS
    body = json.loads(payload)
    assert body["rule"] == "verified_established"
    assert body["evidence_kind"] == "outcome_verified_helped"


def test_promote_established_ignores_settled_wins():
    # THE named-mutation twin (CT-8 self-report): a self-reported outcome_helped
    # (settled, NOT verified) must never establish — promoting on settled_wins
    # instead of verified_wins makes 7 establish here, which reds.
    st = _EstStore(provisional=[7], verified=set(), settled={7})
    out = _est_service(st, reader=st).promote_established()
    assert out == [] and st.trust == {} and st.audits == []
    assert st.verified_calls == [[7]]  # the verified read IS the fuel…
    assert st.settled_calls == []  # …the settled read is never consulted


def test_promote_established_reads_the_provisional_set_only():
    # a quarantined row with a verified win goes via the provisional rung first —
    # the sweep consults verified_wins with EXACTLY the provisional ids.
    st = _EstStore(provisional=[7], verified={7, 9})  # 9 is not provisional
    out = _est_service(st, reader=st).promote_established()
    assert out == [7]
    assert st.verified_calls == [[7]]
    assert 9 not in st.trust


def test_promote_established_inert_when_disabled_or_readerless():
    st = _EstStore(provisional=[7], verified={7})
    assert _est_service(st, reader=None).promote_established() == []
    assert _est_service(st, reader=st, enabled=False).promote_established() == []
    assert st.trust == {} and st.verified_calls == []


def test_promote_established_empty_provisional_set_is_inert():
    st = _EstStore(provisional=[], verified={7})
    assert _est_service(st, reader=st).promote_established() == []
    assert st.verified_calls == []  # nothing to ask about


def test_promote_established_fail_open_on_store_fault():
    class _Boom(_EstStore):
        def provisional_ids(self):
            raise RuntimeError("db down")

    st = _Boom(provisional=[7], verified={7})
    assert _est_service(st, reader=st).promote_established() == []
    assert st.trust == {}


def test_promote_established_skips_unflippable_rows():
    class _NoFlip(_EstStore):
        def set_trust(self, episode_id, new_trust, *, now):
            return False  # lost race / row vanished

    st = _NoFlip(provisional=[7], verified={7})
    out = _est_service(st, reader=st).promote_established()
    assert out == [] and st.audits == []  # no flip ⇒ no audit, not listed
