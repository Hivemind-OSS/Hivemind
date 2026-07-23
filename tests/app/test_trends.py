"""Demand-health trends: half-open window partition, the demand-entropy KPI's
bounds, zero-safety on an empty store, the two-metric window shape, and the
envelope shape contract. Driven against the REAL sqlite store (the aggregates
are SQL — a fake would test the fake)."""

from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pytest

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.gaps import cluster_misses
from hive.app.trends import TrendWindow, WINDOW_DAYS, compute_trends, demand_entropy

_TREND_FIELDS = {
    "recalls_confident",
    "misses_abstained",
    "misses_no_match",
    "confident_rate",
    "demand_entropy",
}

D = 8
DAY = 86_400
NOW = 100 * DAY
CUR_EDGE = NOW - WINDOW_DAYS * DAY  # boundary tick: belongs to PREVIOUS


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


def _episode(s, text, *, ts, vec=U) -> int:
    eid, _ = s.stage(text=text, weight=1.0, proposed_by="writer", ts=ts)
    assert s.complete(
        eid, vec, expected_version=0, trust="established", last_active_ts=ts
    )
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
    assert out["previous"]["misses_no_match"] == 1  # the boundary row
    assert out["previous"]["misses_abstained"] == 1
    total = (
        out["current"]["misses_no_match"]
        + out["current"]["misses_abstained"]
        + out["previous"]["misses_no_match"]
        + out["previous"]["misses_abstained"]
    )
    assert total == 3  # nothing dropped/doubled


def test_window_is_one_week():
    # the KPI window is ONE week: current (now−7d, now], previous (now−14d, now−7d].
    # literal day offsets pin the 7-day semantics (a revert to 14 reds this — the
    # −8d miss would fall into `current` and the −15d miss into `previous`).
    s = _store()
    s.record_miss("recent", U.tobytes(), "a", "no_match", ts=NOW - 6 * DAY)  # → current
    s.record_miss("prior", U.tobytes(), "a", "no_match", ts=NOW - 8 * DAY)  # → previous
    s.record_miss("stale", U.tobytes(), "a", "no_match", ts=NOW - 15 * DAY)  # → neither
    out = _trends(s)
    assert out["current"]["misses_no_match"] == 1
    assert out["previous"]["misses_no_match"] == 1
    assert out["current"]["misses_no_match"] + out["previous"]["misses_no_match"] == 2


# ── the demand-entropy KPI ─────────────────────────────────────────────────────
def test_demand_entropy_bounds():
    assert demand_entropy([]) == 0.0  # no clusters
    assert demand_entropy([7]) == 0.0  # ONE cluster ⇒ 0.0 (no div-by-0)
    assert demand_entropy([5, 5]) == pytest.approx(1.0)  # uniform ⇒ ~1.0
    assert demand_entropy([5, 5, 5, 5]) == pytest.approx(1.0)
    skewed = demand_entropy([97, 1, 1, 1])
    assert 0.0 < skewed < 0.35  # concentrated ⇒ low
    assert 0.0 <= demand_entropy([10**9, 1]) <= 1.0  # clamped


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


# ── zero-safety ─────────────────────────────────────────────────────────────────
def test_rates_zero_safe():
    out = _trends(_store())  # EMPTY store
    for win in ("current", "previous"):
        w = out[win]
        assert set(w) == _TREND_FIELDS  # exactly the two-metric core
        assert w["recalls_confident"] == 0 and w["confident_rate"] == 0.0
        assert w["demand_entropy"] == 0.0
        assert w["misses_abstained"] == 0 and w["misses_no_match"] == 0
    assert out["deltas"]["confident_rate"] == 0.0
    assert out["deltas"]["demand_entropy"] == 0.0


def test_confident_rate_and_miss_counts():
    s = _store()
    eid = _episode(s, "x" * 40, ts=NOW - 5 * DAY)
    cap = _episode(s, "captured row", ts=NOW - 5 * DAY)
    # two confident recalls (distinct traces), one of them serving both rows
    s.record_exposure("t-1", [(eid, 0.5)], agent_id="a", ts=NOW - 4 * DAY)
    s.record_exposure("t-2", [(eid, 0.5), (cap, 0.4)], agent_id="b", ts=NOW - 3 * DAY)
    # one abstain + one no_match in-window
    s.record_miss("m1", U.tobytes(), "a", "abstained", ts=NOW - 2 * DAY)
    s.record_miss("m2", U.tobytes(), "a", "no_match", ts=NOW - 2 * DAY)
    cur = _trends(s)["current"]
    assert cur["recalls_confident"] == 2
    assert cur["misses_abstained"] == 1 and cur["misses_no_match"] == 1
    assert cur["confident_rate"] == pytest.approx(
        2 / 4
    )  # confident / (confident + misses)


# ── the two-metric window shape (the trim guard) ────────────────────────────────
def test_trends_window_is_two_metric_only():
    # the demand-health window is EXACTLY the THEORY §8.3 rot-detection core — the
    # heavier KPI surface (promotions/establishments/supersessions, median-days,
    # dead-capture ratio, token cost) was trimmed.
    w = TrendWindow(
        recalls_confident=1,
        misses_abstained=2,
        misses_no_match=3,
        confident_rate=0.5,
        demand_entropy=0.0,
    )
    assert set(asdict(w).keys()) == _TREND_FIELDS


# ── envelope contract ──────────────────────────────────────────────────────────
def test_health_trends_shape():
    from tests.mcp._helpers import build_real_server, content, tool_call

    server, _clock = build_real_server()
    snap = content(tool_call(server, "hive_health", {"include_trends": True}))
    trends = snap["trends"]
    assert set(trends) == {"current", "previous", "deltas"}
    assert set(trends["current"]) == _TREND_FIELDS
    assert set(trends["previous"]) == _TREND_FIELDS
    assert set(trends["deltas"]) == _TREND_FIELDS
    plain = content(tool_call(server, "hive_health", {}))
    assert "trends" not in plain  # opt-in only


# ── gaps clustering regression (re-homed from the deleted contested tests) ──────
def _miss_row(vec, miss_type="abstained", ts=100, text="how do we rotate the key"):
    return {"query_text": text, "vector": vec, "miss_type": miss_type, "ts": ts}


def test_cluster_misses_report_unchanged():
    # the demand-gap report keeps its shape: no _vec leak, the vector-less refused
    # aggregate still present and visible (the surviving demand-gap channel — the
    # contested report on top of it was cut).
    rows = [_miss_row(U, "abstained", ts=100 + i) for i in range(2)]
    rows += [_miss_row(None, "secret_refused", ts=400, text="")]
    out = cluster_misses(rows, tau=0.75)
    assert all("_vec" not in c for c in out)
    assert {c["miss_count"] for c in out} == {2, 1}
    refused = [c for c in out if c["representative_query"] == ""][0]
    assert refused["miss_types"] == {"secret_refused": 1}
