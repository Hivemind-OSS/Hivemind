"""M10 [C1] — the §6.6 keystone experiment: the move-#6 control-arm causal test.

Does utility-weighted recall *causally* beat the naive baselines on held-out task
success? The harness scores four arms — **utility** vs **utility-off / recency /
frequency** — over the SAME hermetic retrieval (held everything constant except the
ranking signal) and applies the pre-committed decision:

  WIN  = utility beats BOTH recency AND frequency (paired bootstrap CI ``lo > 0``
         on each) AND the lift is **non-saturating** AND it **transfers within
         family** AND it **traces to an ungameable clawback**. Beating *nothing*
         (utility-off) is not a win — that is trivial recency, not learning.
  KILL = utility fails to beat recency CI-significantly ⇒ ``killed=True`` ⇒ moves
         #1/#2/#4/#5/#7 are NOT built (§6.6 / §7). A clean negative kills six
         unbuilt moves cheaply — the experiment the whole MVP exists to run.
  INCONCLUSIVE ≠ NEGATIVE (§12 readiness pre-gate): too few settled outcomes or too
         few distinct credited memories ⇒ ``inconclusive=True`` (NOT ``killed``);
         the remedy is widen the family / extend the window.

Built in Phase 1 (so it can be RUN at the Phase-1→Phase-2 gate); the experiment
runs in Phase 2. Dev-time only — the runtime never imports ``hive.research``.

**Arm-input seam** (the design-review must-fix, named here): ``run_keystone_eval``
consumes a frozen ``KeystoneSpec`` — the per-arm per-task recall@5 vectors, the
accrual curve, the present-vs-ablated transfer vectors, and the settled/distinct
counts. A real Phase-2 run fills that spec by running ``_rank_candidates`` over a
hermetic store for each ``ranking_mode`` (the four arms differ ONLY in the ranking
signal); the membrane never flips the runtime surfacer (utility stays
observed-not-applied in the runtime — the keystone ranks the arms itself).
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import numpy as np

from hive.domain.surfacer import utility_factor
from hive.research.metrics_ir import bootstrap_ci

log = logging.getLogger("hive.research.keystone")

# The four control arms. They differ ONLY in the per-candidate ranking signal.
RANKING_MODES = ("utility", "utility_off", "recency", "frequency")

# §12 readiness minima (inconclusive ≠ negative). Defaults; a caller may tighten.
_DEFAULT_N_SETTLED_MIN = 30
_DEFAULT_M_MEMORIES_MIN = 10


# ── the four-arm ranking seam (the named ranking_mode) ───────────────────────

def _rank_candidates(
    mode: str, candidates: Sequence[tuple[int, float]], *,
    posteriors: Mapping[int, float], ts_map: Mapping[int, int],
    freq_map: Mapping[int, int], weight_map: Mapping[int, float],
    f_min: float = 0.5, f_max: float = 1.5,
) -> list[int]:
    """Rank the recall candidate set ``[(eid, sim), ...]`` (sim-descending) by the
    arm's signal; ties keep sim order (stable sort). The four arms:

    - ``utility``     : ``weight · f(posterior_mean)`` (the SAME ``utility_factor``
                        the runtime surfacer uses — not a divergent reimpl).
    - ``utility_off`` : ``weight`` only (utility ablated — the control).
    - ``recency``     : episode timestamp (newest first).
    - ``frequency``   : exposure/seen count (most-recalled first).

    A causal experiment must hold everything constant except this signal — which is
    exactly what selecting only ``mode`` here does. // O(n log n) time."""
    if mode not in RANKING_MODES:
        raise ValueError(f"unknown ranking_mode {mode!r}; valid={RANKING_MODES}")

    def util_score(eid: int) -> float:
        u = posteriors.get(eid)
        f = 1.0 if u is None else utility_factor(u, f_min, f_max)
        return weight_map.get(eid, 1.0) * f

    keyfn = {
        "utility": lambda c: util_score(c[0]),
        "utility_off": lambda c: weight_map.get(c[0], 1.0),
        "recency": lambda c: ts_map.get(c[0], 0),
        "frequency": lambda c: freq_map.get(c[0], 0),
    }[mode]
    # stable sort: equal keys keep the input (sim-descending) order.
    return [eid for eid, _sim in sorted(candidates, key=keyfn, reverse=True)]


# ── the two BUILD-NEW statistics ─────────────────────────────────────────────

def _ols_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """OLS slope β1 of ys on xs = cov(x,y)/var(x); 0.0 if var(x)==0. // O(n)."""
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    cov = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys))
    var = math.fsum((x - mx) ** 2 for x in xs)
    if var == 0.0:
        return 0.0
    return cov / var


def _non_saturating(
    points: Sequence[tuple[float, float]], *, n_bins: int = 5, n_boot: int = 2000,
    seed: int = 0,
) -> tuple[Optional[bool], tuple[float, float, float], int]:
    """Is the utility lift still RISING (not plateaued) as credit accrues?

    Bin the accrual points ``(cumulative_settled_volume, utility_recall)`` into
    ``n_bins`` equal-width bins over the x-range, drop empty bins, and fit the OLS
    slope β1 of bin-mean-recall vs bin index. A **stratified bootstrap** (resample
    within each non-empty bin) gives the slope CI. Returns
    ``(non_saturating, (β1_hat, lo, hi), n_bins_used)`` where **``non_saturating``
    is ``β1_lo > 0``** — a CI-significant positive slope, NOT a bare positive point
    estimate. ``< 3`` non-empty bins ⇒ ``None`` (inconclusive — cannot fit a trend).
    // O(n_boot · n_bins) time.
    """
    pts = [(float(x), float(y)) for x, y in points]
    if not pts:
        return (None, (0.0, 0.0, 0.0), 0)
    xs = [p[0] for p in pts]
    xmin, xmax = min(xs), max(xs)
    if xmax == xmin:
        return (None, (0.0, 0.0, 0.0), 1)
    width = (xmax - xmin) / n_bins
    bins: list[list[float]] = [[] for _ in range(n_bins)]
    for x, y in pts:
        idx = min(int((x - xmin) / width), n_bins - 1)
        bins[idx].append(y)
    # A SINGLETON bin has zero within-bin sampling variance — the stratified bootstrap
    # (resample within each bin) cannot perturb it, so its mean is FROZEN across every
    # iteration. If the slope-levering endpoint bins are singletons the bootstrap slope
    # CI collapses to ~zero width and `lo > 0` silently degrades into a bare
    # point-estimate test (`hat > 0`) — the exact thing the contract says it is NOT.
    # Count only bins with ≥2 points, then re-apply the inconclusive floor: too few
    # well-populated bins ⇒ None (an honest "cannot fit a CI-significant trend").
    counted = [(i, ys) for i, ys in enumerate(bins) if len(ys) >= 2]
    if len(counted) < 3:
        return (None, (0.0, 0.0, 0.0), len(counted))

    bx = [float(i) for i, _ in counted]
    by = [statistics.fmean(ys) for _, ys in counted]
    slope_hat = _ols_slope(bx, by)

    rng = np.random.default_rng(seed)
    slopes = np.empty(n_boot, dtype=np.float64)
    bin_arrays = [np.asarray(ys, dtype=np.float64) for _, ys in counted]
    for b in range(n_boot):
        rby = [float(rng.choice(arr, size=arr.size, replace=True).mean())
               for arr in bin_arrays]
        slopes[b] = _ols_slope(bx, rby)
    lo = float(np.percentile(slopes, 2.5))
    hi = float(np.percentile(slopes, 97.5))
    return (lo > 0.0, (slope_hat, lo, hi), len(counted))


def _within_family_transfer(
    present_recall: Sequence[float], ablated_recall: Sequence[float], *,
    n_boot: int = 10_000, seed: int = 0,
) -> tuple[bool, tuple[float, float, float]]:
    """Does a memory credited on one task lift a HELD-OUT task's recall@5 inside the
    same family? Per-task delta = ``present − ablated`` (the credited posterior is
    PRESENT vs posterior-ABLATED); CI-significant lift iff ``bootstrap_ci.lo > 0``.
    Returns ``(within_family_transfer, ci)``. The ``present − ablated`` direction is
    load-bearing: inverting it would score a regression as transfer. // O(n_boot·n)."""
    if len(present_recall) != len(ablated_recall):
        raise ValueError(
            f"transfer vectors length mismatch: {len(present_recall)} != "
            f"{len(ablated_recall)}")
    deltas = [float(p) - float(a) for p, a in zip(present_recall, ablated_recall)]
    ci = bootstrap_ci(deltas, n_boot=n_boot, seed=seed)
    return (ci[1] > 0.0, ci)


# ── result carriers ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ArmResult:
    """One control arm's per-task recall@5 vector + its mean."""
    name: str
    mean_recall: float
    per_task: tuple = ()


@dataclass(frozen=True)
class KeystoneSpec:
    """The arm-input fixture (the named seam). Per-arm per-task recall@5 vectors +
    the accrual curve + present/ablated transfer vectors + readiness counts. A real
    Phase-2 run fills this by running ``_rank_candidates`` per ``ranking_mode`` over
    a hermetic store seeded with the credited (episode, family) posteriors."""
    utility_recall: tuple
    recency_recall: tuple
    frequency_recall: tuple
    accrual_points: tuple                    # ((cum_settled_volume, utility_recall), ...)
    transfer_present: tuple
    transfer_ablated: tuple
    settled: int
    distinct_memories: int
    clawback_traced: bool = False
    utility_off_recall: tuple = ()
    n_settled_min: int = _DEFAULT_N_SETTLED_MIN
    m_memories_min: int = _DEFAULT_M_MEMORIES_MIN


@dataclass(frozen=True)
class KeystoneResult:
    """The frozen §6.6 verdict. ``win``/``killed``/``inconclusive`` are mutually the
    decision; the CI tuples + per-arm results make the decision auditable."""
    arms: dict
    beats_recency: bool
    beats_frequency: bool
    non_saturating: bool
    within_family_transfer: bool
    clawback_traced: bool
    win: bool
    killed: bool
    inconclusive: bool
    settled: int
    distinct_memories: int
    recency_ci: tuple = (0.0, 0.0, 0.0)
    frequency_ci: tuple = (0.0, 0.0, 0.0)
    slope_ci: tuple = (0.0, 0.0, 0.0)
    transfer_ci: tuple = (0.0, 0.0, 0.0)
    n_bins_used: int = 0


def _arm(name: str, vec: Sequence[float]) -> ArmResult:
    v = tuple(float(x) for x in vec)
    return ArmResult(name=name, mean_recall=statistics.fmean(v) if v else 0.0,
                     per_task=v)


def run_keystone_eval(spec: KeystoneSpec, *, n_boot: int = 10_000,
                      seed: int = 0) -> KeystoneResult:
    """Score the four arms and apply the §6.6 decision. // see module docstring."""
    # The arms are the SAME tasks scored under different ranking signals, so their
    # per-task vectors MUST be equal length — otherwise `zip` below would SILENTLY
    # truncate the longer arm and fabricate a paired comparison out of unpaired
    # tasks (a recency-wins run could come back as a utility WIN). Refuse, never mask
    # (the same honesty contract _within_family_transfer already enforces).
    n = len(spec.utility_recall)
    if len(spec.recency_recall) != n or len(spec.frequency_recall) != n:
        raise ValueError(
            f"keystone arm vectors must be equal length: utility={n} "
            f"recency={len(spec.recency_recall)} frequency={len(spec.frequency_recall)}")
    if spec.utility_off_recall and len(spec.utility_off_recall) != n:
        raise ValueError(
            f"keystone utility_off arm length {len(spec.utility_off_recall)} != "
            f"utility {n}")
    arms = {
        "utility": _arm("utility", spec.utility_recall),
        "recency": _arm("recency", spec.recency_recall),
        "frequency": _arm("frequency", spec.frequency_recall),
    }
    if spec.utility_off_recall:
        arms["utility_off"] = _arm("utility_off", spec.utility_off_recall)

    # paired per-task deltas vs each baseline → CI-significant iff lo > 0
    rec_deltas = [u - r for u, r in zip(spec.utility_recall, spec.recency_recall)]
    frq_deltas = [u - f for u, f in zip(spec.utility_recall, spec.frequency_recall)]
    recency_ci = bootstrap_ci(rec_deltas, n_boot=n_boot, seed=seed)
    frequency_ci = bootstrap_ci(frq_deltas, n_boot=n_boot, seed=seed)
    beats_recency = recency_ci[1] > 0.0
    beats_frequency = frequency_ci[1] > 0.0

    non_sat_flag, slope_ci, n_bins_used = _non_saturating(
        spec.accrual_points, n_boot=max(1000, n_boot // 5), seed=seed)
    non_saturating = bool(non_sat_flag)      # None (inconclusive trend) ⇒ not confirmed

    within_transfer, transfer_ci = _within_family_transfer(
        spec.transfer_present, spec.transfer_ablated, n_boot=n_boot, seed=seed)

    # §12 readiness pre-gate: inconclusive ≠ negative.
    inconclusive = (spec.settled < spec.n_settled_min
                    or spec.distinct_memories < spec.m_memories_min)

    if inconclusive:
        log.warning("keystone.inconclusive settled=%d (<%d?) distinct_memories=%d "
                    "(<%d?) — widen family / extend window (NOT a negative)",
                    spec.settled, spec.n_settled_min, spec.distinct_memories,
                    spec.m_memories_min)
        killed = False
        win = False
    else:
        # pre-committed kill: utility fails to beat recency CI-significantly.
        killed = not beats_recency
        if killed:
            log.info("keystone.killed recency_ci=%s ⇒ utility did not beat recency "
                     "(pre-committed kill; moves #1/#2/#4/#5/#7 not built)", recency_ci)
        # win requires ALL: beat both baselines, non-saturating, within-family
        # transfer, AND the lift traces to an ungameable clawback.
        win = (beats_recency and beats_frequency and non_saturating
               and within_transfer and spec.clawback_traced)

    return KeystoneResult(
        arms=arms, beats_recency=beats_recency, beats_frequency=beats_frequency,
        non_saturating=non_saturating, within_family_transfer=within_transfer,
        clawback_traced=bool(spec.clawback_traced), win=win, killed=killed,
        inconclusive=inconclusive, settled=spec.settled,
        distinct_memories=spec.distinct_memories, recency_ci=recency_ci,
        frequency_ci=frequency_ci, slope_ci=slope_ci, transfer_ci=transfer_ci,
        n_bins_used=n_bins_used)
