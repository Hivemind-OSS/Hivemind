"""Chunk 3 — the pure transfer scorers + the necessity validity gate + the ship gate.

The headline is per-pair downstream success Δ(recall_on − recall_off), scored ONLY over pairs the
necessity gate certifies valid (the no-recall arm failed AND the oracle arm passed). Degenerate
inputs RAISE rather than return a defined-looking default.
"""
from __future__ import annotations

import pytest

from hive.research.transfer import scoring


def test_transfer_success_coerces_and_raises_on_empty():
    assert scoring.transfer_success([True, 0, "x", 1]) == [True, False, True, True]
    with pytest.raises(ValueError):
        scoring.transfer_success([])


def test_paired_delta_ci_significant_when_on_beats_off():
    pt, lo, hi = scoring.paired_delta_ci([1, 1, 1, 1], [0, 0, 0, 0], seed=0)
    assert pt == 1.0 and lo > 0.0


def test_paired_delta_ci_raises_on_mismatch_or_empty():
    with pytest.raises(ValueError):
        scoring.paired_delta_ci([1, 0], [0])
    with pytest.raises(ValueError):
        scoring.paired_delta_ci([], [])


def test_necessity_gate_keeps_off_fail_and_oracle_pass():
    g = scoring.necessity_gate(off_success=[0, 0, 1], oracle_success=[1, 1, 1], chance=0.34)
    # pair 0,1: off failed + oracle passed → valid. pair 2: off SUCCEEDED → guessable → excluded.
    assert g["valid_mask"] == [True, True, False]
    assert g["excluded"] == [2]
    assert g["n_valid"] == 2


def test_necessity_gate_excludes_unsolvable_oracle_fail():
    g = scoring.necessity_gate(off_success=[0, 0], oracle_success=[1, 0], chance=0.34)
    # pair 1: oracle FAILED → unsolvable even when told → excluded.
    assert g["valid_mask"] == [True, False]
    assert g["excluded"] == [1]


def test_necessity_gate_raises_on_mismatch_or_empty():
    with pytest.raises(ValueError):
        scoring.necessity_gate(off_success=[0, 0], oracle_success=[1], chance=0.34)
    with pytest.raises(ValueError):
        scoring.necessity_gate(off_success=[], oracle_success=[], chance=0.34)


def test_transfer_verdict_reads_the_ci():
    assert scoring.transfer_verdict((0.5, 0.1, 0.9)) == "improve"
    assert scoring.transfer_verdict((-0.5, -0.9, -0.1)) == "regress"
    assert scoring.transfer_verdict((0.0, -0.2, 0.3)) == "inconclusive"


def test_scoring_does_not_import_diagnostics():
    """The gold/diagnostic firewall: the gold-side scorer must not IMPORT the store's-own
    chain-decomposition (a stronger pin lives in the purity suite; this is a fast local one).
    Inspects actual import statements, not a substring (the docstring may name the rule)."""
    import ast
    import sys

    src = open(sys.modules["hive.research.transfer.scoring"].__file__, encoding="utf-8").read()
    imported: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported += [f"{node.module}.{a.name}" for a in node.names]
    assert not any("diagnostics" in m for m in imported), imported
