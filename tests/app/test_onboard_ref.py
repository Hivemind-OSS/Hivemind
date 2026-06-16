"""onboard_ref — the fleet behavioral contract delivered to connecting agents. Pins the
load-bearing capture taxonomy (the dev-team high-signal categories + the noise floor), the
single-source guarantee (instructions and the rules block share ONE taxonomy, no drift), and a
parseable claude-code hooks bundle that wires the recall/capture nudges to lifecycle events."""
from __future__ import annotations

import json

from hive.app.onboard_ref import (
    CAPTURE_TAXONOMY, CLAUDE_CODE_HOOKS, ONBOARDING_RULES_BLOCK, SERVER_INSTRUCTIONS,
)


def test_server_instructions_cover_the_verbs_and_search_first_timing():
    s = SERVER_INSTRUCTIONS
    assert "hive_recall" in s and "hive_capture" in s and "hive_write" in s
    assert "approved_by" in s
    assert "RECALL FIRST" in s                       # the search-before-build nudge


def test_capture_taxonomy_names_the_fleet_high_signal_categories():
    """The dev-team categories the user asked for must be named, or agents won't know what's
    worth storing — and the noise floor must be explicit, or recall fills with junk."""
    t = CAPTURE_TAXONOMY.lower()
    for needle in ("bug + fix", "open", "design choice", "where", "convention",
                   "gotcha", "dead-end", "interface", "env"):
        assert needle in t, f"missing capture category: {needle!r}"
    # the signal/noise discipline is part of the contract
    assert "do not capture" in t and "litmus" in t and "secret" in t


def test_taxonomy_is_single_sourced_into_both_surfaces():
    # one definition, embedded verbatim → the always-delivered instructions and the persisted
    # rules block can never disagree about what to store.
    assert CAPTURE_TAXONOMY in SERVER_INSTRUCTIONS
    assert CAPTURE_TAXONOMY in ONBOARDING_RULES_BLOCK


def test_claude_code_hooks_is_valid_json_with_the_three_nudge_events():
    h = json.loads(CLAUDE_CODE_HOOKS)                # malformed JSON would strand the operator
    assert set(h["hooks"]) == {"UserPromptSubmit", "Stop", "SubagentStop"}
    # recall fires on prompt submit; capture fires on agent AND subagent stop
    assert "hive_recall" in h["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    for ev in ("Stop", "SubagentStop"):
        assert "hive_capture" in h["hooks"][ev][0]["hooks"][0]["command"]
