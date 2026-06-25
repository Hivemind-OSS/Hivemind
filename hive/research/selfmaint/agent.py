"""S4 — the autonomous agent turn + its offline double.

Two ways to drive a seat's ``hive_*`` activity:

* ``run_agent_turn`` — the REAL autonomous turn: a ``claude -p`` subprocess with a Hivemind-only
  ``--mcp-config`` + ``--strict-mcp-config`` (built by ``build_claude_argv``), inside which the
  model EMITS its own ``hive_recall`` / ``hive_capture`` / ``hive_outcome`` / ``hive_flag`` calls
  against the warm daemon. This is the deployed system. The subprocess seam is injectable so the
  argv + fail-closed envelope parse are unit-testable with no real CLI; the agent's autonomous
  ``hive_*`` calls land server-side, so the served/hurt observations of a REAL turn are
  reconciled from the daemon (the run composes that at S6) — ``result_text`` carries the reply.

* ``FakeAgent`` — the offline double: it scripts a fixed sequence of tool calls through a real
  ``McpHttpClient`` against ``build_real_server``, so the capture→recall→outcome loop is
  fake-verified deterministically with no LLM and assembles a full ``AgentTurnObs`` (served ids
  from recall hits, hurt ids from outcome). This is the workhorse the offline tests + the e2e
  smoke use.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from hive.research.selfmaint.daemon import RunResult, build_claude_argv

_DEFAULT_TIMEOUT_S = 180

# The MCP tools a FLEET agent is granted (``mcp__<server>__<tool>`` for the "hive" server in the
# generated --mcp-config): the read + capture + pure-evidence verbs it emits during real work. The
# privileged retirement verbs (write / prune / supersede) are deliberately NOT granted — those are
# the orchestrator's, issued through the harness, so the agent boundary stays read+surface only.
FLEET_AGENT_TOOLS = (
    "mcp__hive__hive_recall", "mcp__hive__hive_capture", "mcp__hive__hive_outcome",
    "mcp__hive__hive_flag", "mcp__hive__hive_health")

# The build profile: the four coding tools a real claude -p needs to author the greenfield repo
# (Read / Write / Edit / Bash) PLUS the same five hive verbs FLEET_AGENT_TOOLS grants. A build turn
# runs IN the build repo (``cwd``) and emits its own ``hive_*`` calls while it codes; the privileged
# retirement verbs (prune / supersede / write) stay the orchestrator's, not granted here.
BUILD_AGENT_TOOLS = ("Read", "Write", "Edit", "Bash", *FLEET_AGENT_TOOLS)


@dataclass(frozen=True)
class AgentTurnObs:
    """One seat's turn, as observed. Self-asserting: the collections default empty (an inert turn
    under-claims rather than fabricating activity), and ``result_text`` is the raw reply."""
    seat: str
    tool_calls: tuple = ()          # (verb, status) per call the seat made
    served_ids: tuple = ()          # episode_ids served to this seat across its recalls
    flagged_hurt: tuple = ()        # episode_ids the seat reported as hurt (hive_outcome)
    result_text: str = ""


class FakeAgent:
    """Scripts a fixed sequence of ``(verb, kwargs)`` tool calls through a client (a real
    ``McpHttpClient`` against ``build_real_server``, or a recording double). The offline stand-in
    for an autonomous turn — deterministic, LLM-free."""

    def __init__(self, seat: str, client, script: Sequence[tuple]) -> None:
        self._seat = seat
        self._client = client
        self._script = list(script)

    def run(self) -> AgentTurnObs:
        calls: list = []
        served: list = []
        hurt: list = []
        for verb, kwargs in self._script:
            res = getattr(self._client, verb)(**kwargs)
            status = res.get("status") if isinstance(res, dict) else None
            calls.append((verb, status))
            if verb == "recall":
                served.extend(int(h["episode_id"]) for h in (res.get("reference_context") or [])
                              if isinstance(h, dict) and "episode_id" in h)
            elif verb == "outcome":
                hurt.extend(int(x) for x in (res.get("hurt") or []))
        return AgentTurnObs(seat=self._seat, tool_calls=tuple(calls),
                            served_ids=tuple(served), flagged_hurt=tuple(hurt))


def _default_agent_runner(argv: list[str], cwd: Optional[str] = None) -> RunResult:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=_DEFAULT_TIMEOUT_S,
                           cwd=cwd)
        return RunResult(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"timed out after {_DEFAULT_TIMEOUT_S}s")
    except FileNotFoundError as e:
        return RunResult(127, "", str(e))


def run_agent_turn(seat: str, prompt: str, *, mcp_config_path: str, agent_id: str,
                   runner: Optional[Callable[[list[str]], RunResult]] = None,
                   allowed_tools: Sequence[str] = FLEET_AGENT_TOOLS,
                   model: Optional[str] = None, system: Optional[str] = None,
                   cwd: Optional[str] = None) -> AgentTurnObs:
    """One real autonomous ``claude -p`` turn against the warm daemon. Fail-CLOSED: a non-zero
    exit, an unparseable reply, or an ``is_error`` / non-``success`` envelope RAISES (never a
    silent empty result). The agent's autonomous ``hive_*`` effects land server-side; the
    served/hurt observations are reconciled from the daemon by the run (the obs's collections stay
    empty here — a real turn under-claims rather than guessing the trace from a plain JSON
    envelope).

    ``cwd`` is the directory the subprocess runs IN (the build repo for a build-profile turn). The
    injected ``runner`` seam stays argv-only (so the argv contract is unit-testable with no CWD);
    ``cwd`` is closed over by the DEFAULT runner only, which forwards it to ``subprocess.run``."""
    argv = build_claude_argv(prompt, mcp_config_path=mcp_config_path,
                             allowed_tools=allowed_tools, system=system, model=model)
    runner = runner or (lambda a: _default_agent_runner(a, cwd=cwd))
    res = runner(argv)
    if res.returncode != 0:
        raise RuntimeError(f"claude -p exited {res.returncode}: {(res.stderr or '').strip()[:200]}")
    try:
        env = json.loads(res.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(f"claude -p reply was not JSON: {res.stdout[:120]!r}") from e
    if not isinstance(env, dict) or "result" not in env:
        raise RuntimeError(f"claude -p reply missing 'result': {res.stdout[:120]!r}")
    if env.get("is_error") or env.get("subtype") != "success":
        raise RuntimeError(
            f"claude -p returned an error envelope (subtype={env.get('subtype')!r}, "
            f"is_error={env.get('is_error')!r})")
    return AgentTurnObs(seat=seat, result_text=str(env["result"]))
