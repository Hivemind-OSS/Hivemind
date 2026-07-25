"""M06 protocol surface (v3): exactly-8 tools, the dropped verbs absent (the
AgentCortex-era verbs, the removed approval queue, the onboarding handshake, AND no
hive_evidence), no approver/AGI/beacon apparatus anywhere, JSON-RPC error semantics,
the schema-enforcement belt (malformed call never reaches a port — ★, now including
array-ITEM object checking for the anchors grammar), and loop-survival on a raising
handler (stack never returned to the agent)."""

from __future__ import annotations

import io
import json

from hive.app.contract import BAD_VS_STALE, WRITE_VS_CAPTURE
from hive.app.mcp_server import (
    HiveMCPServer,
    MCPRequest,
    ServerIdentity,
    run_stdio,
)
from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.admission import WriteResult
from hive.domain.kinds import KIND_NAMES
from hive.domain.secret_scan import scan as _scan
from tests.fakes._fakes import FakeIndex
from tests.mcp._helpers import (
    build_real_server,
    content,
    is_error,
    tool_call,
    write_text,
)

_TOOLS = {
    "hive_write",
    "hive_capture",
    "hive_recall",
    "hive_supersede",
    "hive_prune",
    "hive_outcome",
    "hive_flag",
    "hive_health",
}
_DROPPED = {
    "hive_init",
    "hive_fetch",
    "hive_pending",
    "hive_approve",
    "hive_reject",
    "hive_evidence",
    "hive_consolidate",
    "hive_schemas",
    "hive_recall_cold",
    "hive_restore_cold",
    "hive_reconsolidate",
    "hive_audit",
}


def test_tool_list_is_the_expected_verb_set():
    server, _ = build_real_server()
    resp = server.handle(MCPRequest(1, "tools/list", {}))
    names = {t["name"] for t in resp.result["tools"]}
    assert names == _TOOLS
    assert len(resp.result["tools"]) == len(_TOOLS)
    assert names.isdisjoint(_DROPPED)
    # the static table and the live reply are the same source
    assert {t["name"] for t in TOOL_DEFINITIONS} == _TOOLS


def test_write_description_directs_recall_before_write():
    """The call-adjacent reminder: a write serves immediately, so recall the topic first
    to skip a duplicate and correct in place (replaces=) instead of adding a rival."""
    desc = next(
        t["description"] for t in TOOL_DEFINITIONS if t["name"] == "hive_write"
    ).lower()
    assert "recall the topic" in desc  # recall-before-write at the call site
    assert "replaces" in desc  # correct in place rather than duplicate


def test_capture_description_requires_verifiable_evidence():
    desc = next(
        t["description"] for t in TOOL_DEFINITIONS if t["name"] == "hive_capture"
    ).lower()
    assert "verifiable evidence" in desc


def test_write_and_capture_descriptions_carry_the_decision_rule():
    for name in ("hive_write", "hive_capture"):
        desc = next(t["description"] for t in TOOL_DEFINITIONS if t["name"] == name)
        assert WRITE_VS_CAPTURE in desc, (
            f"{name} missing the write-vs-capture decision rule"
        )


def test_retirement_descriptions_diagnose_bad_vs_stale_and_the_gate():
    """prune handles BAD; supersede handles STALE — and BOTH call sites state the
    machine gate (unqualified = benign noop, never an error; no approver exists)."""
    prune = next(
        t["description"] for t in TOOL_DEFINITIONS if t["name"] == "hive_prune"
    )
    supersede = next(
        t["description"] for t in TOOL_DEFINITIONS if t["name"] == "hive_supersede"
    )
    assert "incorrect" in prune.lower() or "misleading" in prune.lower()
    assert "successor" in supersede.lower()
    assert BAD_VS_STALE in prune
    for desc in (prune, supersede):
        assert "MACHINE-GATED" in desc
        assert "noop" in desc.lower()


def test_no_approver_or_agi_survives_in_any_schema_or_description():
    dumped = json.dumps(TOOL_DEFINITIONS)
    assert "approved_by" not in dumped
    assert "AGI" not in dumped
    assert "proposed_by" not in dumped  # identity stays transport-resolved
    assert "include_onboarding" not in dumped  # the install channel is deleted


def test_initialize_carries_server_instructions():
    """The foolproof delivery channel: the MCP ``initialize`` result carries the v3
    usage contract in ``instructions`` — fresh at every connect, no install step."""
    server, _ = build_real_server()
    init = server.handle(MCPRequest(1, "initialize", {}))
    assert init.result["serverInfo"]["name"] == "hive"
    assert "protocolVersion" in init.result
    instr = init.result.get("instructions", "")
    assert isinstance(instr, str) and instr  # present + non-empty
    assert "hive_recall" in instr and "hive_capture" in instr and "hive_write" in instr
    for dead in ("approved_by", "AGI", "HIVEMIND-RULES", "contract_version"):
        assert dead not in instr
    pong = server.handle(MCPRequest(2, "ping", {}))
    assert pong.result == {} and pong.error is None


def test_tool_results_carry_no_contract_version_beacon():
    """v3: the per-result contract-version beacon is DELETED — no dict envelope may
    carry it (the contract reaches agents via initialize instructions instead)."""
    server, _ = build_real_server()
    write_text(server, "a beaconless fact")
    for name, args in (
        ("hive_health", {}),
        ("hive_recall", {"query": "a beaconless fact"}),
        ("hive_recall", {"query": "nothing matches this query at all"}),
        ("hive_capture", {"text": "an insight"}),
        ("hive_outcome", {}),
    ):
        env = content(tool_call(server, name, args))
        assert "contract_version" not in env, f"{name} still carries the beacon"


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


class _EmptyRegistryStore:
    """Just enough store for the pre-port belt tests: an empty repo registry (the
    handler reads it lazily, only when scope args are present)."""

    def repo_registry(self):
        return []

    def get_episode(self, _eid):
        return None


def _server_with(admission):
    return HiveMCPServer(
        admission=admission,
        recall=None,
        store=_EmptyRegistryStore(),
        embedder=None,
        identity=ServerIdentity("t", "a"),
        now=lambda: 0,
    )


def test_malformed_call_rejected_before_port_touched():
    adm = _CountingAdmission()
    server = _server_with(adm)
    # hive_write with NO `text` (required) ⇒ isError, admission.write never called.
    r1 = tool_call(server, "hive_write", {})
    assert is_error(r1)
    assert adm.write_calls == 0


def test_stale_approved_by_arg_is_ignored_extra():
    # CT-12: an extra approved_by is IGNORED (permissive belt) — the write proceeds.
    adm = _CountingAdmission()
    server = _server_with(adm)
    r = tool_call(server, "hive_write", {"text": "x", "approved_by": "alice"})
    assert not is_error(r)
    assert adm.write_calls == 1


def test_bad_type_rejected_by_schema():
    adm = _CountingAdmission()
    server = _server_with(adm)
    r = tool_call(server, "hive_write", {"text": 123})  # text must be string
    assert is_error(r)
    assert adm.write_calls == 0


def test_non_enum_polarity_rejected_by_schema_belt():
    adm = _CountingAdmission()
    server = _server_with(adm)
    r = tool_call(server, "hive_write", {"text": "x", "polarity": "maybe"})
    assert is_error(r)
    assert adm.write_calls == 0
    rc = tool_call(server, "hive_capture", {"text": "x", "polarity": "sideways"})
    assert is_error(rc)


def test_capture_and_write_advertise_kind_enum_and_anchors_grammar():
    """SOFT structure: kind is an enum over the registry vocabulary; anchors/repos are
    the v3 structured scope carriers — all OPTIONAL (absent from required[]),
    server-defaulted when omitted. The advertised item schema IS the enforced one."""
    by_name = {t["name"]: t for t in TOOL_DEFINITIONS}
    for name in ("hive_write", "hive_capture"):
        schema = by_name[name]["inputSchema"]
        props = schema["properties"]
        assert props["kind"]["enum"] == sorted(KIND_NAMES)
        assert props["anchors"]["type"] == "array"
        assert props["anchors"]["items"]["required"] == ["repo", "anchor"]
        assert props["repos"]["type"] == "array"
        assert "kind" not in schema["required"] and "anchors" not in schema["required"]
        assert "anchor" not in props  # the v2 free-text anchor is gone


def test_non_enum_kind_rejected_by_schema_belt():
    adm = _CountingAdmission()
    server = _server_with(adm)
    r = tool_call(server, "hive_write", {"text": "x", "kind": "rumor"})
    assert is_error(r)
    assert adm.write_calls == 0
    rc = tool_call(server, "hive_capture", {"text": "x", "kind": "rumor"})
    assert is_error(rc)


def test_anchors_array_items_are_object_checked_by_the_belt():
    """The v3 belt extension: each anchors[] item is validated against the SAME
    advertised item schema — a non-object item, a missing required key, a mistyped
    value, or an unknown extra key is rejected BEFORE any port is touched."""
    adm = _CountingAdmission()
    server = _server_with(adm)
    bad_shapes = (
        ["not-an-object"],  # item type
        [{"repo": "alpha"}],  # missing required anchor
        [{"repo": 7, "anchor": "x.py::f"}],  # mistyped repo
        [{"repo": "alpha", "anchor": "x.py::f", "extra": "k"}],  # unknown key
    )
    for anchors in bad_shapes:
        r = tool_call(server, "hive_write", {"text": "x", "anchors": anchors})
        assert is_error(r), f"belt must reject anchors={anchors!r}"
    assert adm.write_calls == 0
    # a well-shaped item passes the BELT (registry membership is the grammar
    # gate's job downstream, a clean refusal — not a schema reject)
    r = tool_call(
        server,
        "hive_write",
        {"text": "x", "anchors": [{"repo": "ghost", "anchor": "x.py::f"}]},
    )
    assert not is_error(r)
    env = content(r)
    assert env["status"] == "refused"  # unregistered name refuses clean
    assert adm.write_calls == 0  # nothing reached admission


def test_repos_array_items_are_type_checked_by_the_belt():
    adm = _CountingAdmission()
    server = _server_with(adm)
    r = tool_call(server, "hive_write", {"text": "x", "repos": [7]})
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
    lines = (
        "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "hive_recall", "arguments": {"query": "q"}},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}),
            ]
        )
        + "\n"
    )
    run_stdio(server, io.StringIO(lines), out)
    raw = out.getvalue().splitlines()
    assert len(raw) == 2  # loop survived to line 2
    first = json.loads(raw[0])
    assert first["result"]["isError"] is True
    assert "Traceback" not in raw[0]  # stack not leaked to stdout
    assert json.loads(raw[1])["result"] == {}  # ping answered after the raise


def test_parse_error_replies_32700_and_continues():
    server, _ = build_real_server()
    out = io.StringIO()
    run_stdio(
        server,
        io.StringIO(
            "{not json}\n"
            + json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}})
            + "\n"
        ),
        out,
    )
    raw = out.getvalue().splitlines()
    assert json.loads(raw[0])["error"]["code"] == -32700
    assert json.loads(raw[1])["id"] == 7  # loop continued


def test_non_dict_params_does_not_crash_loop():
    """A request whose `params` is an array (not an object) must not crash the loop
    (AUDIT wf_1943a559: AttributeError on req.params.get escaped to run_stdio)."""
    server, _ = build_real_server()
    out = io.StringIO()
    run_stdio(
        server,
        io.StringIO(
            "\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": [1, 2, 3],
                        }
                    ),
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
                    ),
                ]
            )
            + "\n"
        ),
        out,
    )
    raw = out.getvalue().splitlines()
    assert len(raw) == 2  # loop survived the bad params
    assert json.loads(raw[1])["result"] == {}  # ping answered after


def test_scalar_payload_does_not_crash_loop():
    """A bare scalar JSON line (`5`) must not crash the `"id" not in payload` check."""
    server, _ = build_real_server()
    out = io.StringIO()
    run_stdio(
        server,
        io.StringIO(
            "5\n"
            + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}})
            + "\n"
        ),
        out,
    )
    raw = out.getvalue().splitlines()
    assert (
        json.loads(raw[0])["error"]["code"] == -32600
    )  # invalid request (not an object)
    assert json.loads(raw[1])["id"] == 9  # loop continued


def test_handle_internal_error_is_caught_by_loop():
    """Any unforeseen handle() raise becomes a JSON-RPC -32603, never a loop crash."""
    server, _ = build_real_server()

    def _boom(_req):
        raise RuntimeError("unexpected")

    server.handle = _boom
    out = io.StringIO()
    run_stdio(
        server,
        io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}})
            + "\n"
        ),
        out,
    )
    err = json.loads(out.getvalue().splitlines()[0])["error"]
    assert err["code"] == -32603
    assert "Traceback" not in out.getvalue()  # stack not leaked


def test_the_advertised_drift_enum_is_projected_from_its_one_owner():
    """J5. The advertised vocabulary and the emittable vocabulary are the SAME
    object, not two lists that happen to agree. test_verdict_writer_coverage
    enforces the other direction (every member has a production writer), so I3
    now holds both ways: a member added to WIRE_VERDICTS changes what agents are
    told in the same edit, and one added without a writer reds."""
    from hive.app.drift import WIRE_VERDICTS

    recall = next(t for t in TOOL_DEFINITIONS if t["name"] == "hive_recall")
    assert " | ".join(WIRE_VERDICTS) in recall["description"], (
        "the served drift enum must be the joined WIRE_VERDICTS, verbatim"
    )
    for verdict in WIRE_VERDICTS:
        assert verdict in recall["description"], verdict


def test_repos_property_names_the_branch_form():
    """J7 / A-13. The `repos` schema property must state the SAME grammar the
    write/capture directive composed into the same served string directs — a
    schema that says "registered repo names" while the prose directs
    `repos=['name@branch']` is one fact with two spellings."""
    for name in ("hive_write", "hive_capture"):
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == name)
        described = tool["inputSchema"]["properties"]["repos"]["description"]
        assert "name@branch" in described, (
            f"{name}'s repos property must name the branch form: {described}"
        )


def test_the_boundary_holds_no_raw_meta_reads():
    """J8. Tip and tracked-ref resolution have ONE owner (hive.app.drift.tip_for);
    the boundary must not reach past it into the meta table with raw SQL. Asserted
    over the SOURCE (the test_sync_keys idiom) because a runtime check cannot see
    a read that never fires."""
    import inspect

    import hive.app.mcp_server as mcp_module

    source = inspect.getsource(mcp_module)
    assert "FROM meta" not in source, (
        "the MCP boundary must resolve tips through drift.tip_for, never by "
        "reading the meta table directly"
    )
