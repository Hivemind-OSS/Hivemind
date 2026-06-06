"""M06 protocol surface: exactly-5 tools, the dropped verbs absent (the AgentCortex-7
AND the removed approval queue hive_pending/approve/reject), JSON-RPC error semantics,
the schema-enforcement belt (malformed call never reaches a port — ★), and loop-survival
on a raising handler (stack never returned to the agent)."""
from __future__ import annotations

import io
import json

from hive.app.mcp_server import (
    HiveMCPServer, MCPRequest, ServerIdentity, run_stdio,
)
from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.admission import WriteResult
from hive.domain.secret_scan import scan as _scan
from tests.fakes._fakes import FakeIndex
from tests.mcp._helpers import build_real_server, content, is_error, tool_call

_FIVE = {"hive_write", "hive_recall", "hive_fetch", "hive_init", "hive_health"}
_DROPPED = {"hive_pending", "hive_approve", "hive_reject",
            "hive_consolidate", "hive_schemas", "hive_recall_cold",
            "hive_restore_cold", "hive_reconsolidate", "hive_audit", "hive_outcome"}


def test_tool_list_is_exactly_5():
    server, _ = build_real_server()
    resp = server.handle(MCPRequest(1, "tools/list", {}))
    names = {t["name"] for t in resp.result["tools"]}
    assert names == _FIVE
    assert len(resp.result["tools"]) == 5
    assert names.isdisjoint(_DROPPED)
    # the static table and the live reply are the same source
    assert {t["name"] for t in TOOL_DEFINITIONS} == _FIVE


def test_initialize_and_ping():
    server, _ = build_real_server()
    init = server.handle(MCPRequest(1, "initialize", {}))
    assert init.result["serverInfo"]["name"] == "hive"
    assert "protocolVersion" in init.result
    pong = server.handle(MCPRequest(2, "ping", {}))
    assert pong.result == {} and pong.error is None


def test_unknown_method_is_jsonrpc_error():
    server, _ = build_real_server()
    resp = server.handle(MCPRequest(1, "frobnicate", {}))
    assert resp.error is not None and resp.error["code"] == -32601


def test_unknown_tool_is_jsonrpc_error():
    server, _ = build_real_server()
    resp = tool_call(server, "hive_nope", {})
    assert resp.error is not None and resp.error["code"] == -32602


# ── ★ schema belt: a malformed call returns isError WITHOUT touching a port ──────
class _CountingAdmission:
    def __init__(self):
        self.write_calls = 0

    def write(self, *a, **k):
        self.write_calls += 1
        return WriteResult("approved", 1, "deadbeef", _scan("ok"))


def _server_with(admission):
    return HiveMCPServer(
        admission=admission, recall=None, store=None, embedder=None,
        install_planner=None, identity=ServerIdentity("t", "a"), now=lambda: 0)


def test_malformed_call_rejected_before_port_touched():
    adm = _CountingAdmission()
    server = _server_with(adm)
    # hive_write with NO `text` (required) ⇒ isError, admission.write never called.
    r1 = tool_call(server, "hive_write", {"approved_by": "u"})
    assert is_error(r1)
    assert adm.write_calls == 0
    # hive_write with NO `approved_by` (required) ⇒ isError, admission.write never called.
    r2 = tool_call(server, "hive_write", {"text": "an insight"})
    assert is_error(r2)
    assert adm.write_calls == 0


def test_bad_enum_rejected_by_schema():
    server, _ = build_real_server()
    # workflow not in {bugfix,dep-upgrade,general}
    r = tool_call(server, "hive_recall", {"query": "x", "workflow": "emacs"})
    assert is_error(r)


def test_bad_type_rejected_by_schema():
    adm = _CountingAdmission()
    server = _server_with(adm)
    r = tool_call(server, "hive_write", {"text": 123})   # text must be string
    assert is_error(r)
    assert adm.write_calls == 0


# ── loop survives a raising handler; the stack is never returned to the agent ────
class _RaisingRecall:
    index = FakeIndex()

    def recall(self, *a, **k):
        raise RuntimeError("kaboom-with-secret-AKIAEXAMPLE")


def test_tool_exception_does_not_crash_loop():
    server, _ = build_real_server()
    server.recall = _RaisingRecall()
    out = io.StringIO()
    lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "hive_recall", "arguments": {"query": "q"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}),
    ]) + "\n"
    run_stdio(server, io.StringIO(lines), out)
    raw = out.getvalue().splitlines()
    assert len(raw) == 2                                   # loop survived to line 2
    first = json.loads(raw[0])
    assert first["result"]["isError"] is True
    assert "Traceback" not in raw[0]                       # stack not leaked to stdout
    assert json.loads(raw[1])["result"] == {}              # ping answered after the raise


def test_parse_error_replies_32700_and_continues():
    server, _ = build_real_server()
    out = io.StringIO()
    run_stdio(server, io.StringIO("{not json}\n" + json.dumps(
        {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}) + "\n"), out)
    raw = out.getvalue().splitlines()
    assert json.loads(raw[0])["error"]["code"] == -32700
    assert json.loads(raw[1])["id"] == 7                   # loop continued


def test_non_dict_params_does_not_crash_loop():
    """A request whose `params` is an array (not an object) must not crash the loop
    (AUDIT wf_1943a559: AttributeError on req.params.get escaped to run_stdio)."""
    server, _ = build_real_server()
    out = io.StringIO()
    run_stdio(server, io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1, 2, 3]}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}),
    ]) + "\n"), out)
    raw = out.getvalue().splitlines()
    assert len(raw) == 2                                   # loop survived the bad params
    assert json.loads(raw[1])["result"] == {}              # ping answered after


def test_scalar_payload_does_not_crash_loop():
    """A bare scalar JSON line (`5`) must not crash the `"id" not in payload` check."""
    server, _ = build_real_server()
    out = io.StringIO()
    run_stdio(server, io.StringIO("5\n" + json.dumps(
        {"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}}) + "\n"), out)
    raw = out.getvalue().splitlines()
    assert json.loads(raw[0])["error"]["code"] == -32600   # invalid request (not an object)
    assert json.loads(raw[1])["id"] == 9                   # loop continued


def test_handle_internal_error_is_caught_by_loop():
    """Any unforeseen handle() raise becomes a JSON-RPC -32603, never a loop crash."""
    server, _ = build_real_server()

    def _boom(_req):
        raise RuntimeError("unexpected")
    server.handle = _boom
    out = io.StringIO()
    run_stdio(server, io.StringIO(json.dumps(
        {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}) + "\n"), out)
    err = json.loads(out.getvalue().splitlines()[0])["error"]
    assert err["code"] == -32603
    assert "Traceback" not in out.getvalue()               # stack not leaked
