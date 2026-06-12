"""CV4 — convergence trends: half-open window partition, the demand-entropy
KPI's bounds, zero-safety on an empty store, and the envelope shape contract.
Driven against the REAL sqlite store (the aggregates are SQL — a fake would
test the fake)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.gaps import cluster_misses
from hive.app.trends import WINDOW_DAYS, compute_trends, demand_entropy

D = 8
DAY = 86_400
NOW = 100 * DAY
CUR_EDGE = NOW - WINDOW_DAYS * DAY            # boundary tick: belongs to PREVIOUS


def _unit(*xs) -> np.ndarray:
    v = np.array(list(xs) + [0.0] * (D - len(xs)), dtype=np.float32)
    return v / np.linalg.norm(v)


U, W = _unit(1.0), _unit(0.0, 1.0)


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"), index=ExhaustiveCosineIndex(D))


def _clusterer(rows):
    return cluster_misses(rows, tau=0.75)


def _trends(s, now=NOW):
    return compute_trends(s, _clusterer, now=now)


def _episode(s, text, *, ts, approver=None, vec=U) -> int:
    eid, _ = s.stage(text=text, weight=1.0, source="t", tags="",
                     proposed_by="writer", ts=ts)
    assert s.complete(eid, vec, expected_version=0, trust="established",
                      approver=approver, approved_ts=ts, last_active_ts=ts)
    return eid


# ── window partition ───────────────────────────────────────────────────────────
def test_windows_partition_correctly():
    s = _store()
    # three misses: exactly AT the boundary (→ previous), just after (→ current),
    # and inside the previous window — each lands in EXACTLY one window
    s.record_miss("at edge", U.tobytes(), "a", "no_match", ts=CUR_EDGE)
    s.record_miss("just in", U.tobytes(), "a", "no_match", ts=CUR_EDGE + 1)
    s.record_miss("older", U.tobytes(), "a", "abstained", ts=CUR_EDGE - DAY)
    out = _trends(s)
    assert out["current"]["misses_no_match"] == 1
    assert out["previous"]["misses_no_match"] == 1            # the boundary row
    assert out["previous"]["misses_abstained"] == 1
    total = (out["current"]["misses_no_match"] + out["current"]["misses_abstained"]
             + out["previous"]["misses_no_match"] + out["previous"]["misses_abstained"])
    assert total == 3                                         # nothing dropped/doubled


# ── the demand-entropy KPI ─────────────────────────────────────────────────────
def test_demand_entropy_bounds():
    assert demand_entropy([]) == 0.0                          # no clusters
    assert demand_entropy([7]) == 0.0                         # ONE cluster ⇒ 0.0 (no div-by-0)
    assert demand_entropy([5, 5]) == pytest.approx(1.0)       # uniform ⇒ ~1.0
    assert demand_entropy([5, 5, 5, 5]) == pytest.approx(1.0)
    skewed = demand_entropy([97, 1, 1, 1])
    assert 0.0 < skewed < 0.35                                # concentrated ⇒ low
    assert 0.0 <= demand_entropy([10**9, 1]) <= 1.0           # clamped


def test_demand_entropy_composes_cluster_masses_not_misses():
    s = _store()
    # 4 misses near U + 4 near W ⇒ TWO uniform clusters ⇒ entropy ~1.0; the
    # normalization base is the CLUSTER count (2), never the miss count (8)
    for i in range(4):
        s.record_miss("ask u", U.tobytes(), "a", "abstained", ts=NOW - 100 + i)
        s.record_miss("ask w", W.tobytes(), "a", "abstained", ts=NOW - 50 + i)
    out = _trends(s)
    assert out["current"]["demand_entropy"] == pytest.approx(1.0, abs=1e-6)
    # one direction only ⇒ one cluster ⇒ 0.0
    s2 = _store()
    for i in range(5):
        s2.record_miss("ask u", U.tobytes(), "a", "abstained", ts=NOW - 100 + i)
    assert _trends(s2)["current"]["demand_entropy"] == 0.0


# ── zero-safety + lifecycle counters ───────────────────────────────────────────
def test_rates_and_ratios_zero_safe():
    out = _trends(_store())                                   # EMPTY store
    for win in ("current", "previous"):
        w = out[win]
        assert w["recalls_confident"] == 0 and w["confident_rate"] == 0.0
        assert w["demand_entropy"] == 0.0 and w["dead_capture_ratio"] == 0.0
        assert w["median_days_to_promotion"] is None
        assert w["est_tokens_served"] == 0
    assert out["deltas"]["median_days_to_promotion"] is None  # None-safe delta
    assert out["deltas"]["confident_rate"] == 0.0


def test_counters_rates_and_tokens():
    s = _store()
    text = "x" * 40                                           # 40 chars ⇒ 10 est tokens
    eid = _episode(s, text, ts=NOW - 5 * DAY, approver="h")
    cap = _episode(s, "captured row", ts=NOW - 5 * DAY)       # approver=None ⇒ capture
    # two confident recalls (distinct traces), one of them serving both rows
    s.record_exposure("t-1", [(eid, 0.5)], agent_id="a", ts=NOW - 4 * DAY)
    s.record_exposure("t-2", [(eid, 0.5), (cap, 0.4)], agent_id="b", ts=NOW - 3 * DAY)
    # one abstain + one no_match in-window
    s.record_miss("m1", U.tobytes(), "a", "abstained", ts=NOW - 2 * DAY)
    s.record_miss("m2", U.tobytes(), "a", "no_match", ts=NOW - 2 * DAY)
    # lifecycle events: promote (creation→promotion 3d), establish, supersede,
    # and one quarantined TTL expiry against the one capture
    s.insert_audit(cap, "promote", "server", NOW - 2 * DAY, '{"rule":"demand"}')
    s.insert_audit(cap, "establish", "server", NOW - DAY, '{"rule":"survival"}')
    s.insert_audit(eid, "supersede", "h", NOW - DAY, '{}')
    s.insert_audit(cap, "ttl_expired", "server", NOW - DAY,
                   json.dumps({"from": "quarantined"}))
    s.insert_audit(cap, "ttl_expired", "server", NOW - DAY,
                   json.dumps({"from": "provisional"}))       # NOT a dead capture
    cur = _trends(s)["current"]
    assert cur["recalls_confident"] == 2
    assert cur["misses_abstained"] == 1 and cur["misses_no_match"] == 1
    assert cur["confident_rate"] == pytest.approx(2 / 4)
    assert cur["n_promotions"] == 1 and cur["n_establishments"] == 1
    assert cur["n_supersessions"] == 1
    assert cur["median_days_to_promotion"] == pytest.approx(3.0)
    assert cur["dead_capture_ratio"] == pytest.approx(1 / 1)  # 1 expiry / 1 capture
    # t-1 served 40 chars (10 tokens); t-2 served 40 + len("captured row")=12 (3)
    assert cur["est_tokens_served"] == 10 + 10 + 3


# ── envelope contract ──────────────────────────────────────────────────────────
def test_health_trends_shape():
    from tests.mcp._helpers import build_real_server, content, tool_call
    server, _clock = build_real_server()
    snap = content(tool_call(server, "hive_health", {"include_trends": True}))
    trends = snap["trends"]
    assert set(trends) == {"current", "previous", "deltas"}
    fields = {"recalls_confident", "misses_abstained", "misses_no_match",
              "confident_rate", "demand_entropy", "n_promotions",
              "n_establishments", "n_supersessions", "median_days_to_promotion",
              "dead_capture_ratio", "est_tokens_served"}
    assert set(trends["current"]) == fields
    assert set(trends["previous"]) == fields
    assert set(trends["deltas"]) == fields
    plain = content(tool_call(server, "hive_health", {}))
    assert "trends" not in plain                              # opt-in only
