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


# ── seat_schedule + arm-threaded outcome_fn (fake clients/agent, ONE OFF arm — no
#    orchestrator/recall path) ─────────────────────────────────────────────────────────
class _FakeSeedClient:
    """A minimal orchestrator client: every ``write`` is approved with a fresh episode_id, so
    ``seed_store`` builds a real source_id → id mapping with no daemon."""
    def __init__(self) -> None:
        self._next = 0

    def write(self, text, *, approved_by, **kw) -> dict:
        self._next += 1
        return {"status": "approved", "id": self._next}


def _fake_off_factory(arm):
    return ArmClients(seat_client=lambda seat: object(),
                      orchestrator_client=_FakeSeedClient(),
                      teardown=lambda: None)


# a 3-task / 2-seat window; the bad fact falsifies no task so the single OFF arm needs no
# recall/outcome to score — the schedule + arm-threading are what we observe.
_SCHED_WINDOW = RepoWindow(
    facts=(RepoFact("keep::license", "keep_neutral", "MIT",
                    "This project is distributed under the MIT license.", kind="note"),),
    tasks=(RepoTask("t1", "a?"), RepoTask("t2", "b?"), RepoTask("t3", "c?")),
)


def test_round_robin_runs_exactly_one_seat_per_task_in_rotation():
    # seat_schedule="round_robin": task i is taken by EXACTLY ONE seat = seats[i % len(seats)].
    # Mutation: make round_robin still iterate all seats → both seats appear per task → this reds.
    turns: list = []

    def agent_turn(seat, task, client) -> AgentTurnObs:
        turns.append((task.task_id, seat))
        return AgentTurnObs(seat)

    run_selfmaint(arms=(Arm("maintenance_off", retire=False),), repo_window=_SCHED_WINDOW,
                  seed=7, daemon_factory=_fake_off_factory, agent_turn=agent_turn,
                  llm_factory=lambda: None, model="fake", llm_digest="d",
                  seats=("seat-1", "seat-2"), seat_schedule="round_robin")
    assert turns == [("t1", "seat-1"), ("t2", "seat-2"), ("t3", "seat-1")]


def test_default_all_schedule_runs_every_seat_per_task():
    # backward-compatible default: every seat takes every task (the existing behavior).
    turns: list = []

    def agent_turn(seat, task, client) -> AgentTurnObs:
        turns.append((task.task_id, seat))
        return AgentTurnObs(seat)

    run_selfmaint(arms=(Arm("maintenance_off", retire=False),), repo_window=_SCHED_WINDOW,
                  seed=7, daemon_factory=_fake_off_factory, agent_turn=agent_turn,
                  llm_factory=lambda: None, model="fake", llm_digest="d",
                  seats=("seat-1", "seat-2"))
    assert turns == [("t1", "seat-1"), ("t1", "seat-2"),
                     ("t2", "seat-1"), ("t2", "seat-2"),
                     ("t3", "seat-1"), ("t3", "seat-2")]


def test_outcome_fn_receives_the_running_arm():
    # _run_arm threads arm=arm into outcome_fn so a real-run gold can scope a per-arm build dir.
    # Mutation: drop arm=arm from the call → the recorder sees arm=None → this reds.
    seen: list = []

    def agent_turn(seat, task, client) -> AgentTurnObs:
        return AgentTurnObs(seat)

    def recording_outcome(task, *, arm, **kw) -> bool:
        seen.append(arm.name)
        return True

    run_selfmaint(arms=(Arm("maintenance_off", retire=False),), repo_window=_SCHED_WINDOW,
                  seed=7, daemon_factory=_fake_off_factory, agent_turn=agent_turn,
                  llm_factory=lambda: None, outcome_fn=recording_outcome,
                  model="fake", llm_digest="d", seats=("seat-1",), seat_schedule="round_robin")
    assert seen == ["maintenance_off", "maintenance_off", "maintenance_off"]


# ── the agent's hurt_evidence reaches the orchestrator's prompt at the checkpoint review ──
class _RecordingSeedClient(_FakeSeedClient):
    """A seed client that also serves an EMPTY worklist + records prunes — so the checkpoint
    review surfaces only the agent's hurt candidate and judges it."""
    def __init__(self) -> None:
        super().__init__()
        self.prunes: list = []

    def health(self, **kw) -> dict:
        return {"ok": True, "conflicts": [], "suspect_consensus": []}

    def prune(self, episode_id, *, approved_by) -> dict:
        self.prunes.append((episode_id, approved_by))
        return {"status": "pruned", "episode_id": episode_id, "approved_by": approved_by}


def test_hurt_evidence_is_threaded_into_the_orchestrator_prompt():
    # _run_arm must collect obs.hurt_evidence (eid→reason) alongside flagged_hurt and pass it as
    # hurt_evidence= to run_orchestrator_review, so the gate judges on the agent's artifact-grounded
    # reason. Mutation: drop the pending_evidence collection (pass nothing) → the reason is absent
    # from the captured prompt → this reds.
    orch = _RecordingSeedClient()

    def daemon_factory(arm) -> ArmClients:
        return ArmClients(seat_client=lambda seat: object(),
                          orchestrator_client=orch, teardown=lambda: None)

    def agent_turn(seat, task, client) -> AgentTurnObs:
        # the agent flagged memory 7 hurt AND stated WHY it is factually wrong.
        return AgentTurnObs(seat, flagged_hurt=(7,),
                            hurt_evidence=((7, "because spec says KeyError"),))

    captured: list = []

    class _CapturingLLM:
        calls: list = []

        def complete(self, prompt, *, system=None) -> str:
            captured.append(prompt)
            return '{"decisions":[{"episode_id":7,"verb":"keep","reason":"insufficient"}]}'

    run_selfmaint(arms=(Arm("maintenance_on", retire=True),), repo_window=_SCHED_WINDOW,
                  seed=7, daemon_factory=daemon_factory, agent_turn=agent_turn,
                  llm_factory=lambda: _CapturingLLM(), outcome_fn=lambda task, **kw: True,
                  model="fake", llm_digest="d", seats=("seat-1",), checkpoint_every=1)
    assert captured, "the orchestrator review never ran"
    assert any("because spec says KeyError" in p for p in captured)


# ── the autonomous arm: the AGENT's own prunes are the retirements (NO orchestrator) ──
class _ExplodingLLM:
    """An orchestrator LLM that RAISES if its judgment is ever asked — the autonomous arm must
    never reach the checkpoint review, so any ``complete`` call is a contract violation."""
    def complete(self, prompt, *, system=None) -> str:        # pragma: no cover - must not run
        raise AssertionError("the autonomous arm reached run_orchestrator_review")


def test_agent_prunes_arm_retires_via_the_agent_not_the_orchestrator():
    # An agent_prunes arm (agi_mode=True, retire=False) takes the agent's OWN hive_prune calls
    # (obs.pruned_ids) as the retirement set — the agent judged through the shipped tool, no
    # orchestrator. The single seeded source maps to episode_id 1, which the agent prunes.
    # Mutation: drop the obs.pruned_ids collection in _run_arm (or route it through the
    # orchestrator) → the retired-source assertion reds (and an orchestrator route would also
    # explode the LLM).
    factory_calls: list = []

    def llm_factory():
        factory_calls.append(1)
        return _ExplodingLLM()

    def agent_turn(seat, task, client) -> AgentTurnObs:
        # the agent verified the seeded note (episode_id 1) is factually wrong and self-pruned it.
        return AgentTurnObs(seat, pruned_ids=(1,))

    report = run_selfmaint(
        arms=(Arm("agi", agi_mode=True, agent_prunes=True, retire=False),),
        repo_window=_SCHED_WINDOW, seed=7, daemon_factory=_fake_off_factory,
        agent_turn=agent_turn, llm_factory=llm_factory, outcome_fn=lambda task, **kw: True,
        model="fake", llm_digest="d", seats=("seat-1",))
    arm = next(a for a in report["arms"] if a["name"] == "agi")
    assert arm["retired_sources"] == ["keep::license"]        # the agent's prune retired the source
    assert factory_calls == []                                # the orchestrator was never invoked
