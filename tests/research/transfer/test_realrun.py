"""Chunk 7 — the real-run glue, exercised offline against the REAL in-process daemon (no claude).

These pin the glue's daemon-facing logic with a fake embedder and direct client calls: the recorder
observes a capture text then a served recall text (after a vouch establishes it); the hidden-test
grader discriminates a correct from a default reader solution; and the prompt/tool wiring conceals
the value and gates recall. The full real ``claude -p`` integration is the staged run (Chunk 8).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from hive.research.transfer.run import Arm
from tests.mcp._helpers import FakeProvider
from tests.research.transfer.realrun import (
    _RECALL_TOOL, _downstream_prompt, _downstream_tools, make_transfer_realrun_seams,
)

_BENCH = Path(__file__).resolve().parents[4] / "Benchmark" / "transfer"
pytestmark = pytest.mark.skipif(not (_BENCH / "substrate.py").exists(),
                                reason="../Benchmark/transfer not present")


def _seams(tmp_path):
    return make_transfer_realrun_seams(
        benchmark_dir=str(_BENCH), seed=0, embedder=FakeProvider(d=64),
        isolation_config_dir=str(tmp_path), work_base=str(tmp_path / "work"))


def _generator():
    spec = importlib.util.spec_from_file_location("_tbench", _BENCH / "substrate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_tbench"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_recorder_observes_capture_then_served_recall(tmp_path):
    seams = _seams(tmp_path)
    clients = seams["daemon_factory"](Arm("recall_on"))
    try:
        st = seams["state"]["recall_on"]
        text = "the account identifier is stored under the acct field key"
        clients.seat_client("writer-1").capture(text)
        assert any(e.captured_texts for e in st.recorder.entries), "capture text not observed"
        # a bare capture is quarantined → not servable; the orchestrator vouch establishes it
        clients.orchestrator_client.write(text, approved_by="orchestrator")
        clients.seat_client("reader-1").recall(text)   # identical text → fake cosine 1.0 → serves
        assert any(e.served_texts for e in st.recorder.entries), "served recall text not observed"
    finally:
        clients.teardown()


def test_outcome_fn_grades_hidden_test(tmp_path):
    seams = _seams(tmp_path)
    mod = _generator()
    pd = mod.transfer_window(0)["pairs"][0]
    correct = mod.reference_solution(pd, side="downstream")
    wrong = mod.reference_solution(pd, side="downstream", token=pd["default_value"])

    clients = seams["daemon_factory"](Arm("recall_on"))
    try:
        st = seams["state"]["recall_on"]
        pair = seams["transfer_window"].pairs[0]
        ddir = Path(st.work) / pair.pair_id / "downstream"
        ddir.mkdir(parents=True, exist_ok=True)
        (ddir / "solution.py").write_text(correct, encoding="utf-8")
        assert seams["outcome_fn"](pair, arm=Arm("recall_on")) is True
        (ddir / "solution.py").write_text(wrong, encoding="utf-8")
        assert seams["outcome_fn"](pair, arm=Arm("recall_on")) is False
    finally:
        clients.teardown()


def test_downstream_tools_gate_recall():
    assert _RECALL_TOOL in _downstream_tools(True)
    assert _RECALL_TOOL not in _downstream_tools(False)


def test_downstream_prompt_conceals_value_and_gates_recall(tmp_path):
    seams = _seams(tmp_path)
    pair = seams["transfer_window"].pairs[0]
    no_recall = _downstream_prompt(pair, can_recall=False, oracle_value=None)
    assert pair.fact.value not in no_recall and "hive_recall" not in no_recall
    with_recall = _downstream_prompt(pair, can_recall=True, oracle_value=None)
    assert "hive_recall" in with_recall and pair.fact.value not in with_recall
    oracle = _downstream_prompt(pair, can_recall=False, oracle_value=pair.fact.value)
    assert pair.fact.value in oracle                # the oracle ceiling is handed the fact
