"""The pure lifecycle predicates: ``is_servable`` (the single serving rule — approved
AND established-or-fresh-provisional) and ``decayed`` (lazy TTL decay for quarantined/
provisional rows). Truth-table + boundary pins; the DemandRule/LifecycleService tests
live in test_demand_rule.py."""
from __future__ import annotations

import inspect

import pytest

from hive.domain import lifecycle as lifecycle_mod
from hive.domain.lifecycle import (
    DEPRECATED, ESTABLISHED, PROVISIONAL, QUARANTINED, TRUST_STATES,
    DemandRule, LifecycleService, decayed, is_servable,
)

TTL = 100          # provisional_ttl_s used by the truth table
NOW = 1_000


def test_trust_state_constants():
    assert TRUST_STATES == (QUARANTINED, PROVISIONAL, ESTABLISHED, DEPRECATED)
    assert QUARANTINED == "quarantined" and DEPRECATED == "deprecated"


# ── the serving predicate, exhaustively: each trust state × fresh/lapsed × status ──
@pytest.mark.parametrize("status", ["pending", "approved"])
@pytest.mark.parametrize("trust", TRUST_STATES)
@pytest.mark.parametrize("fresh", [True, False])
def test_is_servable_truth_table(status: str, trust: str, fresh: bool):
    last_active = NOW - 1 if fresh else NOW - TTL - 1
    expected = (status == "approved") and (
        trust == ESTABLISHED or (trust == PROVISIONAL and fresh))
    assert is_servable(status=status, trust=trust, last_active_ts=last_active,
                       now=NOW, provisional_ttl_s=TTL) is expected


def test_is_servable_provisional_ttl_boundary():
    # servable ≡ last_active_ts > now − ttl (STRICT): at exactly the boundary the row
    # is no longer served (fail-closed direction).
    assert is_servable(status="approved", trust=PROVISIONAL,
                       last_active_ts=NOW - TTL + 1, now=NOW, provisional_ttl_s=TTL) is True
    assert is_servable(status="approved", trust=PROVISIONAL,
                       last_active_ts=NOW - TTL, now=NOW, provisional_ttl_s=TTL) is False


def test_is_servable_established_ignores_freshness():
    # established is served regardless of last_active_ts (it never age-decays).
    assert is_servable(status="approved", trust=ESTABLISHED, last_active_ts=0,
                       now=10**12, provisional_ttl_s=1) is True


def test_is_servable_unknown_trust_fails_closed():
    # total function: an unrecognized trust label is NOT servable (never raises).
    assert is_servable(status="approved", trust="bogus", last_active_ts=NOW,
                       now=NOW, provisional_ttl_s=TTL) is False


# ── decay boundaries ───────────────────────────────────────────────────────────
Q_TTL, P_TTL = 100, 200


def _dec(**kw) -> bool:
    base = dict(trust=QUARANTINED, last_active_ts=0, created_ts=0, now=NOW,
                quarantine_ttl_s=Q_TTL, provisional_ttl_s=P_TTL)
    base.update(kw)
    return decayed(**base)


def test_decayed_quarantine_boundary():
    # dead iff now − max(created_ts, last_active_ts) > quarantine_ttl (STRICT).
    assert _dec(trust=QUARANTINED, created_ts=NOW - Q_TTL, last_active_ts=0) is False
    assert _dec(trust=QUARANTINED, created_ts=NOW - Q_TTL - 1, last_active_ts=0) is True


def test_decayed_quarantine_activity_refreshes():
    # max(created, last_active): a recent touch keeps an old quarantined row alive.
    assert _dec(trust=QUARANTINED, created_ts=NOW - 10 * Q_TTL,
                last_active_ts=NOW - 1) is False


def test_decayed_provisional_boundary():
    # provisional liveness reads last_active_ts ONLY (created_ts irrelevant).
    assert _dec(trust=PROVISIONAL, last_active_ts=NOW - P_TTL, created_ts=0) is False
    assert _dec(trust=PROVISIONAL, last_active_ts=NOW - P_TTL - 1, created_ts=0) is True
    assert _dec(trust=PROVISIONAL, last_active_ts=NOW - P_TTL - 1,
                created_ts=NOW) is True          # a recent created_ts does NOT revive it


def test_decayed_established_and_deprecated_never():
    assert _dec(trust=ESTABLISHED, now=10**12) is False
    assert _dec(trust=DEPRECATED, now=10**12) is False


def test_promotion_stamp_freshness():
    # promotion stamps last_active_ts=now, so a fresh promotion is never
    # instantly dead AND is immediately servable.
    assert _dec(trust=PROVISIONAL, last_active_ts=NOW, created_ts=NOW - 10**9) is False
    assert is_servable(status="approved", trust=PROVISIONAL, last_active_ts=NOW,
                       now=NOW, provisional_ttl_s=1) is True


# ── ONE mechanical rung: sweep is decay-only (the survival rung is removed) ──────
class _DecayOnlyStore:
    """Minimal duck-typed store: sweep() drives only sweep_decayed. A surviving
    survival pass would call survival_candidates — absent here on purpose, so any
    mechanical establish attempt would raise (and the test would notice)."""

    def sweep_decayed(self, *, now, q_ttl_s, p_ttl_s):
        return {QUARANTINED: 0, PROVISIONAL: 1}


def _svc(store) -> LifecycleService:
    return LifecycleService(
        store=store, index=None,
        rule=DemandRule(demand_m=3, demand_tau=0.75, competitor_tau=0.85),
        now=lambda: NOW, demand_window_s=1, quarantine_ttl_s=1, provisional_ttl_s=1)


def test_sweep_is_decay_only():
    # sweep() returns EXACTLY sweep_decayed's verdict — no 'established' key: the
    # decay sweep never establishes (that is promote_established's job, driven by
    # canonical-line outcome verification, not by TTL bookkeeping).
    out = _svc(_DecayOnlyStore()).sweep()
    assert out == {QUARANTINED: 0, PROVISIONAL: 1}
    assert "established" not in out
    # the survival ctor seam is gone from the service…
    assert "survival_rule" not in inspect.signature(LifecycleService.__init__).parameters
    # …and the SurvivalRule type is gone from the domain module.
    assert not hasattr(lifecycle_mod, "SurvivalRule")
    assert not hasattr(lifecycle_mod, "ExposureRow")
