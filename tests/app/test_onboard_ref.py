"""onboard_ref — the fleet behavioral contract delivered to connecting agents. Pins the
load-bearing capture taxonomy (the dev-team high-signal categories + the noise floor), the
single-source guarantee (instructions and the rules block share ONE taxonomy, no drift), and a
parseable claude-code hooks bundle that wires the recall/capture nudges to lifecycle events."""
from __future__ import annotations

import json

from hive.app import onboard_ref
from hive.app.onboard_ref import (
    BAD_VS_STALE, CAPTURE_TAXONOMY, CLAUDE_CODE_HOOKS, ONBOARDING_REFERENCE,
    SERVER_INSTRUCTIONS, VALUE_RUBRIC, WRITE_VS_CAPTURE,
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


def test_instructions_require_recall_before_write():
    """A write serves IMMEDIATELY at established trust, so the writer must recall the topic
    first and act on what already exists — skip a duplicate, correct in place with replaces=,
    or resolve a contradiction — instead of adding a rival memory to the store."""
    s = SERVER_INSTRUCTIONS
    low = s.lower()
    assert "before you write" in low     # recall-before-write is mandatory, not optional
    assert "don't duplicate" in low      # discovery 1: an already-captured fact
    assert "replaces" in s               # discovery 2: correct in place (a required supersession)
    assert "rival" in low                # discovery 3: don't add a rival to a conflicting memory


def test_instructions_make_conflict_escalation_explicit():
    """Surfacing a conflict is not enough — the agent must ALERT the human and request a
    resolution, and for a symmetric contradiction (no obvious loser) ask which memory wins
    before retiring; it must never retire on its own judgment (Law 3: retirement is
    human-vouched)."""
    s = SERVER_INSTRUCTIONS
    low = s.lower()
    assert "alert your human" in low          # raise it to the human, don't sit on it
    assert "your own judgment" in low         # never retire autonomously
    assert "symmetric contradiction" in low   # the no-obvious-loser case
    assert "which memory wins" in low         # ask the human to pick the winner


def test_instructions_cover_bare_retirement_and_hurt_evidence():
    """A lone recalled memory the agent believes is plain WRONG — nothing to replace it, no
    second memory to name a winner — has a served path: request hive_prune (human-vouched bare
    retirement, distinct from supersede which needs a winner), or hive_outcome(hurt=) which
    records evidence only. Closes the RESOLVE-clause gap (duplicates/contradictions were
    covered; bare deletion of one bad memory was not)."""
    s = SERVER_INSTRUCTIONS
    assert "hive_prune" in s and "hive_outcome" in s
    assert "hurt" in s.lower()


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


def test_store_philosophy_is_stigmergic_lean_and_maintainer_framed():
    """The opening must teach the MENTAL MODEL: a stigmergic fleet coordinating through traces with
    no direct messaging, a lean flow-not-stock store, and the agent as a maintainer — not just a
    place to dump text. Without it the verbs are mechanics with no governing philosophy."""
    s = SERVER_INSTRUCTIONS.lower()
    assert "stigmergic" in s                       # the coordination model
    assert "trace" in s                            # coordination via traces left behind
    assert "no direct communication" in s          # indirect — no agent-to-agent messaging
    assert "maintainer" in s                       # standing responsibility, not just a writer
    assert "lean" in s                             # the lean-store target
    assert "flow" in s and "bigger store" in s     # flow-not-stock: a bigger store is worse


def test_value_rubric_defines_what_is_worth_storing_and_grounding():
    """The value bar an agent applies before storing — durable / reusable / non-obvious /
    evidence-grounded — plus the anchor grounding requirement, served inside the taxonomy."""
    t = CAPTURE_TAXONOMY.lower()
    for crit in ("durable", "reusable", "non-obvious", "evidence"):
        assert crit in t, f"missing value criterion: {crit!r}"
    assert "anchor" in t                            # ground every trace in code (file/module/symbol)
    assert VALUE_RUBRIC in CAPTURE_TAXONOMY         # served as one source inside the taxonomy


def test_instructions_serve_the_write_vs_capture_decision():
    """Agents must be able to CHOOSE the verb: capture (useful + evidence, no human, can wait) vs
    write (load-bearing/highest-value or needs human confirmation, must-serve-now), with the
    AGI_MODE self-authorization exception named (otherwise write needs human review)."""
    s = SERVER_INSTRUCTIONS
    low = s.lower()
    assert WRITE_VS_CAPTURE in s                    # the decision rule served verbatim (one source)
    assert "load-bearing" in low                    # write's bar
    assert "verifiable evidence" in low             # capture's bar
    assert "AGI_MODE" in s                          # the self-authorization exception, else human


def test_instructions_diagnose_bad_vs_stale_for_prune_vs_supersede():
    """The retire decision: STALE (was true, has a successor) -> supersede/replace; BAD (incorrect/
    misleading, nothing to replace) -> prune. Without the diagnosis an agent prunes what it should
    supersede (losing the successor) or vice-versa."""
    s = SERVER_INSTRUCTIONS
    assert BAD_VS_STALE in s
    low = s.lower()
    assert "stale" in low and "successor" in low    # stale -> replace with the successor
    assert "incorrect" in low and "misleading" in low  # bad -> prune


def test_instructions_require_single_pointed_recall_query_set():
    """A bulk multi-question query dilutes toward the centroid and abstains, so the contract directs
    splitting an information need into a SET of single-pointed queries, one hive_recall each."""
    low = SERVER_INSTRUCTIONS.lower()
    assert "single-pointed" in low
    assert "one intent" in low
    assert "never bundle" in low                    # no bulk multi-question query


def test_claude_code_hooks_is_valid_json_with_the_three_nudge_events():
    h = json.loads(CLAUDE_CODE_HOOKS)                # malformed JSON would strand the operator
    assert set(h["hooks"]) == {"UserPromptSubmit", "Stop", "SubagentStop"}
    # recall fires on prompt submit; capture fires on agent AND subagent stop
    assert "hive_recall" in h["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    for ev in ("Stop", "SubagentStop"):
        assert "hive_capture" in h["hooks"][ev][0]["hooks"][0]["command"]
