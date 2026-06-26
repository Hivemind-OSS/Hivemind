"""onboard_ref — the fleet behavioral contract delivered to connecting agents. Pins the
load-bearing capture taxonomy (the dev-team high-signal categories + the noise floor), the
single-source guarantee (instructions and the rules block share ONE taxonomy, no drift), and a
parseable claude-code hooks bundle that wires the recall/capture nudges to lifecycle events."""
from __future__ import annotations

import json
import re

from hive.app import onboard_ref
from hive.app.onboard_ref import (
    AGENT_RULES_BLOCK, AUTO_APPROVE_TOOLS, BAD_VS_STALE, CAPTURE_TAXONOMY,
    CLAUDE_CODE_HOOKS, CONTRACT_VERSION, ONBOARDING_PROCEDURE, ONBOARDING_REFERENCE,
    RESULT_ONBOARDING_DIRECTIVE, RULES_END, RULES_START, SERVER_INSTRUCTIONS,
    VALUE_RUBRIC, WRITE_VS_CAPTURE,
    bundle_digest, render_agent_rules_block, render_allowlist,
)
from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.kinds import render_taxonomy

# Regenerate when the bundle legitimately changes (and bump CONTRACT_VERSION) — the pre-commit
# contract-version guard does this automatically, or by hand:
#   python -c "from hive.app.onboard_ref import bundle_digest; print(bundle_digest())"
_GOLDEN_BUNDLE_SHA256 = "4161380b4c8ce07db1fd9a4712ceafe097f9f9520042c54cad4760f55fff2e00"


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


def test_onboarding_offers_optional_versioned_block_over_the_served_floor():
    # the §5 evolution: the served initialize instructions remain the always-on floor, AND an
    # OPTIONAL, versioned agent-contract block MAY now be installed as an enhancement layer. The
    # old pre-minimization self-install symbol stays gone; the new block is its own named, version-
    # stamped constant. The reference still carries the identity/auth notes + the optional hooks,
    # and now advertises the bundle version so a fresh agent knows what it would install.
    assert not hasattr(onboard_ref, "ONBOARDING_RULES_BLOCK")      # the old symbol stays gone
    assert hasattr(onboard_ref, "AGENT_RULES_BLOCK")               # the optional block now exists
    assert "hive-init" not in ONBOARDING_REFERENCE                 # no hive_init handshake marker
    assert "Mcp-Session-Id" in ONBOARDING_REFERENCE               # identity/auth reference kept
    assert CLAUDE_CODE_HOOKS in ONBOARDING_REFERENCE              # optional hooks kept
    assert CONTRACT_VERSION in ONBOARDING_REFERENCE               # the bundle version advertised


# ── contract-versioning: the installable, version-stamped agent-contract bundle ──
def test_contract_version_pins_bundle_hash():
    """KEYSTONE (Law 7): the served bundle (the agent rules block + the claude hooks + the
    auto-approve allowlist) is pinned to CONTRACT_VERSION by a golden hash. Editing ANY of the
    three without bumping CONTRACT_VERSION + regenerating this golden goes RED — silent contract
    drift at equal version becomes unconstructable (Law 1). To regenerate, run the one-liner in
    the module-level comment above and bump CONTRACT_VERSION."""
    assert bundle_digest() == _GOLDEN_BUNDLE_SHA256, (
        "you edited the contract bundle; bump CONTRACT_VERSION and regenerate this golden")


def test_agent_rules_block_is_marker_wrapped_and_version_stamped():
    block = render_agent_rules_block()
    assert block is AGENT_RULES_BLOCK or block == AGENT_RULES_BLOCK   # the constant == the renderer
    assert block.startswith(RULES_START)                  # opens with the START marker
    assert block.rstrip().endswith(RULES_END)             # closes with the END marker
    # the START marker carries the version so a one-regex extract reads it straight off the file
    assert CONTRACT_VERSION in RULES_START
    assert f"contract-version={CONTRACT_VERSION}" in block


def test_agent_rules_block_extracts_and_replaces_with_one_version_agnostic_regex():
    """The idempotent-install contract: an installed block is found and REPLACED in place by ONE
    regex anchored on the version-AGNOSTIC marker prefixes — so a re-onboard from any prior version
    overwrites exactly one block, never clobbering surrounding content and never duplicating."""
    block = render_agent_rules_block()
    host = f"# Existing project rules\n\nkeep me above\n\n{block}\n\nkeep me below\n"
    pat = re.compile(r"<!-- HIVEMIND-RULES:START.*?-->.*?<!-- HIVEMIND-RULES:END -->", re.DOTALL)
    found = pat.findall(host)
    assert len(found) == 1 and found[0] == block          # exactly the block, nothing more
    # replacing with a (future-version) block leaves ONE block and preserves the surroundings
    v_next = block.replace(f"contract-version={CONTRACT_VERSION}", "contract-version=v.99")
    replaced = pat.sub(lambda _m: v_next, host)
    assert replaced.count("HIVEMIND-RULES:START") == 1    # no duplicate block
    assert "contract-version=v.99" in replaced            # the new version won
    assert "keep me above" in replaced and "keep me below" in replaced  # no clobber


def test_agent_rules_block_renders_from_single_source():
    """The block PROJECTS from onboard_ref's OWN sub-constants — no new drift source. The
    write-vs-capture decision and the bad-vs-stale retire diagnosis ride verbatim, and the verb
    roster names every served verb (the keystone hash covers the exact text)."""
    block = render_agent_rules_block()
    assert WRITE_VS_CAPTURE in block                      # capture-vs-write decision, one source
    assert BAD_VS_STALE in block                          # retire diagnosis, one source
    for verb in ("hive_recall", "hive_capture", "hive_write", "hive_supersede",
                 "hive_prune", "hive_outcome", "hive_health"):
        assert verb in block, f"verb roster missing {verb!r}"


def test_onboarding_targets_project_not_global():
    """Scope is load-bearing (operator-stated): the agent resolves ITS OWN runtime's rules file at
    the REPO ROOT and writes there; hooks + the allowlist go to the PROJECT .claude/settings.json —
    never a home/global file."""
    proc = ONBOARDING_PROCEDURE
    for f in ("CLAUDE.md", ".cursorrules", ".windsurfrules", ".clinerules", "AGENTS.md"):
        assert f in proc, f"procedure missing the runtime rules file {f!r}"
    assert ".claude/settings.json" in proc                # the PROJECT settings target
    assert "~/.claude" not in proc                        # never the home/global settings file
    low = proc.lower()
    assert "project" in low and ("repo root" in low or "repository root" in low)


def test_auto_approve_tools_match_schema_partition():
    """D4: the auto-approve set is the schema complement of the human-gated verbs — exactly the
    verbs whose inputSchema does NOT require approved_by. The privileged write/supersede/prune
    (whose approved_by prompt IS the human checkpoint) are excluded. Pins AUTO_APPROVE_TOOLS to the
    schema so it can't drift (the onboard_ref↔tool_defs import cycle forbids deriving it inline)."""
    human_gated = {t["name"] for t in TOOL_DEFINITIONS
                   if "approved_by" in t["inputSchema"].get("required", [])}
    auto = {t["name"] for t in TOOL_DEFINITIONS} - human_gated
    assert set(AUTO_APPROVE_TOOLS) == auto
    assert human_gated == {"hive_write", "hive_supersede", "hive_prune"}
    assert not (set(AUTO_APPROVE_TOOLS) & human_gated)    # no privileged verb is auto-approved
    # the rendered allowlist names each auto-approve verb under the mcp__<server>__ prefix
    rendered = render_allowlist("hive")
    for verb in AUTO_APPROVE_TOOLS:
        assert f"mcp__hive__{verb}" in rendered
    assert "hive_write" not in rendered                   # the privileged verbs never rendered


def test_server_instructions_carry_version_and_reonboard_trigger():
    """The floor advertises the bundle version + the self-heal trigger: every tool result echoes
    contract_version, and a mismatch against the installed marker (or no block) is the re-onboard
    signal. The install is DIRECTED (MAY -> MUST), not merely offered, while the contract still
    honestly degrades to the floor (Law 6 — a lying contract is worse than none). Without this an
    agent can't know its installed block went stale."""
    s = SERVER_INSTRUCTIONS
    assert CONTRACT_VERSION in s                          # the live version is advertised at connect
    assert "contract_version" in s                        # the beacon field is named
    low = s.lower()
    assert "re-onboard" in low or "re-install" in low or "reinstall" in low  # the self-heal trigger
    assert RULES_START in s                               # the verbatim block is served on the floor
    assert "must install" in low                          # MAY -> MUST: the install is directed
    assert "degrades safely" in low                       # ... but still honest about the floor (Law 6)


def test_result_onboarding_directive_directs_full_reonboard_from_fresh_source():
    """The per-result onboarding directive rides every tool result alongside contract_version,
    turning the passive version beacon into an ACTIONABLE re-onboard prompt that re-reaches the
    agent on every turn (so it survives context compaction). It projects the live CONTRACT_VERSION,
    names the FULL contract to (re)install — the rules block + the claude hooks + the auto-approve
    allowlist, not just the block — and points at the FRESH served source (re-run initialize /
    reconnect, or read the hive_health description) so a cached stale block is never reinstalled."""
    d = RESULT_ONBOARDING_DIRECTIVE
    assert CONTRACT_VERSION in d                           # the live version the agent compares against
    low = d.lower()
    assert "re-onboard" in low or "reonboard" in low or "re-install" in low  # the actionable verb
    # the FULL bundle is named — dropping any of the three reds this (the §5.6 mutation proof)
    assert "block" in low                                 # the rules block
    assert "hooks" in low                                 # the claude lifecycle hooks
    assert "allowlist" in low                             # the read-verb auto-approve allowlist
    # points at the FRESH served source, never a possibly-cached copy
    assert "initialize" in low or "reconnect" in low
    assert "hive_health" in d
    # honest: skipping the install degrades to the floor, so this is salience, not a correctness gate
    assert "degrades safely" in low or "floor" in low


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
