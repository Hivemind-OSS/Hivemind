"""UtilitySurfacer — a free-standing pure domain object [A1] that both M04 (caller)
and M08 (input supplier) depend on. Reorders gate-passed candidates by
``weight · f(utility)`` once a posterior is confident; DEMOTES on confident-negative
utility (the un-cripple of the reference's ``1+max(0,u)`` which could only promote).

Phase-1 inert: ``enabled=False`` ⇒ byte-identical passthrough. ε-explore: a fraction
of recalls ignore utility entirely (guardrail-1) so novel memories keep exposure.
"""
from __future__ import annotations

import random
from typing import Mapping, Sequence

from hive.domain.models import Scored


def utility_factor(u: float, f_min: float, f_max: float) -> float:
    """f(u) = f_min + (f_max−f_min)·u, clamped to [f_min, f_max].
    f(0.5)=1.0 neutral, f(1)=f_max promote, f(0)=f_min demote (for the default band)."""
    f = f_min + (f_max - f_min) * u
    return max(f_min, min(f_max, f))


class UtilitySurfacer:
    def __init__(
        self, *, enabled: bool, epsilon_explore: float,
        f_min: float, f_max: float, rng: random.Random,
    ) -> None:
        self.enabled = bool(enabled)
        self.epsilon_explore = float(epsilon_explore)
        self.f_min = float(f_min)
        self.f_max = float(f_max)
        self.rng = rng

    def order(
        self, scored: Sequence[Scored], utility_map: Mapping[int, float],
        *, family_scope: str,
    ) -> list[Scored]:
        """Stable-sort by ``weight · f(util)`` DESC; ties keep base order. O(n log n).
        enabled False ⇒ byte-identical passthrough; ε-explore ⇒ identity this call;
        eid absent from utility_map ⇒ f=1.0 (un-confident never moves ranking);
        never adds an eid absent from ``scored`` (abstain-no-resurrect at the surfacer)."""
        base = list(scored)
        if not self.enabled or not base:
            return base
        if self.epsilon_explore > 0.0 and self.rng.random() < self.epsilon_explore:
            return base  # exploration: ignore utility for this call (guardrail-1)

        def key(s: Scored) -> float:
            u = utility_map.get(s.episode_id)
            f = 1.0 if u is None else utility_factor(u, self.f_min, self.f_max)
            return s.weight * f

        return sorted(base, key=key, reverse=True)  # stable ⇒ ties keep base order
