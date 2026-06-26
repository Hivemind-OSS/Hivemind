"""C7 — hive_flag: the advisory Layer-2 verb. Records an UNTRUSTED conflict/supersedes marker
surfaced in hive_health(include_conflicts); it NEVER retires anything (resolution stays human
via hive_supersede). Gated by conflict.enabled; validation rejects before any write; the
resolution is secret-scanned."""
from __future__ import annotations

from hive.app.config import ConflictConfig
from hive.app.onboard_ref import CONTRACT_VERSION, RESULT_ONBOARDING_DIRECTIVE
from tests.mcp._helpers import build_real_server, content, is_error, tool_call, write_text

_AKIA = "AKIAIOSFODNN7EXAMPLE"


def _on():
    return build_real_server(conflict=ConflictConfig(enabled=True))


def _two(server):
    a = write_text(server, "deploy uses make deploy")["id"]
    b = write_text(server, "deploy uses ship.sh now")["id"]
    return a, b


def test_flag_records_advisory_and_surfaces_in_worklist():
    server, _ = _on()
    a, b = _two(server)
    out = content(tool_call(server, "hive_flag",
                            {"a": a, "b": b, "kind": "conflict"}))
    assert out["status"] == "flagged" and out["recorded"] is True
    snap = content(tool_call(server, "hive_health", {"include_conflicts": True}))
    adv = [c for c in snap["conflicts"] if c["source"] == "advisory"]
    assert any({c["a_id"], c["b_id"]} == {a, b} for c in adv)


def test_flag_idempotent_recorded_false_on_reflag():
    server, _ = _on()
    a, b = _two(server)
    tool_call(server, "hive_flag", {"a": a, "b": b, "kind": "conflict"})
    again = content(tool_call(server, "hive_flag", {"a": b, "b": a, "kind": "conflict"}))
    assert again["status"] == "flagged" and again["recorded"] is False


def test_flag_unknown_episode_rejected_no_write():
    server, _ = _on()
    a, _b = _two(server)
    out = content(tool_call(server, "hive_flag", {"a": a, "b": 9999, "kind": "conflict"}))
    assert out["status"] == "rejected"
    snap = content(tool_call(server, "hive_health", {"include_conflicts": True}))
    assert [c for c in snap.get("conflicts", []) if c["source"] == "advisory"] == []


def test_flag_supersedes_requires_winner():
    server, _ = _on()
    a, b = _two(server)
    out = content(tool_call(server, "hive_flag",
                            {"a": a, "b": b, "kind": "supersedes"}))   # no winner
    assert out["status"] == "rejected"


def test_flag_secret_resolution_refused():
    server, _ = _on()
    a, b = _two(server)
    out = content(tool_call(server, "hive_flag",
                            {"a": a, "b": b, "kind": "conflict",
                             "resolution": f"key {_AKIA}"}))
    assert out["status"] == "refused"
    assert "aws_akia" in out["scan"]["rules"]


def test_flag_disabled_returns_disabled_when_feature_off():
    server, _ = build_real_server()           # no conflict config ⇒ feature off
    a = write_text(server, "alpha")["id"]
    b = write_text(server, "beta")["id"]
    out = content(tool_call(server, "hive_flag", {"a": a, "b": b, "kind": "conflict"}))
    assert out == {"status": "disabled", "contract_version": CONTRACT_VERSION,
                   "onboarding": RESULT_ONBOARDING_DIRECTIVE}


def test_flag_never_retires_either_episode():
    # the load-bearing guarantee at the tool boundary: a supersedes-flag records an advisory
    # note but leaves BOTH episodes fully servable (resolution is the human hive_supersede).
    server, _ = _on()
    a, b = _two(server)
    out = content(tool_call(server, "hive_flag",
                            {"a": a, "b": b, "kind": "supersedes", "winner": b,
                             "resolution": "b is the corrected deploy"}))
    assert out["status"] == "flagged"
    for eid in (a, b):
        ep = server.store.get_episode(eid)
        assert ep.trust == "established" and ep.superseded_by is None
    # and it is still recallable (nothing retired)
    r = content(tool_call(server, "hive_recall", {"query": "deploy uses make deploy"}))
    assert any(h["episode_id"] == a for h in r["reference_context"])


def test_flag_schema_belt_rejects_missing_and_bad_kind():
    server, _ = _on()
    a, b = _two(server)
    assert is_error(tool_call(server, "hive_flag", {"a": a, "b": b}))       # missing kind
    assert is_error(tool_call(server, "hive_flag",
                              {"a": a, "b": b, "kind": "rumor"}))            # bad enum
