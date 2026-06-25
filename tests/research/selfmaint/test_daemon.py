"""S0 — the real-MCP/HTTP driver (``McpHttpClient``), the daemon lifecycle controller
(``DaemonHandle``), and the agent-argv builder (``build_claude_argv``).

The client is exercised against the REAL Hivemind stack behind REAL HTTP: a loopback
``ThreadingHTTPServer`` fronting ``build_real_server`` via the shipped ``_build_handler`` —
exactly how ``tests/mcp/test_http_server.py`` drives the endpoint contract, the way the
deployed system runs. So a tool call here traverses the true
parse→dispatch→handler→envelope path, and the defensive-parse contract is proven against the
ACTUAL envelopes the server emits (not a hand-rolled mock of them).

Pinned contracts (each with a deliberate mutation that reds a named test):
  * defensive envelope parse (BUG-002/003/005): a JSON-RPC error envelope → ``McpClientError``,
    never a ``KeyError``; the key-omitting ``refused`` / ``disabled`` / ``noop`` / ``agi-refused``
    / abstained envelopes come back as dicts without raising.
  * per-seat identity: the client sends ``X-Hive-Agent-Id`` and NEVER an ``Origin`` header
    (Origin → 403 before the handler).
  * BUG-007: ``build_claude_argv`` always carries ``--strict-mcp-config`` (+ a Hivemind-only
    ``--mcp-config``) so a per-call ``claude -p`` agent loads ONLY the warm daemon, never the
    operator's MCP servers.
  * ``HIVE_AGI__MODE`` is set on the ``hive up`` env iff the arm is AGI — byte-inert by default.
"""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from hive.app.config import AutonomyConfig
from hive.app.http_server import _build_handler
from hive.app.mcp_server import MCPResponse, ServerIdentity
from hive.research.selfmaint.daemon import (
    DaemonError, DaemonHandle, McpClientError, McpHttpClient, RunResult,
    build_claude_argv,
)
from tests.mcp._helpers import build_real_server

_AGENT_HEADER = "X-Hive-Agent-Id"


# ── loopback servers (real handler; a real Hivemind server OR a recording spy) ──────
def _serve(handler_server, *, auth_required: bool = False, verify=None):
    """Start a real loopback ``ThreadingHTTPServer`` for ``handler_server``; return
    ``(url, stop)``. ``auth_required=False`` is the tokenless loopback door the harness uses."""
    verify = verify or (lambda tok: None)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _build_handler(handler_server, verify, threading.Lock(), auth_required=auth_required))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"

    def stop():
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)
    return url, stop


def _live(**server_kw):
    """``(url, server, clock, stop)`` — the REAL Hivemind MCP server behind real HTTP."""
    server, clock = build_real_server(**server_kw)
    url, stop = _serve(server)
    return url, server, clock, stop


class _IdentitySpy:
    """Records the per-request ``identity`` each ``handle`` sees and returns a canned
    tools/call result, so the client's identity-header threading is observable in isolation."""
    def __init__(self) -> None:
        self.identity = ServerIdentity("default", "srv")
        self.calls: list = []

    def handle(self, req, *, identity=None):
        self.calls.append((req, identity))
        return MCPResponse(id=req.id, result={
            "content": [{"type": "text", "text": json.dumps({"ok": True})}], "isError": False})


class _MalformedResultSpy:
    """Returns a 200 JSON-RPC reply whose ``result`` LACKS the tools/call ``content`` framing —
    a shape the real server never emits, so the client's content-framing guard can be pinned in
    isolation (the daemon always returns well-formed content for a known tool)."""
    identity = ServerIdentity("default", "srv")

    def handle(self, req, *, identity=None):
        return MCPResponse(id=req.id, result={"isError": False})    # no "content" key


# ── McpHttpClient: the happy path + identity threading ──────────────────────────────
def test_recall_roundtrip_over_real_http():
    url, server, _clock, stop = _live()
    try:
        c = McpHttpClient(url, agent_id="seat-1")
        res = c.write("alpha beta gamma delta", approved_by="human")
        assert res["status"] != "refused" and "id" in res
        eid = res["id"]
        rec = c.recall("alpha beta gamma delta")          # exact text ⇒ cosine 1.0 ⇒ served
        assert rec["abstained"] is False
        assert any(h["episode_id"] == eid for h in rec["reference_context"])
    finally:
        stop()


def test_client_threads_distinct_agent_id_header():
    # AC1: each seat carries a DISTINCT identity (the demand-promotion anti-gaming clause needs
    # ≥2 identities). The client sends X-Hive-Agent-Id; the resolved identity is per-seat.
    spy = _IdentitySpy()
    url, stop = _serve(spy)
    try:
        assert McpHttpClient(url, agent_id="seat-A").health()["ok"] is True
        assert McpHttpClient(url, agent_id="seat-B").health()["ok"] is True
        assert [ident.agent_id for (_req, ident) in spy.calls] == ["seat-A", "seat-B"]
    finally:
        stop()


def test_client_sends_no_origin_header_so_loopback_serves():
    # The shipped handler 403s on ANY Origin header (DNS-rebinding belt) BEFORE the handler.
    # A successful health over the real handler proves the client sends no Origin.
    url, _server, _clock, stop = _live()
    try:
        assert McpHttpClient(url, agent_id="seat-1").health()["ok"] is True
    finally:
        stop()


def test_optional_bearer_token_gates_the_tunnel_door():
    # The token param is a real contract (not decoration): on an auth_required door a good token
    # passes and a missing token 401s → McpClientError carrying the http_status.
    server, _clock = build_real_server()
    url, stop = _serve(server, auth_required=True,
                       verify=lambda tok: "seat" if tok == "tok-good" else None)
    try:
        assert McpHttpClient(url, agent_id="s", token="tok-good").health()["ok"] is True
        with pytest.raises(McpClientError) as ei:
            McpHttpClient(url, agent_id="s").health()
        assert ei.value.http_status == 401
    finally:
        stop()


# ── McpHttpClient: defensive parse of every key-omitting envelope (no KeyError) ─────
def test_recall_abstains_on_empty_store_without_keyerror():
    url, _server, _clock, stop = _live()
    try:
        rec = McpHttpClient(url, agent_id="seat-1").recall("nothing has been written yet")
        assert rec["abstained"] is True
        assert rec["reference_context"] == []
    finally:
        stop()


def test_prune_unknown_id_is_noop_not_keyerror():
    url, _server, _clock, stop = _live()
    try:
        res = McpHttpClient(url, agent_id="orch").prune(999_999, approved_by="human")
        assert res["status"] == "noop" and res["episode_id"] == 999_999
    finally:
        stop()


def test_supersede_unknown_pair_is_noop_not_keyerror():
    url, _server, _clock, stop = _live()
    try:
        res = McpHttpClient(url, agent_id="orch").supersede(111, 222, approved_by="human")
        assert res["status"] == "noop"
    finally:
        stop()


def test_capture_disabled_returns_disabled_without_keyerror():
    # autonomy OFF ⇒ hive_capture returns {status:"disabled"} and OMITS "id".
    url, _server, _clock, stop = _live(autonomy=AutonomyConfig(enabled=False))
    try:
        res = McpHttpClient(url, agent_id="seat-1").capture("a durable insight")
        assert res["status"] == "disabled" and "id" not in res
    finally:
        stop()


def test_flag_disabled_without_conflict_service_without_keyerror():
    # a server built with no ConflictConfig has no flag_service ⇒ {status:"disabled"}.
    url, _server, _clock, stop = _live()
    try:
        res = McpHttpClient(url, agent_id="seat-1").flag(1, 2, kind="conflict")
        assert res["status"] == "disabled"
    finally:
        stop()


def test_agi_override_refused_when_agi_mode_off():
    # §6 O7 firewall + fail-closed: AGI_OVERRIDE under AGI_MODE OFF ⇒ {status:"refused",
    # agi_mode:False}, OMITTING "id"; nothing is written or retired. Parsed without KeyError.
    url, _server, _clock, stop = _live(agi_mode=False)
    try:
        c = McpHttpClient(url, agent_id="seat-1")
        w = c.write("must not persist", approved_by="AGI_OVERRIDE")
        assert w["status"] == "refused" and w["agi_mode"] is False and "id" not in w
        eid = c.write("real fact one two", approved_by="human")["id"]
        p = c.prune(eid, approved_by="AGI_OVERRIDE")
        assert p["status"] == "refused" and p["agi_mode"] is False
        assert any(h["episode_id"] == eid                       # the prune was refused — still served
                   for h in c.recall("real fact one two")["reference_context"])
    finally:
        stop()


def test_agi_override_honored_when_agi_mode_on():
    # the secondary AGI-autonomous arm: with AGI_MODE ON the sentinel passes to the handler.
    url, _server, _clock, stop = _live(agi_mode=True)
    try:
        c = McpHttpClient(url, agent_id="seat-1")
        eid = c.write("retire me later please", approved_by="human")["id"]
        p = c.prune(eid, approved_by="AGI_OVERRIDE")
        assert p["status"] == "pruned" and p["episode_id"] == eid
    finally:
        stop()


def test_outcome_records_known_ids_and_skips_unknown():
    # hive_outcome is O7-safe evidence: known ids recorded, unknown ids skipped (never forged).
    url, _server, _clock, stop = _live()
    try:
        c = McpHttpClient(url, agent_id="seat-1")
        eid = c.write("fact that will hurt", approved_by="human")["id"]
        res = c.outcome(hurt=[eid, 888_888])
        assert res["status"] == "recorded" and res["hurt"] == [eid] and res["helped"] == []
    finally:
        stop()


def test_call_raises_mcpclienterror_on_jsonrpc_error_envelope():
    # THE defensive-parse pin (BUG-003): an unknown tool name yields a JSON-RPC error envelope
    # (result is None). The client must raise a clean McpClientError, NEVER a KeyError/TypeError.
    # Mutation: revert the defensive `.get("content")` framing in _call to an unconditional
    # index and this reds (KeyError/TypeError leaks instead of McpClientError).
    url, _server, _clock, stop = _live()
    try:
        with pytest.raises(McpClientError):
            McpHttpClient(url, agent_id="seat-1")._call("hive_does_not_exist", {})
    finally:
        stop()


def test_call_raises_mcpclienterror_on_malformed_tool_result():
    # the second defensive-parse guard: a 200 reply whose result lacks the content framing must
    # raise McpClientError, never a KeyError/IndexError. Mutation: drop the try/except around the
    # `result["content"][0]["text"]` index → this reds (KeyError leaks).
    url, stop = _serve(_MalformedResultSpy())
    try:
        with pytest.raises(McpClientError):
            McpHttpClient(url, agent_id="seat-1").health()
    finally:
        stop()


# ── build_claude_argv: the BUG-007 invariant ────────────────────────────────────────
def test_build_claude_argv_carries_strict_mcp_config_and_hive_only_config():
    # BUG-007: a per-call `claude -p` agent must load ONLY the warm Hivemind daemon. Without
    # --strict-mcp-config every call boots the operator's MCP servers (≈500 MB node procs) that
    # are NOT torn down per call, leaking GBs until OOM. Mutation: drop --strict-mcp-config → red.
    argv = build_claude_argv("do the work", mcp_config_path="/tmp/hive.mcp.json")
    assert argv[:3] == ["claude", "-p", "do the work"]
    assert "--strict-mcp-config" in argv
    i = argv.index("--mcp-config")
    assert argv[i + 1] == "/tmp/hive.mcp.json"               # the Hivemind-only config IS loaded
    assert argv[argv.index("--output-format") + 1] == "json"


def test_build_claude_argv_threads_optional_system_and_model():
    argv = build_claude_argv("q", mcp_config_path="/c.json", system="sys", model="claude-x")
    assert argv[argv.index("--append-system-prompt") + 1] == "sys"
    assert argv[argv.index("--model") + 1] == "claude-x"


def test_build_claude_argv_grants_allowed_tools_for_headless_mcp():
    # BUG-011: headless claude -p REFUSES an un-allowed MCP tool (the agent silently cannot
    # recall/capture/flag), so the autonomous turn must be granted exactly the Hivemind tools it
    # emits, via --allowedTools. Mutation: drop the allowed_tools handling → this reds.
    argv = build_claude_argv("q", mcp_config_path="/c.json",
                             allowed_tools=("mcp__hive__hive_recall", "mcp__hive__hive_outcome"))
    assert argv[argv.index("--allowedTools") + 1] == "mcp__hive__hive_recall mcp__hive__hive_outcome"


def test_build_claude_argv_omits_allowed_tools_when_none():
    # backward-compatible: no tools ⇒ no flag (the BUG-007 argv tests pass none).
    assert "--allowedTools" not in build_claude_argv("q", mcp_config_path="/c.json")


# ── DaemonHandle: lifecycle + the AGI-mode env switch ───────────────────────────────
class _RecRunner:
    """Records ``(argv, env)`` for each lifecycle shell-out; returns a fixed return code."""
    def __init__(self, rc: int = 0) -> None:
        self.calls: list = []
        self._rc = rc

    def run(self, argv, env) -> RunResult:
        self.calls.append((list(argv), dict(env)))
        return RunResult(self._rc)


def test_up_sets_agi_mode_env_when_on():
    url, _server, _clock, stop = _live()
    try:
        rec = _RecRunner()
        dh = DaemonHandle(url=url, hive_cmd=("hive",), runner=rec.run,
                          env={"PATH": "/x"}, health_timeout_s=0.0, sleep=lambda _s: None)
        dh.up(agi_mode=True)
        (argv, env), = rec.calls
        assert argv == ["hive", "up"]
        assert env.get("HIVE_AGI__MODE") == "true"
    finally:
        stop()


def test_up_omits_agi_mode_env_when_off():
    # byte-inert default-OFF: the OFF/headline arms must NOT set the knob. Mutation: set it
    # unconditionally → this reds.
    url, _server, _clock, stop = _live()
    try:
        rec = _RecRunner()
        dh = DaemonHandle(url=url, hive_cmd=("hive",), runner=rec.run,
                          env={"PATH": "/x"}, health_timeout_s=0.0, sleep=lambda _s: None)
        dh.up(agi_mode=False)
        (_argv, env), = rec.calls
        assert "HIVE_AGI__MODE" not in env
    finally:
        stop()


def test_up_confirms_health_after_start():
    url, _server, _clock, stop = _live()
    try:
        rec = _RecRunner(rc=0)
        dh = DaemonHandle(url=url, runner=rec.run, env={}, health_timeout_s=0.0,
                          sleep=lambda _s: None)
        dh.up(agi_mode=False)                                # no raise ⇒ health confirmed ok
    finally:
        stop()


def test_up_raises_daemonerror_when_endpoint_never_healthy():
    # rc=0 but the MCP endpoint never answers ⇒ DaemonError after the bounded wait.
    rec = _RecRunner(rc=0)
    dh = DaemonHandle(url="http://127.0.0.1:1/mcp", runner=rec.run, env={},
                      health_timeout_s=0.0, sleep=lambda _s: None)
    with pytest.raises(DaemonError):
        dh.up(agi_mode=False)


def test_up_raises_daemonerror_on_lifecycle_failure():
    rec = _RecRunner(rc=1)
    dh = DaemonHandle(url="http://127.0.0.1:1/mcp", runner=rec.run, env={},
                      health_timeout_s=0.0, sleep=lambda _s: None)
    with pytest.raises(DaemonError):
        dh.up(agi_mode=False)


def test_down_invokes_the_down_verb():
    rec = _RecRunner(rc=0)
    dh = DaemonHandle(url="http://127.0.0.1:1/mcp", hive_cmd=("hive",), runner=rec.run, env={})
    dh.down()
    (argv, _env), = rec.calls
    assert argv == ["hive", "down"]
