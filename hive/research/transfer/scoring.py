"""Pure transfer scorers + the necessity validity gate + the shared ship gate (Chunk 3).

The headline is per-pair downstream success **Δ(recall_on − recall_off)** under the one ship rule
(``metrics_ir.bootstrap_ci`` — ships iff ``lo > 0``), scored ONLY over pairs the **necessity gate**
certifies valid: the no-recall arm must have FAILED (the fact was genuinely required — not guessable)
AND the oracle arm must have PASSED (the task is solvable once the fact is known). A pair failing
either bound is excluded and reported, never silently scored.

This module is the GOLD side of the gold/diagnostic firewall: it MUST NOT import ``diagnostics``
(the store's-own chain-decomposition), so "did the downstream task pass" can never double as the
detection signal. A purity test pins the non-import.
"""
from __future__ import annotations

from typing import Sequence

from hive.research.metrics_ir import bootstrap_ci


def transfer_success(outcomes: Sequence[object]) -> list[bool]:
    """Per-pair downstream success as a bool vector. RAISES on an empty sequence (an undefined
    success axis is refused, not masked)."""
    if not outcomes:
        raise ValueError("transfer_success requires >=1 outcome (empty is undefined)")
    return [bool(o) for o in outcomes]


def paired_delta_ci(on: Sequence[float], off: Sequence[float], *, n_boot: int = 10_000,
                    alpha: float = 0.05, seed: int = 0) -> tuple[float, float, float]:
    """Per-pair paired Δ(on − off) percentile-bootstrap CI ``(point, lo, hi)``. RAISES on a length
    mismatch or an empty vector (``bootstrap_ci`` also raises on empty; the length check is the
    transfer-specific guard)."""
    if len(on) != len(off):
        raise ValueError(f"paired_delta_ci length mismatch: {len(on)} != {len(off)}")
    deltas = [float(a) - float(b) for a, b in zip(on, off)]
    return bootstrap_ci(deltas, n_boot=n_boot, alpha=alpha, seed=seed)


def necessity_gate(*, off_success: Sequence[float], oracle_success: Sequence[float],
                   chance: float, oracle_floor: float = 1.0 - 1e-9) -> dict:
    """Per-pair benchmark VALIDITY (enforced, not asserted). A pair is valid iff the no-recall arm
    did not beat chance (``off_success <= chance`` — the fact was genuinely required, not guessable)
    AND the oracle arm cleared ``oracle_floor`` (the task is solvable once the fact is known). Inputs
    are per-pair success rates (a single-run bool coerces to 0.0/1.0). RAISES on a length mismatch or
    empty (a verdict on no pairs would be decorative)."""
    if len(off_success) != len(oracle_success):
        raise ValueError(
            f"necessity_gate length mismatch: {len(off_success)} != {len(oracle_success)}")
    if not off_success:
        raise ValueError("necessity_gate requires >=1 pair (empty is undefined)")
    valid_mask = [(float(o) <= chance) and (float(orc) >= oracle_floor)
                  for o, orc in zip(off_success, oracle_success)]
    excluded = [i for i, v in enumerate(valid_mask) if not v]
    return {"valid_mask": valid_mask, "excluded": excluded,
            "n_valid": sum(valid_mask), "chance": float(chance)}


def transfer_verdict(ci: tuple[float, float, float], *, higher_better: bool = True) -> str:
    """Read a CI triple as ``improve`` / ``regress`` / ``inconclusive`` under the ship rule, honoring
    the axis direction (success is higher-better)."""
    _pt, lo, hi = ci
    if higher_better:
        if lo > 0.0:
            return "improve"
        if hi < 0.0:
            return "regress"
    else:
        if hi < 0.0:
            return "improve"
        if lo > 0.0:
            return "regress"
    return "inconclusive"
