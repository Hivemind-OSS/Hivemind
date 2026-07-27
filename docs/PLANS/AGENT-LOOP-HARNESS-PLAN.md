# AGENT-LOOP-HARNESS — mechanically closing the memory loop client-side

A shipped, adoptable harness that turns Hivemind's served discipline from **advisory** (the model
complies if it remembers to) into **mechanical** (the loop cannot be left open without a deliberate,
recorded decision). A runtime-agnostic core plus one thin per-framework adapter; Claude Code ships
first.

Status: PLAN — awaiting human confirmation. No code is written until it is confirmed.

Revised against the tree at `21484a7` (2026-07-25). The prior revision was written at `923c2d4`,
before the four commits that changed the retirement gate's own-line rule, the served staleness
rider, the secret-scan refusal envelope, and the mirror identity binding; §3b, §6, §7 and §12 are
the sections that moved as a result.

---

## 1 · Scope

### In scope (the intents)

| # | Intent |
|---|---|
| I1 | When a task engages the codebase, the agent **recalls before changing it**. |
| I2 | A **bundled recall is surfaced on the evidence it produced**, not predicted from its wording. |
| I3 | At task end a **store decision is made**: store the load-bearing lesson, or record that nothing cleared the bar. |
| I4 | Clearly-valuable-but-ambiguous-use memory goes to `hive_capture` rather than `hive_write`. |
| I5 | **Recall precedes every store** — to catch a duplicate or a rival before writing. |
| I6 | **Surfaced issues are resolved**: a hit the server itself marks actionable is answered with a maintenance verb that the server actually honored, or explicitly deferred. |
| I7 | `hive_outcome` records evidence when a recalled memory materially **helped** or **hurt**. |
| I8 | A memory stored during codebase work carries a **code binding** (`anchors`) or a **repo scope** (`repos`), or is explicitly declared general. |
| I9 | Enforcement is **hook-driven** wherever a hook can carry it. |
| I10 | Closure is **maximally enforced** — an open loop blocks the turn rather than merely warning. |
| I11 | The build is **modular**: a second codegen framework is an adapter, not a fork (§4e). |
| I12 | **Bare minimum** — only strictly necessary functionality ships (§4g names every cut). |

I2, I6 and I8 changed shape in this revision; §3b records what each now closes and §4g records why
the prior forms were cut.

### Explicitly out of scope

- **Codegraph currency / graph-before-grep.** Withdrawn. For the record: `hive/matrix/` is a
  *server-side* engine (AST fingerprints feeding drift verdicts) with no agent-facing query surface;
  the agent-facing codegraph here is the separate `graphify` CLI. §13 keeps the seam named.
- Any change to the server, the eight MCP verbs, the served contract, or the trust lifecycle.
  **This plan adds zero server behavior.**
- **Any attempt to influence hive identity.** See §2.5 — structurally refused, and already
  unnecessary.
- **The MAINTAIN leg** (`hive_health` worklists). It is a *fleet-maintainer* loop measured over the
  store, not a per-task loop measured over a session — and the harness's unit of enforcement is the
  session (§2.5). §3b records it as a deliberate non-goal rather than an omission.

---

## 2 · The tension, stated plainly, and its resolution

`CONTEXT/THEORY.md` §5 and `CONTEXT/INTERACTIONS.md` §9 **deliberately abolished the entire
client-side surface**:

> "there is no `hive_init` handshake, no installable rules block, no client-side hooks, no
> allowlist, no version marker, and no re-onboard loop — nothing client-side exists to drift."
> — THEORY §5
>
> "Also refused by construction: any onboarding / install / re-onboard interaction (nothing
> exists client-side to install or heal)." — INTERACTIONS §9

That refusal is load-bearing: v3 killed client onboarding because **a client-side copy of the
contract forks from the served floor and the server has no way to heal it.** This plan puts software
back on the client. The cost is paid explicitly, not waved away.

It is payable because the refusal targets a *specific* failure — a second copy of the contract the
server is expected to keep current. Three laws make this harness structurally incapable of being
that thing:

**Law H1 — No second contract.** The harness states **zero** hive semantics. It never says what
`provisional` means, when demand promotes, what clears the storage bar, or how to choose a verb. It
emits only three kinds of fact: (a) what it mechanically observed, (b) which decision is still
unmade, (c) where the authority lives ("per your served hive contract"). Enforced mechanically — a
test scans every harness source file and every emitted reason string for a named semantic vocabulary
and fails on a hit (CT-H10). The served `initialize.instructions` remains the only contract.

**Law H2 — DETECT-only, no trust handle, no transport.** The harness never calls a hive verb, never
opens a socket, never opens its own MCP session, and holds no store handle. It observes hook
payloads and blocks or allows a tool call *the agent itself chose to make*. Consequences: THEORY §10
O7 (no unbidden retirement) is untouched, and Law 3's anti-gaming currency stays clean — a harness
that opened its own session would mint a second identity and could manufacture the
identity-diversity the demand rung requires. It has no transport at all, so it is on the far side of
that line by construction.

**Law H3 — Coupling is pinned by a red test.** The only coupling to the server is a small set of
*names and shapes*: the verb names, the recall envelope shape, the retirement-qualifying drift
tier, and the affirmative-status vocabulary. All are pinned — the harness verb tuple must equal
`hive.app.tool_defs.TOOL_NAMES` and its qualifying-drift tuple must equal
`hive.domain.retirement.QUALIFYING_DRIFT` (CT-H8) — and the envelope parser is fed a **real** recall
response from the real `HiveMCPServer` over a real temp store, the substrate
`tests/contract/conftest.py` already provides (CT-H9). A renamed verb, a widened tier, or a moved
key is a **failing test in this repo**, not silent client rot. That is Law 7 applied to the harness
itself.

**A note on the boundary H3 does *not* cross.** A name the harness can pin by importing a server
constant in its test is a coupling; a *grammar* it would have to restate is a second contract. The
anchor grammar is the live example: `hive/app/anchors.py` accepts `anchor` as free text, so a
single-colon `path/file.py:Symbol` is stored silently and then joins nothing
(`change_evidence._anchor_match_level` partitions on `"::"` alone — the BUG-058 mechanism, whose fix
corrected what is *advertised*, not what is *accepted*). Gating that client-side would mean copying
a grammar the server owns, with no import to pin it against at runtime. It is therefore recorded as
a **server-side gap** (§13), not built here.

**What is genuinely given up:** an adopter who installs and never pulls again holds stale software.
Honest mitigation, not a fix: because of H1 the harness carries no contract text, so staleness costs
*enforcement*, never *correctness* — a stale harness under-blocks and degrades toward the
pre-harness advisory behavior. That direction is the safe one (Law 6), and it is why H1 is
non-negotiable.

**Doc obligations follow (§12 step 10):** THEORY §5 and INTERACTIONS §9 are edited in the same change
to name the harness and its three laws, since the code is truth.

---

## 2.5 · Application, distribution, and identity

### The unit of enforcement is the SESSION

Hooks fire on one session's events; the ledger is keyed `(session_id, agent_id)`. Installing is not
an alternative to that — installing is how you **select which sessions get it**.

**Ships as a Claude Code PLUGIN, not a settings merge.** This is adopted from
`activeloopai/hivemind`'s harness layout (§2.6), and it is strictly simpler than the settings-merge
install originally planned here. Verified against Claude Code **2.1.220** (§12 step 8 records the
verification method):

- a plugin is `.claude-plugin/plugin.json` + `hooks/hooks.json` at the plugin root, and **plugin
  hooks respond to the same lifecycle events as user-defined hooks** — no behavioral difference;
- `${CLAUDE_PLUGIN_ROOT}` resolves to the plugin's install directory, so the hook command needs no
  path discovery, no copy, and no absolute path baked into a settings file;
- `${CLAUDE_PLUGIN_DATA}` (`~/.claude/plugins/data/{id}/`) is a **persistent** directory that
  survives plugin updates — named as such by `claude plugin uninstall --keep-data` ("Preserve the
  plugin's persistent data directory") — and is the correct home for the ledger.
  `${CLAUDE_PLUGIN_ROOT}` is explicitly ephemeral, so state must not live there;
- dev/adoption without a marketplace: `claude --plugin-dir harnesses` for a session, or install from
  the local path.

**What this deletes** from the previous plan — the whole reason to adopt it: the settings-file
marker-reconcile, the BUG-043 duplicate-hook/orphan concern, the `~/.claude/hive-loop/` copy, the
installed-vs-repo divergence check, `install --user|--project|--emit`, and `uninstall`. `loopctl`
disappears entirely (§4g). Plugin install/update/uninstall is the platform's job, and it versions
the artifact for us.

| Selector | Reach |
|---|---|
| plugin installed (user scope) | every session on the machine — **the primary path** |
| `claude --plugin-dir harnesses` | that one invocation — dev and CI |
| `claude --setting-sources` / `--bare` | can exclude settings hooks; see the bound below |

**The bound on "maximally enforce," stated plainly:** a session is governed only if it loaded the
plugin. `--bare` skips hooks entirely (its own help text: "Minimal mode: skip hooks, LSP, plugin…").
So enforcement is strong *within* a governed session and bypassable *at launch*. No hook can close
that, and it should not: it is what keeps the bench measurable (BUG-015's landed fix — a user-scope
Stop hook silently replacing every measured answer).

| Artifact | Carries the harness? | Why |
|---|---|---|
| `git clone` of this repo | **yes** — `harnesses/` is tracked | same posture as `skills/`: shipped source, explicit adoption |
| built wheel | no | `packages.find include = ["hive*","tests*"]` excludes `harnesses*` |
| server image | no | the Dockerfile copies `hive/` only |
| **active enforcement** | **no, until the plugin is installed + session restart** | a clone yields inert files |

Clone-gets-it is necessary but not sufficient, because **agents do not clone this repo — operators
do.** A fleet agent works in `some-other-repo` and reaches the server over MCP; it never sees this
tree. A user-scope plugin install covers every repo the operator's agents touch, and **§4d's arming
predicate is what makes that safe** — a repo with no reachable hive is byte-inert. The one residual
cost of that reach is the BUG-015 vector, mitigated three independent ways: the landed
`--setting-sources ""` bench fix, `HIVE_LOOP__ENABLED=0`, and the arming predicate (CT-H12 + the
live tier).

### 2.6 · Compared with `activeloopai/hivemind` (reviewed 2026-07-24)

A different product that happens to share the name: TypeScript, Deeplake-backed, captures every
session's prompts/tool-calls/responses as traces and codifies them into shareable skills. Its tree
is `harnesses/{claude-code,codex,cursor,hermes,openclaw,pi}` — though `cursor/` is only a `.gitkeep`
and `hermes/` only `skills/`, so the breadth is partly aspirational.

**Same as this plan:** the `harnesses/<framework>/` convention itself; shared logic behind
per-framework wiring; one user-scope installer.

**Adopted from them:** the plugin packaging above, and `async: true` on hooks that never need to
block — **now verified present** in 2.1.220's hook-config schema (§12 step 8), which is what makes
§4d's arming move affordable.

**Deliberately not adopted, and why — these are not simplicity trade-offs but structural
unavailability:**

| They do | This cannot, because |
|---|---|
| `capture.js` on `UserPromptSubmit`/`PostToolUse`/`Stop`/`SubagentStop` — **auto-capture every prompt, tool call, and response** | it contradicts this server's core stance, served in its own contract: "keep it LEAN — a FLOW not a stock, a BIGGER store is a WORSE one," plus the DURABLE/REUSABLE/NON-OBVIOUS storage bar. It would also flood the demand signal that promotion is measured from (Law 3) |
| `recall.js` on `UserPromptSubmit` — **auto-inject recall results** before the model sees the prompt | a harness that *calls* recall needs transport, which violates H2, mints a second identity, and corrupts the anti-gaming currency the demand rung depends on. It would also formulate the query *for* the agent |
| 9 bundled entry points (`session-start.js`, `capture.js`, `recall.js`, `pre-tool-use.js`, `graph-on-stop.js`, …) | one dispatcher switching on the event name is fewer moving parts and puts every rule in one readable file (§4b). This is the one axis where **this plan is the simpler of the two** |

**The honest summary of "which is simpler":** theirs is simpler as *packaging* — so that half is now
copied verbatim. Theirs is also simpler *for the agent*, because it does the work instead of
requiring it; that half is unavailable here, since this server's trust is mechanical and derives
from what agents actually demand and report. This plan is simpler as an *enforcement engine*: one
entry point instead of nine, two env vars, no cloud, no trace store, no transport.

### Identity: the requirement is already met, and the harness must not touch it

The ask — *new sessions must count as different agents* — is **already true, and the harness is
structurally forbidden from participating.**

- **Forbidden:** INV-2 (THEORY §5) — "the caller cannot assert identity … `proposed_by` is *always*
  the transport-resolved per-session identity (a header or the session id, never a tool argument);
  there is no client *tool* field for it." There is no argument on `hive_write` for a session, so
  the harness cannot pass one. Identity resolves at the transport, before `handle()` (INV-1).
- **Already true — measured, not assumed:** with `autonomy.enabled=True` and `n_misses_7d=7`
  clearing `demand_m=3`, `hive_health` returned **no `solo_hint` key**. Per
  `hive/app/mcp_server.py:_solo_hint` (line 1304) the only remaining path to `None` is
  `len({m.agent_id for m in misses}) > 1` — so **≥2 distinct identities are already recorded on
  this store.** Each new session's MCP `initialize` mints a fresh `Mcp-Session-Id` which the client
  echoes, and that is the identity. Nothing client-side is needed.
- **What the harness therefore owes:** nothing to hive identity, and one thing to itself — key its
  ledger by `(session_id, agent_id)`, both of which ride the hook payload, so a user-scope install
  never merges two sessions' loops and a subagent's loop is its own. **This is a loop-scoping
  choice, not a mirror of hive identity:** hive identity is per *session*, so a subagent shares its
  parent's `Mcp-Session-Id` while getting its own ledger. The consequence is real and accepted — a
  subagent that changes code must recall for its own work even if its parent already did. That is
  the correct direction (a subagent that edits blind is exactly the case I1 exists for), and it is
  asserted rather than assumed (CT-H12).
- **Deliberately not built:** no `solo_hint` mirror, no identity check, no health read. The server
  already surfaces identity collapse; duplicating it client-side would be a second contract (H1)
  and would need transport (H2).

---

## 3 · The mechanization limit (read before scoring the design)

Four intents are **graded judgments a hook cannot verify**: I3 ("is this lesson load-bearing?"), I4
(write-vs-capture), I7 (helped-vs-hurt — `hive_outcome()` with empty arrays is a legitimate answer,
so "an outcome call happened" is satisfiable without judgment), and the general-memory arm of I8
(the served contract explicitly allows "neither = general", so an untagged store can be correct).

The harness therefore enforces **the decision point, never the decision**: it makes skipping the
question impossible and answering it well cheap (the block reason names the exact ids in play).
Claiming more would be a lying contract. Every affected contract test asserts the gate fired and a
decision was recorded — never that the decision was correct. Judgment is checked only in the live
tier (§10c), graded against a rubric at suite level.

**The corollary that shapes §7:** where a rule *can* be decided from evidence the server already
produced, it is decided there rather than predicted from wording. A heuristic that predicts intent
from text can only trade false denies against false allows, and a false deny on a hive verb has no
user override and teaches the agent that the loop is hostile — the exact opposite of the goal. §4g
records the two rules cut on this basis and the one that was moved.

## 3b · Closure matrix — every leg of the served contract, and what closes it

The served contract (`hive/app/contract.py:SERVER_INSTRUCTIONS`, plus the per-hit
`REMEDIATION_NOTICE` at `:66`) is the authority. This table is the completeness argument: each of
its legs is either mechanically closed by a named mechanism below, or recorded here as
un-mechanizable / out of scope. Nothing is silently absent.

| Served leg | Mechanism | Closed |
|---|---|---|
| "RECALL FIRST" | **G1** denies a mutation while the session holds no recall; **D1** is the turn-end backstop for mutations observed before the session armed | ✅ for code-changing sessions |
| "SINGLE-POINTED — never bundle" | **F1** feedback on a recall that both carried ≥2 intents **and** abstained — the server's own abstain is the evidence | ✅ post-hoc, zero false positives |
| recall scope (`repos` / `anchor_prefix`) | — | ⛔ un-mechanizable: a GLOBAL recall is contract-legal, so no scope is wrong |
| "Empty/abstained: proceed, NEVER invent" | — | ⛔ un-mechanizable: requires judging the answer |
| "Drifted = REFERENCE ONLY — re-verify" | **D3** opens on a served hit the server itself marks actionable: `drift.type ∈ QUALIFYING_DRIFT`, a `remediation` rider (keyed off that same verdict), or an id in the `conflicts` note | ✅ |
| "STORE, per durable lesson" | **D4**, closed by a store verb that landed or a `no-store` sentinel | ✅ decision point (§3) |
| "WHAT + WHERE + WHY … anchors / repos / neither = general" | **D5** opens when a store landed in an armed session carrying neither `anchors` nor `repos`; closed by a `general` sentinel | ✅ decision point (§3) |
| write-vs-capture | **D4**'s reason names both verbs and defers the choice to the served contract | ✅ decision point (§3) |
| "Don't duplicate — recall first" | **G2** denies a store while the session holds no recall | ✅ |
| "nothing else clears the bar -> store nothing" | **D4**'s `no-store` sentinel is a first-class close, not a bypass | ✅ |
| "OUTCOME, task-end" | **D2** opens when a recall served ≥1 hit and no outcome call followed | ✅ |
| "RETIRE (machine-gated) … unqualified = benign no-op" | **D3**, credited only on an **affirmative** envelope status (§7), so a no-op call cannot close it | ✅ |
| "hive_flag is advisory only" | **D3** accepts a `recorded` flag as a close — a recorded advisory *is* a decision, and the harness does not rank verbs (H1) | ✅ |
| "MAINTAIN: hive_health worklists" | — | ⛔ out of scope (§1): a fleet-maintainer loop over the store, not a session loop |

Two rows carry the whole "is it minimal?" answer: the three ⛔ rows are each un-mechanizable or
out-of-unit, and every ✅ row is carried by exactly one mechanism. **2 gates, 2 feedbacks, 5 debts,
3 sentinels** — no mechanism appears twice, and removing any one opens a row.

---

## 4 · Design

### 4a · Options considered

**Option A — one script per hook event** (`session_start.py`, `pre_tool.py`, `post_tool.py`,
`stop.py`): the obvious mapping from `settings.json` to files.

Rejected. **Temporal Decomposition** by the textbook definition — structure follows *when code runs*
rather than *what knowledge each unit owns*. The ledger schema would be known by four files
(**Information Leakage** → change one key, edit four), each re-implementing payload parsing, arming,
and the fail-open guard (**Repetition**), and "what are the rules?" would cost four file reads. It
also has no seam for a second framework: a Cursor harness would fork all four. Reasoned scores:
complexity 6, cognitive load 6, leakage 7, extensibility 4, agent-navigability 5.

**Option B — a pure decision core behind a thin per-framework adapter** — the shape this repo is
already built on (Law 4). Chosen.

### 4b · The winner, and why it beat A

```
   harnesses/adapters/claude_code.py    ← ALL I/O + ALL Claude Code vocabulary
            │ depends on ↓   (never ↑)
   ┌────────────────────────────────────────────────┐
   │ core/decide.py   PURE. every rule. one table   │
   │ core/events.py   frozen Event / Decision / Env │
   │ core/hive.py     the hive coupling, single-owned│
   └────────────────────────────────────────────────┘
            ↑ serialized by
     core/state.py   (LoopState ⇄ disk, atomic, versioned)
```

- **Deep module** (principle 4): `decide(event, state, env) -> (Decision, LoopState)` — one pure
  reducer, narrow surface (three frozen inputs), behind every rule in the system.
- **Info leakage → 2/10**: ledger schema known only by `state.py`; hook wire format and every
  Claude-Code tool name known only by `claude_code.py`; hive verb names, envelope shape, the
  qualifying-drift tier and the affirmative-status set only by `hive.py`.
- **Cognitive load → 3/10**: "what are the rules?" is one file, headed by a single `dict` literal
  mapping event kind → rule. That keeps Option A's one virtue (a readable event→behavior map) on one
  screen instead of four files.
- **Extensibility → 9/10** (principle 15 — increments are abstractions, not features): see §4e.
- **Agent-navigability → 8/10**: small self-contained files; contracts enforced by frozen
  dataclasses + `__post_init__` + truth tables, not prose.
- **Fit** — the decisive margin: it mirrors the hexagon the repo already is, so an agent that knows
  Hivemind already knows the harness.

Reasoned scores anchored to the rubric, not measurements.

### 4c · Errors defined out of existence (principle 11 — the wedge-proofing)

BUG-017 was precisely this harness's failure mode: a Stop hook blocking the first turn of a session
over pre-existing dirt, costing a turn and a compliance paragraph. Four structural guarantees, not
four remembered cautions:

1. **Three-valued ledger state.** Missing ≠ recorded-clean ≠ recorded-with-debt. Missing or
   unparseable ⇒ `inert`, never a debt. (Direct BUG-017 lesson.)
2. **One block per debt, hard-coded.** Each *distinct* debt blocks at most once per session; after
   that it degrades to advisory. A stubborn or confused agent can always finish — wedging is
   unconstructable, not avoided. Not a knob (THEORY §9 #14) and the reason `waive` was cut (§4g).
3. **One block per turn-end, naming EVERY open debt.** With five debt keys, checking them in order
   and blocking on the first would serialize up to five blocked turns for one session's dirt. A
   single block that names the whole open set costs one turn and carries strictly more information;
   every named key is marked blocked in the same write. A debt that first opens *after* that block
   may block once more, which is the honest bound — never a fifth turn spent on the harness.
4. **`stop_hook_active` short-circuit**, and **inert unless armed** — `decide` returns `inert` when
   the ledger says the session is not armed, so there is no arming precondition a caller could
   forget.

`HIVE_LOOP__ENABLED=0` is the kill switch that makes every hook byte-inert (the repo's
byte-inert-when-off idiom).

### 4d · Arming: observation, never prediction — and off the blocking path

"Relates directly to the codebase" is mechanized as **observed engagement**, not prompt
classification — the existing `design-review-contract.py` classifies by regex and misfires both
ways, and a wrong classification here would either wedge a conversational turn or silently skip a
real one.

The session arms when **both** hold:
- a repo-relative **source file** was read, globbed, grepped, or edited (a source extension under
  `cwd`, excluding `graphify-out/`, `CONTEXT/`, `docs/`, and dotdirs); **and**
- **hive is reachable in this session** — proven by an observed hive tool call, or inferred from a
  `hive` MCP server entry in the project's `.mcp.json` / settings (a local file read; H2 forbids a
  probe).

Both false ⇒ every hook inert. This also honors the standing rule that hive is for codebase-direct
work only: a business/strategy/conversational session is byte-inert.

**Arming is observed at `PostToolUse`, not `PreToolUse`.** Nothing about arming needs to deny a
call, and observing it post-hoc is what removes `Read|Glob|Grep` — the highest-frequency tools in
any session — from the blocking matcher entirely (§4f). The one-call lag this introduces is
immaterial: a session that arms on its first source read has armed before the mutation or store that
G1/G2 gate, because those are different, later calls.

### 4e · The portability seam (I11) — what a second framework actually costs

`core/` contains **no framework vocabulary**. Two normalizations buy that, and they are the whole
abstraction:

1. **`Event.kind`** — a closed vocabulary the core owns: `SESSION_RESUMED | TOOL_PRE | TOOL_POST |
   TURN_END`. The adapter maps its framework's event names onto these four (Claude Code:
   `SessionStart` → `SESSION_RESUMED`, `Stop`/`SubagentStop` → `TURN_END`, everything unmapped ⇒
   dropped). The core never learns that "Stop" is a word.
2. **`Event.role`** — a closed vocabulary the core owns: `MUTATE | READ | OTHER`, with a hive call
   carried separately as `Event.hive_verb`. The adapter classifies its framework's tool names into
   it, so `Edit|Write|MultiEdit|NotebookEdit` and the mutating-`Bash` regex live in
   `claude_code.py`, never in the core. The core reasons about roles.

**Adding a framework = one new `adapters/<name>.py` (payload→Event, Decision→that framework's
response shape, `main()`) plus that framework's own wiring file. Zero core edits, zero core test
edits.** Pinned by CT-H13: the core is scanned for framework vocabulary (`hook_`, `Stop`,
`PreToolUse`, `tool_name`, `Edit`, `NotebookEdit`, `permissionDecision`, …) and fails on a hit — the
same mechanical technique as H1, so the seam cannot rot into a Claude-Code-only core.

The honest limit: a framework with **no blocking hook** (advisory-only callbacks) can host the
journalling and the context injection but not the teeth. That is a property of the framework, not a
design gap; the adapter's `Decision`→response mapping degrades `deny`/`block` to advisory context
and says so at install time.

### 4f · Latency: measured, and what it buys or costs

**Measured** (20 sequential invocations of a realistic adapter shape — payload parse + ledger read +
`*.partial` + `os.replace`): **~34 ms per invocation**, dominated by Python interpreter startup
(`python3 -S -E` shaves it to ~29 ms; the work itself is sub-millisecond).

- **`timeout: 5` in `hooks.json` is a hang guard, not a budget** — 5000 ms against a 34 ms
  operation. It exists so a stalled filesystem on the ledger write cannot hold a turn open, not
  because the work is slow.
- **The real cost is invocation VOLUME, not duration**, and it is driven entirely by how broad the
  **blocking** matcher is. A `PostToolUse` hook carrying `async: true` is off the turn's critical
  path, so breadth there is cheap; breadth on `PreToolUse` is not.
- **No existing precedent in this repo.** A previous revision claimed the cost profile was "already
  the accepted baseline" because of two `PreToolUse` graphify hooks. That is **false as of
  `21484a7`**: this repo's `.claude/settings.json` carries `"hooks": {}`, and the user-scope hooks
  present are `PostToolUse` on `Write|Edit`, `SessionStart`, and `Stop` — none on the read path. The
  latency budget below therefore stands on its own measurement, not on precedent.

**The resulting matcher split**, which is the whole of the cost decision:

| Hook | Matcher | Blocks? | Cost |
|---|---|---|---|
| `PreToolUse` | `Edit\|Write\|MultiEdit\|NotebookEdit\|Bash\|mcp__hive__hive_write\|mcp__hive__hive_capture` | yes (G1, G2) | ~34 ms on mutations and stores only — a small fraction of any session's calls |
| `PostToolUse` | `Read\|Glob\|Grep\|Edit\|Write\|MultiEdit\|NotebookEdit\|Bash\|mcp__hive__.*` | never | `async: true` ⇒ off the critical path |
| `SessionStart` | — | never | once |
| `Stop` / `SubagentStop` | — | yes (debts) | once per turn |

The broad matcher is on the hook that cannot block and does not wait. **The harness is absent from
the read hot path**, which is what dropping the orientation budget (§4g) bought and what the
verified `async: true` makes free.

### 4g · Bare minimum (I12) — everything cut, and what survived scrutiny

**Cut**, with the reason each is not strictly necessary:

| Cut | Why it isn't necessary |
|---|---|
| **`loopctl.py` in its entirety** — install / uninstall / check / emit / status / waive / dry-run | plugin packaging (§2.5) makes install, update, uninstall, and versioning the platform's job. `status` was already weak (a CLI cannot resolve the current `session_id`); `waive` was belt-on-belt over §4c #2; `check` existed only to detect the vendored-copy drift that plugins remove |
| the settings-merge installer + marker reconcile | deleted with `loopctl`; BUG-043's failure mode is now structurally absent |
| `audit.jsonl` + rotation | useful for tuning, never for closing the loop |
| the `SessionEnd` hook | its only jobs were audit + ledger deletion; a stale ledger is inert (session ids never repeat) and pruned lazily by mtime on load. Hooks 5 → 4 |
| **the orientation budget** — deny the 4th `READ` while no recall exists | it was the **sole** reason the harness sat on `Read\|Glob\|Grep`, at ~34 ms per call on the highest-frequency tools in any session. Its intent (I1) is fully carried by G1, which denies the mutation itself — the recall still happens before the code changes; only the wasted exploration tokens before it are unrecovered. Paying a blocking hook on every read to save tokens is the wrong trade, and the §4d arming move lands with it |
| **the single-pointedness *gate*** — deny a recall whose text carries ≥2 intents | §4d refuses regex classification for arming because it "misfires both ways" — and this was the same technique, applied with a `deny` that has no user override, to the one verb the loop most needs the agent to keep using. It is also redundant with a signal the server already emits: a bundled query **abstains**, which is self-correcting evidence. Retained as **F1**, a `PostToolUse` feedback conditioned on `abstained == true` — it fires only when the bundle actually failed, so its false-positive rate is zero and it costs no blocking call |
| **token-overlap on recall-before-store** — deny a store unless a journaled recall shares ≥2 content tokens with its `text` | the harness cannot embed, so it cannot judge topic; the rule could only approximate it. The failure mode is not a wasted call but **active harm**: the cheapest way past a false deny is a decoy recall worded to match the pending store text, and `hive/domain/recall.py:335` records "a miss on every non-answer" with `:631` firing `lifecycle.on_miss(vec, agent_id)` synchronously — so a decoy miss lands near the vector about to be written and can promote *another identity's* provisional memory through the demand rung. That is the anti-gaming currency H2 exists to protect. Reduced to **G3**: a store requires ≥1 journaled recall in this session, which is I5's actual content and cannot be gamed into existence |
| `HIVE_LOOP__ORIENT_BUDGET` knob | the budget it configured no longer exists |
| `HIVE_LOOP__BLOCK_BUDGET` knob | hard-coded 1 — THEORY §9 #14: one right answer belongs in code, not exposed to be mis-set |
| a custom ledger path scheme | `${CLAUDE_PLUGIN_DATA}` is the documented persistent dir; the invented XDG path is gone |

**Added**, the one net addition, with the closure hole it fills:

| Added | Why it is necessary |
|---|---|
| **D5 `scope_missing`** — a store that landed in an armed session carrying neither `anchors` nor `repos` opens a debt, closed by a `general` sentinel | an untagged memory is inert in **every** server mechanism at once: `hive/app/drift.py` reads its drift as `n/a` (nothing to verify), it never joins the census subject feed, so it earns no `change_outcome` and therefore no `outcome_verified_helped`, so it can never reach `established` — and it dies `deprecated` at the 45-day provisional TTL while still being correct. That is the same silent-death class as BUG-058, on the store side, and nothing else in this design detects it. The check is pure shape (are the carriers present?), zero judgment; the *decision* — that a lesson really is general — stays with the agent via the sentinel, per §3 |

Result: **4 hooks, 2 env vars, 5 modules, 1 executable entry point, no CLI. 2 blocking gates,
2 feedbacks, 5 turn-end debts, 3 sentinels.**

**Kept under protest, with the reason — overturn any of these and the plan shrinks further:**

- **`SessionStart`** — the *only* mechanism by which debt survives compaction. Without it a long
  session silently loses its loop, which is the closure hole most likely to matter.
- **D1 `recall_missing`** — near-redundant with G1, which denies every mutation until a recall
  exists. It survives as the backstop for the one reachable gap: mutations observed *before* the
  session armed (e.g. a `docs/` edit, then a source edit). It is three lines; the alternative is a
  closure hole with no owner.
- **Refused-store parsing** — necessary for *correctness*, not polish: a refused `hive_write` must
  not falsely close D4.
- **Affirmative-status parsing on maintenance verbs** — the same argument one level up. The server's
  retirement is machine-gated and an unqualified call returns `{"status": "noop"}`
  (`mcp_server.py:1092`). Crediting D3 on the *call* rather than the *outcome* would teach agents to
  fire ritual no-op retirements at a client-side gate — the precise anti-pattern the refused-store
  rule already refuses to permit for stores.

Both `loopctl check` and `loopctl uninstall` were previously in this list; plugin packaging removed
the problems they solved, which is the clearest evidence the §2.6 adoption was the right call.

---

## 5 · File tree

**`harnesses/` IS the plugin root.** A plugin install copies the plugin directory, and per the
reference "only symlinks that resolve **within** the plugin's own directory are preserved" — so a
`harnesses/claude-code/` plugin could not reach a shared `harnesses/core/` sibling without a build
step or a vendored duplicate. Making `harnesses/` itself the root keeps one self-contained artifact,
no build step, and no duplication; the cost is that a second framework's adapter (a few KB of Python)
rides along in the Claude Code plugin, which is acceptable and arguably desirable.

```
harnesses/
  .claude-plugin/
    plugin.json                   # name: hive-loop, version, description, license
  hooks/
    hooks.json                    # 4 events → one command, via ${CLAUDE_PLUGIN_ROOT}
  __init__.py
  README.md                       # the three laws; the §4e seam; adoption; how to add a framework
  core/                           # runtime-AGNOSTIC. no framework vocabulary (CT-H13).
    __init__.py
    events.py                     # frozen Event (normalized) / Decision / Env
    state.py                      # LoopState + versioned atomic load/save
    hive.py                       # SINGLE owner of the hive coupling: verb names, envelope
                                  #   parsing, qualifying-drift tier, affirmative statuses
    decide.py                     # PURE rules + the Event.kind → rule table
  adapters/
    __init__.py
    claude_code.py                # payload → Event; Decision → hook JSON; main(). ALL I/O, ALL CC names
tests/
  harness/
    __init__.py
    test_decide_rules.py          # pure truth tables at every boundary
    test_state_ledger.py
    test_events_parsing.py        # property-based
    test_seams.py                 # CT-H8 pins + CT-H13 core-purity scan, fast tier
  contract/
    test_agent_loop_harness.py    # FROZEN contract suite, CT-H1..CT-H13
```

Five modules, one executable entry point (`harnesses/adapters/claude_code.py`), two declarative
files. No CLI. **That path is the single spelling** — a previous revision spelled it three
inconsistent ways across §5, §9 and §12; the entry point is invoked as
`python -m harnesses.adapters.claude_code` in tests and by absolute path under
`${CLAUDE_PLUGIN_ROOT}` at runtime.

`harnesses*` is already excluded from the wheel by `[tool.setuptools.packages.find] include =
["hive*", "tests*"]` — no packaging change (the mechanism that already excludes `scripts/`). The
tree is importable as `harnesses.core.*` / `harnesses.adapters.*` for `mypy --strict` and the test
tiers, and the hook command invokes the adapter by path under `${CLAUDE_PLUGIN_ROOT}`, so runtime
needs no package install.

---

## 6 · The state model

`core/state.py`, the only module that knows this shape:

```python
@dataclass(frozen=True, slots=True)
class LoopState:
    version: int = 1                        # unknown version ⇒ treated as absent (silence, never crash)
    armed: bool = False                     # §4d both clauses satisfied
    hive_seen: bool = False                 # a hive tool call was observed
    recalls: int = 0                        # recall calls observed (I5's precondition — a COUNT,
                                            #   not the query text: nothing reads the text now)
    mutations: int = 0                      # MUTATE-role calls observed
    served: tuple[int, ...] = ()            # episode_ids returned in reference_context
    actionable: tuple[int, ...] = ()        # served ids the SERVER marked actionable — see below
    outcome_after_serve: bool = False       # an outcome call followed the last serving recall
    stored: int = 0                         # store calls whose envelope was NOT a refusal
    unscoped_store: bool = False            # a landed store carried neither anchors nor repos
    maintained: tuple[int, ...] = ()        # ids in a maintenance call the server AFFIRMED
    deferred: tuple[int, ...] = ()          # ids deferred by a final-message sentinel
    no_store_why: str = ""                  # the recorded "nothing cleared the bar" rationale
    general_why: str = ""                   # the recorded "this lesson is general" rationale
    blocks: tuple[str, ...] = ()            # debt keys already blocked once
```

**`actionable` replaces the previous `drifted` ∪ `conflicted` pair, and narrows it.** The prior
revision opened the maintenance debt on any `drift ∉ {"fresh","n/a"}`. That set includes
`unverifiable` and `branch_scoped`, and the server refuses to act on either:
`hive/domain/retirement.py:79` — *"fresh/branch_scoped/unverifiable are never in it: unverifiable is
the fail-safe unknown, and unknown never retires"* — and `INTERACTIONS.md:501` records that an
un-materialized anchor reading `unverifiable` is **normal**, not a defect. A debt built on that set
would demand a retirement the gate structurally declines. A served id is `actionable` iff **any** of:

- `hit["drift"]["type"] ∈ QUALIFYING_DRIFT` — `{anchor_missing, anchor_changed,
  blast_radius_changed}`, imported by name and pinned by CT-H8;
- the hit carries a `remediation` key — the server's own per-hit stale rider, attached
  exactly when the hit's routed `drift.type` is in `retirement.QUALIFYING_DRIFT`;
- the id appears in the envelope's top-level `conflicts` list.

Three signals, one debt, all three emitted by the server itself. The harness never computes
staleness; it reads the verdict the server already published.

- **Location:** `${HIVE_LOOP__STATE_DIR}` else `${CLAUDE_PLUGIN_DATA}` else a temp dir —
  `<dir>/<sha1(repo_root)>/<session_id>-<agent_id>.json`. `${CLAUDE_PLUGIN_DATA}`
  (`~/.claude/plugins/data/{id}/`) is the platform's persistent directory and survives plugin
  updates; `${CLAUDE_PLUGIN_ROOT}` is explicitly ephemeral and is never written to. Nothing is
  written inside the repo — no `.gitignore` change, no risk in a foreign clone.
- **Keyed by `(session_id, agent_id)`** — both ride the hook payload. §2.5 records what this does
  and does not mean about hive identity.
- **Atomic write:** `*.partial` + `os.replace` — the backup-partial-file lesson; a crash mid-write
  can never leave a truncated ledger that reads as a debt.
- **Fail-safe read:** missing / unparseable / unknown `version` ⇒ `None` ⇒ `inert`. Three-valued.
- **Lazy prune:** on load, ledger files older than 7 days in this repo's directory are deleted.
  This is why no `SessionEnd` hook is needed (§4g).

---

## 7 · The rules

`core/decide.py`'s table, keyed by the four normalized `Event.kind`s (§4e). Anything else ⇒ `inert`.

### `SESSION_RESUMED`
Emit context **only** when a ledger for this repo carried open debt — the reason this event exists is
that compaction erases the in-flight loop, so the post-compaction agent inherits the debt list
mechanically. Otherwise inert. Cannot block.

### `TOOL_PRE`
Two gates. Emitted by the adapter as a deny-with-reason. Nothing else on this event.

| Gate | Trigger | Mechanical predicate | Decision |
|---|---|---|---|
| **G1 recall-before-mutation** (I1) | `MUTATE` | `armed and recalls == 0` | **deny** — the reason names that no recall is journaled for this session |
| **G2 recall-before-store** (I5) | `HIVE_WRITE` / `HIVE_CAPTURE` | `armed and recalls == 0` | **deny** |

Both predicates are counts of observed calls — no text is parsed, no intent is predicted, and
neither can be satisfied by anything other than actually recalling. §4g records the two richer forms
that were cut and why each was net-negative.

### `TOOL_POST`
Arming (§4d), journalling, and two feedback cases. Never blocks; `async: true`.

- **arm** — `touched_source and hive_configured` ⇒ `armed = True`.
- `HIVE_RECALL` → parse the envelope; extend `served`; extend `actionable` per §6's three signals;
  reset `outcome_after_serve` when hits were served.
- `HIVE_OUTCOME` → set `outcome_after_serve`.
- `HIVE_WRITE` / `HIVE_CAPTURE` → increment `stored` **only if** the envelope is not
  `status="refused"`; set `unscoped_store` when the call's args carried neither `anchors` nor
  `repos`; when a `replaces` rider was present, credit `maintained` under the maintenance rule
  below (the rider now routes through the one retirement owner *after* the write lands, so its
  outcome is reported in the same envelope).
- `HIVE_SUPERSEDE` / `HIVE_PRUNE` / `HIVE_FLAG` → extend `maintained` with the ids named **only if**
  the envelope's `status` is affirmative. The server's full status vocabulary is
  `{superseded, pruned, recorded}` (affirmative) and `{noop, refused, rejected, disabled}` (not) —
  the harness credits on the **allow-list**, so an unrecognized future status fails safe by not
  crediting (the debt stays open, which is the direction that under-closes rather than
  falsely-closes). Pinned by CT-H8.
- **F1 bundled recall** — a `HIVE_RECALL` whose `abstained` is `true` **and** whose query carried
  ≥2 intents (≥2 `?`, a `;`/newline-joined clause pair, ≥2 distinct interrogatives, or a conjunction
  joining two verb phrases) → **feedback** reporting both observations. Conditioning on the server's
  own abstain is what makes this zero-false-positive: a long, dense, specific single claim that
  answers confidently never trips it, and a bundle that answers anyway is not a problem to report.
- **F2 refused store** — an envelope carrying `status="refused"` → **feedback** reporting the
  refusal verbatim. The envelope now carries `scan.findings=[{rule,start,end}]` and a `reason`
  naming the spans (the BUG-018 fix, `a8f9a4f`), so the feedback points at the offending span
  instead of leaving the writer to bisect. Necessary for correctness: without it D4 closes falsely.
  It states **no** workaround — per H1 the harness carries no domain knowledge.
- **Envelope parsing is a fail-open side-channel** (Law 6): the parse *shell* is guarded, not just
  the handler — the shape is coerced before any `.get()`, and `content[].text` is JSON-parsed only if
  present. Any fault records nothing and never breaks the read.

### `TURN_END`
Blocks. Inert when `stop_hook_active`, not `armed`, or every open debt already blocked once.
**One block per turn-end, naming every currently-open debt** (§4c #3); each named key is marked in
`blocks` in the same write.

| # | Key | Open when | Closed by |
|---|---|---|---|
| **D1** | `recall_missing` (I1) | `mutations > 0 and recalls == 0` | any recall |
| **D2** | `outcome_missing` (I7) | `served != () and not outcome_after_serve` | any outcome call |
| **D3** | `maintenance_missing` (I6) | `actionable − maintained − deferred ≠ ∅` | a maintenance verb the server **affirmed** for that id, or a `defer` sentinel |
| **D4** | `store_missing` (I3, I4) | `mutations > 0 and stored == 0 and no_store_why == ""` | a store verb that landed, or a `no-store` sentinel |
| **D5** | `scope_missing` (I8) | `unscoped_store and general_why == ""` | a `general` sentinel |

**Declarations are final-message sentinels, not tool calls.** The turn-end payload carries the final
assistant message — verified present as `last_assistant_message` in 2.1.220's Stop payload schema —
so the agent closes a judgment debt by ending its reply with one line:

```
HIVE-LOOP: no-store — <why nothing cleared the bar>
HIVE-LOOP: defer 12,40 — <why these stay open>
HIVE-LOOP: general — <why this lesson binds to no repo or path>
```

Chosen over a `loopctl` subcommand because a CLI invoked from a tool call cannot learn the current
`session_id`, and resolving by newest-mtime would race between parallel sessions in one repo. The
sentinel removes that failure mode rather than handling it, and costs no tool call. The line stays
visible to the human — an audit affordance, not a defect. A sentinel with an empty rationale does
not close its debt (CT-H3), which is what keeps it a decision rather than a bypass.

---

## 8 · Configuration

`Env`, frozen, validated in `__post_init__`. `decide` never reads `os.environ`; the adapter resolves
`Env` at the I/O edge and hands it in, so the core stays pure. **Two vars — every other knob was cut
(§4g).**

| Var | Default | Meaning |
|---|---|---|
| `HIVE_LOOP__ENABLED` | `1` | `0` ⇒ every hook byte-inert (exit 0, no output) |
| `HIVE_LOOP__STATE_DIR` | `${CLAUDE_PLUGIN_DATA}` | ledger location; also the test seam |

No knob decides *whether* a gate enforces: a measured-good discipline with one right answer is
encoded in code, not exposed to be mis-set (THEORY §9 #14). `HIVE_LOOP__ENABLED` is the single
all-or-nothing seam. A malformed value ⇒ the default plus one stderr line, never a crash.

---

## 9 · Intent → contract → test traceability

Frozen suite: `tests/contract/test_agent_loop_harness.py`. Each test drives the **real entry point
as a process** — `python -m harnesses.adapters.claude_code` with a real payload on stdin — asserting
exit code and emitted JSON. Not one mock. (There is no second entry point: `loopctl` was cut in
§4g, and a previous revision left a stale reference to it here.)

| Intent | Contract (given / when / then) | Scenarios covered | Test |
|---|---|---|---|
| I1 | **Given** an armed session with no recall, **when** a mutating `TOOL_PRE` payload arrives, **then** deny with a reason naming the missing recall | armed+no-recall→deny; armed+recall→allow; unarmed→allow; disabled→allow; mutating vs read-only `Bash`; `MultiEdit`/`NotebookEdit`; **a `READ` payload is never denied** (the dropped orientation budget, asserted so it cannot creep back) | CT-H1 |
| I2 | **Given** a `HIVE_RECALL` `TOOL_POST`, **when** the envelope abstained **and** the query carried ≥2 intents, **then** feedback; **never** a deny | abstained+bundled→feedback; abstained+single→silent; **confident+bundled→silent**; confident+single→silent; empty; non-string; exit code is never a block | CT-H2 |
| I3 | **Given** mutations, no landed store, no sentinel, **when** the turn ends, **then** block naming `store_missing`; a `no-store` sentinel closes it | no store→block; after write→pass; after capture→pass; sentinel with why→pass; sentinel without why→still blocks; **refused write→still blocks**; 0 mutations→pass | CT-H3 |
| I4 | **Given** the `store_missing` block, **then** the reason names both store verbs, defers the choice to the served contract, and passes the H1 scan | both verb names present; no semantic vocabulary | CT-H4 |
| I5 | **Given** a store `TOOL_PRE` in an armed session with no journaled recall, **then** deny | no recall→deny; recall→allow; both verbs; unarmed→allow; **the deny never inspects `text`** (the dropped token-overlap rule) | CT-H5 |
| I6 | **Given** a recall that served an id the **server** marked actionable, **when** the turn ends with no affirmed maintenance naming it, **then** block naming the ids | each of the three signals in isolation (`QUALIFYING_DRIFT`, `remediation` rider, `conflicts`); **`unverifiable`→no debt**; **`branch_scoped`→no debt**; **`fresh`/`n/a`→no debt**; closed by an affirmed prune / supersede / write(replaces=) / recorded flag / sentinel; **a `noop` prune does NOT close it**; partial close→blocks the remainder | CT-H6 |
| I7 | **Given** a recall that served ≥1 hit and no outcome call, **when** the turn ends, **then** block naming the served ids | served+none→block; served+outcome→pass; abstained recall→pass; outcome *before* the serving recall→still blocks | CT-H7 |
| I8 | **Given** a landed store carrying neither `anchors` nor `repos` in an armed session, **when** the turn ends, **then** block naming `scope_missing`; a `general` sentinel closes it | anchors only→pass; repos only→pass; both→pass; neither→block; neither+sentinel→pass; neither+empty sentinel→still blocks; **a refused store never opens it** | CT-H8a |
| H3 | harness verb tuple == `tool_defs.TOOL_NAMES`; qualifying-drift tuple == `retirement.QUALIFYING_DRIFT`; affirmative-status set is disjoint from the non-affirmative set and their union covers every `"status"` literal in `mcp_server.py` | equality both directions on all three | CT-H8 |
| H3 | **Given** a **real** recall envelope from the real `HiveMCPServer` over a real temp store, **then** the parser extracts served ids, drift verdicts, the `remediation` rider, and conflict ids | confident multi-hit; abstained; each drift verdict; stale rider present/absent; conflicts; malformed `content`; non-JSON text; `null` | CT-H9 |
| H1 | **Given** every file under `harnesses/`, **then** no trust-lifecycle semantic token appears — in source or in any emitted reason | source scan; every reason string produced by CT-H1..H8a re-scanned; also fails on `socket`/`http`/`urllib`/`requests`/`subprocess` (H2) and on credential-shaped env reads | CT-H10 |
| I9 | **Given** the shipped plugin, **then** `claude plugin validate --strict` exits 0, `hooks/hooks.json` registers exactly the four handled events, every command is the one adapter under `${CLAUDE_PLUGIN_ROOT}`, and `claude --plugin-dir harnesses` loads it | `validate --strict` exit 0; `EVENT_KINDS` keys == `hooks.json` events (both directions, so neither can drift); one command string; the blocking matcher contains no read tool (§4f); no state path under `${CLAUDE_PLUGIN_ROOT}`; `--plugin-dir` load lists the plugin | CT-H11 |
| I10 | **Given** any debt, **when** it already blocked once / `stop_hook_active` / disabled, **then** exit 0 without blocking — a session can never wedge | **five open debts produce ONE block naming all five**, and the next turn-end passes; `stop_hook_active=true`; `HIVE_LOOP__ENABLED=0`; missing ledger; corrupt ledger; unknown version; malformed env value; a subagent's `(session_id, agent_id)` ledger never reads the parent's | CT-H12 |
| I11 | **Given** every file under `harnesses/core/`, **then** no framework vocabulary appears | scan for `hook_`, `Stop`, `PreToolUse`, `tool_name`, `Edit`, `NotebookEdit`, `permissionDecision`, `settings.json` | CT-H13 |

Every intent maps to ≥1 test except **I12** (bare minimum), which is a property of what was
*not* built and is therefore verified by inspecting §4g's cut table rather than by a test — named
here so the claim is exact. Every test maps back to an intent or a harness law; **every ✅ row of
§3b maps to a test, and every ⛔ row is absent from this table by construction.** The suite is
**frozen** on authoring: written first, observed red against the un-built harness as *failing
assertions* (never import errors — the `require_*` / `load_module` guard convention
`tests/contract/conftest.py` already uses), and never edited to make implementation pass. A genuine
defect in it is an ESCALATE to the human.

**`frozen_paths`:** `tests/contract/test_agent_loop_harness.py`

---

## 10 · Test plan

**10a · Contract tier (the gate)** — CT-H1..CT-H13 above, real processes, real payloads.

**10b · Unit tier** — `tests/harness/`:
- `test_decide_rules.py` — truth tables at every boundary: `recalls` at 0/1; `blocks` empty/full;
  each debt open/closed; each of the three `actionable` signals in isolation and every non-signal
  drift verdict; each affirmative and non-affirmative status; each F1 signal in isolation crossed
  with `abstained` true/false.
- `test_state_ledger.py` — round-trip; atomic write leaves no `.partial`; truncated ⇒ `None`;
  unknown `version` ⇒ `None`; `(session, agent)` keys never collide; the 7-day lazy prune.
- `test_events_parsing.py` — **property-based (Hypothesis, already a dev dep)**: `Event.parse` over
  arbitrary JSON values never raises and never returns a partially-built object. The payload space
  is adversarial and open-ended and a parse-*shell* crash is a logged prior failure, so it earns a
  generative test rather than hand-picked rows.
- `test_seams.py` — CT-H8 and CT-H13 duplicated here for fast feedback.

**10c · Live tier (opt-in, marker `harness_live`)** — this tier exists because a whole bug family
(BUG-007 / BUG-011 / BUG-015) is **real-CLI-only** — BUG-015 specifically was a user-scope Stop hook
silently replacing every measured answer. Two asserted cases, always: a scratch repo **with** hive
arms and closes; a scratch repo **without** hive stays byte-inert (the §2.5 user-scope leakage
guard).

For the graded half, prefer the platform over a hand-rolled `claude -p` rubric: **`claude plugin
eval --ablation with-without`** runs cases against the plugin *with a no-plugin baseline arm* and
reports the score delta — which is exactly the Δ(harness on − off) this tier wants, with graders and
thresholds it does not have to build. It is flagged early access in 2.1.220, so it is a **candidate,
not a dependency**: if it is unavailable or unstable at build time, fall back to the `claude -p`
scratch-repo run scored against a fixed rubric. Deselected by default; `make check` runs
`-m "not embed and not harness_live"`. Suite-level threshold per §3.

**10d · Mutation discipline (Law 7)** — each broken, a named test watched go red, restored: delete
the `stop_hook_active` short-circuit; delete the one-block-per-debt check; drop the `armed` clause;
drop `.partial`+replace atomicity; drop one verb from the tuple; **widen the qualifying-drift tuple
to include `unverifiable`**; **credit a `noop` maintenance status as affirmative**; make a refused
store count as stored; drop the `general_why` clause from `scope_missing`.

---

## 11 · External-interaction inventory

| Boundary | Failure behavior | Failure-path test |
|---|---|---|
| **stdin** (payload) | unparseable / non-object / unmapped event ⇒ `inert`, exit 0, no output. **Fail-open** — a side-channel never breaks the turn | `test_events_parsing.py`, CT-H12 |
| **stdout** (hook JSON) | serialization fault ⇒ caught, exit 0, no output | CT-H12 |
| **ledger file** | missing / truncated / unknown version ⇒ `None` ⇒ `inert`. Write fault ⇒ swallowed, decision still emitted (a lost journal under-blocks — the safe direction) | `test_state_ledger.py` |
| **`.mcp.json` / settings** (hive-presence inference) | absent / unreadable / malformed ⇒ `hive_seen=False` ⇒ unarmed ⇒ inert | CT-H12 |
| **recall envelope** (the one parsed server shape) | any parse fault ⇒ nothing recorded for that hit ⇒ no debt opened. An unrecognized `status` on a maintenance verb ⇒ **not credited** ⇒ the debt stays open. The two directions differ deliberately: an unreadable *serve* must not manufacture a debt, and an unreadable *outcome* must not close one | CT-H9, CT-H6 |
| **`${CLAUDE_PLUGIN_DATA}`** (ledger home) | unset ⇒ fall back to a temp dir ⇒ the ledger is per-process and the harness degrades to advisory, never crashes. Never written under `${CLAUDE_PLUGIN_ROOT}` (documented ephemeral) | `test_state_ledger.py`, CT-H11 |
| **plugin install / update / uninstall** | the platform's job — no settings file is read or written by this harness, so BUG-043's duplicate-hook/orphan failure mode is structurally absent rather than mitigated | CT-H11 |
| **nested `claude -p`** (user-scope leakage, the BUG-015 vector) | inert by three independent paths: `--setting-sources ""`, `HIVE_LOOP__ENABLED=0`, the arming predicate | CT-H12 + live tier |
| **network** | **none exists** — CT-H10 fails on `socket`/`http`/`urllib`/`requests`/`subprocess` anywhere under `harnesses/` | CT-H10 |
| **env vars** | both optional with documented defaults; malformed ⇒ default + one stderr line | CT-H12 |

**Secrets fail-fast probe:** the harness requires **no** secret and no credential — it has no
transport. The probe is the inverse, inside CT-H10: the scan fails on any env read matching
`TOKEN|SECRET|KEY|PASSWORD`, so a future edit cannot quietly introduce one. There is therefore no
required env var to unset — the strongest form of the guarantee.

---

## 12 · Implementation plan (ordered; each step safe on its own)

1. **Author the frozen contract suite** `tests/contract/test_agent_loop_harness.py`
   (CT-H1..CT-H13, incl. CT-H8a) and register the `harness_live` marker in `pyproject.toml` — the
   `markers` list currently holds only `embed`. Run it: capture red evidence — every test failing as
   an **assertion**, none as an import error. Do not proceed without it.
2. **Package skeletons** — `harnesses/__init__.py`, `harnesses/core/__init__.py`,
   `harnesses/adapters/__init__.py`, so the tree is importable and typecheckable.
3. **`core/hive.py`** — the single owner of the hive coupling. No logic.
   ```python
   HIVE_VERBS: frozenset[str]                    # the eight, == tool_defs.TOOL_NAMES
   RECALL, WRITE, CAPTURE, OUTCOME, SUPERSEDE, PRUNE, FLAG, HEALTH: str
   MAINTENANCE_VERBS: frozenset[str]
   QUALIFYING_DRIFT: frozenset[str]              # == retirement.QUALIFYING_DRIFT
   AFFIRMED_STATUS: frozenset[str]               # {"superseded","pruned","recorded"}
   def parse_recall(result: object) -> tuple[tuple[int,...], tuple[int,...]]   # (served, actionable)
   def is_refusal(result: object) -> str          # "" when not refused, else rules + spans
   def is_affirmed(result: object) -> bool        # status in AFFIRMED_STATUS
   def maintained_ids(verb: str, args: Mapping[str, object]) -> tuple[int, ...]
   ```
4. **`core/events.py`** — frozen carriers.
   ```python
   class Kind(StrEnum):  SESSION_RESUMED, TOOL_PRE, TOOL_POST, TURN_END
   class Role(StrEnum):  MUTATE, READ, OTHER            # a hive call rides Event.hive_verb

   @dataclass(frozen=True, slots=True)
   class Event:  kind: Kind; session_id: str; agent_id: str; repo_root: str
                 role: Role = Role.OTHER; hive_verb: str = ""; args: Mapping[str, object] = ...
                 result: object = None; final_message: str = ""; turn_continuing: bool = False
                 touched_source: bool = False; hive_configured: bool = False

   @dataclass(frozen=True, slots=True)
   class Decision:  action: str; text: str = ""
       # __post_init__: action ∈ {"inert","context","deny","block","feedback"};
       # a non-inert action with empty text RAISES (Law 2 — no silent no-op decision)
       INERT: ClassVar["Decision"]

   @dataclass(frozen=True, slots=True)
   class Env:  enabled: bool; state_dir: str
       @classmethod
       def from_environ(cls, environ: Mapping[str, str]) -> "Env"
   ```
   Note `Event` carries **no** framework field — the adapter has already normalized (§4e), which is
   what CT-H13 enforces.
5. **`core/state.py`** — `LoopState` (§6) plus `ledger_path` / `load` (three-valued, never raises,
   lazy 7-day prune) / `save` (`*.partial` + `os.replace`, faults swallowed).
6. **`core/decide.py`** — the pure core; `mypy --strict` clean; imports no `os`, `sys`, `pathlib`,
   `socket`, `subprocess`.
   ```python
   RULES: dict[Kind, Callable[[Event, LoopState, Env], tuple[Decision, LoopState]]]
   def decide(event: Event, state: LoopState, env: Env) -> tuple[Decision, LoopState]
   # helpers: _intent_count(query) -> int      # >=2, AND abstained ⇒ F1 feedback
   #          _open_debts(state) -> tuple[str, ...]      # ALL of them, §4c #3
   #          _sentinels(msg) -> tuple[str, str, tuple[int, ...]]
   ```
   Every reason string lives in one `REASONS` dict here, so CT-H10 has one place to police.
7. **`adapters/claude_code.py`** — the only module doing I/O and the only one naming Claude Code.
   ```python
   EVENT_KINDS: dict[str, Kind]          # SessionStart→SESSION_RESUMED, PreToolUse→TOOL_PRE,
                                         # PostToolUse→TOOL_POST, Stop/SubagentStop→TURN_END
   MUTATING_TOOLS: frozenset[str]        # Edit | Write | MultiEdit | NotebookEdit
   MUTATING_BASH: re.Pattern[str]        # sed -i, tee, >, mv, rm, patch, git apply/checkout/restore
   READ_TOOLS: frozenset[str]            # Read | Glob | Grep — arming observation ONLY
   SOURCE_EXTS / EXCLUDED_DIRS: frozenset[str]
   def to_event(payload: Mapping[str, object], repo_root: str) -> Event | None
   def emit(decision: Decision) -> tuple[str, int]   # (stdout, exit code)
   def main(stdin=sys.stdin, environ=os.environ) -> int
   # deny    → {"hookSpecificOutput":{...,"permissionDecision":"deny","permissionDecisionReason":t}}, 0
   # block   → {"decision":"block","reason":t}, 0
   # context → {"hookSpecificOutput":{...,"additionalContext":t}}, 0
   # feedback→ t to stderr, exit 2
   # inert   → no output, exit 0 ;  every exception path → exit 0, silent (fail-open)
   ```
   `session_id` and `agent_id` both ride the payload (verified present in 2.1.220's hook-input
   builder), as does `last_assistant_message` on the Stop payload — the §7 sentinel depends on it.
   One command string serves every event because `hook_event_name` rides the payload — no argv, so
   the fragment has nothing to mis-wire.
8. **`.claude-plugin/plugin.json` + `hooks/hooks.json`** — the two declarative files that replace
   the whole installer. `plugin.json`: `name: "hive-loop"`, description, version, license.
   `hooks.json` registers exactly four events, each running the one command
   `python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude_code.py"` with `timeout: 5`, matchers per §4f's
   table. **`async: true` on `PostToolUse` is confirmed available** — `async` is present in
   2.1.220's hook-config schema (`async: v.literal(!0)`) and the runtime carries the
   `asyncResponse` path — so the broad journalling matcher stays off the turn's critical path.
   Verify per-event acceptance with `claude plugin validate --strict` plus one live run rather than
   assuming the schema key implies every event honors it; without it the sync 5 s timeout is still
   ample and only the §4f cost argument weakens.
   No installer, no marker reconcile, no restart notice to print — but the README must still state
   that hooks load at session start, so a fresh install takes effect next session.
9. **Wire the gate.** `Makefile`: `typecheck` is currently `mypy hive/ --strict` → becomes
   `mypy hive/ harnesses/ --strict`; `test` is currently `pytest -m "not embed"` → becomes
   `pytest -m "not embed and not harness_live"`. Ruff already covers `.`.
10. **Docs, same change** (each a house rule):
    - `harnesses/README.md` (new). Leads with the §2.5 adoption path — **install the plugin at user
      scope, because agents work in other repos than this one** — states that a clone yields inert
      files until the plugin is installed, notes that hooks load at session start, and documents the
      §4e seam so adding a framework is obvious.
    - `README.md` repo tree + `llms.txt` — a new top-level directory must appear.
    - `CONTEXT/THEORY.md` §5 — amend the "no client-side hooks" clause (line 481) to name the
      harness and its three laws; the code is truth.
    - `CONTEXT/INTERACTIONS.md` — a new §10 recording the harness as **client-side, DETECT-only,
      holds no trust handle, holds no identity handle (INV-2)**, and adjust §9's "nothing exists
      client-side" clause (line 513). CLAUDE.md requires an entry for a new hook or gate.
    - `CONTEXT/BUGS.md` — no new bug from this build. The anchor-acceptance gap (§13) is already
      logged as **BUG-077, UNSOLVED**; leave it open, since this plan deliberately declines to fix
      it client-side.
    - `CHANGELOG.md` entry.
    - `TODOS.md` — TODO 8, 9, and 11 all describe the **abolished** v3 onboarding substrate
      (`onboard_ref.py`, `render_onboarding_payload`, the contract-version beacon, an installed
      rules block); `hive/app/onboard_ref.py` does not exist. Mark them withdrawn and
      cross-reference this harness as the surviving form of "hook-enforced teeth."
11. **`make check`**, then **`/verify`** — load via `claude --plugin-dir harnesses`, restart, and
    drive every gate live; the runtime surface is the whole point, and the BUG-007/011/015 family
    proves green offline tests do not settle hook behavior.

---

## 13 · Deferred, with the seam named

**The anchor-acceptance gap (server-side, not this plan).** `hive/app/anchors.py:104` accepts
`anchor` as free text — only non-empty is checked — while
`change_evidence._anchor_match_level` partitions on `"::"` alone. So a single-colon
`path/file.py:Symbol` is stored silently, reads a healthy drift verdict, joins no census subject,
earns no `change_outcome`, and dies at the 45-day TTL while still being correct. BUG-058 fixed what
is *advertised*; acceptance is still unguarded. It is not built here because gating it client-side
means restating a grammar the server owns with nothing to pin it against (§2, Law H3's boundary) —
it belongs at `normalize_anchors`, which already raises `BadAnchors` for every other grammar
violation and maps cleanly to the `refused` envelope. **Logged as BUG-077 (UNSOLVED)** — found
while reviewing this plan, and tracked independently of it.

**Codegraph leg** (withdrawn). If wanted later: a `graph_currency` debt in `_open_debts` (mutations
since the last refresh, or `graph.json`'s `built_at_commit` ≠ `HEAD`) and a graph-before-grep gate. Two facts for whoever builds it: the agent-facing surface is `graphify` (`query` / `explain` /
`path` / `affected` / `update` / `check-update`), not `hive/matrix/`; and `graphify hook install`
writes to `.git/hooks/`, which **this repo bypasses** (`core.hooksPath=.githooks`). Note that such a
gate would put the harness back on the read hot path, which §4g just paid to leave.

**The MAINTAIN leg** (`hive_health` worklists), §3b's third ⛔. If wanted later it is not a session
debt but a separate cadence — a periodic maintainer run, not a per-turn hook — and building it as a
turn-end debt would block sessions on fleet-scale state they did not create.

**Second framework** (Cursor / Codex / Windsurf / Aider): one new `harnesses/adapters/<name>.py`
plus that framework's wiring file. Zero core edits, enforced by CT-H13. §4e names the one honest
limit — a framework without a blocking hook gets journalling and context but not teeth. Worth noting
from §2.6: `activeloopai/hivemind` advertises six harnesses but `cursor/` is an empty `.gitkeep` and
`hermes/` carries only skills, so breadth is easy to claim and hard to deliver — one adapter that
actually enforces beats five that wire nothing.

---

## 14 · Definition of done

1. `make check` green — `ruff format --check .`, `ruff check .`, `mypy hive/ harnesses/ --strict`,
   `pytest -m "not embed and not harness_live"`.
2. CT-H1..CT-H13 (incl. CT-H8a) green, with the step-1 **red-first evidence** recorded.
3. The nine §10d mutations each broke a named test and were restored.
4. Every ✅ row of the §3b closure matrix maps to a green contract test, and every ⛔ row is still
   absent from §9 — the completeness claim is checked, not asserted.
5. `/verify` — the plugin loaded for real (`--plugin-dir`, and once installed at user scope) and
   every gate driven: a denied pre-recall edit, a denied pre-recall store, a bundled recall that
   abstained producing feedback (and a bundled recall that answered producing **none**), a turn-end
   block naming multiple open debts at once, each of the three sentinels closing its debt, a refused
   store *not* closing `store_missing`, a `noop` prune *not* closing `maintenance_missing`, an
   `unverifiable` hit opening **no** debt, `HIVE_LOOP__ENABLED=0` proving byte-inert, and a session
   in a **non-hive** repo proving silence there.
6. Docs from step 10 landed in the same change.
7. This plan moved to `docs/PLANS/IMPLEMENTED/`.
