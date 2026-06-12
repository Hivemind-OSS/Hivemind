"""SurvivalRule (the pure provisional→established verdict) + the sweep-time
survival pass in LifecycleService, store-independent via a duck-typed fake.

The rule's headline property mirrors the demand rule's STRUCTURAL anti-gaming:
exposure consisting solely of the writer's own reads never establishes — the
writer cannot manufacture both supply and consumption. The span clause keeps a
one-burst row provisional (spread over time, not volume alone)."""
from __future__ import annotations

import json

import pytest

from hive.domain.lifecycle import (
    ESTABLISHED, PROVISIONAL, QUARANTINED, DemandRule, ExposureRow,
    LifecycleService, SurvivalRule, decayed,
)
from tests.fakes import FakeIndex

NOW = 100 * 86_400
DAY = 86_400


def _exp(agent: str, ts: int) -> ExposureRow:
    return ExposureRow(agent_id=agent, ts=ts)


def _rule(e: int = 2, days: int = 14, m: int = 5) -> SurvivalRule:
    return SurvivalRule(survival_e=e, survival_days=days, survival_min_exposures=m)


def _spread(writer: str = "writer-1", t0: int = 0) -> list[ExposureRow]:
    """The establishing fixture: writer + 2 distinct readers, 5 exposures,
    first-to-last span exactly 14 days (the INCLUSIVE boundary)."""
    return [_exp(writer, t0), _exp("reader-a", t0 + DAY),
            _exp("reader-b", t0 + 3 * DAY), _exp("reader-a", t0 + 7 * DAY),
            _exp("reader-b", t0 + 14 * DAY)]


# ── the rule ───────────────────────────────────────────────────────────────────
def test_rule_ctor_validates():
    for kw in ({"survival_e": 0}, {"survival_days": 0}, {"survival_min_exposures": 0}):
        with pytest.raises(ValueError):
            SurvivalRule(**{"survival_e": 2, "survival_days": 14,
                            "survival_min_exposures": 5, **kw})


def test_establishes_at_spread_thresholds():
    d = _rule().decide(writer="writer-1", exposures=_spread(), now=NOW)
    assert d.establish is True and d.reason == "survival"
    assert d.n_exposures == 5 and d.n_other_identities == 2
    assert d.span_days == pytest.approx(14.0)   # exactly survival_days passes (inclusive)


def test_writer_only_exposure_never_establishes():
    # the anti-gaming arm ★: volume + span both satisfied, ALL reads by the writer
    exposures = [_exp("writer-1", i * 4 * DAY) for i in range(5)]   # 16d span, 5 reads
    d = _rule().decide(writer="writer-1", exposures=exposures, now=NOW)
    assert d.establish is False and d.reason == "insufficient_identities"
    assert d.n_other_identities == 0
    # one non-writer reader is still below survival_e=2
    d2 = _rule().decide(writer="writer-1",
                        exposures=exposures + [_exp("reader-a", 16 * DAY)], now=NOW)
    assert d2.establish is False and d2.reason == "insufficient_identities"
    assert d2.n_other_identities == 1


def test_burst_without_span_does_not_establish():
    # 10 exposures, 3 distinct readers, all inside ONE hour ⇒ span fails
    exposures = [_exp(f"reader-{i % 3}", 1000 + i * 360) for i in range(10)]
    d = _rule().decide(writer="writer-1", exposures=exposures, now=NOW)
    assert d.establish is False and d.reason == "insufficient_span"
    assert d.n_exposures == 10 and d.n_other_identities == 3
    # one second short of the span boundary still refuses
    edge = [_exp("reader-a", 0), _exp("reader-b", 1),
            _exp("reader-a", 2), _exp("reader-b", 3),
            _exp("reader-a", 14 * DAY - 1)]
    d2 = _rule().decide(writer="writer-1", exposures=edge, now=NOW)
    assert d2.establish is False and d2.reason == "insufficient_span"


def test_below_min_exposures_does_not_establish():
    exposures = [_exp("reader-a", 0), _exp("reader-b", 7 * DAY),
                 _exp("reader-a", 14 * DAY), _exp("reader-b", 15 * DAY)]
    d = _rule(m=5).decide(writer="writer-1", exposures=exposures, now=NOW)
    assert d.establish is False and d.reason == "insufficient_exposures"
    assert d.n_exposures == 4 and d.n_other_identities == 2


def test_empty_exposures_refuse_totally():
    d = _rule().decide(writer="writer-1", exposures=[], now=NOW)
    assert d.establish is False and d.reason == "insufficient_identities"
    assert d.n_exposures == 0 and d.span_days == 0.0


def test_established_by_survival_never_decays():
    # the decayed truth table extended: established is TTL-immune regardless of
    # how stale its clocks are (supersession is its only retirement)
    assert decayed(trust=ESTABLISHED, last_active_ts=0, created_ts=0,
                   now=10**12, quarantine_ttl_s=1, provisional_ttl_s=1) is False


# ── the sweep-time survival pass, against a duck-typed recording store ──────────
class _SurvivalStore:
    """The store slice the survival pass drives. Mirrors the real adapter's
    semantics: candidates are PROVISIONAL-only (an established row drops out of
    the next scan — idempotency), exposures are windowed strictly-after."""

    def __init__(self, *, writer: str = "writer-1",
                 exposures: list[ExposureRow] | None = None):
        self.exposures = list(exposures or [])
        self.writer = writer
        self.trust: dict[int, str] = {7: PROVISIONAL}
        self.audits: list[tuple] = []
        self.calls: list[tuple] = []

    def sweep_decayed(self, *, now, q_ttl_s, p_ttl_s):
        self.calls.append(("sweep_decayed", now, q_ttl_s, p_ttl_s))
        return {QUARANTINED: 0, PROVISIONAL: 0}

    def survival_candidates(self, *, since_ts, min_exposures):
        self.calls.append(("survival_candidates", since_ts, min_exposures))
        eligible = [e for e in self.exposures if e.ts > since_ts]
        if self.trust.get(7) == PROVISIONAL and len(eligible) >= min_exposures:
            return [(7, self.writer)]
        return []

    def exposures_for(self, episode_id, *, since_ts):
        self.calls.append(("exposures_for", episode_id, since_ts))
        return [e for e in self.exposures if e.ts > since_ts]

    def set_trust(self, episode_id, new_trust, *, now):
        self.trust[episode_id] = new_trust
        return True

    def insert_audit(self, episode_id, kind, actor, ts, payload):
        self.audits.append((episode_id, kind, actor, ts, payload))
        return len(self.audits)

    # unused demand-trigger surface (sweep never touches these here)
    def quarantined_candidates(self, **kw):  # pragma: no cover - defensive
        raise AssertionError("sweep must not scan quarantine for survival")

    def misses_window(self, since_ts):       # pragma: no cover - defensive
        raise AssertionError("sweep must not read misses for survival")


P_TTL = 45 * DAY


def _service(store, *, survival_rule=_rule(), enabled: bool = True):
    return LifecycleService(
        store=store, index=FakeIndex(),
        rule=DemandRule(demand_m=3, demand_tau=0.75, competitor_tau=0.85),
        now=lambda: NOW, demand_window_s=14 * DAY,
        quarantine_ttl_s=14 * DAY, provisional_ttl_s=P_TTL,
        enabled=enabled, survival_rule=survival_rule)


def test_sweep_establishes_and_audits():
    st = _SurvivalStore(exposures=_spread(t0=NOW - 20 * DAY))
    svc = _service(st)
    out = svc.sweep()
    assert out == {QUARANTINED: 0, PROVISIONAL: 0, "established": 1}
    assert st.trust[7] == ESTABLISHED
    eid, kind, actor, ts, payload = st.audits[0]
    assert (eid, kind, actor, ts) == (7, "establish", "server", NOW)
    body = json.loads(payload)
    assert body == {"rule": "survival", "n_exposures": 5, "n_other_identities": 2,
                    "span_days": 14.0, "reason": "survival"}
    # the candidate scan + exposure read are windowed to the provisional horizon
    assert ("survival_candidates", NOW - P_TTL, 5) in st.calls
    assert ("exposures_for", 7, NOW - P_TTL) in st.calls
    # idempotent: the established row leaves the candidate set; no second audit
    out2 = svc.sweep()
    assert out2 == {QUARANTINED: 0, PROVISIONAL: 0, "established": 0}
    assert len(st.audits) == 1


def test_sweep_writer_only_exposure_never_establishes():
    st = _SurvivalStore(
        exposures=[_exp("writer-1", NOW - i * 4 * DAY) for i in range(5)])
    out = _service(st).sweep()
    assert out["established"] == 0
    assert st.trust[7] == PROVISIONAL and st.audits == []


def test_sweep_without_survival_rule_is_byte_stable():
    st = _SurvivalStore(exposures=_spread(t0=NOW - 20 * DAY))
    out = _service(st, survival_rule=None).sweep()
    assert out == {QUARANTINED: 0, PROVISIONAL: 0}      # no "established" key
    assert st.trust[7] == PROVISIONAL and st.audits == []


def test_sweep_disabled_is_inert():
    st = _SurvivalStore(exposures=_spread(t0=NOW - 20 * DAY))
    assert _service(st, enabled=False).sweep() == {}
    assert st.calls == [] and st.audits == []


def test_solo_established_stays_human_only():
    # AC9: in solo mode the MECHANICAL ladder tops out at provisional. The solo
    # demand rule promotes on elapsed-span (one identity is enough), but the
    # survival rule deliberately keeps the distinct-identity key — writer-only
    # exposure never establishes, so `established` is reachable only through
    # hive_write (the human vouch). HITL held structurally, not by wording.
    import numpy as np

    from hive.domain.lifecycle import DemandRule, MissRow

    cand = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    solo = DemandRule(demand_m=3, demand_tau=0.75, competitor_tau=0.85,
                      solo_mode=True, solo_min_span_days=1)
    misses = [MissRow(vector=cand, agent_id="solo-dev", ts=t)
              for t in (0, DAY // 2, DAY)]                    # 24h span, ONE identity
    promo = solo.decide(candidate_vec=cand, candidate_writer="solo-dev",
                        misses=misses, competitor_top_sim=-1.0)
    assert promo.promote is True                              # → provisional unlocked
    # …but the same single identity reading it for a month establishes NOTHING
    reads = [_exp("solo-dev", i * 5 * DAY) for i in range(6)]
    surv = _rule().decide(writer="solo-dev", exposures=reads, now=NOW)
    assert surv.establish is False and surv.reason == "insufficient_identities"
