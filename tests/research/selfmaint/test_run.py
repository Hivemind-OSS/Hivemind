"""S5 — the runner over the arms + the provenance-stamped report (offline e2e, no API).

A full pipeline run with fakes: real ``McpHttpClient`` seats against ``build_real_server`` behind
loopback, a deterministic smoke agent that recalls the relevant seeds and flags hurt on a served
bad fact (the stand-in for "the agent hit the contradiction"), and a prune-all orchestrator that
judges the surfaced hurts. The bad fact falsifies an EARLY and a LATE task, so maintenance-ON's
retirement after the early checkpoint demonstrably spares the late task that OFF still fails.

This is a plumbing/e2e smoke, not a powered result (n=3 ⇒ the bootstrap CIs are honestly
inconclusive); it pins that the runner composes seed → fleet → review → diagnostics → score →
report correctly, the ON↔OFF contrast moves the right scalars, and the report refuses on
incomplete provenance.
"""
from __future__ import annotations

import re

import pytest

from hive.research.bench.llm import FakeLLM
from hive.research.selfmaint.agent import AgentTurnObs
from hive.research.selfmaint.daemon import McpHttpClient
from hive.research.selfmaint.run import (
    Arm, ArmClients, build_selfmaint_report, run_selfmaint,
)
from hive.research.selfmaint.substrate import (
    RepoFact, RepoTask, RepoWindow, build_selfmaint_substrate,
)

from ._selfmaint_helpers import loopback_server
from tests.mcp._helpers import build_real_server

_SEED = 7

# the bad fact falsifies t1 (early) AND t3 (late); a keep-valuable feeds t2.
_WINDOW = RepoWindow(
    facts=(
        RepoFact("bad::arity", "prunable_bad", "2",
                 "The parse function takes 2 positional arguments.", kind="stale"),
        RepoFact("keep::config", "keep_valuable", "config/app.toml",
                 "The application configuration file is config/app.toml.", kind="note"),
        RepoFact("keep::license", "keep_neutral", "MIT",
                 "This project is distributed under the MIT license.", kind="note"),
    ),
    tasks=(
        RepoTask("t1", "parse arity?", falsified_by=frozenset({"bad::arity"}), expected="2"),
        RepoTask("t2", "config path?", needs_valuable=frozenset({"keep::config"}),
                 expected="config/app.toml"),
        RepoTask("t3", "parse arity again?", falsified_by=frozenset({"bad::arity"}), expected="2"),
    ),
)


def _smoke_pieces():
    """Build the deterministic agent turn + the prune-all orchestrator factory off the SAME specs
    the runner will synthesize (same window + seed ⇒ identical specs)."""
    specs, tasks = build_selfmaint_substrate(_WINDOW, seed=_SEED)
    bad_texts = {s.text for s in specs if s.regime == "prunable_bad"}
    relevant = {t.task_id: [s.text for s in specs
                            if s.source_id in (t.falsified_by | t.needs_valuable)] for t in tasks}

    def agent_turn(seat, task, client) -> AgentTurnObs:
        served, hurt = [], []
        for text in relevant.get(task.task_id, []):
            for hit in client.recall(text).get("reference_context", []):
                eid = hit["episode_id"]
                served.append(eid)
                if hit.get("text") in bad_texts:        # the agent detected a contradiction
                    hurt.extend(client.outcome(hurt=[eid]).get("hurt", []))
        return AgentTurnObs(seat, served_ids=tuple(served), flagged_hurt=tuple(hurt))

    def _prune_all(prompt, system):
        ids = re.findall(r"episode_id=(\d+)", prompt)
        return '{"decisions":[' + ",".join(
            f'{{"episode_id":{i},"verb":"prune","reason":"hurt"}}' for i in ids) + "]}"

    return agent_turn, (lambda: FakeLLM(_prune_all))


def _daemon_factory(arm):
    server, _clock = build_real_server()
    url, stop = loopback_server(server)
    return ArmClients(
        seat_client=lambda seat: McpHttpClient(url, agent_id=seat),
        orchestrator_client=McpHttpClient(url, agent_id="orchestrator"),
        teardown=stop)


def _run(arms):
    agent_turn, llm_factory = _smoke_pieces()
    return run_selfmaint(arms=arms, repo_window=_WINDOW, seed=_SEED,
                         daemon_factory=_daemon_factory, agent_turn=agent_turn,
                         llm_factory=llm_factory, model="fake", llm_digest="deadbeef",
                         seats=("seat-1", "seat-2"))


def _arm(report, name):
    return next(a for a in report["arms"] if a["name"] == name)


# ── the headline ON↔OFF contrast moves the right scalars ─────────────────────────────
def test_on_retires_bad_and_off_does_not():
    report = _run((Arm("maintenance_on"), Arm("maintenance_off", retire=False)))
    on, off = _arm(report, "maintenance_on"), _arm(report, "maintenance_off")
    assert on["prune_recall"] == 1.0 and on["false_prune_rate"] == 0.0   # retired the bad, no keep
    assert off["prune_recall"] == 0.0                                    # OFF retires nothing
    # the late task is the payoff: ON spared a bad-serve that OFF still pays.
    assert sum(on["bad_serve_vec"]) < sum(off["bad_serve_vec"])
    assert report["deltas"]["prune_recall_delta"] == 1.0
    assert "clean_win" in report["deltas"]


def test_on_success_does_not_regress_below_off():
    report = _run((Arm("maintenance_on"), Arm("maintenance_off", retire=False)))
    on, off = _arm(report, "maintenance_on"), _arm(report, "maintenance_off")
    assert sum(on["success_vec"]) >= sum(off["success_vec"])             # maintenance never hurts success


# ── the prune-everything anti-gaming control visibly tanks precision ─────────────────
def test_prune_everything_tanks_false_prune():
    report = _run((Arm("maintenance_on"),
                   Arm("maintenance_off", retire=False),
                   Arm("prune_everything", prune_everything=True)))
    pe = _arm(report, "prune_everything")
    assert pe["false_prune_rate"] == 1.0                                 # every keep wrongly retired
    assert _arm(report, "maintenance_on")["false_prune_rate"] == 0.0     # the headline does not


# ── provenance is complete + the report refuses on incomplete provenance ─────────────
def test_report_is_provenance_stamped():
    report = _run((Arm("maintenance_on"), Arm("maintenance_off", retire=False)))
    prov = report["provenance"]
    for key in ("repo_window_hash", "seed", "model", "llm_digest", "seed_value_map_hash",
                "agi_mode_by_arm"):
        assert prov.get(key) not in (None, "")


def test_build_report_refuses_on_incomplete_provenance():
    arms = [{"name": "maintenance_on", "success": [True]}]
    with pytest.raises(ValueError):
        build_selfmaint_report(arms_results=arms, deltas={},
                               provenance={"seed": 7})            # missing required keys


def test_build_report_refuses_on_missing_success_vector():
    full_prov = {"repo_window_hash": "h", "seed": 7, "k_passhat": 1, "model": "m",
                 "llm_digest": "d", "agi_mode_by_arm": {}, "seed_value_map_hash": "v"}
    with pytest.raises(ValueError):
        build_selfmaint_report(arms_results=[{"name": "x", "success": []}], deltas={},
                               provenance=full_prov)
