"""Pure decided-fraction precision + budget gating.

Per evidence class, value = decided / total. A class that measured nothing
(total == 0) is vacuous: value is None and it gates, because nothing below
budget was claimed. A class below budget is shown, not gated — its lines ride
the receipt as context — and the carrier makes a below-budget entry that
claims to gate unconstructable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PrecisionEntry:
    cls: str
    decided: int
    total: int
    value: float | None  # None exactly when total == 0 (vacuous)
    budget: float
    gating: bool

    def __post_init__(self) -> None:
        if not 0 <= self.decided <= self.total:
            raise ValueError(
                f"decided must satisfy 0 <= decided <= total, got "
                f"decided={self.decided}, total={self.total}"
            )
        if not 0.0 <= self.budget <= 1.0:
            raise ValueError(f"budget must be within [0.0, 1.0], got {self.budget}")
        expected = self.decided / self.total if self.total > 0 else None
        if self.value != expected:
            raise ValueError(
                f"value must equal decided/total (or None when total == 0); "
                f"got value={self.value}, expected {expected}"
            )
        if self.gating and self.value is not None and self.value < self.budget:
            raise ValueError(
                f"a below-budget class cannot gate: value={self.value} < "
                f"budget={self.budget}"
            )


def assess_precision(
    counts: Mapping[str, tuple[int, int]], *, budget: float
) -> tuple[PrecisionEntry, ...]:
    """One entry per evidence class, in the mapping's order."""
    entries = []
    for cls, (decided, total) in counts.items():
        value = decided / total if total > 0 else None
        gating = value is None or value >= budget
        entries.append(
            PrecisionEntry(
                cls=cls,
                decided=decided,
                total=total,
                value=value,
                budget=budget,
                gating=gating,
            )
        )
    return tuple(entries)
