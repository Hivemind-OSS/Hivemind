"""onboard_ref — the fleet behavioral contract delivered to connecting agents. Pins the
load-bearing capture taxonomy (the dev-team high-signal categories + the noise floor), the
single-source guarantee (instructions and the rules block share ONE taxonomy, no drift), and a
parseable claude-code hooks bundle that wires the recall/capture nudges to lifecycle events."""
from __future__ import annotations

import json
import re

from hive.app import onboard_ref
from hive.app.onboard_ref import (
    AGENT_RULES_BLOCK, AUTO_APPROVE_TOOLS, BAD_VS_STALE, CAPTURE_RECALL_FLOOR,
    CAPTURE_TAXONOMY, CLAUDE_CODE_HOOKS, CONTRACT_VERSION, EDGE_CLI,
    MIN_EDGE_VERSION, MINT_DIRECTIVE, NOISE_FLOOR, ONBOARDING_PROCEDURE,
    ONBOARDING_REFERENCE, REMEDIATION_NOTICE, RULES_END, RULES_START,
    SERVER_INSTRUCTIONS, VALUE_RUBRIC, VERIFY_DIRECTIVE, WRITE_VS_CAPTURE,
    bundle_digest, render_agent_rules_block, render_allowlist, render_onboarding_payload,
)
from hive.app.tool_defs import TOOL_DEFINITIONS
from hive.domain.kinds import render_taxonomy

# Regenerate when the bundle legitimately changes (and bump CONTRACT_VERSION) — the pre-commit
# contract-version guard does this automatically, or by hand:
#   python -c "from hive.app.onboard_ref import bundle_digest; print(bundle_digest())"
_GOLDEN_BUNDLE_SHA256 = "944a41629bb208f6749c79f506dd93aeadf3a7dbdbe8ac9530d04e3750fb3d86"


def test_server_instructions_cover_the_verbs_and_search_first_timing():
    s = SERVER_INSTRUCTIONS
    assert "hive_recall" in s and "hive_capture" in s and "hive_write" in s
    assert "approved_by" in s
    assert "RECALL FIRST" in s                       # the search-before-build nudge


def test_instructions_cover_the_conflict_surface():
    """Agents must learn the resolution loop: hive_supersede retires (human-vouched), hive_flag
    records an advisory note (never retires) — both named on the floor. The health worklist
    entry point (include_conflicts) is call-adjacent detail; it rides hive_supersede's own
    (unchanged) description rather than the capped floor — checked there instead."""
    s = SERVER_INSTRUCTIONS
    assert "hive_supersede" in s and "hive_flag" in s
    assert "advisory" in s.lower()               # the two classes are named
    supersede = next(t["description"] for t in TOOL_DEFINITIONS if t["name"] == "hive_supersede")
    assert "include_conflicts" in supersede      # the worklist entry point, call-adjacent


def test_instructions_require_recall_before_write():
    """A write serves IMMEDIATELY at established trust, so the writer must recall the topic
    first and act on what already exists — skip a duplicate, or correct in place with
    replaces=, instead of adding a rival memory to the store. The "don't duplicate" directive
    and replaces= mechanism are floor-required; the fuller rival/contradiction framing is
    call-adjacent detail that rides hive_write's own (unchanged) description."""
    s = SERVER_INSTRUCTIONS
    low = s.lower()
    assert "don't duplicate" in low      # discovery 1: an already-captured fact
    assert "replaces" in s               # discovery 2: correct in place (a required supersession)
    write_desc = next(t["description"] for t in TOOL_DEFINITIONS if t["name"] == "hive_write").lower()
    assert "rival" in write_desc         # discovery 3: don't add a rival to a conflicting memory


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
    reserved for crucial memories. The approver-generalized-beyond-a-human nuance is
    call-adjacent detail checked at hive_write's own description
    (test_write_description_generalizes_the_approver)."""
    s = SERVER_INSTRUCTIONS.lower()
    assert "default" in s                            # capture framed as the default
    assert "immediately" in s                        # write = the must-serve-now fast-path


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
    # it verbatim, so the advertised / served / enforced copies cannot drift. The per-kind body
    # templates are un-compressible verbatim reference (don't fit the capped floor alongside
    # everything else) — they ride the uncapped onboarding payload's kind_taxonomy key instead;
    # the short kind GLOSS still rides the hive_write/hive_capture descriptions unchanged.
    assert render_taxonomy() in CAPTURE_TAXONOMY
    assert render_taxonomy() in render_onboarding_payload()["kind_taxonomy"]


def test_onboarding_offers_optional_versioned_block_over_the_served_floor():
    # the served initialize instructions remain the always-on floor, AND an OPTIONAL, versioned
    # agent-contract block MAY now be installed as an enhancement layer. ONBOARDING_REFERENCE is
    # now a SHORT pointer (identity note + where to fetch) — the hooks JSON itself is
    # un-compressible verbatim detail that rides the uncapped onboarding payload, not this
    # capped description field.
    assert not hasattr(onboard_ref, "ONBOARDING_RULES_BLOCK")      # the old symbol stays gone
    assert hasattr(onboard_ref, "AGENT_RULES_BLOCK")               # the optional block now exists
    assert "hive-init" not in ONBOARDING_REFERENCE                 # no hive_init handshake marker
    assert "Mcp-Session-Id" in ONBOARDING_REFERENCE               # identity/auth reference kept
    assert CLAUDE_CODE_HOOKS == render_onboarding_payload()["hooks"]   # hooks served, not embedded
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
    """The floor advertises the self-heal trigger: every tool result echoes contract_version, and
    a mismatch against the installed marker (or no block) is the re-onboard signal. The install
    is DIRECTED (MAY -> MUST), not merely offered, while the contract still honestly degrades to
    the floor (Law 6 — a lying contract is worse than none). Without this an agent can't know
    its installed block went stale. The verbatim block itself no longer rides this capped field
    (that duplication was BUG-023's root cause) — it's fetched via the pointer below, and the
    live version is authoritatively advertised by the beacon on every tool result, not
    hardcoded into this text."""
    s = SERVER_INSTRUCTIONS
    assert "contract_version" in s                        # the beacon field is named
    assert "include_onboarding" in s                       # where to fetch the verbatim block
    low = s.lower()
    assert "re-onboard" in low or "re-install" in low or "reinstall" in low  # the self-heal trigger
    assert "must install" in low                          # MAY -> MUST: the install is directed
    assert "degrades safely" in low                       # ... but still honest about the floor (Law 6)
    assert render_onboarding_payload()["contract_version"] == CONTRACT_VERSION


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


# ── C5: the universal capture/recall discipline floor (cross-platform, in-block) ──
def test_rules_block_serves_the_capture_recall_discipline_floor():
    """The persistence block ITSELF carries the discipline as plain rules-file markdown
    valid verbatim on ANY runtime (CLAUDE.md / AGENTS.md / .cursorrules / .windsurfrules):
    the task-end capture shape (WHAT + WHERE as file:symbol + WHY, in both body and
    anchor), capture-only-never-write at task end, the DO-NOT-CAPTURE noise floor, the
    zero-capture escape, and recall-before-edit-by-anchor."""
    block = render_agent_rules_block()
    assert CAPTURE_RECALL_FLOOR in block                  # one source, embedded verbatim
    f = CAPTURE_RECALL_FLOOR
    assert "WHAT" in f and "WHY" in f                     # the capture shape…
    assert "file.py:symbol" in f and "anchor" in f        # …anchored as WHERE, both places
    low = f.lower()
    assert "never hive_write" in low                      # capture-only at task end
    assert "capture nothing" in low                       # the zero-capture escape
    assert "recall before edit" in low                    # recall-before-edit-by-anchor…
    assert "hive_recall" in f                             # …via the recall verb


def test_floor_carries_the_noise_floor_single_sourced():
    """The DO-NOT-CAPTURE floor has ONE definition: the same NOISE_FLOOR constant rides
    the served taxonomy AND the in-block discipline floor — no second copy to drift."""
    assert NOISE_FLOOR in CAPTURE_TAXONOMY
    assert NOISE_FLOOR in CAPTURE_RECALL_FLOOR
    low = NOISE_FLOOR.lower()
    assert "do not capture" in low and "litmus" in low and "secret" in low


def test_floor_documents_its_own_tiering():
    """The tiering is stated in the served text itself: an ADVISORY rules-text floor on
    every platform, made DETERMINISTIC where the platform has lifecycle hooks — so a
    hook-less runtime knows exactly what tier it is on."""
    low = CAPTURE_RECALL_FLOOR.lower()
    assert "advisory" in low
    assert "deterministic" in low
    assert "hooks" in low


def test_floor_reaches_the_served_instructions():
    """The always-on floor (the initialize instructions) carries the discipline text at
    connect — every platform receives it without installing anything."""
    assert CAPTURE_RECALL_FLOOR in SERVER_INSTRUCTIONS


def test_claude_code_hooks_is_valid_json_with_the_six_lifecycle_events():
    h = json.loads(CLAUDE_CODE_HOOKS)                # malformed JSON would strand the operator
    hooks = h["hooks"]
    assert set(hooks) == {"UserPromptSubmit", "PreToolUse", "PostToolUse",
                          "SessionStart", "Stop", "SubagentStop"}
    # the two static echo nudges are KEPT (no CLI dependency — pure context injection)
    assert "hive_recall" in hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "hive_capture" in hooks["SubagentStop"][0]["hooks"][0]["command"]

    def cmd(ev):
        return hooks[ev][0]["hooks"][0]["command"]
    # the four engine events are BARE `hive-edge hook <event>` commands — one console script,
    # no sh -c wrapper and no venv-reexec mint_fp.py path
    assert cmd("PreToolUse") == "hive-edge hook pre-capture"
    assert cmd("PostToolUse") == "hive-edge hook post-recall"
    assert cmd("SessionStart") == "hive-edge hook session-start"
    assert cmd("Stop") == "hive-edge hook stop"
    # the capture/recall matchers are the exact MCP tool names
    assert hooks["PreToolUse"][0]["matcher"] == "mcp__hive__hive_capture"
    assert hooks["PostToolUse"][0]["matcher"] == "mcp__hive__hive_recall"
    assert "mint_fp.py" not in CLAUDE_CODE_HOOKS      # the venv-reexec fallback is gone


# ── the harness-agnostic edge-CLI contract: mint/verify/upgrade + the serving rider ──
def test_edge_cli_names_install_version_floor_and_upgrade():
    """EDGE_CLI teaches the tooling loop on EVERY runtime: the version check, the uv install
    line, the MIN_EDGE_VERSION floor, and `hive-edge upgrade` with the rollback-pin caveat."""
    e = EDGE_CLI
    assert "hive-edge --version" in e
    assert "uv tool install hive-edge" in e            # the install line (repo link included)
    assert MIN_EDGE_VERSION in e                        # the capability floor
    assert "hive-edge upgrade" in e
    assert "pin" in e.lower()                           # the rollback-pin refusal caveat


def test_edge_cli_and_directives_ride_both_channels():
    """The mint/verify ONE-LINERS (ongoing behavioral discipline, not install mechanics) reach
    the agent on the served floor (SERVER_INSTRUCTIONS) AND in the installable rules block — one
    source, no drift. EDGE_CLI's own install/upgrade detail (the exact version-check and upgrade
    commands) is un-compressible verbatim install-mechanics — it's the release valve moved to
    the uncapped onboarding payload instead, checked there."""
    block = render_agent_rules_block()
    for needle in ("hive-edge mint", "hive-edge verify"):
        assert needle in SERVER_INSTRUCTIONS, f"{needle!r} missing from the served floor"
        assert needle in block, f"{needle!r} missing from the rules block"
    payload_edge_cli = render_onboarding_payload()["edge_cli"]
    assert "hive-edge --version" in payload_edge_cli
    assert MIN_EDGE_VERSION in payload_edge_cli


def test_reonboard_names_the_edge_upgrade_step():
    """RE-ONBOARD must run `hive-edge upgrade` so the CLI matches the new contract."""
    assert "hive-edge upgrade" in ONBOARDING_PROCEDURE


def test_floor_mint_directive_names_fp_meta_and_the_conditional_hook():
    """The TASK-END capture directive: the floor names the one-liner (`hive-edge mint`); the
    full mechanics (the combdrift/fp meta shape, the where-the-hook-is-firing conditional) are
    un-compressible verbatim detail that rides the uncapped onboarding payload's procedure
    text instead — the release valve for exact call mechanics."""
    f = CAPTURE_RECALL_FLOOR
    assert "hive-edge mint" in f
    procedure = render_onboarding_payload()["procedure"]
    assert MINT_DIRECTIVE in procedure                  # single-sourced into the payload
    assert "combdrift/fp" in procedure and "meta" in procedure
    assert "installed and firing" in procedure          # the conditional hook clause


def test_floor_verify_directive_names_stale_reference_fallback_and_reconciliation():
    """The RECALL-BEFORE-EDIT directive: the floor names the one-liner (`hive-edge verify`); the
    full mechanics (stale => REFERENCE ONLY, the last_verified / include_stale_suspects
    fallback, the local-verdict-wins reconciliation) ride the uncapped onboarding payload's
    procedure text instead."""
    f = CAPTURE_RECALL_FLOOR
    assert "hive-edge verify" in f
    procedure = render_onboarding_payload()["procedure"]
    assert VERIFY_DIRECTIVE in procedure
    assert "stale" in procedure and "REFERENCE ONLY" in procedure
    assert "last_verified" in procedure and "include_stale_suspects" in procedure
    assert "point-local verdict wins" in procedure.lower()


def test_edge_directives_carry_the_conditional_on_both_triggers():
    """Both the mint and the verify directive carry the where-installed-and-firing conditional,
    so a hookless runtime knows to run the CLI itself (mutation 9 target)."""
    assert "installed and firing" in MINT_DIRECTIVE
    assert "installed and firing" in VERIFY_DIRECTIVE


def test_remediation_notice_names_the_option_vocabulary_without_hive_flag():
    """The server rider text carries the full single-anchor option set — outcome-now vs the
    human-gated supersede/replaces-vs-prune per BAD_VS_STALE — and NEVER names hive_flag
    (a memory-vs-memory tool, not a single stale anchor)."""
    r = REMEDIATION_NOTICE
    for token in ("hive_outcome", "hive_supersede", "hive_write(replaces=", "hive_prune",
                  "BAD_VS_STALE", "never retire on your own judgment"):
        assert token in r, token
    assert "hive_flag" not in r


# ── single-source: WRITE_VS_CAPTURE / BAD_VS_STALE must appear exactly once in the served
# floor — a second copy is the exact duplication this module's rewrite eliminates ──
def test_write_vs_capture_and_bad_vs_stale_single_instance():
    assert SERVER_INSTRUCTIONS.count(WRITE_VS_CAPTURE) == 1
    assert SERVER_INSTRUCTIONS.count(BAD_VS_STALE) == 1
