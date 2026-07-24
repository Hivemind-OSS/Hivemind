"""Contract tests for the pure decided-fraction precision + budget gating."""

from __future__ import annotations

import pytest

from hive.census import PrecisionEntry, assess_precision


def test_exact_fractions_and_budget_split() -> None:
    node, edge = assess_precision({"node": (2, 3), "edge": (10, 10)}, budget=0.9)
    assert node.cls == "node"
    assert (node.decided, node.total) == (2, 3)
    assert node.value == 2 / 3
    assert node.budget == 0.9
    assert node.gating is False  # below budget: shown, not gated
    assert edge.cls == "edge"
    assert edge.value == 1.0
    assert edge.gating is True


def test_vacuous_class_gates_with_null_value() -> None:
    (entry,) = assess_precision({"differential": (0, 0)}, budget=0.9)
    assert entry.value is None  # nothing measured, nothing claimed
    assert entry.gating is True


def test_boundary_value_equal_to_budget_gates() -> None:
    (entry,) = assess_precision({"execution": (9, 10)}, budget=0.9)
    assert entry.value == 0.9
    assert entry.gating is True


def test_entries_ride_in_input_order() -> None:
    counts = {"node": (1, 1), "edge": (0, 2), "regression": (2, 2)}
    entries = assess_precision(counts, budget=0.5)
    assert [entry.cls for entry in entries] == ["node", "edge", "regression"]


def test_entry_rejects_gating_below_budget() -> None:
    with pytest.raises(ValueError):
        PrecisionEntry(
            cls="node", decided=1, total=3, value=1 / 3, budget=0.9, gating=True
        )


def test_entry_rejects_impossible_counts_and_budget() -> None:
    with pytest.raises(ValueError):
        PrecisionEntry(
            cls="node", decided=4, total=3, value=1.0, budget=0.9, gating=True
        )
    with pytest.raises(ValueError):
        PrecisionEntry(
            cls="node", decided=-1, total=3, value=None, budget=0.9, gating=False
        )
    with pytest.raises(ValueError):
        PrecisionEntry(
            cls="node", decided=1, total=1, value=1.0, budget=1.5, gating=True
        )
    with pytest.raises(ValueError):
        PrecisionEntry(
            cls="node", decided=1, total=1, value=1.0, budget=-0.1, gating=True
        )


def test_entry_rejects_value_inconsistent_with_counts() -> None:
    with pytest.raises(ValueError):
        PrecisionEntry(
            cls="node", decided=2, total=3, value=0.5, budget=0.9, gating=False
        )
    with pytest.raises(ValueError):
        PrecisionEntry(
            cls="node", decided=2, total=3, value=None, budget=0.9, gating=False
        )
    with pytest.raises(ValueError):
        PrecisionEntry(
            cls="node", decided=0, total=0, value=0.0, budget=0.9, gating=True
        )
