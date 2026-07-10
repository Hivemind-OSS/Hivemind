"""Fit, payload, and onboarding-loop contracts for the served agent contract. These import
symbols that don't exist on the pre-rewrite constants (METADATA_FIELD_LIMIT, CONTRACT_FLOOR,
render_onboarding_payload) and are expected to fail collection until they're added -- the
red state this file is meant to start in."""
from __future__ import annotations

import json

from hive.app.onboard_ref import (
    AUTO_APPROVE_TOOLS,
    CONTRACT_FLOOR,
    CONTRACT_VERSION,
    METADATA_FIELD_LIMIT,
    RULES_END,
    RULES_START,
    SERVER_INSTRUCTIONS,
    render_agent_rules_block,
    render_onboarding_payload,
)
from hive.app.tool_defs import TOOL_DEFINITIONS


# ── A. fit guards — every capped-metadata field must fit under the measured client
#    truncation point; the uncapped result payload carries no such limit ──
def test_metadata_field_limit_is_the_measured_truncation_point():
    assert METADATA_FIELD_LIMIT == 2048


def test_server_instructions_fit_metadata_limit():
    assert len(SERVER_INSTRUCTIONS) <= METADATA_FIELD_LIMIT


def test_all_tool_descriptions_fit_metadata_limit():
    over = [(t["name"], len(t["description"])) for t in TOOL_DEFINITIONS
            if len(t["description"]) > METADATA_FIELD_LIMIT]
    assert over == [], f"over the metadata field limit: {over}"


# ── C (new members) — CONTRACT_FLOOR is the single source composed into both delivery
#    channels; WRITE_VS_CAPTURE/BAD_VS_STALE never reappear as a second copy ──
def test_floor_is_single_sourced_into_both_channels():
    assert CONTRACT_FLOOR in SERVER_INSTRUCTIONS
    assert CONTRACT_FLOOR in render_agent_rules_block()


# ── D. payload — the uncapped result channel carries the full install contract ──
def test_onboarding_payload_carries_the_full_install_contract():
    payload = render_onboarding_payload()
    for key in ("contract_version", "rules_block", "procedure", "hooks", "allowlist",
                "edge_cli", "identity"):
        assert key in payload, f"missing payload key: {key!r}"
    assert payload["contract_version"] == CONTRACT_VERSION
    assert payload["rules_block"].startswith(RULES_START)
    assert payload["rules_block"].rstrip().endswith(RULES_END)
    hooks = json.loads(payload["hooks"])
    assert set(hooks["hooks"]) == {"UserPromptSubmit", "PreToolUse", "PostToolUse",
                                   "SessionStart", "Stop", "SubagentStop"}
    for verb in AUTO_APPROVE_TOOLS:
        assert f"mcp__hive__{verb}" in payload["allowlist"]
    assert "hive_write" not in payload["allowlist"]


def test_onboarding_payload_carries_the_kind_taxonomy():
    """The per-kind body templates + polar-language/density rules aren't in the always-on
    floor (they don't fit the metadata limit alongside everything else); they still have to
    reach the agent SOMEWHERE, so the uncapped payload is their served channel."""
    from hive.domain.kinds import render_taxonomy
    payload = render_onboarding_payload()
    assert "kind_taxonomy" in payload
    assert render_taxonomy() in payload["kind_taxonomy"]


# ── F. onboarding loop — the beacon + two triggers resolve to one call ──
def test_floor_names_both_onboarding_triggers_and_the_fetch_call():
    low = CONTRACT_FLOOR.lower()
    assert "include_onboarding" in CONTRACT_FLOOR
    assert "hive_health" in CONTRACT_FLOOR
    # trigger A: no installed block — a specific phrase, not generic word-presence (a weak
    # "no" + "installed" substring check passed even after mutation-testing deleted this
    # clause, since both words recur unrelated elsewhere in the floor)
    assert "no block installed" in low
    # trigger B: contract_version mismatch
    assert "contract_version" in CONTRACT_FLOOR
    assert "differs from your installed marker" in low


def test_floor_states_degrade_to_floor():
    assert "degrades safely" in CONTRACT_FLOOR


def test_health_description_points_to_include_onboarding():
    health = next(t for t in TOOL_DEFINITIONS if t["name"] == "hive_health")
    assert "include_onboarding" in health["description"]
    assert "include_onboarding" in health["inputSchema"]["properties"]
    assert health["inputSchema"]["properties"]["include_onboarding"] == {"type": "boolean"}
