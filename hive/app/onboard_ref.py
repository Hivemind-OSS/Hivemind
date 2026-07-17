"""Static onboarding + the fleet behavioral contract, single-sourced and served by the server.

The served ``initialize`` instructions are the always-on FLOOR (foolproof, every client surfaces
them at connect). Every string delivered over a capped MCP metadata field (``initialize.
instructions``, each tool ``description``) fits under ``METADATA_FIELD_LIMIT`` — the measured
client truncation point — so it always arrives whole. The floor directs the agent to install a
versioned agent-contract bundle (MUST — a salience directive over a floor-sufficient,
non-blocking mechanism the server cannot verify): it is version-stamped by ``CONTRACT_VERSION``,
beaconed on every tool result (so a connected agent whose installed copy drifts re-onboards), and
a missing or stale block degrades safely to the served floor (Law 6). The bundle is
single-sourced from the served constants — advertised == served == installed — and pinned to
``CONTRACT_VERSION`` by a keystone hash so editing it without a version bump goes RED (Law 1/7).

The served pieces:

- ``CONTRACT_FLOOR`` — the single canonical behavioral text: what Hivemind is, how recall/
  capture/write/resolve work, the value bar and noise floor, the cross-platform capture/recall
  discipline, and the persistence pointer (fetch/install via
  ``hive_health(include_onboarding=true)``, re-onboard on a ``contract_version`` mismatch,
  degrade-to-floor if skipped). Composed into BOTH delivery channels so there is one source of
  behavioral truth, not a copy per channel.
- ``SERVER_INSTRUCTIONS`` — ``CONTRACT_FLOOR`` delivered via the MCP ``initialize``
  ``instructions`` field (every client surfaces it at connect: Claude Code, Cursor, Codex). The
  foolproof channel.
- ``AGENT_RULES_BLOCK`` (``render_agent_rules_block``) — the OPTIONAL, marker-wrapped,
  version-stamped block an agent transcribes VERBATIM into its OWN runtime's PROJECT rules file.
  Wraps the same ``CONTRACT_FLOOR`` with a heading and a project-scope/re-onboard footer; the
  persistence layer against context compaction — absent or stale, it degrades to the served
  floor.
- ``render_onboarding_payload()`` — the structured install payload served over the UNCAPPED
  ``hive_health(include_onboarding=true)`` tool-result channel: the rules block, the install
  procedure, the claude-code hooks, the auto-approve allowlist, the edge-CLI reference, the full
  identity/auth reference, and the full per-kind capture taxonomy. Everything too large or too
  install-mechanical for a capped field lives here, in full, on demand.
- ``ONBOARDING_REFERENCE`` — a SHORT pointer surfaced via the ``hive_health`` tool description:
  an identity note plus where to fetch the full contract. A SECONDARY channel for clients that
  read tool descriptions; the always-on floor still installs nothing by itself.
- ``CAPTURE_TAXONOMY`` — the SINGLE definition of what is worth storing: the value bar
  (``VALUE_RUBRIC``) + the kind vocabulary (RENDERED from ``hive.domain.kinds`` so it cannot
  drift from the enforced enum) + the noise floor.
- ``CLAUDE_CODE_HOOKS`` — OPTIONAL, claude-code ONLY: real lifecycle hooks that give the
  recall/capture nudges active teeth. Other IDEs have no event-hook substrate, so correctness
  must never depend on these — they are an enhancement over the always-delivered instructions.
"""
from __future__ import annotations

import hashlib

from hive.domain.kinds import render_taxonomy

# ── the versioned agent-contract bundle (CONTRACT_VERSION versions all three: the rules block,
#    the claude hooks, and the rendered allowlist; the keystone hash in tests/app pins them) ──

# The measured truncation point for a capped MCP metadata field (``initialize.instructions`` and
# each tool ``description``): the delivery path hard-clips any string longer than this, mid-word,
# with no ellipsis. Applies ONLY to those two field kinds — never the result payload of a tool
# call, which is proven uncapped (BUG-023).
METADATA_FIELD_LIMIT: int = 2048

# The single owner of the bundle version. Bump it (and regenerate the keystone golden) on ANY
# change to AGENT_RULES_BLOCK / CLAUDE_CODE_HOOKS / the rendered allowlist. The beacon stamps this
# on every tool result; an agent whose installed marker differs re-onboards.
CONTRACT_VERSION: str = "v.13"

# The minimum hive-edge CLI version this contract requires (mint / verify / hook capability).
# Rendered into the EDGE_CLI floor text below. When the CLI's capability moves, bump
# this in the SAME release as the hive-edge change (Hive-edge's version-coupling rule) so the
# pre-commit guard bumps CONTRACT_VERSION and the new floor propagates to every connected edge. A
# contract-only edit that does not change CLI capability leaves this untouched.
MIN_EDGE_VERSION: str = "0.7.0"

# The edge-CLI tooling loop, served in the install payload: mint / verify ride ONE console
# script so every harness runs identical code — the hooks only AUTOMATE these calls, they never
# own the computation. (Census computation left the edge CLI in 0.7.0 — it is server-side now;
# see CENSUS_DIRECTIVE.) Names the version check, the PyPI install line, the MIN_EDGE_VERSION
# floor, and `hive-edge upgrade`, with the rollback-pin caveat so the routine below-floor nudge
# can never silently re-apply a regression a human deliberately rolled back.
EDGE_CLI: str = (
    "EDGE CLI (all runtimes): mint / verify ride the `hive-edge` console script, so every "
    "harness runs identical code. Check `hive-edge --version`; if it is absent, install it "
    "from PyPI: `uv tool install hive-edge`. If it is below MIN_EDGE_VERSION ("
    + MIN_EDGE_VERSION + ") or your contract version is outdated, run `hive-edge upgrade` — UNLESS a "
    "rollback pin is active, in which case `hive-edge upgrade` reports the pin and refuses to "
    "auto-override it, so defer to your human instead. No `hive-edge` on PATH is not an error: mint "
    "and verify simply no-op and capture / recall are unaffected until you install it."
)

# Marker fences make the installed block a one-regex, idempotent replace (no clobber, no duplicate).
# The START marker carries the version so an extract reads it straight off the rules file; the
# extract/replace regex anchors on the version-AGNOSTIC prefixes ``HIVEMIND-RULES:START`` /
# ``HIVEMIND-RULES:END`` so a re-onboard from any prior version overwrites exactly one block.
RULES_START: str = f"<!-- HIVEMIND-RULES:START contract-version={CONTRACT_VERSION} -->"
RULES_END: str = "<!-- HIVEMIND-RULES:END -->"

# The read/surface verbs an agent runs without a human in the loop — auto-approved so they do not
# prompt per call. This is the SCHEMA COMPLEMENT of the human-gated set (the verbs whose inputSchema
# requires ``approved_by``: hive_write / hive_supersede / hive_prune — whose IDE prompt IS the human
# checkpoint matching their vouch, Law 3). Held here as a constant (the onboard_ref↔tool_defs import
# cycle forbids deriving it inline) and PINNED to the schema partition by
# test_auto_approve_tools_match_schema_partition, so it cannot drift. == BUG-011's FLEET_AGENT_TOOLS.
AUTO_APPROVE_TOOLS: tuple[str, ...] = (
    "hive_recall", "hive_capture", "hive_health", "hive_outcome", "hive_flag",
)

# Runtime → its PROJECT-ROOT rules file. The agent knows its own runtime, resolves ONE row, and
# writes the block THERE — never a home/global file. (Cursor also accepts ``.cursor/rules/``.)
RUNTIME_RULES_FILES: tuple[tuple[str, str], ...] = (
    ("Claude Code", "CLAUDE.md"),
    ("Cursor", ".cursorrules"),
    ("Windsurf", ".windsurfrules"),
    ("Cline", ".clinerules"),
    ("Codex / OpenCode / other", "AGENTS.md"),
)
_RUNTIME_TABLE = "\n".join(f"    {runtime} -> {fname}" for runtime, fname in RUNTIME_RULES_FILES)


def render_allowlist(server: str = "hive") -> str:
    """The auto-approve allowlist lines for the served procedure: one ``mcp__{server}__{verb}`` per
    read/surface verb (``AUTO_APPROVE_TOOLS``), to paste into the PROJECT ``.claude/settings.json``
    ``permissions.allow``. The human-gated write/supersede/prune verbs are deliberately ABSENT (their
    IDE prompt is the human checkpoint matching their ``approved_by`` vouch, Law 3). ``server`` is the
    registered MCP name (default ``hive`` per the ``connect`` line; substitute the registered name)."""
    return "\n".join(f"mcp__{server}__{verb}" for verb in AUTO_APPROVE_TOOLS)


# The full, PROJECT-scoped install steps for the OPTIONAL persistence block — un-compressible
# verbatim install detail, so it rides the UNCAPPED payload only (never a capped field). Every
# write is project-scoped (the repo root, shared with the team) — NEVER a home/global file
# (operator-stated, load-bearing). The version stamp IS the marker; the re-onboard trigger is a
# contract_version mismatch. The actuation (the agent actually writing) is the agent's — the
# server only serves this.
ONBOARDING_PROCEDURE: str = (
    "OPTIONAL PERSISTENCE — install the contract as a versioned block so it survives context "
    "compaction within a session. It is an ENHANCEMENT over the always-on served floor; skipping it "
    "degrades safely to the floor. Every write below is PROJECT-scoped (the repository root, shared "
    "with your team) — never a home or machine-global file.\n"
    "0) Resolve YOUR runtime's rules file at the REPOSITORY ROOT (project scope):\n"
    + _RUNTIME_TABLE + "\n"
    "1) Write the served HIVEMIND-RULES block into that file BETWEEN its markers. If a "
    "HIVEMIND-RULES:START..END block is already present, REPLACE it in place (one block — no "
    "duplicate, no clobber of the surrounding content).\n"
    "2) [Claude Code only] Merge the lifecycle hooks AND the auto-approve allowlist into the PROJECT "
    ".claude/settings.json (the repo's settings file, never your home-directory or machine-global "
    "one). Auto-approve ONLY the read/surface verbs so they do not prompt per call:\n"
    + render_allowlist() + "\n"
    "   The write / supersede / prune verbs are deliberately omitted — their approval prompt is the "
    "human checkpoint that matches their approved_by vouch.\n"
    "3) [ALL runtimes] Install the edge CLI so mint / verify run identically on every "
    "harness: " + EDGE_CLI + "\n"
    "4) The version stamp IS the START marker (contract-version=" + CONTRACT_VERSION + "). You MUST "
    "RE-ONBOARD when a hive_* tool result's contract_version differs from your installed marker (or no "
    "block is installed): repeat steps 0-2 to reinstall the FULL contract — the HIVEMIND-RULES block, "
    "plus on Claude Code the lifecycle hooks AND the auto-approve allowlist — from the freshly served "
    "copies, and run `hive-edge upgrade` so the edge CLI meets the new MIN_EDGE_VERSION (unless a "
    "rollback pin is active — see EDGE CLI)."
)

# The capture-time mint directive — the full mechanics (un-compressible verbatim detail), served
# in the install payload. The floor itself carries only a one-liner pointer (`hive-edge mint`);
# this is the release valve for the exact call shape. The Claude-Code pre-capture hook AUTOMATES
# this exact call; a hookless harness runs it by hand — the hook adds TRIGGERING, never knowledge.
# `--repo` is resolved via `git rev-parse --show-toplevel`, never the raw cwd, so an anchor looked
# up from a subdirectory is not mis-rooted.
MINT_DIRECTIVE: str = (
    "When the anchor names a resolvable callable in your tree, MINT its fingerprints — "
    "`hive-edge mint --repo <repo-root> --anchor <file:symbol>` (resolve <repo-root> with `git "
    "rev-parse --show-toplevel`, never your raw cwd) — and pass the printed "
    "{\"combdrift/fp\": ..., \"matrix/subgraph_fp\": ...} map as hive_capture's meta (the interface "
    "fp and the dependency-neighborhood fp). Mint synchronously against the tree you just saw "
    "(0-false-stale). "
    "Where the served Claude-Code pre-capture hook is installed and firing it mints for you; run it "
    "yourself on every other harness, or if the hook is disabled or failed. No `hive-edge` on PATH -> "
    "install per EDGE CLI; still absent -> omit meta (capture is unaffected). "
    "When a memory is true ONLY on specific branch(es), add `--branch-scope` (tags the current "
    "branch) or `--branch-scope <names…>` (an explicit set) to the mint call: the resulting "
    "git/branches tag is a set-valued RELEVANCE SCOPE — it marks which lines the memory applies "
    "to, it is never auto-attached (no hook mints it) and never required (most memories are "
    "branch-agnostic; leave them untagged)."
)

# The census-feed explainer — census ingestion is SERVER-AUTOMATIC (U1): the operator connects
# the repo to the server-side sync daemon once, and tracked-branch movement lands as
# change_outcome evidence with nothing to wire per repo or per device. The directive still rides
# the install payload so agents are TOLD there is nothing to wire — the `hive-edge census init`
# verb this text used to command is DELETED from the edge CLI (0.7.0), and an untold agent would
# hunt for a wiring step or invent one. It also names the one manual path (the operator's
# existing `hive ingest` escape hatch) so nobody improvises a new verb.
CENSUS_DIRECTIVE: str = (
    "CENSUS: the change_outcome evidence feed is SERVER-AUTOMATIC. Once your operator connects "
    "this repo to the hive's server-side sync, merges AND direct commits on the tracked line "
    "land as `change_outcome` evidence (corroborate/contradict) for served memories and keep "
    "the per-repo code graph current — there is NOTHING to wire per repo or per device: no "
    "hooks to install, and no census verb exists in the edge CLI. The one manual path is the "
    "operator's escape hatch for one-off receipts: `hive ingest` (the `hive` server CLI, run "
    "on the hive host) feeds a census receipt into the same append-only ledger. If the feed "
    "looks dark, tell your operator — hive_health(include_census_health=true) carries the sync "
    "state — never attempt to wire census yourself."
)

# The recall-time verify directive — the full mechanics, served in the install payload (same
# release-valve split as MINT_DIRECTIVE). A `stale` verdict is REFERENCE-ONLY and CARRIES its own
# remediation options; a local point verdict WINS over a server `remediation` rider (the server
# tier is only the fallback for anchors you could not verify yourself). The advisory `radius`
# field is a SEPARATE channel: it warns that the anchor's dependency neighborhood moved and never
# touches the anchor verdict.
VERIFY_DIRECTIVE: str = (
    "Before ACTING on a recalled hit whose anchor names a file/symbol in your tree, verify it — "
    "`hive-edge verify --repo <repo-root> --anchor <file:symbol> [--fp <the hit's meta combdrift/fp>] "
    "[--subgraph-fp <the hit's meta matrix/subgraph_fp>]`. `stale` = the code moved past this "
    "memory: treat it as REFERENCE ONLY, do NOT follow it, and take the remediation options the "
    "stale verdict CARRIES (act now without approval: hive_outcome(hurt=[episode_id]); or escalate "
    "the human-gated retirement per BAD_VS_STALE — supersede/replaces for STALE, prune for BAD — "
    "never retire on your own judgment). The result may also carry an advisory `radius` field: "
    "`changed` = code in this memory's dependency neighborhood moved since it was stored — "
    "re-verify against the current code before relying on it — never a retirement trigger by "
    "itself (the memory may still be correct). "
    "`unverifiable`, or no `hive-edge`, -> fall back to the hit's last_verified stamp and the "
    "hive_health(include_stale_suspects) worklist. If the hit ALSO carries a server \"remediation\" "
    "rider and your own verify disagrees, YOUR point-local verdict wins — a local `current` overrides "
    "the server rider; treat the server tier strictly as the fallback for anchors you could not "
    "verify yourself, never a second vote on ones you could. Where the served post-recall hook is "
    "installed and firing the verify notice arrives annotated; verify manually on every other harness. "
    "When the hit's meta carries a git/branches tag, pass it as `--branches <the token>`: a "
    "`branch_scoped` verdict means the memory is scoped to another line — likely not relevant on "
    "your branch, NOT stale, and it carries no retirement options (disregard it unless you are "
    "working that line). And when an off-set hit verifies `current` on YOUR branch, the fact now "
    "holds here too: propose to your human a SUPERSEDING memory tagged with ALL relevant branches "
    "(`hive_write(replaces=…)` or `hive_supersede` — the human-gated flow; never retire the "
    "scoped original on your own judgment)."
)

# The per-hit remediation rider the SERVER attaches to a recall hit it already knows is stale
# (last_verified.state == "stale"), so a stale memory carries its options on EVERY harness with ZERO
# client tooling. Mirrors hive-edge's verify remediation vocabulary (each side asserts its OWN copy;
# the two are never diffed). hive_flag is deliberately ABSENT — it is a memory-vs-memory conflict tool
# (two episode ids), not an option for a single stale anchor. This rider is serving-side text, NOT
# part of the version-pinned bundle: a wording edit trips no version bump — an accepted, named drift
# risk, since the vocabulary (not the exact bytes) is what the parity tests hold on each side.
REMEDIATION_NOTICE: str = (
    "This memory's anchor no longer matches the code (stale) — treat it as REFERENCE ONLY, do not "
    "follow it. Options, by what you may do alone vs what needs a human: (1) act now, no approval — "
    "hive_outcome(hurt=[<episode_id>]) for this stale anchor. (2) Escalate a human-gated retirement, "
    "diagnosing per BAD_VS_STALE: if the memory has a SUCCESSOR (STALE), hive_supersede(loser, winner) "
    "or hive_write(replaces=...) carrying the corrected fact; if it was wrong even when written (BAD), "
    "hive_prune. ALERT your human — never retire on your own judgment."
)

# What is WORTH storing — the value bar. Rides BOTH the served taxonomy (below) and
# CONTRACT_FLOOR directly, one source, terse enough to fit a capped field.
VALUE_RUBRIC: str = (
    "Worth storing: DURABLE (true beyond this session), REUSABLE (a teammate hits this), "
    "NON-OBVIOUS, EVIDENCE-GROUNDED — name its anchor."
)

# The noise floor — the ONE definition of what NOT to capture. Rides the taxonomy (below) and
# CAPTURE_RECALL_FLOOR, so the two served copies cannot drift.
NOISE_FLOOR: str = (
    "DO NOT CAPTURE the obvious, transient, secret, or already-recallable. Litmus: saves time, "
    "not obvious -> capture."
)

# The fleet capture taxonomy — the one definition of WHAT to store. The value bar (VALUE_RUBRIC) +
# the kind vocabulary (kinds + body templates + the polar-language and density rules, RENDERED from
# hive.domain.kinds so it can never drift from the enforced enum) + the noise floor (what NOT to
# capture). The kind-vocabulary detail (body templates, polar-language/density rules) is
# un-compressible verbatim reference — it rides the install payload's `kind_taxonomy` key, not any
# capped field; the short kind GLOSS still rides the hive_write/hive_capture descriptions unchanged.
CAPTURE_TAXONOMY: str = VALUE_RUBRIC + "\n\n" + render_taxonomy() + "\n\n" + NOISE_FLOOR

# claude-code ONLY — OPTIONAL lifecycle hooks that give the nudges teeth. Merge into the PROJECT
# .claude/settings.json (never ~/.claude) WITHOUT clobbering existing keys. Two static echo nudges
# carry NO CLI dependency: UserPromptSubmit prints to STDOUT (claude-code injects it as context => a
# recall nudge + the contract_version drift reminder — a static echo cannot read the beacon, so it
# prompts the agent to compare live vs installed, model-driven self-heal, D5); SubagentStop prints a
# subagent-end capture reminder to STDERR. The four ENGINE events shell the one `hive-edge` console
# script (bare commands — no sh -c wrapper, no venv-reexec): PreToolUse mints the fp into a capture,
# PostToolUse verifies recalled hits, SessionStart records the work baseline, and Stop is the
# work-delta capture gate (BUG-017 three-valued). Correctness never depends on these — a hookless
# runtime runs the identical `hive-edge` calls itself from the floor text.
CLAUDE_CODE_HOOKS: str = (
    '{"hooks": {'
    '"UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": '
    '"echo \'[hivemind] Recall first: hive_recall the topic before building or re-deriving; prefer established memories over guessing. Re-onboard if a hive_* result contract_version differs from your installed HIVEMIND-RULES marker.\'"}]}], '
    '"PreToolUse": [{"matcher": "mcp__hive__hive_capture", "hooks": [{"type": "command", "command": '
    '"hive-edge hook pre-capture"}]}], '
    '"PostToolUse": [{"matcher": "mcp__hive__hive_recall", "hooks": [{"type": "command", "command": '
    '"hive-edge hook post-recall"}]}], '
    '"SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": '
    '"hive-edge hook session-start"}]}], '
    '"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": '
    '"hive-edge hook stop"}]}], '
    '"SubagentStop": [{"matcher": "", "hooks": [{"type": "command", "command": '
    '"echo \'[hivemind] Subagent finished — capture anything durable it learned with hive_capture before its context is gone.\' 1>&2"}]}]'
    '}}'
)

# The write-vs-capture decision rule — served verbatim in CONTRACT_FLOOR (and thus both delivery
# channels), one source, no drift. Terse: the fuller rationale for each clause lives in the
# surrounding CONTRACT_FLOOR prose, not repeated here.
WRITE_VS_CAPTURE: str = (
    "Write needs NOW-serve or human confirm — a LANDED FIX or truly open bug defaults to write. "
    "No approver available -> capture instead."
)

# The retire-diagnosis — STALE (replace) vs BAD (prune) — served verbatim in CONTRACT_FLOOR and in
# the hive_prune tool description, one source, no drift.
BAD_VS_STALE: str = (
    "STALE (has a SUCCESSOR) -> hive_supersede/replaces=; BAD (incorrect/misleading) -> "
    "hive_prune. Old ts: not a guarantee."
)

# The cross-platform capture/recall discipline: recall-before-edit-by-anchor + the task-end
# capture shape + the noise floor + the zero-capture escape + the advisory/deterministic tiering,
# as plain rules-file markdown valid VERBATIM in any runtime's project rules file. Rides
# CONTRACT_FLOOR — and through it the served initialize instructions — so every MCP client on
# every platform receives the same discipline. Terse: the mint/verify CALLS are named as
# one-liners here; their full mechanics (MINT_DIRECTIVE / VERIFY_DIRECTIVE) ride the install
# payload, not this capped-field-bound text.
CAPTURE_RECALL_FLOOR: str = (
    "RECALL BEFORE EDIT an anchor: hive_recall it, verify with `hive-edge verify`. TASK-END, "
    "per lesson: WHAT + WHERE as path/file.py:symbol + WHY; mint with `hive-edge mint`. NEVER "
    "hive_write autonomously at task end. " + NOISE_FLOOR + " Nothing clears the bar -> capture "
    "nothing: declining is compliance. ADVISORY text; DETERMINISTIC where hooks exist — same "
    "rules, harder trigger."
)

# The single canonical behavioral floor — composed ONCE from the sub-constants above and embedded
# VERBATIM into both delivery channels (SERVER_INSTRUCTIONS and the installed rules block), so
# there is exactly one source of behavioral text (Decision 2A). Fits under METADATA_FIELD_LIMIT by
# terse wording, never by dropping a required directive: what does not fit here rides the uncapped
# install payload instead (render_onboarding_payload) — the install procedure, the exact edge-CLI
# install/upgrade commands, the full mint/verify call mechanics, the per-kind body templates, and
# the hooks JSON are all un-compressible verbatim detail that belongs there, not here.
CONTRACT_FLOOR: str = (
    "Hivemind — a STIGMERGIC shared episodic memory for a FLEET (NO direct communication): "
    "hive_recall answers only when confident — no trace is served as a command. MAINTAINER: "
    "keep it LEAN — a FLOW not a stock, a BIGGER store is a WORSE one.\n\n"
    "EVERY TASK:\n"
    "- RECALL FIRST, SINGLE-POINTED — one intent per query, never bundle; grounded in the code "
    "you are actually touching. Empty/abstained: proceed, NEVER invent a memory.\n"
    "- hive_capture by default (VERIFIABLE EVIDENCE; lands quarantined); polarity=\"dont\" for a "
    "PROHIBITION.\n"
    "- WRITE serves immediately (fast-path): LOAD-BEARING facts need an approver's yes, unless "
    "AGI_MODE (approved_by=\"AGI_OVERRIDE\"), don't duplicate. " + WRITE_VS_CAPTURE + "\n"
    "- RESOLVE: ALERT your human, never retire on your own judgment — symmetric contradiction, "
    "ask which memory wins. hive_prune(episode_id). hive_outcome(hurt=[id]) — evidence "
    "only. hive_flag(a, b, kind=...) — advisory only. " + BAD_VS_STALE + "\n\n"
    + VALUE_RUBRIC + "\n\n"
    "Identity: per-session (Mcp-Session-Id) — never the bearer token.\n"
    + CAPTURE_RECALL_FLOOR + "\n\n"
    "PERSISTENCE — you MUST install the versioned block via "
    "hive_health(include_onboarding=true); RE-ONBOARD: no block installed, or contract_version "
    "differs from your installed marker. Missing/stale degrades safely to this floor."
)


def render_agent_rules_block() -> str:
    """The OPTIONAL, version-stamped agent-contract block an agent transcribes VERBATIM into its
    runtime's PROJECT rules file (D2 — the server authors it, the agent copies it; composing it from
    the agent's own understanding would drift at equal version). Wraps the single-sourced
    ``CONTRACT_FLOOR`` with a heading and a project-scope/re-onboard footer — no new drift source —
    and is marker-wrapped + version-stamped so a one-regex extract/replace is idempotent. Its exact
    bytes are pinned to ``CONTRACT_VERSION`` by the keystone hash (Law 1/7): silent drift at equal
    version is unconstructable. ENHANCEMENT layer — a missing or stale block degrades to the served
    floor (Law 6)."""
    return (
        RULES_START + "\n"
        "## Hivemind — shared fleet memory (contract " + CONTRACT_VERSION + ")\n"
        "This project is linked to a Hivemind MCP server (the hive_* tools). This block is the "
        "persistence copy of the contract the server also delivers at connect (the always-on "
        "floor) — keep it current.\n\n"
        + CONTRACT_FLOOR + "\n\n"
        "PROJECT-SCOPED: this block lives in THIS repo's rules file (never a home/global file). On "
        "Claude Code, auto-approve the read verbs (hive_recall, hive_capture, hive_health, "
        "hive_outcome, hive_flag) in the project .claude/settings.json; the human-gated hive_write / "
        "hive_supersede / hive_prune keep their approval prompt.\n"
        "RE-ONBOARD (required) when a hive_* result's contract_version differs from contract-version="
        + CONTRACT_VERSION + " in this marker (or this block is absent): call "
        "hive_health(include_onboarding=true) and reinstall in place — the block, plus on Claude "
        "Code the lifecycle hooks and the auto-approve allowlist.\n"
        + RULES_END
    )


# The block as a module constant (== the renderer's output; the renderer is the single author).
AGENT_RULES_BLOCK: str = render_agent_rules_block()


def bundle_digest() -> str:
    """SHA-256 of the installable contract bundle (the agent rules block + the claude hooks + the
    auto-approve allowlist) — the SINGLE owner of the keystone hash pinning the bundle to
    CONTRACT_VERSION (Law 1/7). The keystone test asserts it equals the golden literal and the
    pre-commit version guard regenerates that golden from this same function, so the hashed-byte
    composition has one definition and cannot fork across its consumers."""
    bundle = render_agent_rules_block() + CLAUDE_CODE_HOOKS + render_allowlist()
    return hashlib.sha256(bundle.encode("utf-8")).hexdigest()


# The full identity/auth reference — un-compressible verbatim detail, served in the install
# payload (a short identity NOTE, not this full paragraph, rides ONBOARDING_REFERENCE and
# CONTRACT_FLOOR instead).
IDENTITY_REFERENCE: str = (
    "Identity is per-agent-SESSION, resolved the SAME way on both doors: the server-minted "
    "`Mcp-Session-Id` any conforming client echoes, or an explicit `X-Hive-Agent-Id` header for "
    "readable provenance. A fleet of K agents promotes identically whether 1 or N engineers run "
    "it (the engineer/token count never enters promotion). The bearer token only AUTHENTICATES "
    "the remote (tunnel) door — it is never the identity; the local loopback door is tokenless."
)


def render_onboarding_payload() -> dict:
    """The structured install payload for the UNCAPPED ``hive_health(include_onboarding=true)``
    result channel (Decision 1A) — proven uncapped where the two capped metadata fields are not
    (BUG-023). Carries every piece of the contract too large or too install-mechanical for a
    capped field: the rules block, the full install procedure (with the full mint/verify call
    mechanics appended), the claude-code hooks, the auto-approve allowlist, the edge-CLI
    reference, the full identity/auth reference, and the full per-kind capture taxonomy.
    Structured (not one blob) so each piece is independently readable. Byte-inert when not
    requested (``_handle_health`` only calls this under the ``include_onboarding`` request
    flag)."""
    return {
        "contract_version": CONTRACT_VERSION,
        "rules_block": AGENT_RULES_BLOCK,
        "procedure": (ONBOARDING_PROCEDURE + "\n\n" + MINT_DIRECTIVE + "\n\n" + VERIFY_DIRECTIVE
                      + "\n\n" + CENSUS_DIRECTIVE),
        "hooks": CLAUDE_CODE_HOOKS,
        "allowlist": render_allowlist(),
        "edge_cli": EDGE_CLI,
        "identity": IDENTITY_REFERENCE,
        "kind_taxonomy": render_taxonomy(),
    }


# The served identity/auth + fetch pointer surfaced via the hive_health tool DESCRIPTION — a
# SHORT SECONDARY channel for clients that read tool descriptions. The always-on floor still
# installs nothing by itself; the versioned block is opt-in (§5 enhancement layer). The full
# reference (block + procedure + hooks + allowlist + edge CLI + identity + kind taxonomy) is the
# render_onboarding_payload() result, fetched via include_onboarding=true — never repeated here.
ONBOARDING_REFERENCE: str = (
    "Identity is per-agent-session (the server-minted `Mcp-Session-Id` any conforming client "
    "echoes, or an explicit `X-Hive-Agent-Id` header) — the bearer token only authenticates the "
    "remote door, it is never the identity. Full installable onboarding (contract "
    + CONTRACT_VERSION + ": the rules block, install procedure, hooks, allowlist, edge-CLI "
    "reference, and capture taxonomy) is served in THIS call's result under \"onboarding\" when "
    "include_onboarding=true — call it on first touch (no block installed) or whenever a result's "
    "contract_version differs from your installed marker."
)

# The server-level usage contract delivered via the MCP ``initialize`` result's ``instructions``
# field — the spec-canonical channel every client surfaces to the model at CONNECT time (no tool
# call, no skill, no manual step), so it reaches every connecting agent automatically. This is the
# foolproof delivery layer; the only REQUIRED client-side step is MCP registration itself. Single-
# sourced from CONTRACT_FLOOR — the identical text also composed into the installed rules block —
# so the two delivery channels cannot drift (Decision 2A).
SERVER_INSTRUCTIONS: str = CONTRACT_FLOOR
