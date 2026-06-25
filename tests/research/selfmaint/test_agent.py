"""S4 — the autonomous agent turn + its offline double.

``FakeAgent`` is the offline workhorse: it scripts a fixed sequence of ``hive_*`` calls through a
real ``McpHttpClient`` against the real Hivemind stack (``build_real_server`` behind loopback), so
the capture→recall→outcome loop is fake-verified with no real LLM. ``run_agent_turn`` is the real
``claude -p`` path (the agent emits its OWN calls against the warm daemon); its argv contract
(BUG-007: ``--strict-mcp-config`` + a Hivemind-only ``--mcp-config``) and fail-closed envelope
parse are pinned here with an injected runner — the real autonomous run is exercised at S6.
"""
from __future__ import annotations

import json

import pytest

from hive.research.selfmaint import agent as agent_mod
from hive.research.selfmaint.agent import (
    BUILD_AGENT_TOOLS, AgentTurnObs, FakeAgent, run_agent_turn,
)
from hive.research.selfmaint.daemon import McpHttpClient, RunResult

from ._selfmaint_helpers import live_daemon


# ── FakeAgent against the real stack ────────────────────────────────────────────────
def test_fake_agent_recall_then_outcome_against_real_stack():
    url, _server, _clock, stop = live_daemon()
    try:
        client = McpHttpClient(url, agent_id="seat-1")
        eid = client.write("the parser takes two args", approved_by="human")["id"]
        agent = FakeAgent("seat-1", client, [
            ("recall", {"query": "the parser takes two args"}),
            ("outcome", {"hurt": [eid]}),
        ])
        obs = agent.run()
        assert isinstance(obs, AgentTurnObs) and obs.seat == "seat-1"
        assert eid in obs.served_ids                 # the recall served the established memory
        assert obs.flagged_hurt == (eid,)            # the agent reported it hurt its task
        assert [verb for verb, _status in obs.tool_calls] == ["recall", "outcome"]
    finally:
        stop()


def test_fake_agent_captures_quarantined_without_serving():
    # a capture lands quarantined (stored, unserved); a subsequent recall abstains on it.
    url, _server, _clock, stop = live_daemon()
    try:
        client = McpHttpClient(url, agent_id="seat-1")
        obs = FakeAgent("seat-1", client, [
            ("capture", {"text": "a durable insight worth promoting"}),
            ("recall", {"query": "a durable insight worth promoting"}),
        ]).run()
        assert obs.served_ids == ()                  # quarantined ⇒ not served
    finally:
        stop()


# ── run_agent_turn: the real claude -p argv + fail-closed parse ──────────────────────
class _CannedRunner:
    """Records the argv and returns a canned ``claude -p --output-format json`` envelope."""
    def __init__(self, envelope: dict, returncode: int = 0) -> None:
        self.argv: list = []
        self._env = envelope
        self._rc = returncode

    def run(self, argv) -> RunResult:
        self.argv = list(argv)
        return RunResult(self._rc, json.dumps(self._env), "")


def test_run_agent_turn_argv_carries_strict_mcp_config():
    runner = _CannedRunner({"result": "did the work", "subtype": "success", "is_error": False})
    obs = run_agent_turn("seat-1", "do the work", mcp_config_path="/tmp/hive.json",
                         agent_id="seat-1", runner=runner.run)
    assert "--strict-mcp-config" in runner.argv
    assert runner.argv[runner.argv.index("--mcp-config") + 1] == "/tmp/hive.json"
    # the fleet agent is granted exactly its hive tools (headless MCP needs --allowedTools, BUG-011)
    granted = runner.argv[runner.argv.index("--allowedTools") + 1]
    assert "mcp__hive__hive_recall" in granted and "mcp__hive__hive_outcome" in granted
    assert "mcp__hive__hive_prune" not in granted        # the privileged verbs stay the orchestrator's
    assert obs.seat == "seat-1" and obs.result_text == "did the work"


def test_run_agent_turn_fails_closed_on_error_envelope():
    runner = _CannedRunner({"result": "boom", "subtype": "error_during_execution",
                            "is_error": True})
    with pytest.raises(RuntimeError):
        run_agent_turn("seat-1", "q", mcp_config_path="/c.json", agent_id="seat-1",
                       runner=runner.run)


# ── the build profile: the coding tools + the hive tools, all under --strict-mcp-config ──
def test_build_agent_tools_grants_coding_and_hive_tools():
    # the build profile = the four coding tools PLUS the five hive verbs the agent emits.
    for coding in ("Read", "Write", "Edit", "Bash"):
        assert coding in BUILD_AGENT_TOOLS
    assert "mcp__hive__hive_recall" in BUILD_AGENT_TOOLS         # the hive verbs ride along
    assert "mcp__hive__hive_prune" not in BUILD_AGENT_TOOLS      # the privileged verbs stay out


def test_run_agent_turn_build_profile_argv_carries_coding_and_hive_tools():
    # a build-profile turn (allowed_tools=BUILD_AGENT_TOOLS) grants the coding tools AND the hive
    # tools to one --strict-mcp-config'd claude -p. Mutation: drop the coding tools from
    # BUILD_AGENT_TOOLS → "Write" leaves the granted set → this reds.
    runner = _CannedRunner({"result": "built", "subtype": "success", "is_error": False})
    run_agent_turn("builder", "build the kvstore", mcp_config_path="/tmp/hive.json",
                   agent_id="builder", runner=runner.run, allowed_tools=BUILD_AGENT_TOOLS)
    assert "--strict-mcp-config" in runner.argv
    granted = runner.argv[runner.argv.index("--allowedTools") + 1]
    for coding in ("Read", "Write", "Edit", "Bash"):
        assert coding in granted
    assert "mcp__hive__hive_recall" in granted


def test_run_agent_turn_default_runner_receives_cwd(monkeypatch):
    # the injected-runner seam stays argv-only, so cwd is closed over by the DEFAULT runner: a
    # real (un-injected) turn must forward cwd to subprocess.run. Mutation: drop cwd from the
    # default-runner closure (or stop forwarding it) → cwd never reaches subprocess.run → this reds.
    seen: dict = {}

    class _Done:
        returncode = 0
        stdout = json.dumps({"result": "ok", "subtype": "success", "is_error": False})
        stderr = ""

    def _spy(argv, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        return _Done()

    monkeypatch.setattr(agent_mod.subprocess, "run", _spy)
    run_agent_turn("builder", "build it", mcp_config_path="/tmp/hive.json",
                   agent_id="builder", cwd="/tmp/arm_on")
    assert seen["cwd"] == "/tmp/arm_on"
