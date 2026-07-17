"""Contracts over the RunState -> EvidenceTag mapping — the honesty pins.

The composer must be able to tag classes 1-2 without re-deriving the mapping,
and the mapping itself must never over-claim: a green test suite is evidence
of "no regression *observed*", never a machine-checked proof, and an
abstention (not_run/errored) is always tagged abstain — for both classes.
"""

from __future__ import annotations

import pytest

from hive.verifier.evidence import EvidenceTag, tag_tests, tag_typecheck
from hive.verifier.result import ClassResult, RunState


def _class_result(state: RunState) -> ClassResult:
    """A minimal guard-valid ClassResult in the given state."""
    ran = state in ("passed", "failed")
    return ClassResult(
        state=state,
        ran=ran,
        passed=1 if state == "passed" else 0,
        failed=1 if state == "failed" else 0,
        errored=0,
        diagnostics=(),
        runners=("toolx",) if ran else (),
        reason=None if ran else f"abstained: {state}",
        report_format="junit" if ran else None,
    )


# --- the full truth tables ---------------------------------------------------------

TYPECHECK_TABLE: dict[RunState, EvidenceTag] = {
    "passed": "machine-checked",
    "failed": "machine-checked",
    "not_run": "abstain",
    "errored": "abstain",
}

TESTS_TABLE: dict[RunState, EvidenceTag] = {
    "passed": "bounded-estimate",
    "failed": "bounded-estimate",
    "not_run": "abstain",
    "errored": "abstain",
}


@pytest.mark.parametrize("state", ["passed", "failed", "not_run", "errored"])
def test_tag_typecheck_truth_table(state: RunState) -> None:
    assert tag_typecheck(_class_result(state)) == TYPECHECK_TABLE[state]


@pytest.mark.parametrize("state", ["passed", "failed", "not_run", "errored"])
def test_tag_tests_truth_table(state: RunState) -> None:
    assert tag_tests(_class_result(state)) == TESTS_TABLE[state]


# --- the honesty pins ------------------------------------------------------------


@pytest.mark.parametrize("state", ["passed", "failed", "not_run", "errored"])
def test_tag_tests_never_returns_machine_checked(state: RunState) -> None:
    # A green suite bounds the regression estimate; it proves nothing.
    assert tag_tests(_class_result(state)) != "machine-checked"


@pytest.mark.parametrize("state", ["not_run", "errored"])
def test_abstentions_always_map_to_abstain_for_both_classes(state: RunState) -> None:
    assert tag_typecheck(_class_result(state)) == "abstain"
    assert tag_tests(_class_result(state)) == "abstain"
