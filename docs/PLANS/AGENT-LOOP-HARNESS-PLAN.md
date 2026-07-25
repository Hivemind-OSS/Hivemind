# AGENT-LOOP-HARNESS — mechanically closing the memory loop client-side

A shipped, adoptable harness that turns Hivemind's served discipline from **advisory** (the model
complies if it remembers to) into **mechanical** (the loop cannot be left open without a deliberate,
recorded decision). A runtime-agnostic core plus one thin per-framework adapter; Claude Code ships
first.

Status: PLAN — awaiting human confirmation. No code is written until it is confirmed.

---

## 1 · Scope

### In scope (the intents)

| # | Intent |
|---|---|
| I1 | When a task engages the codebase, the agent **recalls before working**. |
| I2 | Recalls are **single-pointed** — one intent per query, never bundled. |
| I3 | At task end a **store decision is made**: store the load-bearing lesson, or record that nothing cleared the bar. |
| I4 | Clearly-valuable-but-ambiguous-use memory goes to `hive_capture` rather than `hive_write`. |
| I5 | **Recall precedes every store** — to catch a duplicate or a rival before writing. |
| I6 | **Surfaced issues are resolved**: a drifted hit or a `conflicts` note is answered with `hive_prune` / `hive_write(replaces=)` / `hive_supersede` / `hive_flag`, or explicitly deferred. |
| I7 | `hive_outcome` records evidence when a recalled memory materially **helped** or **hurt**. |
| I8 | Enforcement is **hook-driven** wherever a hook can carry it. |
| I9 | Closure is **maximally enforced** — an open loop blocks the turn rather than merely warning. |
| I10 | The build is **modular**: a second codegen framework is an adapter, not a fork (§4e). |
| I11 | **Bare minimum** — only strictly necessary functionality ships (§4f names every cut). |

### Explicitly out of scope

- **Codegraph currency / graph-before-grep.** Withdrawn. For the record: `hive/matrix/` is a
  *server-side* engine (AST fingerprints feeding drift verdicts) with no agent-facing query surface;
  the agent-facing codegraph here is the separate `graphify` CLI. §13 keeps the seam named.
- Any change to the server, the eight MCP verbs, the served contract, or the trust lifecycle.
  **This plan adds zero server behavior.**
- **Any attempt to influence hive identity.** See §2.5 — structurally refused, and already
  unnecessary.

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

**Law H3 — Coupling is pinned by a red test.** The only coupling to the server is the verb *names*
and the recall *envelope shape*. Both are pinned: the harness verb tuple must equal
`hive.app.tool_defs.TOOL_NAMES` (CT-H8), and the envelope parser is fed a **real** recall response
from the real `HiveMCPServer` over a real temp store — the substrate `tests/contract/conftest.py`
already provides (CT-H9). A renamed verb or a moved key is a **failing test in this repo**, not
silent client rot. That is Law 7 applied to the harness itself.

**What is genuinely given up:** an adopter who installs and never pulls again holds stale software.
Honest mitigation, not a fix: because of H1 the harness carries no contract text, so staleness costs
*enforcement*, never *correctness* — a stale harness under-blocks and degrades toward the
pre-harness advisory behavior. That direction is the safe one (Law 6), and it is why H1 is
non-negotiable.

**Doc obligations follow (§12 step 9):** THEORY §5 and INTERACTIONS §9 are edited in the same change
to name the harness and its three laws, since the code is truth.

---

## 2.5 · Application, distribution, and identity

### The unit of enforcement is the SESSION

Hooks fire on one session's events; the ledger is keyed `(session_id, agent_id)`. Installing is not
an alternative to that — installing is how you **select which sessions get it**.

**Ships as a Claude Code PLUGIN, not a settings merge.** This is adopted from
`activeloopai/hivemind`'s harness layout (§2.6), and it is strictly simpler than the settings-merge
install originally planned here. Verified against the plugins reference:

- a plugin is `.claude-plugin/plugin.json` + `hooks/hooks.json` at the plugin root, and **plugin
  hooks respond to the same lifecycle events as user-defined hooks** — no behavioral difference;
- `${CLAUDE_PLUGIN_ROOT}` resolves to the plugin's install directory, so the hook command needs no
  path discovery, no copy, and no absolute path baked into a settings file;
- `${CLAUDE_PLUGIN_DATA}` (`~/.claude/plugins/data/{id}/`) is a documented **persistent** directory
  that survives plugin updates — the correct home for the ledger. `${CLAUDE_PLUGIN_ROOT}` is
  explicitly ephemeral ("changes when the plugin updates … don't write state there"), so state must
  not live there;
- dev/adoption without a marketplace: `claude --plugin-dir harnesses` for a session, or install from
  the local path.

**What this deletes** from the previous plan — the whole reason to adopt it: the settings-file
marker-reconcile, the BUG-043 duplicate-hook/orphan concern, the `~/.claude/hive-loop/` copy, the
installed-vs-repo divergence check, `install --user|--project|--emit`, and `uninstall`. `loopctl`
disappears entirely (§4f). Plugin install/update/uninstall is the platform's job, and it versions
the artifact for us.

| Selector | Reach |
|---|---|
| plugin installed (user scope) | every session on the machine — **the primary path** |
| `claude --plugin-dir harnesses` | that one invocation — dev and CI |
| `claude --setting-sources` / `--bare` | can exclude settings hooks; see the bound below |

**The bound on "maximally enforce," stated plainly:** a session is governed only if it loaded the
plugin. `--bare` skips hooks entirely. So enforcement is strong *within* a governed session and
bypassable *at launch*. No hook can close that, and it should not: it is what keeps the bench
measurable (BUG-015's landed fix — a user-scope Stop hook silently replacing every measured answer).

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
block (they use it for capture and setup; **not found in the plugins reference read here, so verify
before relying on it** — §12 step 8).

**Deliberately not adopted, and why — these are not simplicity trade-offs but structural
unavailability:**

| They do | This cannot, because |
|---|---|
| `capture.js` on `UserPromptSubmit`/`PostToolUse`/`Stop`/`SubagentStop` — **auto-capture every prompt, tool call, and response** | it contradicts this server's core stance, served in its own contract: "keep it LEAN — a FLOW not a stock, a BIGGER store is a WORSE one," plus the DURABLE/REUSABLE/NON-OBVIOUS storage bar. It would also flood the demand signal that promotion is measured from (Law 3) |
| `recall.js` on `UserPromptSubmit` — **auto-inject recall results** before the model sees the prompt | a harness that *calls* recall needs transport, which violates H2, mints a second identity, and corrupts the anti-gaming currency the demand rung depends on. It would also formulate the query *for* the agent, defeating I2's single-pointedness |
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
  `hive/app/mcp_server.py:_solo_hint` (line 1244) the only remaining path to `None` is
  `len({m.agent_id for m in misses}) > 1` — so **≥2 distinct identities are already recorded on
  this store.** Each new session's MCP `initialize` mints a fresh `Mcp-Session-Id` which the client
  echoes, and that is the identity. Nothing client-side is needed.
- **What the harness therefore owes:** exactly one thing — key its ledger by `(session_id,
  agent_id)` so a user-scope install never merges two sessions' loops into one, and a subagent's
  loop is its own (§6). That is the whole of its identity responsibility.
- **Deliberately not built:** no `solo_hint` mirror, no identity check, no health read. The server
  already surfaces identity collapse; duplicating it client-side would be a second contract (H1)
  and would need transport (H2).

---

## 3 · The mechanization limit (read before scoring the design)

Three intents are **graded judgments a hook cannot verify**: I3 ("is this lesson load-bearing?"),
I4 (write-vs-capture), and I7 (helped-vs-hurt — `hive_outcome()` with empty arrays is a legitimate
answer, so "an outcome call happened" is satisfiable without judgment).

The harness therefore enforces **the decision point, never the decision**: it makes skipping the
question impossible and answering it well cheap (the block reason names the exact ids in play).
Claiming more would be a lying contract. Every affected contract test asserts the gate fired and a
decision was recorded — never that the decision was correct. Judgment is checked only in the live
tier (§10c), graded against a rubric at suite level.

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
   harnesses/claude_code/adapter.py     ← ALL I/O + ALL Claude Code vocabulary
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
  Claude-Code tool name known only by `adapter.py`; hive verb names and envelope shape only by
  `hive.py`.
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
over pre-existing dirt, costing a turn and a compliance paragraph. Three structural guarantees, not
three remembered cautions:

1. **Three-valued ledger state.** Missing ≠ recorded-clean ≠ recorded-with-debt. Missing or
   unparseable ⇒ `inert`, never a debt. (Direct BUG-017 lesson.)
2. **One block per debt, hard-coded.** Each *distinct* debt blocks at most once per session; after
   that it degrades to advisory. A stubborn or confused agent can always finish — wedging is
   unconstructable, not avoided. Not a knob (§9 #14) and the reason `waive` was cut (§4f).
3. **`stop_hook_active` short-circuit**, and **inert unless armed** — `decide` returns `inert` when
   the ledger says the session is not armed, so there is no arming precondition a caller could
   forget.

`HIVE_LOOP__ENABLED=0` is the kill switch that makes every hook byte-inert (the repo's
byte-inert-when-off idiom).

### 4d · Arming: observation, never prediction

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

### 4e · The portability seam (I10) — what a second framework actually costs

`core/` contains **no framework vocabulary**. Two normalizations buy that, and they are the whole
abstraction:

1. **`Event.kind`** — a closed vocabulary the core owns: `SESSION_RESUMED | TOOL_PRE | TOOL_POST |
   TURN_END`. The adapter maps its framework's event names onto these four (Claude Code:
   `SessionStart` → `SESSION_RESUMED`, `Stop`/`SubagentStop` → `TURN_END`, everything unmapped ⇒
   dropped). The core never learns that "Stop" is a word.
2. **`Event.tool_role`** — a closed vocabulary the core owns: `HIVE_<verb> | MUTATE | READ | OTHER`.
   The adapter classifies its framework's tool names into it, so `Edit|Write|MultiEdit|NotebookEdit`
   and the mutating-`Bash` regex live in `adapter.py`, never in the core. The core reasons about
   roles.

**Adding a framework = one new `adapters/<name>.py` (payload→Event, Decision→that framework's
response shape, `main()`) plus that framework's own wiring file. Zero core edits, zero core test
edits.** Pinned by CT-H13: the core is scanned for framework vocabulary (`hook_`, `Stop`,
`PreToolUse`, `tool_name`, `Edit`, `NotebookEdit`, `permissionDecision`, …) and fails on a hit — the
same mechanical technique as H1, so the seam cannot rot into a Claude-Code-only core.

The honest limit: a framework with **no blocking hook** (advisory-only callbacks) can host the
journalling and the context injection but not the teeth. That is a property of the framework, not a
design gap; the adapter's `Decision`→response mapping degrades `deny`/`block` to advisory context
and says so at install time.

### 4g · Latency: measured, and what it buys or costs

**Measured** (20 sequential invocations of a realistic adapter shape — payload parse + ledger read +
`*.partial` + `os.replace`): **~34 ms per invocation**, dominated by Python interpreter startup
(`python3 -S -E` shaves it to ~29 ms; the work itself is sub-millisecond).

- **`timeout: 5` in `hooks.json` is a hang guard, not a budget** — 5000 ms against a 34 ms
  operation. It exists so a stalled filesystem on the ledger write cannot hold a turn open, not
  because the work is slow.
- **`async: true` would save ~34 ms, on the journalling hook only** — hence nice-to-have, not
  necessary (§12 step 8).
- **The real cost is invocation VOLUME, not duration.** At ~34 ms, a 300-tool-call session pays
  ~10 s cumulative wall clock. That is driven entirely by how broad the `PreToolUse` matcher is.
- **Grounding:** this cost profile is already the accepted baseline in this repo — the two existing
  `PreToolUse` graphify hooks in `.claude/settings.json` each spawn `python3 -c "…"` on every
  `Bash` / `Read` / `Glob` call today.

**The matcher-breadth consequence, which is also the price tag on G5.** `Read|Glob|Grep` are the
highest-frequency tools in any session, and **G5 is the only gate that needs them in `PreToolUse`**
(nothing else denies a read). So:

| G5 | `PreToolUse` matcher | Arming observed at |
|---|---|---|
| **kept** | `Edit\|Write\|MultiEdit\|NotebookEdit\|Read\|Glob\|Grep\|Bash\|mcp__hive__.*` | `PreToolUse` (already there) |
| **dropped** | `Edit\|Write\|MultiEdit\|NotebookEdit\|Bash\|mcp__hive__.*` — only tools a gate can deny | `PostToolUse` (never blocks, `async`-eligible) |

Dropping G5 therefore removes the harness from the hot path entirely, not just one rule. That is the
strongest argument yet for G5 being first-to-drop (§4f) — and if it is dropped, the matcher narrowing
and the arming move must land with it, or the cost is paid for nothing.

### 4f · Bare minimum (I11) — everything cut, and what survived scrutiny

**Cut**, with the reason each is not strictly necessary:

| Cut | Why it isn't necessary |
|---|---|
| **`loopctl.py` in its entirety** — install / uninstall / check / emit / status / waive / dry-run | plugin packaging (§2.5) makes install, update, uninstall, and versioning the platform's job. `status` was already weak (a CLI cannot resolve the current `session_id`); `waive` was belt-on-belt over §4c #2; `check` existed only to detect the vendored-copy drift that plugins remove |
| the settings-merge installer + marker reconcile | deleted with `loopctl`; BUG-043's failure mode is now structurally absent |
| `audit.jsonl` + rotation | useful for tuning, never for closing the loop |
| the `SessionEnd` hook | its only jobs were audit + ledger deletion; a stale ledger is inert (session ids never repeat) and pruned lazily by mtime on load. Hooks 5 → 4 |
| `HIVE_LOOP__ORIENT_BUDGET` knob | hard-coded 3 — §9 #14: one right answer belongs in code, not exposed to be mis-set |
| `HIVE_LOOP__BLOCK_BUDGET` knob | hard-coded 1, same rule |
| a custom ledger path scheme | `${CLAUDE_PLUGIN_DATA}` is the documented persistent dir; the invented XDG path is gone |

Result: **4 hooks, 2 env vars, 5 modules, 1 executable entry point, no CLI.**

**Kept under protest, with the reason — overturn any of these and the plan shrinks further:**

- **`SessionStart`** — the *only* mechanism by which debt survives compaction. Without it a long
  session silently loses its loop, which is the closure hole most likely to matter.
- **G5, the orientation budget** — without it, "read 40 files then edit" is ungated, and those
  exploration tokens are exactly what recall exists to save (I1's actual purpose). It is also the
  gate most likely to feel noisy, and per §4g it is the **sole reason the harness sits on the hot
  path** (`Read|Glob|Grep`) at ~34 ms per call: **first thing to drop**, and dropping it must take
  the matcher narrowing and the arming move with it.
- **Refused-store parsing** — necessary for *correctness*, not polish: a refused `hive_write` must
  not falsely close the store debt.
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
    hive.py                       # SINGLE owner of the hive coupling: verb names + envelope parsing
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
    test_seams.py                 # CT-H8 verb pin + CT-H13 core-purity scan, fast tier
  contract/
    test_agent_loop_harness.py    # FROZEN contract suite, CT-H1..CT-H13
```

Five modules, one executable entry point (`adapters/claude_code.py`), two declarative files. No CLI.

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
    orient_calls: int = 0                   # READ-role calls observed while no recall exists
    recalls: tuple[str, ...] = ()           # the query text of each observed recall
    mutations: int = 0                      # MUTATE-role calls observed
    served: tuple[int, ...] = ()            # episode_ids returned in reference_context
    drifted: tuple[int, ...] = ()           # served ids whose drift is neither "fresh" nor "n/a"
    conflicted: tuple[int, ...] = ()        # ids named in a conflicts side-channel note
    outcome_after_serve: bool = False       # an outcome call followed the last serving recall
    stored: tuple[str, ...] = ()            # store verbs observed (REFUSED ones excluded — §7)
    maintained: tuple[int, ...] = ()        # ids named in an observed maintenance verb call
    deferred: tuple[int, ...] = ()          # ids deferred by a final-message sentinel
    no_store_why: str = ""                  # the recorded "nothing cleared the bar" rationale
    blocks: tuple[str, ...] = ()            # debt keys already blocked once
```

- **Location:** `${HIVE_LOOP__STATE_DIR}` else `${CLAUDE_PLUGIN_DATA}` else a temp dir —
  `<dir>/<sha1(repo_root)>/<session_id>-<agent_id>.json`. `${CLAUDE_PLUGIN_DATA}`
  (`~/.claude/plugins/data/{id}/`) is the platform's documented **persistent** directory and survives
  plugin updates; `${CLAUDE_PLUGIN_ROOT}` is explicitly ephemeral and is never written to. Nothing is
  written inside the repo — no `.gitignore` change, no risk in a foreign clone.
- **Keyed by `(session_id, agent_id)`** — the whole of the harness's identity duty (§2.5): a
  user-scope install never merges two sessions, and a subagent's loop is its own.
- **Atomic write:** `*.partial` + `os.replace` — the backup-partial-file lesson; a crash mid-write
  can never leave a truncated ledger that reads as a debt.
- **Fail-safe read:** missing / unparseable / unknown `version` ⇒ `None` ⇒ `inert`. Three-valued.
- **Lazy prune:** on load, ledger files older than 7 days in this repo's directory are deleted.
  This is why no `SessionEnd` hook is needed (§4f).

---

## 7 · The rules

`core/decide.py`'s table, keyed by the four normalized `Event.kind`s (§4e). Anything else ⇒ `inert`.

### `SESSION_RESUMED`
Emit context **only** when a ledger for this repo carried open debt — the reason this event exists is
that compaction erases the in-flight loop, so the post-compaction agent inherits the debt list
mechanically. Otherwise inert. Cannot block.

### `TOOL_PRE`
Emitted by the adapter as a deny-with-reason.

| Gate | Trigger | Mechanical predicate | Decision |
|---|---|---|---|
| **G1 arm** | any | §4d both clauses | update state, allow |
| **G2 single-pointed** (I2) | `HIVE_RECALL` | the query carries ≥2 intents: ≥2 `?`, a `;`/newline-joined clause pair, ≥2 distinct interrogatives, or a conjunction joining two verb phrases | **deny** — "this query carries N intents; issue one recall per intent" |
| **G3 recall-before-store** (I5) | `HIVE_WRITE` / `HIVE_CAPTURE` | no journaled recall query shares ≥2 content tokens (stopword-stripped) with the `text` argument | **deny** |
| **G4 recall-before-mutation** (I1) | `MUTATE` | `armed and recalls == ()` | **deny** |
| **G5 orientation budget** (I1) | `READ` | `armed and recalls == () and orient_calls >= 3` | **deny** |

G5 exists because recall-before-*any*-read forces blind, vague queries — and the served gate abstains
on vague queries, so a blind-recall rule would burn a call and teach the agent the loop is useless.
Three read-only calls of orientation, then the recall is owed. **G2's thresholds are tuned for
precision**: a long, dense, specific single claim must pass (that is the good query shape), so length
alone is never a trigger.

### `TOOL_POST`
Journalling, plus one feedback case.

- `HIVE_RECALL` → parse the envelope; extend `served`, `drifted` (any `drift` outside
  `{"fresh","n/a"}`), `conflicted` (ids in the top-level `conflicts` list); reset
  `outcome_after_serve` when hits were served.
- `HIVE_OUTCOME` → set `outcome_after_serve`.
- `HIVE_WRITE` / `HIVE_CAPTURE` → record the verb **only if the result is not a refusal**; extend
  `maintained` when `replaces` is present.
- `HIVE_SUPERSEDE` / `HIVE_PRUNE` / `HIVE_FLAG` → extend `maintained` with the ids named.
- **refused store** — a result carrying `status="refused"` → **feedback** reporting the refusal
  verbatim (the rule names the server returned) and that nothing was persisted. Necessary for
  correctness: without it the store debt closes falsely. It states **no** workaround — per H1 the
  harness carries no domain knowledge, and the entropy-scan boundary (BUG-018, UNSOLVED,
  false-positives on dense path tokens) is exactly the kind of repo knowledge that belongs *in the
  store*, not hard-coded into a client that would rot when TODO 13 fixes it.
- **Envelope parsing is a fail-open side-channel** (Law 6): the parse *shell* is guarded, not just
  the handler — the shape is coerced before any `.get()`, and `content[].text` is JSON-parsed only if
  present. Any fault records nothing and never breaks the read.

### `TURN_END`
Blocks. Inert when `stop_hook_active`, not `armed`, or the debt already blocked once. Debts checked
in order; the first unmet one blocks:

| Key | Open when | Closed by |
|---|---|---|
| `recall_missing` (I1) | `mutations > 0 and recalls == ()` | any recall |
| `outcome_missing` (I7) | `served != () and not outcome_after_serve` | any outcome call |
| `maintenance_missing` (I6) | `(drifted ∪ conflicted) − maintained − deferred ≠ ∅` | a maintenance verb naming the id, or a `defer` sentinel |
| `store_missing` (I3, I4) | `mutations > 0 and stored == () and no_store_why == ""` | a store verb, or a `no-store` sentinel |

**Declarations are final-message sentinels, not tool calls.** The turn-end payload carries the final
assistant message, so the agent closes a judgment debt by ending its reply with one line:

```
HIVE-LOOP: no-store — <why nothing cleared the bar>
HIVE-LOOP: defer 12,40 — <why these stay open>
```

Chosen over a `loopctl` subcommand because a CLI invoked from a tool call cannot learn the current
`session_id`, and resolving by newest-mtime would race between parallel sessions in one repo. The
sentinel removes that failure mode rather than handling it, and costs no tool call. The line stays
visible to the human — an audit affordance, not a defect.

---

## 8 · Configuration

`Env`, frozen, validated in `__post_init__`. `decide` never reads `os.environ`; the adapter resolves
`Env` at the I/O edge and hands it in, so the core stays pure. **Two vars — every other knob was cut
(§4f).**

| Var | Default | Meaning |
|---|---|---|
| `HIVE_LOOP__ENABLED` | `1` | `0` ⇒ every hook byte-inert (exit 0, no output) |
| `HIVE_LOOP__STATE_DIR` | `${CLAUDE_PLUGIN_DATA}` | ledger location; also the test seam |

No knob decides *whether* a gate enforces: a measured-good discipline with one right answer is
encoded in code, not exposed to be mis-set (§9 #14). `HIVE_LOOP__ENABLED` is the single
all-or-nothing seam. A malformed value ⇒ the default plus one stderr line, never a crash.

---

## 9 · Intent → contract → test traceability

Frozen suite: `tests/contract/test_agent_loop_harness.py`. Each test drives the **real entry points
as processes** — `python -m harnesses.claude_code.adapter` with a real payload on stdin, and
`loopctl` likewise — asserting exit code and emitted JSON. Not one mock.

| Intent | Contract (given / when / then) | Scenarios covered | Test |
|---|---|---|---|
| I1 | **Given** an armed session with no recall, **when** a mutating `TOOL_PRE` payload arrives, **then** deny with a reason naming the missing recall | armed+no-recall→deny; armed+recall→allow; unarmed→allow; disabled→allow; orientation at 2/3/4 reads; mutating vs read-only `Bash`; `MultiEdit`/`NotebookEdit` | CT-H1 |
| I2 | **Given** a recall `TOOL_PRE`, **when** the query carries ≥2 intents, **then** deny with a split instruction; single-pointed allows | 2 `?`; `;`-joined; newline-joined; two interrogatives; conjoined verb phrases; **long dense single claim → ALLOW**; empty; non-string | CT-H2 |
| I3 | **Given** mutations, no store verb, no sentinel, **when** the turn ends, **then** block naming `store_missing`; a `no-store` sentinel closes it | no store→block; after write→pass; after capture→pass; sentinel with why→pass; sentinel without why→still blocks; **refused write→still blocks**; 0 mutations→pass | CT-H3 |
| I4 | **Given** the `store_missing` block, **then** the reason names both store verbs, defers the choice to the served contract, and passes the H1 scan | both verb names present; no semantic vocabulary | CT-H4 |
| I5 | **Given** a store `TOOL_PRE`, **when** no journaled recall overlaps its text, **then** deny | no recall→deny; unrelated recall→deny; overlapping→allow; both verbs; missing `text` | CT-H5 |
| I6 | **Given** a recall that served a drifted hit or a conflicts note, **when** the turn ends with no maintenance naming that id, **then** block naming the ids | drifted; conflicts; both; closed by prune / supersede / write(replaces=) / flag / sentinel; partial close→blocks the remainder | CT-H6 |
| I7 | **Given** a recall that served ≥1 hit and no outcome call, **when** the turn ends, **then** block naming the served ids | served+none→block; served+outcome→pass; abstained recall→pass; outcome *before* the serving recall→still blocks | CT-H7 |
| H3 | harness verb tuple == `tool_defs.TOOL_NAMES` | equality both directions | CT-H8 |
| H3 | **Given** a **real** recall envelope from the real `HiveMCPServer` over a real temp store, **then** the parser extracts served ids, drift verdicts, conflict ids | confident multi-hit; abstained; drifted; conflicts; malformed `content`; non-JSON text; `null` | CT-H9 |
| H1 | **Given** every file under `harnesses/`, **then** no trust-lifecycle semantic token appears — in source or in any emitted reason | source scan; every reason string produced by CT-H1..H7 re-scanned; also fails on `socket`/`http`/`urllib`/`requests`/`subprocess` (H2) and on credential-shaped env reads | CT-H10 |
| I8 | **Given** the shipped plugin, **then** `plugin.json` is a valid manifest, `hooks/hooks.json` registers exactly the four handled events, every command is the one adapter under `${CLAUDE_PLUGIN_ROOT}`, and `claude --plugin-dir harnesses` loads it | manifest schema valid; `EVENT_KINDS` keys == `hooks.json` events (both directions, so neither can drift); one command string; no state path under `${CLAUDE_PLUGIN_ROOT}`; `--plugin-dir` load lists the plugin | CT-H11 |
| I9 | **Given** any debt, **when** it already blocked once / `stop_hook_active` / disabled, **then** exit 0 without blocking — a session can never wedge | second turn-end passes; `stop_hook_active=true`; `HIVE_LOOP__ENABLED=0`; missing ledger; corrupt ledger; unknown version; malformed env value | CT-H12 |
| I10 | **Given** every file under `harnesses/core/`, **then** no framework vocabulary appears | scan for `hook_`, `Stop`, `PreToolUse`, `tool_name`, `Edit`, `NotebookEdit`, `permissionDecision`, `settings.json` | CT-H13 |

Every intent maps to ≥1 test; every test maps back to an intent or a harness law. The suite is
**frozen** on authoring: written first, observed red against the un-built harness as *failing
assertions* (never import errors — the `require_*` / `load_module` guard convention
`tests/contract/conftest.py` already uses), and never edited to make implementation pass. A genuine
defect in it is an ESCALATE to the human.

**`frozen_paths`:** `tests/contract/test_agent_loop_harness.py`

---

## 10 · Test plan

**10a · Contract tier (the gate)** — CT-H1..CT-H13 above, real processes, real payloads.

**10b · Unit tier** — `tests/harness/`:
- `test_decide_rules.py` — truth tables at every boundary: `orient_calls` at 2/3/4; `blocks` at
  0/1; each debt open/closed; token overlap at 1/2/3 shared tokens; each G2 signal in isolation.
- `test_state_ledger.py` — round-trip; atomic write leaves no `.partial`; truncated ⇒ `None`;
  unknown `version` ⇒ `None`; `(session, agent)` keys never collide; the 7-day lazy prune.
- `test_events_parsing.py` — **property-based (Hypothesis, already a dev dep)**: `Event.parse` over
  arbitrary JSON values never raises and never returns a partially-built object. The payload space
  is adversarial and open-ended and a parse-*shell* crash is a logged prior failure, so it earns a
  generative test rather than hand-picked rows.
- `test_seams.py` — CT-H8 and CT-H13 duplicated here for fast feedback.

**10c · Live tier (opt-in, marker `harness_live`)** — a real `claude -p` session in a scratch repo
against a live server, scored against a fixed rubric: did the loop close without a human? Plus two
asserted cases: a scratch repo **with** hive arms and closes; a scratch repo **without** hive stays
byte-inert (the §2.5 user-scope leakage guard). This tier exists because a whole bug family
(BUG-007 / BUG-011 / BUG-015) is **real-CLI-only** — BUG-015 specifically was a user-scope Stop hook
silently replacing every measured answer. Deselected by default; `make check` runs
`-m "not embed and not harness_live"`. Suite-level threshold per §3.

**10d · Mutation discipline (Law 7)** — each broken, a named test watched go red, restored: delete
the `stop_hook_active` short-circuit; delete the one-block-per-debt check; drop the `armed` clause;
drop `.partial`+replace atomicity; drop one verb from the tuple; loosen one G2 signal; make a
refused store count as `stored`.

---

## 11 · External-interaction inventory

| Boundary | Failure behavior | Failure-path test |
|---|---|---|
| **stdin** (payload) | unparseable / non-object / unmapped event ⇒ `inert`, exit 0, no output. **Fail-open** — a side-channel never breaks the turn | `test_events_parsing.py`, CT-H12 |
| **stdout** (hook JSON) | serialization fault ⇒ caught, exit 0, no output | CT-H12 |
| **ledger file** | missing / truncated / unknown version ⇒ `None` ⇒ `inert`. Write fault ⇒ swallowed, decision still emitted (a lost journal under-blocks — the safe direction) | `test_state_ledger.py` |
| **`.mcp.json` / settings** (hive-presence inference) | absent / unreadable / malformed ⇒ `hive_seen=False` ⇒ unarmed ⇒ inert | CT-H12 |
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

1. **Author the frozen contract suite** `tests/contract/test_agent_loop_harness.py` (CT-H1..CT-H13)
   and register the `harness_live` marker in `pyproject.toml`. Run it: capture red evidence — every
   test failing as an **assertion**, none as an import error. Do not proceed without it.
2. **Package skeletons** — `harnesses/__init__.py`, `core/__init__.py`,
   `claude_code/__init__.py`, so the tree is importable and typecheckable.
3. **`core/hive.py`** — the single owner of the hive coupling. No logic.
   ```python
   HIVE_VERBS: frozenset[str]                    # the eight, == tool_defs.TOOL_NAMES
   RECALL, WRITE, CAPTURE, OUTCOME, SUPERSEDE, PRUNE, FLAG, HEALTH: str
   MAINTENANCE_VERBS: frozenset[str]
   def parse_recall(result: object) -> tuple[tuple[int,...], tuple[int,...], tuple[int,...]]
   def is_refusal(result: object) -> str          # "" when not refused, else the rule names
   def maintained_ids(verb: str, args: Mapping[str, object]) -> tuple[int, ...]
   ```
4. **`core/events.py`** — frozen carriers.
   ```python
   class Kind(StrEnum):  SESSION_RESUMED, TOOL_PRE, TOOL_POST, TURN_END
   class Role(StrEnum):  MUTATE, READ, OTHER            # HIVE_* carried as Event.hive_verb

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
   # helpers: _intent_count(query) -> int      # >=2 ⇒ deny (G2)
   #          _overlaps(query, text) -> bool   # >=2 shared content tokens (G3)
   #          _open_debts(state) -> tuple[str, ...]
   #          _sentinels(msg) -> tuple[str, tuple[int, ...]]
   ```
   Every reason string lives in one `REASONS` dict here, so CT-H10 has one place to police.
7. **`adapters/claude_code.py`** — the only module doing I/O and the only one naming Claude Code.
   ```python
   EVENT_KINDS: dict[str, Kind]          # SessionStart→SESSION_RESUMED, PreToolUse→TOOL_PRE,
                                         # PostToolUse→TOOL_POST, Stop/SubagentStop→TURN_END
   MUTATING_TOOLS: frozenset[str]        # Edit | Write | MultiEdit | NotebookEdit
   MUTATING_BASH: re.Pattern[str]        # sed -i, tee, >, mv, rm, patch, git apply/checkout/restore
   READ_TOOLS: frozenset[str]            # Read | Glob | Grep
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
   One command string serves every event because `hook_event_name` rides the payload — no argv, so
   the fragment has nothing to mis-wire.
8. **`.claude-plugin/plugin.json` + `hooks/hooks.json`** — the two declarative files that replace
   the whole installer. `plugin.json`: `name: "hive-loop"`, description, version, license.
   `hooks.json` registers exactly four events, each running the one command
   `python3 "${CLAUDE_PLUGIN_ROOT}/adapters/claude_code.py"` with `timeout: 5`:
   `SessionStart`; `PreToolUse` (matcher
   `Edit|Write|MultiEdit|NotebookEdit|Read|Glob|Grep|Bash|mcp__hive__.*`); `PostToolUse` (matcher
   `mcp__hive__.*`); `Stop` and `SubagentStop`.
   **`PostToolUse` is journalling-only and never blocks, so try `async: true` on it** — observed in
   `activeloopai/hivemind`'s `hooks.json` but not found in the plugins reference read here, so
   verify it is honored before relying on it; without it the sync 5 s timeout is already ample.
   No installer, no marker reconcile, no restart notice to print — but the README must still state
   that hooks load at session start, so a fresh install takes effect next session.
9. **Wire the gate.** `Makefile`: `typecheck` → `mypy hive/ harnesses/ --strict`; `test` →
    `pytest -m "not embed and not harness_live"`. Ruff already covers `.`.
10. **Docs, same change** (each a house rule):
    - `harnesses/README.md` (new). Leads with the §2.5 adoption path — **install the plugin at user
      scope, because agents work in other repos than this one** — states that a clone yields inert
      files until the plugin is installed, notes that hooks load at session start, and documents the
      §4e seam so adding a framework is obvious.
    - `README.md` repo tree + `llms.txt` — a new top-level directory must appear.
    - `CONTEXT/THEORY.md` §5 — amend the "no client-side hooks" clause to name the harness and its
      three laws; the code is truth.
    - `CONTEXT/INTERACTIONS.md` — a new §10 recording the harness as **client-side, DETECT-only,
      holds no trust handle, holds no identity handle (INV-2)**, and adjust §9's "nothing exists
      client-side" clause. CLAUDE.md requires an entry for a new hook or gate.
    - `CONTEXT/BUGS.md` — no new bug; note under BUG-018 that the harness surfaces the refusal
      verbatim and deliberately hard-codes no workaround.
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

**Codegraph leg** (withdrawn). If wanted later: a `graph_currency` debt in `_open_debts` (mutations
since the last refresh, or `graph.json`'s `built_at_commit` ≠ `HEAD`) and a `G6 graph-before-grep`
gate. Two facts for whoever builds it: the agent-facing surface is `graphify` (`query` / `explain` /
`path` / `affected` / `update` / `check-update`), not `hive/matrix/`; and `graphify hook install`
writes to `.git/hooks/`, which **this repo bypasses** (`core.hooksPath=.githooks`). Observed while
planning: `built_at_commit` is `ab36e62` against `HEAD` `1f14e61` — the graph is one commit stale,
which is the concrete gap that leg would close.

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
2. CT-H1..CT-H13 green, with the step-1 **red-first evidence** recorded.
3. The seven §10d mutations each broke a named test and were restored.
4. `/verify` — the plugin loaded for real (`--plugin-dir`, and once installed at user scope) and
   every gate driven: a denied pre-recall edit, a denied bundled recall, a denied recall-less store,
   a blocked turn-end for each of the four debts, both sentinels closing their debt, a refused store
   *not* closing the store debt, `HIVE_LOOP__ENABLED=0` proving byte-inert, and a session in a
   **non-hive** repo proving silence there.
5. Docs from step 10 landed in the same change.
6. This plan moved to `docs/PLANS/IMPLEMENTED/`.
