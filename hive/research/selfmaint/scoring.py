"""S3 — the Pareto scorers + the one paired-CI ship gate (PURE).

The load-bearing arithmetic of the self-maintenance demonstration. Every scorer grades a regime
against the EXTERNAL gold — the substrate's labeled regimes (prunable-bad / keep-valuable /
keep-neutral source ids), confirmed by executed-test task outcomes (``task_gold``) — and NEVER
the store's own deprecation verdict. That is the circularity firewall, made structural: this
module imports ``metrics_ir`` ONLY. It does not import the diagnostic carrier (``diagnostics``),
which surfaces what the store actually served/retired; those observations flow in as plain
arguments (the SUBJECT being graded), so "is it retired" can never be both the signal and the
gold. ``tests/research/selfmaint/test_scoring.py`` pins the non-import.

Conventions (explicit, never silently degenerate — the ERROR_MASKING failure class the bug
registry warns about):
- The Pareto set is NEVER collapsed to a scalar — raw store-size is gameable by pruning
  everything, so leanness rides alongside precision + success, each with its own CI.
- Degenerate inputs RAISE (empty task list, no externally-confirmed-bad, empty keep set, a
  paired length mismatch). A defined-looking number over nonsense inputs is refused, not masked.
- An empty-store density step is 0.0 (an empty store retains no value — documented, not a 0/0).
"""
from __future__ import annotations

from typing import Iterable, Sequence

from hive.research.metrics_ir import bootstrap_ci

# A bootstrap CI triple ``(point, lo, hi)`` from ``metrics_ir.bootstrap_ci``.
CI = tuple


# ── the Pareto metric set ────────────────────────────────────────────────────────────
def density_curve(servable_by_step: Sequence[Iterable[str]],
                  valuable_sources: Iterable[str]) -> list[float]:
    """High-value DENSITY per step: retained-valuable ÷ total-servable. A higher curve means a
    leaner store (less junk diluting the served surface). An empty-servable step is 0.0 — an
    empty store retains no value (documented; never a 0/0). // O(steps · servable)."""
    valuable = set(valuable_sources)
    out: list[float] = []
    for step in servable_by_step:
        servable = set(step)
        out.append(len(servable & valuable) / len(servable) if servable else 0.0)
    return out


def bad_serve_rate(served_by_task: Sequence[Iterable[str]],
                   bad_sources: Iterable[str]) -> float:
    """Fraction of TASKS that served a labeled-bad fact as a confident hit (lower better). Empty
    task list RAISES — a rate over zero tasks is undefined. // O(tasks · served)."""
    tasks = list(served_by_task)
    if not tasks:
        raise ValueError("bad_serve_rate requires ≥1 task (empty is undefined)")
    bad = set(bad_sources)
    hit = sum(1 for served in tasks if bad & set(served))
    return hit / len(tasks)


def prune_recall(retired: Iterable[str], bad_sources: Iterable[str], *,
                 task_gold: Iterable[str]) -> float:
    """Fraction of EXTERNALLY-CONFIRMED prunable-bad facts that were retired (higher better). The
    denominator is ``bad_sources ∩ task_gold`` — a labeled-bad fact counts only once an executed
    test confirmed it harmful, so the store's own deprecation flag is never the gold. RAISES when
    nothing is externally confirmed (recall is undefined with nothing to prune). // O(n)."""
    confirmed_bad = set(bad_sources) & set(task_gold)
    if not confirmed_bad:
        raise ValueError(
            "prune_recall requires ≥1 externally-confirmed-bad fact (none in bad ∩ task_gold)")
    return len(set(retired) & confirmed_bad) / len(confirmed_bad)


def prune_precision(retired: Iterable[str], keep_sources: Iterable[str], *,
                    task_gold: Iterable[str]) -> tuple[float, float]:
    """``(precision, false_prune_rate)``. ``precision`` = of all retirements, the fraction that
    were JUSTIFIED (hit an externally-confirmed-bad fact in ``task_gold``) — higher better; with
    no retirements it is vacuously 1.0 (no wrong retirement was made). ``false_prune_rate`` = the
    fraction of keep-class memories wrongly retired (over-prune harm) — lower better; over-pruning
    a keep-neutral memory is the precision failure (§4). Empty keep set RAISES. // O(n)."""
    keep = set(keep_sources)
    if not keep:
        raise ValueError("prune_precision requires a non-empty keep set")
    retired = set(retired)
    justified = retired & set(task_gold)
    wrong = retired & keep                      # any keep retired is over-prune harm
    precision = (len(justified) / len(retired)) if retired else 1.0
    false_prune_rate = len(wrong) / len(keep)
    return (precision, false_prune_rate)


def task_success(outcomes: Sequence[object]) -> list[bool]:
    """The per-task executed-test SUCCESS vector (pass iff truthy). Empty RAISES — a report
    refuses to emit on a missing success vector (AC7). // O(n)."""
    out = list(outcomes)
    if not out:
        raise ValueError("task_success requires a non-empty outcome vector")
    return [bool(o) for o in out]


# ── the one ship gate: a paired bootstrap CI on Δ(ON − OFF) ───────────────────────────
def paired_delta_ci(on: Sequence[float], off: Sequence[float], *,
                    n_boot: int = 10_000, alpha: float = 0.05,
                    seed: int = 0) -> tuple[float, float, float]:
    """The per-task paired Δ(ON−OFF) bootstrap CI — the ONE significance rule (no second
    invented). ``on``/``off`` are paired per-task metric vectors; a length mismatch RAISES (an
    unpaired delta is meaningless), and ``bootstrap_ci`` RAISES on an empty vector. // O(n_boot·n)."""
    on, off = list(on), list(off)
    if len(on) != len(off):
        raise ValueError(
            f"paired_delta_ci requires paired vectors: len(on)={len(on)} != len(off)={len(off)}")
    deltas = [float(o) - float(f) for o, f in zip(on, off)]
    return bootstrap_ci(deltas, n_boot=n_boot, alpha=alpha, seed=seed)


def verdict(ci: tuple[float, float, float], *, higher_better: bool) -> str:
    """Read a Δ(ON−OFF) CI as ``"improve"`` / ``"regress"`` / ``"inconclusive"`` per the ship
    rule, honouring the axis direction. For a higher-better axis improvement is ``lo > 0``; for a
    lower-better axis (e.g. bad-serve) improvement is ``hi < 0``. // O(1)."""
    _point, lo, hi = ci
    if higher_better:
        if lo > 0:
            return "improve"
        if hi < 0:
            return "regress"
    else:
        if hi < 0:
            return "improve"
        if lo > 0:
            return "regress"
    return "inconclusive"


def clean_win(*, density_ci: tuple[float, float, float],
              bad_serve_ci: tuple[float, float, float],
              prune_recall_ci: tuple[float, float, float],
              precision_ci: tuple[float, float, float],
              success_ci: tuple[float, float, float]) -> dict:
    """The headline conjunction over the Δ(ON−OFF) CIs (§5), with each axis's verdict carried for
    transparency:

        clean_win = density↑  ∧  bad-serve↓  ∧  prune_recall↑  ∧  ¬precision-regress  ∧  ¬success-regress

    A net-negative / inconclusive regime is therefore NOT a clean win — surfaced as the per-axis
    verdicts, never hidden. // O(1)."""
    axes = {
        "density": verdict(density_ci, higher_better=True),
        "bad_serve": verdict(bad_serve_ci, higher_better=False),
        "prune_recall": verdict(prune_recall_ci, higher_better=True),
        "precision": verdict(precision_ci, higher_better=True),
        "success": verdict(success_ci, higher_better=True),
    }
    won = (axes["density"] == "improve" and axes["bad_serve"] == "improve"
           and axes["prune_recall"] == "improve" and axes["precision"] != "regress"
           and axes["success"] != "regress")
    return {"clean_win": won, **axes}
