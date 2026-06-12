"""Convergence trends for ``hive_health(include_trends=true)`` — CV4.

Lazy SQL over the EXISTING tables (exposure, recall_misses, episodes,
evidence_events), two fixed windows (current 14d vs previous 14d), computed at
report time only: no new table, no scheduler, no write-path change. App-side
like ``gaps.py``; the store's connection is read directly (this module is the
one consumer of cross-table windowed aggregates — pushing six one-off methods
into the store would widen its surface for a single report).

**The convergence KPI, defined:** ``confident_rate`` ↑ AND ``demand_entropy`` ↓
(demand is being answered, and what remains is concentrated/fillable rather
than diffuse noise) with ``dead_capture_ratio`` bounded (the fleet isn't
writing junk). These are COVERAGE proxies — outcome ground truth is the credit
scan's job (CV6), not this report's.

Window semantics: half-open ``(lo, hi]`` — an event exactly at the boundary
``now − 14d`` belongs to the PREVIOUS window, so the two windows partition.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from typing import Callable, Optional, Sequence

import numpy as np

_DAY_S = 86_400
WINDOW_DAYS = 14


@dataclass(frozen=True)
class TrendWindow:
    """One window's aggregates. Every field is total/zero-safe — an empty store
    yields a fully-populated window of zeros (and a None median), never a raise."""
    recalls_confident: int
    misses_abstained: int
    misses_no_match: int
    confident_rate: float                 # confident / (confident + misses); 0.0 on empty
    demand_entropy: float                 # H over miss-cluster mass / ln(C) ∈ [0,1]; 0.0 if <2 clusters
    n_promotions: int
    n_establishments: int
    n_supersessions: int
    median_days_to_promotion: Optional[float]
    dead_capture_ratio: float             # ttl_expired(quarantined) / captures, this window
    est_tokens_served: int                # Σ len(text)//4 over served hits (cost proxy)


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
    """``{"current", "previous", "deltas"}`` — current ``(now−14d, now]`` vs
    previous ``(now−28d, now−14d]``. ``gaps_clusterer`` is the existing miss
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

    kinds = {r["kind"]: int(r["c"]) for r in conn.execute(
        "SELECT kind, COUNT(*) AS c FROM evidence_events "
        "WHERE ts>? AND ts<=? AND kind IN ('promote','establish','supersede') "
        "GROUP BY kind", (lo, hi))}

    dts = [int(r["dt"]) for r in conn.execute(
        "SELECT (ev.ts - e.ts) AS dt FROM evidence_events ev "
        "JOIN episodes e ON e.id = ev.episode_id "
        "WHERE ev.kind='promote' AND ev.ts>? AND ev.ts<=?", (lo, hi))]
    median_days = (statistics.median(dts) / _DAY_S) if dts else None

    # dead captures: quarantine TTL expiries (payload names the source state —
    # parsed, not LIKE-matched) over the window's capture volume. A capture is
    # an approved row with NO approver (writes always carry one).
    expired_q = sum(
        1 for r in conn.execute(
            "SELECT payload FROM evidence_events "
            "WHERE kind='ttl_expired' AND ts>? AND ts<=?", (lo, hi))
        if _payload_from(r["payload"]) == "quarantined")
    captures = int(conn.execute(
        "SELECT COUNT(*) AS c FROM episodes "
        "WHERE ts>? AND ts<=? AND status='approved' AND approved_by IS NULL",
        (lo, hi)).fetchone()["c"])
    dead_ratio = (expired_q / captures) if captures else 0.0

    tokens = int(conn.execute(
        "SELECT COALESCE(SUM(LENGTH(e.text)/4), 0) AS t FROM exposure x "
        "JOIN episodes e ON e.id = x.episode_id "
        "WHERE x.injected_ts>? AND x.injected_ts<=?", (lo, hi)).fetchone()["t"])

    return TrendWindow(
        recalls_confident=confident, misses_abstained=abstained,
        misses_no_match=no_match, confident_rate=round(confident_rate, 6),
        demand_entropy=round(entropy, 6),
        n_promotions=kinds.get("promote", 0),
        n_establishments=kinds.get("establish", 0),
        n_supersessions=kinds.get("supersede", 0),
        median_days_to_promotion=median_days,
        dead_capture_ratio=round(dead_ratio, 6),
        est_tokens_served=tokens)


def _payload_from(raw: str) -> Optional[str]:
    """The ``from`` state inside a ttl_expired audit payload; None on junk."""
    try:
        body = json.loads(raw)
        return body.get("from") if isinstance(body, dict) else None
    except (TypeError, ValueError):
        return None
