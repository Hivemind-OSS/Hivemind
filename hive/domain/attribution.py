"""CreditDelta + PredictionBiasMonitor — the dormant utility-credit carrier and the
readiness instrument (guardrail-3, A6).

The credit-PRODUCER (the ``Attributor`` that split verifiable git outcomes into
``CreditDelta`` posterior updates) was removed with the producer subsystem.
``CreditDelta`` and ``PredictionBiasMonitor`` remain because the utility store still
consumes the former (``apply_credit``) and the readiness apparatus still drives the
latter — both observed-not-applied in Phase 1 (nothing feeds them at runtime now).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # type-only: keeps the domain free of the port at runtime
    from hive.domain.ports import Clock, UtilityStore


@dataclass(frozen=True, slots=True)
class CreditDelta:
    episode_id: int
    family_scope: str
    d_wins: float
    d_losses: float
    source_agent: str = ""


class PredictionBiasMonitor:
    """Guardrail-3 / Phase-2 readiness instrument [A6]. PURE (clock injected, no
    SQL/git). Measures the mean signed gap between what the ranker PREDICTED (the Beta
    posterior mean it would rank by) and what REALITY DELIVERED (the settled reward)
    over the window. Positive ⇒ the ranker over-predicts utility relative to reality
    ("stale, the codebase moved underneath it"). Observed-not-applied in Phase 1 — it
    instruments readiness, it never moves ranking."""

    __slots__ = ("_store", "_clock")

    def __init__(self, store: "UtilityStore", *, clock: "Clock") -> None:
        self._store = store
        self._clock = clock

    def divergence(self, family_scope: str, window_s: int) -> float:
        """Mean signed gap over (settled outcome, exposed eid) pairs in [now−window_s, now].
        For each settled outcome o for this family in the window:
            predicted_i = posterior(eid, family).mean()      # Beta mean a/(a+b), the rank key
            realized    = 1.0 if o.reward_sign > 0 else 0.0  # map to [0,1] like the mean
            gap_i       = predicted_i − realized
        divergence = mean(gap_i) over all pairs; 0.0 when the window holds no outcomes
        (never a confident-empty divergence). O(k) for k settled pairs in window."""
        floor = self._clock.now() - window_s
        rows = self._store.settled_exposures_since(family_scope, floor)
        gaps: list[float] = []
        for row in rows:
            realized = 1.0 if row.reward_sign > 0 else 0.0
            for eid in row.exposed:
                predicted = self._store.posterior(eid, family_scope).mean()
                gaps.append(predicted - realized)
        if not gaps:
            return 0.0
        return math.fsum(gaps) / len(gaps)
