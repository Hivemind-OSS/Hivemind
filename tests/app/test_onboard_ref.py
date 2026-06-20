"""onboard_ref — the fleet behavioral contract delivered to connecting agents. Pins the
load-bearing capture taxonomy (the dev-team high-signal categories + the noise floor), the
single-source guarantee (instructions and the rules block share ONE taxonomy, no drift), and a
parseable claude-code hooks bundle that wires the recall/capture nudges to lifecycle events."""
from __future__ import annotations

import json

from hive.app import onboard_ref
from hive.app.onboard_ref import (
    CAPTURE_TAXONOMY, CLAUDE_CODE_HOOKS, ONBOARDING_REFERENCE, SERVER_INSTRUCTIONS,
)
from hive.domain.kinds import render_taxonomy


def test_server_instructions_cover_the_verbs_and_search_first_timing():
    s = SERVER_INSTRUCTIONS
    assert "hive_recall" in s and "hive_capture" in s and "hive_write" in s
    assert "approved_by" in s
    assert "RECALL FIRST" in s                       # the search-before-build nudge


def test_instructions_cover_the_conflict_surface():
    """Agents must learn the resolution loop: recall surfaces a conflicts channel, the health
    worklist lists candidates, hive_supersede retires (human-vouched), hive_flag records an
    advisory note (never retires). Without this the surfaced conflicts have no served verb."""
    s = SERVER_INSTRUCTIONS
    assert "hive_supersede" in s and "hive_flag" in s
    assert "include_conflicts" in s              # the worklist entry point
    low = s.lower()
    assert "conflict" in low and "advisory" in low   # the two classes are named


def test_instructions_convey_capture_default_vs_approved_fastpath_strategy():
    """Agents must learn the STRATEGY, not just the mechanism: capture is the cheap default
    (let demand/recall-counts promote), and the approved write is the immediate-serve fast-path
    reserved for crucial memories — with the approver generalized beyond a human to an
    orchestrated fleet."""
    s = SERVER_INSTRUCTIONS.lower()
    assert "default" in s                            # capture framed as the default
    assert "immediately" in s                        # write = the must-serve-now fast-path
    assert "orchestrator" in s                       # approver isn't only a human


def test_capture_taxonomy_names_the_kind_vocabulary_and_noise_floor():
    """The taxonomy renders the kind vocabulary (so agents know what's worth storing AND which
    kind labels it), plus the write discipline and an explicit noise floor — or recall fills
    with junk."""
    t = CAPTURE_TAXONOMY.lower()
    for kind in ("bug", "gotcha", "convention", "design_choice", "contract",
                 "dead_end", "env_fact", "note"):
        assert kind in t, f"missing kind: {kind!r}"
    assert "polar language" in t and "density" in t        # the write discipline
    assert "do not capture" in t and "litmus" in t and "secret" in t   # the noise floor


def test_taxonomy_is_rendered_from_the_registry_and_served():
    # the kind vocabulary has ONE source (hive.domain.kinds.render_taxonomy); the taxonomy embeds
    # it verbatim and is itself embedded in the always-delivered instructions, so the advertised /
    # served / enforced copies cannot drift.
    assert render_taxonomy() in CAPTURE_TAXONOMY
    assert CAPTURE_TAXONOMY in SERVER_INSTRUCTIONS


def test_onboarding_is_served_only_no_self_install():
    # the single-source switch: there is NO self-installed rules block — the symbol is gone and
    # the served reference never tells an agent to write a block into its rules file.
    assert not hasattr(onboard_ref, "ONBOARDING_RULES_BLOCK")
    assert "hive-init" not in ONBOARDING_REFERENCE                 # no marker block
    assert "write this block" not in ONBOARDING_REFERENCE.lower()  # no self-install instruction
    # but the served reference still carries the identity/auth notes + the optional hooks
    assert "Mcp-Session-Id" in ONBOARDING_REFERENCE
    assert CLAUDE_CODE_HOOKS in ONBOARDING_REFERENCE


def test_claude_code_hooks_is_valid_json_with_the_three_nudge_events():
    h = json.loads(CLAUDE_CODE_HOOKS)                # malformed JSON would strand the operator
    assert set(h["hooks"]) == {"UserPromptSubmit", "Stop", "SubagentStop"}
    # recall fires on prompt submit; capture fires on agent AND subagent stop
    assert "hive_recall" in h["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    for ev in ("Stop", "SubagentStop"):
        assert "hive_capture" in h["hooks"][ev][0]["hooks"][0]["command"]
