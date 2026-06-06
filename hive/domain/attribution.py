"""Attributor — pure credit policy [A3]. Splits a settled/clawed reward across the
co-injected memories by recall_margin (co-occurrence discount), excludes the
held-out isolation slice [A5], and emits CreditDelta posterior updates. It NEVER
writes episode.weight (the reference's forbidden path is deleted) — credit lands
only on the separate, versioned utility posterior.

Also home to PredictionBiasMonitor (guardrail-3 / §12 readiness instrument, A6).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, AbstractSet, Mapping, Sequence

from hive.domain.models import SettledOutcome

if TYPE_CHECKING:                       # type-only: keeps the domain free of the port at runtime
    from hive.domain.ports import Clock, UtilityStore


@dataclass(frozen=True, slots=True)
class CreditDelta:
    episode_id: int
    family_scope: str
    d_wins: float
    d_losses: float
    source_agent: str = ""


class Attributor:
    """Pure: no SQL/git/clock."""

    def split(
        self,
        outcome: SettledOutcome,
        exposed: Sequence[tuple[int, float]],   # (episode_id, recall_margin)
        isolation: AbstractSet[int],
    ) -> list[CreditDelta]:
        """O(n). share_i = magnitude · margin_i / Σmargins (all-zero ⇒ uniform 1/n).
        sign +1 ⇒ d_wins=share, d_losses=0 ; −1 ⇒ swapped. Isolation ids excluded.
        POST: Σ(d_wins+d_losses) == magnitude (rel-tol 1e-9) over the credited set."""
        credited = [(int(eid), float(m)) for eid, m in exposed if int(eid) not in isolation]
        n = len(credited)
        if n == 0:
            return []
        total = math.fsum(max(m, 0.0) for _, m in credited)
        out: list[CreditDelta] = []
        for eid, margin in credited:
            share = (outcome.magnitude * max(margin, 0.0) / total) if total > 0 else (outcome.magnitude / n)
            d_wins = share if outcome.reward_sign == 1 else 0.0
            d_losses = share if outcome.reward_sign == -1 else 0.0
            out.append(CreditDelta(
                episode_id=eid, family_scope=outcome.family_scope,
                d_wins=d_wins, d_losses=d_losses, source_agent=outcome.source_agent,
            ))
        return out


class PredictionBiasMonitor:
    """Guardrail-3 / §12 Phase-2 readiness instrument [A6]. PURE (clock injected, no
    SQL/git). Measures the mean signed gap between what the ranker PREDICTED (the Beta
    posterior mean it would rank by) and what REALITY DELIVERED (the settled reward)
    over the window. Positive ⇒ the ranker over-predicts utility relative to reality
    ("stale, the codebase moved underneath it"); the producer tick logs WARN when
    |divergence| > utility.prediction_bias_threshold. Observed-not-applied in Phase 1
    — it instruments readiness, it never moves ranking."""

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
