"""CT-11 — the onboarding/contract apparatus is gone; what remains is a trimmed
served contract (plan §4 intent 11, §6 step 4).

``initialize`` serves trimmed v3 instructions under ``METADATA_FIELD_LIMIT``
(BUG-023 guard, now owned by ``hive.app.contract``); tool results carry NO
``contract_version`` beacon; ``include_onboarding`` is not a valid flag; no
AGI/approver field survives anywhere in the advertised schemas; and the
advertised table IS the enforced belt.
"""

from __future__ import annotations

import json

# NOTE: the ``rig`` fixture is auto-discovered from tests/contract/conftest.py
from hive.app.mcp_server import MCPRequest
from tests.contract.conftest import (
    call,
    is_error,
    load_module,
    payload,
    recall,
    write,
)

VERBS = {
    "hive_write",
    "hive_capture",
    "hive_recall",
    "hive_supersede",
    "hive_prune",
    "hive_outcome",
    "hive_flag",
    "hive_health",
}


def _contract_mod():
    mod = load_module("hive.app.contract")
    assert mod is not None, (
        "hive.app.contract does not exist yet (onboard_ref renamed/trimmed — "
        "plan §6 step 4)"
    )
    assert hasattr(mod, "METADATA_FIELD_LIMIT") and hasattr(
        mod, "SERVER_INSTRUCTIONS"
    ), "the trimmed contract module keeps the cap + the served instructions"
    return mod


def _tools(server) -> list[dict]:
    resp = server.handle(MCPRequest(1, "tools/list", {}))
    return resp.result["tools"]


def _schema(server, name: str) -> dict:
    for t in _tools(server):
        if t["name"] == name:
            return t["inputSchema"]
    raise AssertionError(f"tool {name} not advertised")


def test_initialize_serves_trimmed_v3_instructions_under_the_cap(rig):
    mod = _contract_mod()
    resp = rig.server.handle(MCPRequest(1, "initialize", {}))
    instructions = resp.result.get("instructions", "")
    assert instructions, "initialize always serves the usage contract"
    assert len(instructions) <= int(mod.METADATA_FIELD_LIMIT), (
        f"the served instructions must fit the measured client cap "
        f"({len(instructions)} > {mod.METADATA_FIELD_LIMIT}) — BUG-023"
    )
    for dead in (
        "AGI",
        "approved_by",
        "HIVEMIND-RULES",
        "contract_version",
        "contract-version",
        "hive-edge",
    ):
        assert dead not in instructions, (
            f"the deleted apparatus may not survive in the served contract: "
            f"{dead!r} found"
        )
    assert "recall" in instructions.lower(), (
        "the v3 contract still teaches recall-first"
    )


def test_every_tool_description_fits_the_cap(rig):
    mod = _contract_mod()
    cap = int(mod.METADATA_FIELD_LIMIT)
    for t in _tools(rig.server):
        assert len(t.get("description", "")) <= cap, (
            f"{t['name']} description exceeds the client cap"
        )


def test_no_contract_version_beacon_on_tool_results(rig):
    env = write(rig.server, "a beaconless memory")
    assert "contract_version" not in env, (
        f"the contract-version beacon is DELETED — no result may carry it: {env}"
    )
    health = payload(call(rig.server, "hive_health"))
    assert "contract_version" not in health


def test_include_onboarding_is_gone(rig):
    schema = _schema(rig.server, "hive_health")
    assert "include_onboarding" not in schema.get("properties", {}), (
        "include_onboarding is not a valid flag in v3"
    )
    env = payload(call(rig.server, "hive_health", {"include_onboarding": True}))
    assert "onboarding" not in env, (
        f"no install payload channel survives: {sorted(env)}"
    )


def test_schema_sweep_no_approver_no_agi_anywhere(rig):
    dumped = json.dumps(_tools(rig.server))
    assert "approved_by" not in dumped, "no approver field survives in any schema"
    assert "AGI" not in dumped, "no AGI mention survives in any schema"
    assert "proposed_by" not in dumped, "identity stays transport-resolved (INV-2)"


def test_v3_write_and_recall_schemas(rig):
    w = _schema(rig.server, "hive_write")
    assert w.get("required") == ["text"], (
        f"v3 write requires text ALONE (no approver): {w.get('required')}"
    )
    for prop in ("anchors", "repos", "replaces", "meta", "polarity", "kind"):
        assert prop in w.get("properties", {}), f"write schema lacks {prop!r}"
    r = _schema(rig.server, "hive_recall")
    for prop in ("repos", "anchor_prefix"):
        assert prop in r.get("properties", {}), f"recall schema lacks {prop!r}"
    s = _schema(rig.server, "hive_supersede")
    assert sorted(s.get("required", [])) == ["loser", "winner"]
    p = _schema(rig.server, "hive_prune")
    assert p.get("required") == ["episode_id"]


def test_tools_list_is_the_enforced_belt(rig):
    assert {t["name"] for t in _tools(rig.server)} == VERBS
    # the advertised required[] IS what the belt enforces
    resp = call(rig.server, "hive_supersede", {"loser": 1})
    assert is_error(resp), "a missing advertised-required field must be rejected"
    env = payload(resp)
    assert "winner" in env.get("_raw", "") or "winner" in str(env), (
        f"the belt names the missing advertised field: {env}"
    )
    # and hive_recall's scope args pass the belt (advertised ⇒ accepted)
    served = recall(rig.server, "anything at all", repos=[])
    assert "reference_context" in served or served.get("status") == "refused"


def test_write_vs_capture_states_the_regression_avoidance_preference(rig):
    """The write-vs-capture decision rule composed into the floor and both
    write-side tool descriptions must state the operator ruling: prefer
    hive_write for a lesson that saves a future agent from repeating a
    mistake, causing a regression, or relearning a hard-won insight that led
    to landed code — hive_capture stays the ambiguous tail only. This is
    decision guidance beyond the existing serve-now-vs-quarantined mechanics."""
    mod = _contract_mod()
    low = mod.WRITE_VS_CAPTURE.lower()
    assert "hive_write" in low and "hive_capture" in low
    assert "regression" in low or "repeating a mistake" in low, (
        "the preference ruling (repeating a mistake / causing a regression / "
        f"relearning what led to landed code) must ride WRITE_VS_CAPTURE: "
        f"{mod.WRITE_VS_CAPTURE!r}"
    )


def test_write_vs_capture_states_the_branch_anchor_tagging_directive(rig):
    """The same decision rule must also carry the tagging directive: tag the
    line with repos=['name@branch'] + anchors=[{repo, anchor}] wherever the
    memory is about real code — required behavior wherever possible, not an
    afterthought buried only in the schema's field description."""
    mod = _contract_mod()
    low = mod.WRITE_VS_CAPTURE.lower()
    assert "@branch" in low, (
        "the repo/branch + anchor tagging directive must ride WRITE_VS_CAPTURE "
        f"so agents are told to tag the line wherever a memory is about real "
        f"code: {mod.WRITE_VS_CAPTURE!r}"
    )
    assert "anchor" in low


def test_hive_write_and_capture_descriptions_carry_the_tagging_directive(rig):
    """WRITE_VS_CAPTURE composes verbatim into both write-side tool
    descriptions (one source, no drift) — the tagging directive must therefore
    be visible on the two tools an agent actually calls, not just the
    initialize-time floor."""
    for name in ("hive_write", "hive_capture"):
        desc = next(
            t["description"] for t in _tools(rig.server) if t["name"] == name
        ).lower()
        assert "@branch" in desc, (
            f"{name} description must carry the branch+anchor tagging "
            f"directive composed from WRITE_VS_CAPTURE: {desc!r}"
        )
