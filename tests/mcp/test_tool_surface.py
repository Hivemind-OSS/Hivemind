"""M06 protocol surface: exactly-4 tools, the dropped verbs absent (the AgentCortex-7,
the removed approval queue hive_pending/approve/reject, the onboarding handshake hive_init,
AND no hive_evidence — client-fed evidence does not exist in this build), JSON-RPC error
semantics, the schema-enforcement belt (malformed call never reaches a port — ★), and
loop-survival on a raising handler (stack never returned to the agent)."""
from __future__ import annotations

import io
import json

from hive.app.mcp_server import (
    HiveMCPServer, MCPRequest, ServerIdentity, run_stdio,
)
from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.admission import WriteResult
from hive.domain.kinds import KIND_NAMES
from hive.domain.secret_scan import scan as _scan
from tests.fakes._fakes import FakeIndex
from tests.mcp._helpers import build_real_server, content, is_error, tool_call

_FOUR = {"hive_write", "hive_capture", "hive_recall", "hive_health"}
_DROPPED = {"hive_init", "hive_fetch", "hive_pending", "hive_approve", "hive_reject",
            "hive_evidence", "hive_consolidate", "hive_schemas", "hive_recall_cold",
            "hive_restore_cold", "hive_reconsolidate", "hive_audit", "hive_outcome"}


def test_tool_list_is_exactly_4():
    server, _ = build_real_server()
    resp = server.handle(MCPRequest(1, "tools/list", {}))
    names = {t["name"] for t in resp.result["tools"]}
    assert names == _FOUR
    assert len(resp.result["tools"]) == 4
    assert names.isdisjoint(_DROPPED)
    # the static table and the live reply are the same source
    assert {t["name"] for t in TOOL_DEFINITIONS} == _FOUR


def test_write_description_generalizes_the_approver():
    """The approved-write path must read for an orchestrated fleet, not only a human approver —
    so an autonomous sub-agent under an orchestrator knows the orchestrator's sign-off counts."""
    desc = next(t["description"] for t in TOOL_DEFINITIONS if t["name"] == "hive_write")
    assert "approved_by" in desc
    assert "orchestrat" in desc.lower()                  # approver isn't only a human


def test_health_description_carries_served_reference_not_a_self_install_block():
    """Onboarding is served-only: the hive_health DESCRIPTION carries the identity/auth +
    optional-hooks reference, NOT a self-installed rules block — no marker block and no 'write
    this block' instruction. The full contract rides the initialize instructions instead."""
    server, _ = build_real_server()
    resp = server.handle(MCPRequest(1, "tools/list", {}))
    health = next(t for t in resp.result["tools"] if t["name"] == "hive_health")
    desc = health["description"]
    assert "<!-- hive-init:start -->" not in desc         # no self-installed marker block
    assert "write this block" not in desc.lower()          # no self-install instruction
    assert "Mcp-Session-Id" in desc                        # the served identity/auth reference
    server, _ = build_real_server()
    init = server.handle(MCPRequest(1, "initialize", {}))
    assert init.result["serverInfo"]["name"] == "hive"
    assert "protocolVersion" in init.result
    pong = server.handle(MCPRequest(2, "ping", {}))
    assert pong.result == {} and pong.error is None


def test_initialize_carries_server_instructions():
    """The foolproof delivery channel: the MCP ``initialize`` result carries the usage
    contract in ``instructions`` (the spec field every client surfaces at connect), so a
    connecting agent learns how to use the memory WITHOUT calling any tool. Dropping the
    field strands every agent that doesn't read tool descriptions."""
    server, _ = build_real_server()
    instr = server.handle(MCPRequest(1, "initialize", {})).result.get("instructions", "")
    assert isinstance(instr, str) and instr                      # present + non-empty
    # the three load-bearing verbs + the human-approval gate reach the agent up front
    assert "hive_recall" in instr and "hive_capture" in instr and "hive_write" in instr
    assert "approved_by" in instr


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
        identity=ServerIdentity("t", "a"), now=lambda: 0)


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


def test_bad_type_rejected_by_schema():
    adm = _CountingAdmission()
    server = _server_with(adm)
    r = tool_call(server, "hive_write", {"text": 123})   # text must be string
    assert is_error(r)
    assert adm.write_calls == 0


def test_non_enum_polarity_rejected_by_schema_belt():
    """The wire-layer enforcement of the polarity enum: a non-(do|dont|neutral) value
    on hive_write/hive_capture is schema-rejected BEFORE the handler — the port is
    never touched (mirrors the bad-type belt)."""
    adm = _CountingAdmission()
    server = _server_with(adm)
    r = tool_call(server, "hive_write",
                  {"text": "x", "approved_by": "u", "polarity": "maybe"})
    assert is_error(r)
    assert adm.write_calls == 0
    # hive_capture carries the same enum; a bad value is rejected there too.
    rc = tool_call(server, "hive_capture", {"text": "x", "polarity": "sideways"})
    assert is_error(rc)


def test_capture_and_write_advertise_kind_enum_and_anchor():
    """SOFT structure: kind is an enum over the registry vocabulary, anchor a free
    string — both OPTIONAL (absent from required[]), server-defaulted when omitted.
    The advertised enum is the registry, so it can't drift from what the store enforces."""
    by_name = {t["name"]: t for t in TOOL_DEFINITIONS}
    for name in ("hive_write", "hive_capture"):
        schema = by_name[name]["inputSchema"]
        props = schema["properties"]
        assert props["kind"]["enum"] == sorted(KIND_NAMES)
        assert props["anchor"]["type"] == "string"
        assert "kind" not in schema["required"] and "anchor" not in schema["required"]


def test_non_enum_kind_rejected_by_schema_belt():
    """The kind enum is wire-enforced like polarity: a value outside the registry on
    hive_write/hive_capture is schema-rejected BEFORE the handler — the port is never
    touched (SOFT means optional, but checked when present)."""
    adm = _CountingAdmission()
    server = _server_with(adm)
    r = tool_call(server, "hive_write",
                  {"text": "x", "approved_by": "u", "kind": "rumor"})
    assert is_error(r)
    assert adm.write_calls == 0
    rc = tool_call(server, "hive_capture", {"text": "x", "kind": "rumor"})
    assert is_error(rc)


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
