# `harnesses/` — the agent-loop harness (`hive-loop`)

A Claude Code **plugin** that turns the memory discipline from *advisory* — the model complies if
it remembers to — into *mechanical*: a session cannot change code without a recall, cannot store
without one, and cannot end a turn leaving a decision silently unmade.

This directory **is** the plugin root. There is no build step: Node runs the TypeScript directly,
so the committed source is the shipped, runnable file.

---

## Install it at user scope

**Install the plugin for your user, not just this repo.** Agents work in other repositories than
this one — a fleet agent edits `some-other-repo` and reaches the server over MCP; it never sees
this tree. A user-scope install covers every repository your agents touch. A `git clone` of this
repo gives you the files but no enforcement: the plugin has to be installed, and **hooks load at
session start**, so a fresh install takes effect on your *next* session, not the current one.

```bash
# for one invocation — development and CI
claude --plugin-dir /path/to/hivemind/harnesses

# for every session on this machine — the primary path
claude plugin install /path/to/hivemind/harnesses
```

Set the endpoint the manifest points at before your first governed session:

```bash
export HIVE_MCP_URL=https://your-hive-endpoint/mcp
export HIVE_TOKEN=…            # read from your environment; never a literal in the manifest
```

**Requirements:** Node **≥ 23.6**. The hooks run `.ts` directly, which needs type stripping on by
default. On an older runtime the hook fails and the harness is silently inert — the safe direction,
but worth knowing.

**Turn it off:** `HIVE_LOOP__ENABLED=0` makes every hook byte-inert — no output, no ledger, no
file read. `--bare` skips hooks entirely, and a session that never loaded the plugin is not
governed at all. Enforcement is strong *inside* a governed session and bypassable *at launch*; no
hook can close that, and it should not — it is what keeps a benchmark measurable.

---

## What it does

Nothing until a session **arms**, which needs both of:

- a repo-relative **source file** was read, searched or changed (dotfiles, `CONTEXT/`, `docs/`,
  `graphify-out/`, `node_modules/` and `vendor/` do not count), and
- a memory endpoint is wired for the session — the plugin's own declaration, an observed memory
  call, or a `hive` entry in a config file the platform reads. Local file reads only; the harness
  never opens a socket.

A conversational, strategy or business session therefore stays byte-inert.

Once armed:

| When | What happens |
|---|---|
| a call that changes the working tree, with no recall journaled | **denied**, with a reason naming the recall |
| a store call, with no recall journaled | **denied**, same reason shape |
| a recall that **abstained** on a query carrying two or more intents | one line of feedback; never a denial |
| the turn ends with an unmade decision | **one** block naming every open item at once |
| the context is compacted | the open items are re-stated so the loop survives |

The five turn-end items, and what closes each:

| Item | Opens when | Closed by |
|---|---|---|
| `recall_missing` | the tree changed and no recall was journaled | any recall |
| `outcome_missing` | a recall served hits and no outcome call followed | any outcome call |
| `maintenance_missing` | the **server** marked a served hit as needing an answer | a maintenance call the server affirmed, or a `defer` line |
| `store_missing` | the tree changed and no store landed | a store that landed, or a `no-store` line |
| `scope_missing` | a store landed carrying neither `anchors` nor `repos` | a `general` line |

**Each item blocks at most once per session.** After that it degrades to advisory. A stubborn or
confused agent can always finish — wedging is unconstructable, not merely avoided.

### Declaring a decision

Some of these are judgments a hook cannot verify. The harness enforces the *decision point*, never
the decision: you close a judgment item by ending your reply with one line, exactly —

```
HIVE-LOOP: no-store — <why nothing applied>
HIVE-LOOP: defer 12,40 — <why these stay open>
HIVE-LOOP: general — <why this binds to no repo or path>
```

The dash is an em dash (`—`), and **a line with an empty rationale closes nothing** — that is what
keeps it a decision rather than a way past the gate. The line stays visible to the human, which is
the point.

### The observed MCP tool-name prefix

Claude Code namespaces a plugin-declared MCP server, so the memory verbs arrive under a prefix
rather than as bare names. The hook matchers deliberately **do not encode that prefix** — they are
prefix-agnostic (`mcp__.*__hive_write`), and the adapter classifies a call by the segment after the
last `__`. A matcher is a cost filter, not a semantic: over-including costs a few milliseconds on a
foreign tool the adapter then declines to act on, while under-including would silently blank every
hook. So a namespaced server, a project-declared one named `hive`, and a renamed plugin all work
with no edit.

---

## Three laws it is built to

**H1 — no second contract.** The harness states **zero** memory semantics. It never says what a
label means, when something is promoted, or what clears the storage bar. It emits only what it
mechanically observed, which decision is still unmade, and that the authority is your served
contract. A test scans every shipped source file and every reason string for that vocabulary and
fails on a hit. Consequence: a stale harness costs *enforcement*, never *correctness*.

**H2 — detect only.** It never calls a memory verb, never opens a socket, never opens its own
session, and holds no handle. It observes hook payloads and blocks or allows a call the agent
itself chose to make. Declaring the endpoint in the manifest is a line of JSON that Claude Code
reads — the harness process still opens nothing.

**H3 — the coupling is pinned by a red test.** The only thing shared with the server is a small set
of names and shapes, and they are not hand-copied: `scripts/gen_harness_constants.py` generates
`core/hive-constants.ts` from the server source, and `tests/harness/` regenerates it and fails on
any diff, plus records a real server envelope as the fixture the parser asserts against. A renamed
verb or a moved key is a failing test in the server's own suite, not silent client rot.

---

## Layout, and adding a second framework

```
.claude-plugin/plugin.json   name, author, the endpoint declaration
hooks/hooks.json             four lifecycle events → one command
core/                        runtime-AGNOSTIC. no framework vocabulary
  events.ts                  the normalized carriers + total coercion primitives
  state.ts                   the ledger ⇄ disk (the ONE module here that owns I/O)
  hive.ts                    the single owner of the memory-server coupling
  hive-constants.ts          GENERATED from the server; never hand-edited
  decide.ts                  PURE. every rule, in one reducer
adapters/
  claude-code.ts             payload → event, decision → hook JSON, entry point.
                             ALL I/O and ALL Claude Code names live here
test/                        node:test only — no test framework, no test-time dependency
types/node.d.ts              hand-written declarations for the slice of Node used
```

`core/` contains **no framework vocabulary**, which is what makes a second framework cheap. Two
normalizations buy it, and a second adapter codes against them:

1. **`Event.kind`** — `SESSION_RESUMED | TOOL_PRE | TOOL_POST | TURN_END`. The adapter maps its
   framework's event names onto these four; the core never learns what those names were.
2. **`Event.role`** — `MUTATE | READ | OTHER`, with a memory call's verb carried separately. Tool
   names and the mutating-shell test live in the adapter.

**Adding a framework = one new `adapters/<name>.ts` plus that framework's wiring file. Zero core
edits, zero core test edits** — pinned by a scan that fails if a framework word appears under
`core/`. The honest limit: a framework with no blocking hook can host the journalling and the
context injection but not the teeth; its adapter degrades a denial to advisory and says so.

---

## Working on it

```bash
npm --prefix harnesses ci                       # the one dev dependency: the type checker
npm --prefix harnesses exec -- tsc -p harnesses --noEmit
node --test "harnesses/test/*.test.ts"
make check                                       # the canonical gate, both languages
HIVE_LOOP_LIVE=1 node --test harnesses/test/live.test.ts   # opt-in, drives the real CLI
```

The shipped plugin resolves nothing outside `node:*`, runs with no build and needs no
`npm install`. `harnesses/node_modules/` is the gitignored dev gate only — it is not tracked, not
copied by a plugin install, and absent from the wheel and the server image.

The source must stay **erasable** TypeScript — no `enum`, no `namespace`, no parameter properties,
no decorators. Every one of those fails to execute under type stripping, and the type checker
rejects them up front.

**Configuration is two variables and no more.** `HIVE_LOOP__ENABLED` (default `1`) and
`HIVE_LOOP__STATE_DIR` (default the plugin's persistent data directory, else a temp dir). A
malformed value falls back to the default with one line on the error channel, never a crash. No
knob decides *whether* a gate enforces.
