"""Chunk 5 — the two-phase transfer runner, exercised offline with fakes.

A tiny in-memory fake daemon models the only daemon behaviors the runner depends on: vouch makes a
captured value servable; a cross-identity recall-miss fan-out demand-promotes it after ``demand_m``
distinct seats miss. The runner threads the two phases, the necessity gate, the headline Δ, the
chain diagnostics, and the provenance — all without an API.
"""
from __future__ import annotations

import pytest

from hive.research.transfer.run import (
    DEFAULT_ARMS, ArmClients, TransferTurnObs, build_transfer_report, label_transfer_gold,
    run_transfer,
)
from hive.research.transfer.substrate import fixture_transfer_window


class _FakeDaemon:
    def __init__(self, demand_m: int) -> None:
        self.demand_m = demand_m
        self.promoted: set[str] = set()
        self.value_by_pair: dict[str, str] = {}
        self.captured: dict[str, list] = {}
        self.demand: dict[str, set] = {}

    def capture(self, pid, value, text):
        self.value_by_pair[pid] = value
        self.captured.setdefault(pid, []).append(text)

    def vouch(self, pid):
        val = self.value_by_pair.get(pid)
        if val is not None:
            self.promoted.add(val)

    def recall(self, seat, pid):
        val = self.value_by_pair.get(pid)
        if val is not None and val in self.promoted:
            return val
        # miss → record a cross-identity demand vote; promote once demand_m distinct seats miss
        self.demand.setdefault(pid, set()).add(seat)
        if val is not None and len(self.demand[pid]) >= self.demand_m:
            self.promoted.add(val)            # servable for the NEXT reader (this one already missed)
        return None


class _FakeSeat:
    def __init__(self, daemon, seat):
        self._d, self._seat = daemon, seat

    def capture(self, pid, value, text):
        self._d.capture(pid, value, text)

    def recall(self, pid):
        return self._d.recall(self._seat, pid)

    def vouch(self, pid):
        self._d.vouch(pid)


def _agent_turn(seat, task, client):
    pair = task.pair
    if task.role == "upstream":
        client.capture(pair.pair_id, pair.fact.value, pair.fact.carrier_text)
        return TransferTurnObs(seat=seat, captured_texts=(pair.fact.carrier_text,))
    if task.oracle_value is not None:
        return TransferTurnObs(seat=seat, served_values=(task.oracle_value,),
                               served_texts=(pair.fact.carrier_text,))
    if task.can_recall:
        val = client.recall(pair.pair_id)
        if val is not None:
            return TransferTurnObs(seat=seat, served_values=(val,),
                                   served_texts=(pair.fact.carrier_text,))
    return TransferTurnObs(seat=seat)


def _vouch_fn(orch_client, captured):
    out = {}
    for pid, texts in captured.items():
        orch_client.vouch(pid)
        out[pid] = texts[0] if texts else ""
    return out


def _seams(demand_m=2):
    def daemon_factory(arm):
        d = _FakeDaemon(demand_m)
        return ArmClients(seat_client=lambda s, d=d: _FakeSeat(d, s),
                          orchestrator_client=_FakeSeat(d, "orchestrator"),
                          teardown=lambda: None)
    return daemon_factory, _agent_turn, _vouch_fn


def _run(window=None, outcome_fn=label_transfer_gold):
    daemon_factory, agent_turn, vouch_fn = _seams(demand_m=2)
    return run_transfer(
        arms=DEFAULT_ARMS, transfer_window=window or fixture_transfer_window(0), seed=0,
        daemon_factory=daemon_factory, agent_turn=agent_turn, vouch_fn=vouch_fn,
        model="fake", llm_digest="offline", chance=0.34, demand_m=2)


def test_full_pipeline_closes_recall_determines_success():
    rep = _run()
    by = {a["name"]: a for a in rep["arms"]}
    # recall_on recalls the vouched fact → succeeds; recall_off has it servable but no recall tool
    # → fails; oracle is handed the value → succeeds; demand autonomously promotes it → succeeds.
    assert by["recall_on"]["success_vec"] == [1, 1]
    assert by["recall_off"]["success_vec"] == [0, 0]
    assert by["oracle_ceiling"]["success_vec"] == [1, 1]
    assert by["demand_promote"]["success_vec"] == [1, 1]


def test_necessity_gate_certifies_all_pairs_valid():
    rep = _run()
    assert rep["necessity"]["valid_mask"] == [True, True]
    assert rep["necessity"]["n_valid"] == 2


def test_headline_delta_is_significant_improve():
    rep = _run()
    assert rep["deltas"]["success"] == "improve"
    assert rep["deltas"]["success_ci"][1] > 0.0          # lo > 0 → ships
    assert "demand_vs_off_ci" in rep["deltas"]


def test_chain_diagnostics_attached_to_headline_arm():
    rep = _run()
    on = next(a for a in rep["arms"] if a["name"] == "recall_on")
    chain = on["chain_diagnostics"]
    assert all(chain["capture_fidelity"].values())      # every pair captured the right fact
    assert all(chain["served_correct"].values())        # and recall served it


def test_demand_arm_reports_its_cost():
    rep = _run()
    dem = next(a for a in rep["arms"] if a["name"] == "demand_promote")
    # with demand_m=2 and a fanout of 4 readers/pair, 2 early readers per pair pay the miss
    assert dem["demanders_failed"] == 4


def test_necessity_gate_excludes_a_guessable_pair():
    window = fixture_transfer_window(0)
    first = window.pairs[0].pair_id

    def og(pair, *, arm, served_values):
        if arm.name == "recall_off" and pair.pair_id == first:
            return True                                  # off "guesses" pair 0 → not required
        return label_transfer_gold(pair, arm=arm, served_values=served_values)

    daemon_factory, agent_turn, vouch_fn = _seams(demand_m=2)
    rep = run_transfer(arms=DEFAULT_ARMS, transfer_window=window, seed=0,
                       daemon_factory=daemon_factory, agent_turn=agent_turn, vouch_fn=vouch_fn,
                       outcome_fn=og, model="fake", llm_digest="offline", chance=0.34, demand_m=2)
    assert rep["necessity"]["valid_mask"] == [False, True]
    assert rep["necessity"]["excluded"] == [0]


def test_build_transfer_report_refuses_incomplete_provenance():
    with pytest.raises(ValueError):
        build_transfer_report(
            arms_results=[{"name": "recall_on", "success": [True]}],
            deltas={}, necessity={},
            provenance={"transfer_window_hash": "h", "seed": 0, "model": "m"})  # missing keys
