"""Demand-health trends for ``hive_health(include_trends=true)``.

Lazy SQL over the EXISTING tables (exposure, recall_misses), two fixed windows
(current 7d vs previous 7d), computed at report time only: no new table, no
scheduler, no write-path change. App-side like ``gaps.py``; the store's
connection is read directly (this module is the one consumer of cross-table
windowed aggregates — pushing one-off methods into the store would widen its
surface for a single report).

**The demand-health KPI, defined:** ``confident_rate`` ↑ AND ``demand_entropy`` ↓
(demand is being answered, and what remains is concentrated/fillable rather than
diffuse noise). These are the ONLY window into silent fail-open rot (THEORY §8.3)
— fail-open side-channels can corrupt the demand signal invisibly, so the trend
is kept load-bearing. They are COVERAGE proxies — whether a served memory
actually helped (outcome ground truth) is not measured by this report.

Window semantics: half-open ``(lo, hi]`` — an event exactly at the boundary
``now − 7d`` belongs to the PREVIOUS window, so the two windows partition.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Callable, Sequence

import numpy as np

_DAY_S = 86_400
WINDOW_DAYS = 7


@dataclass(frozen=True)
class TrendWindow:
    """One window's demand-health aggregates — the ONLY window into silent
    fail-open rot (THEORY §8.3). Every field is total/zero-safe — an empty store
    yields a fully-populated window of zeros, never a raise."""
    recalls_confident: int
    misses_abstained: int
    misses_no_match: int
    confident_rate: float                 # confident / (confident + misses); 0.0 on empty
    demand_entropy: float                 # H over miss-cluster mass / ln(C) ∈ [0,1]; 0.0 if <2 clusters


def demand_entropy(cluster_masses: Sequence[int]) -> float:
    """Normalized entropy of the miss-cluster mass distribution: ``H/ln(C)``
    over C clusters. 0.0 when C < 2 (one gap, or none — nothing diffuse; also
    the ln(1)=0 guard). Clamped to [0,1]. Falling = unmet demand is
    concentrating into fillable gaps; ~1.0 = diffuse noise. // O(C)."""
    masses = [float(m) for m in cluster_masses if m > 0]
    c = len(masses)
    if c < 2:
        return 0.0
    total = math.fsum(masses)
    h = -math.fsum((m / total) * math.log(m / total) for m in masses)
    return max(0.0, min(1.0, h / math.log(c)))


def compute_trends(store, gaps_clusterer: Callable[[list], list[dict]], *,
                   now: int) -> dict:
    """``{"current", "previous", "deltas"}`` — current ``(now−7d, now]`` vs
    previous ``(now−14d, now−7d]``. ``gaps_clusterer`` is the existing miss
    clustering (rows → cluster dicts with ``miss_count``), injected so the
    entropy composes the SAME neighborhoods the gap report and the demand rule
    see (window caps reused — the clusterer's top-N is the entropy's support).
    A None-median delta is None, never a fake 0. // report-time SQL only."""
    t = int(now)
    cur = _window(store, gaps_clusterer, lo=t - WINDOW_DAYS * _DAY_S, hi=t)
    prev = _window(store, gaps_clusterer, lo=t - 2 * WINDOW_DAYS * _DAY_S,
                   hi=t - WINDOW_DAYS * _DAY_S)
    deltas: dict = {}
    for k, c in asdict(cur).items():
        p = getattr(prev, k)
        deltas[k] = None if (c is None or p is None) else round(c - p, 6)
    return {"current": asdict(cur), "previous": asdict(prev), "deltas": deltas}


def _window(store, gaps_clusterer, *, lo: int, hi: int) -> TrendWindow:
    conn = store.conn

    # confident recalls: one exposure batch per confident serve, keyed trace_id
    confident = int(conn.execute(
        "SELECT COUNT(DISTINCT trace_id) AS c FROM exposure "
        "WHERE injected_ts>? AND injected_ts<=?", (lo, hi)).fetchone()["c"])

    by_type = {r["miss_type"]: int(r["c"]) for r in conn.execute(
        "SELECT miss_type, COUNT(*) AS c FROM recall_misses "
        "WHERE ts>? AND ts<=? GROUP BY miss_type", (lo, hi))}
    abstained = by_type.get("abstained", 0)
    no_match = by_type.get("no_match", 0)
    denom = confident + abstained + no_match
    confident_rate = (confident / denom) if denom else 0.0

    # demand entropy over the window's vector-bearing misses, clustered by the
    # SAME machinery the gap report uses (refused rows carry no vector — they
    # are policy refusals, not coverage demand)
    rows = [{"query_text": r["query_text"],
             "vector": np.frombuffer(r["query_vector"], dtype=np.float32).copy(),
             "miss_type": r["miss_type"], "ts": int(r["ts"])}
            for r in conn.execute(
                "SELECT query_text, query_vector, miss_type, ts FROM recall_misses "
                "WHERE ts>? AND ts<=? AND query_vector IS NOT NULL ORDER BY id",
                (lo, hi))]
    clusters = gaps_clusterer(rows) if rows else []
    entropy = demand_entropy([c.get("miss_count", 0) for c in clusters])

    return TrendWindow(
        recalls_confident=confident, misses_abstained=abstained,
        misses_no_match=no_match, confident_rate=round(confident_rate, 6),
        demand_entropy=round(entropy, 6))
