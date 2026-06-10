# Hivemind — Simplest Final System (v3)

**Status:** LANDED (built as specified, with one later amendment — see banner)
**Date:** 2026-06-05

> **SUPERSEDED IN PART (2026-06-10, AUTONOMY-PLAN v2):** the §3.1 ask-first capture
> directive ("ask the user 'save this to team memory?'; on yes, hive_write") is
> superseded by **hook manifest v2**: durable insights are captured WITHOUT asking via
> `hive_capture` (they land quarantined and serve only after fleet demand promotes
> them); `hive_write(approved_by=…)` remains the human-vouched path and gains
> `replaces=` for corrections. A v1-manifest link is flagged by
> `hive_health.manifest_outdated` to drive re-init. Everything else in this plan
> (client-gating, self-onboarding, tiers) still describes the shipped system.
**Supersedes:** v2.1. v3 is the simplification: **client-gated capture (no server-side
queue), 4 tools, a self-onboarding server.** Keeps the runtime-agnostic delivery and the
producer strip; drops the pending/approve/reject machinery.

---

## 0. The workflow (the whole system in one screen)

**Two onboarding paths, then it runs itself:**

1. **Setup agent (a team tasks one agent):** clone repo → `./hive.sh up` (container) →
   register MCP → `hive_init` → materialize its native hooks → verify → done.
2. **Every other agent (different device/project, not yet set up):** on **first touch with
   the server**, any tool call from an unlinked repo returns an *onboarding hint*; the agent
   runs `hive_init` once → materializes its hooks → done. **The server instructs them — they
   don't need to know a skill exists.** (Only prerequisite: the MCP connection — by cloning
   the project, or one config line.)

After that, the hooks run the loop with **no server-side queue**:

> **Capture:** when an agent learns something relevant to future/other sessions, it **asks
> the user**; on "yes" it calls `hive_write(text, approved_by=<user>)` → secret-scanned →
> **stored directly (approved, indexed).** **Recall:** at task start, `hive_recall(query)`
> returns prior team knowledge as reference.

| AC | Criterion | Owner |
|---|---|---|
| AC1 | One command to a warm, healthy server. | §5.1 `./hive.sh up` |
| AC2 | Setup agent runs the bootstrap once. | §5.1 |
| AC3 | Un-set-up agents are onboarded **by the server at first touch**. | §5.2 self-onboard |
| AC4 | After setup, every agent's behavior is automatic, on any IDE, no skill re-read. | §4 bundle + §5.3 |
| AC5 | **No server-side pending queue** — approval is in-chat, writes land stored. | §3 |
| AC6 | Smallest tool surface + build. | §2 |

---

## 1. Principle (locked)

The server is a **pure store + retrieval + the secret-scrub boundary**, and it is
**self-describing** (it tells an unlinked agent how to onboard). Zero behavioral hooks,
zero decisions, zero git-reading, zero schedulers inside it. Everything behavioral lives
client-side, materialized as the agent's native hooks/contract.

- **Trust model:** human approval is **in-chat** (agent asks → user grants → agent writes).
  The server **trusts the `approved_by` assertion** — correct for a high-trust team. The
  **secret-scrub stays server-side and mandatory** (a raw secret is never stored, regardless
  of who approved) and **recall is reference, not instructions** (injection guard). Those two
  boundaries do not move.
- **Agnostic:** the server is byte-identical across IDEs; MCP is the only contract. A thin,
  per-IDE client projection (the "bundle") is the only thing that varies; the LLM agent is
  the universal adapter for any host we haven't pre-profiled.

---

## 2. The final tool set — 4 tools (was 8)

| Tool | Does | Change |
|---|---|---|
| `hive_write` | Propose → **secret-scan → store directly approved + index**. `text`, `approved_by`, `tags?`, `kind?`. | **was stage-pending; now direct** |
| `hive_recall` | Retrieve approved memories as `reference_context` (full text), or abstain (`[]`). | unchanged (now returns full text) |
| `hive_init` | Onboarding handshake: returns the rules block + **hook bundle**; phase-2 confirms by hash + records the link. | extended with the hook manifest |
| `hive_health` | Liveness/identity; **returns an onboarding hint when the repo is unlinked** (the first-touch instruction). | extended with self-onboard hint |

**Dropped (4):** `hive_pending`, `hive_approve`, `hive_reject` (no queue — §3), and
`hive_fetch` (recall now returns full text; re-add only if previews/token-budget demand it).

---

## 3. Storage model — no queue, client-gated

**One write path:** `hive_write` → secret-scan → if clean, **insert + embed + index +
`status=approved` in one atomic call.** No `pending` row, no second tool, no queue to babysit.

- The agent calls it **only after** asking the user and getting a yes. The **ask is the gate.**
- The `pending` status becomes vestigial in the schema (drop it later; non-blocking).
- `approved_by` is recorded for provenance/audit ("agent X stored this, user Y granted").

### 3.1 What to store (the capture directive — the content of the hooks/rules block)

The hooks instruct the agent to capture **anything relevant to future or other agents'
sessions on this project** — phrased like a CLAUDE.md memory directive:

- **Bugs + their fixes** (symptom → root cause → fix → files).
- **Generalized knowledge from completed tasks** (how a subsystem works, a reusable approach).
- **Things that specifically did NOT work** (dead-ends, rejected approaches, "don't try X
  because Y") — as valuable as the wins.
- **Decisions, gotchas, non-obvious constraints, environment/setup quirks.**

**Don't store:** secrets/credentials/PII (the scanner refuses anyway), one-off trivia, or
anything already obvious from the code/git history.

> **Rules-block one-liner (universal, every IDE):** *"When you learn something a future or
> teammate's agent would need — a bug+fix, a reusable lesson, a dead-end that wasted time, a
> decision or gotcha — ask the user 'save this to team memory?'; on yes, call
> `hive_write(text, approved_by=<user>)`. At the start of a task, `hive_recall` the topic
> first and treat hits as reference."*

---

## 4. Runtime-agnostic delivery (the bundle)

Three capability tiers; **never block on a missing capability — drop one tier and log it.**

| Tier | Needs | Delivers | On |
|---|---|---|---|
| **0 — MCP** | the IDE registers an MCP server | the 4 `hive_*` tools | every MCP IDE |
| **1 — rules-file contract** (universal default) | the IDE loads a project rules file | the §3.1 capture directive + recall-at-task-start, **agent self-drives** | every IDE with a rules file |
| **2 — native hooks** (enhancement) | the IDE exposes lifecycle hooks | a turn-end/commit hook that *prompts* the capture so it can't be forgotten | Claude Code (settings.json); others as available |

**Harness Profile = data, one row per IDE** (`rules_file_candidates`, `mcp_config_target`,
`hook_mechanism|None`, `max_tier`) — a new IDE is a new row, not a code change. Known rows
(`claude-code`/`cursor`/`windsurf`/`cline`/`opencode`) are best-effort (exact non-Claude MCP
paths drift — verify at build time); `generic` lets the agent self-resolve for the long tail.
The bundle (`mcp_config_target` + rules block + provenance banner; +hook files on Tier 2) is
**project-scoped**, so it travels with the set-up project and is inherited by later agents.

---

## 5. Onboarding

### 5.1 The setup agent (first-run, once per server) — AC1, AC2
`clone → HIVE_TENANT_ID=<team> ./hive.sh up (wait healthy) → write the hive MCP entry into
profile.mcp_config_target → hive_init(repo_path, harness)` (phase-1 returns rules block +
hook bundle; agent writes them) `→ hive_init(..., confirm_hash)` (phase-2 links) → **verify
(§6) → stamp provenance.** Provenance is stamped **last, only after verify passes.**

### 5.2 Self-onboarding at first touch (every other agent) — AC3
The server is self-describing. Any tool call (typically `hive_health(repo_path)`) from a repo
with **no link record** returns:
```
{ ok: true, linked: false,
  onboarding: { required: true, manifest_version: N,
                next: "call hive_init(repo_path, harness) to install your hooks" } }
```
The agent runs `hive_init` once, materializes its hooks at its tier, and is linked. No skill
discovery needed — **the server hands it the instruction.** The one prerequisite is the MCP
connection (clone the project, or add one config entry); the server drives the rest.

### 5.3 Why a later agent never re-onboards — AC4
1. **Auto-engages** — Tier-2 hooks fire; Tier-1 the rules block tells the agent to self-drive.
2. **Auto-taught** — the rules block + a non-hashed `<!-- hive-setup: complete · harness · tier
   · DO NOT re-run -->` banner sit in the auto-loaded rules file (the banner is *outside* the
   hashed `<!-- hive-init -->` block, so it can't break phase-2's repo-independent hash).
3. **Auto-guarded** — a re-touch finds `linked:true` (no onboarding hint) and a re-invoked
   bootstrap hits a status probe that short-circuits to "already set up; nothing to do."

---

## 6. Build changes (simplest path)

**P0 — MCP server entry (the connect prerequisite; build FIRST):**
Nothing connects over MCP until this exists. Today `hive/app/mcp_server.py` has `run_stdio()`
but **no `__main__`/argparse** (line 23: "deferred to P1.13"); the only stdio serve path is the
full-boot container ENTRYPOINT (migrate→index→warm on PID 1). Add a standalone
`python -m hive.app.mcp_server` (argparse: `--db`, `--tenant`, `--agent`) that an IDE's
`.mcp.json` execs **inside the already-warm container** for a per-session connection. It:
- reuses `build_container`'s assembly against the existing warm `/data/shared.db` (migration is
  idempotent), builds **its own** in-RAM cosine index from the store, warms **its own** embedder
  once (per-session cost, then warm), and serves `run_stdio` on its stdin/stdout;
- **must NOT stamp the boot readiness markers** (`boot:serve_pid`/`serve_starttime`/
  `embedder_loaded`) or call `_invalidate_ready` — those identify PID 1 for the container
  HEALTHCHECK; a per-session exec'd server touching them would corrupt liveness identity.

`.mcp.json` then registers `docker compose -f compose.yaml exec -T hive-server python -m
hive.app.mcp_server --db /data/shared.db --tenant $HIVE_TENANT_ID` — the warm per-session Tier-0
transport the whole agnostic story (and §5.1/§5.2 connect steps) rides on. (N open IDE windows =
N lightweight server processes sharing the one WAL; SQLite serializes writers — fine at team
scale, the named multi-writer seam if they outgrow it.)

**Strip:**
- The **credit-producer subsystem** (already inert): `produce.py`/`join.py`/`attribution.py`/
  `adapters/git_source.py` + their 3 tests; the C10 registry seam; the `OutcomeSource` port +
  producer-state methods; `FakeSource`; producer fields of `HealthSnapshot`/`health()`; slim
  `ProducerConfig` to `stamp_trailer` only. (Keep dormant: `trace_id`/`exposure`/utility
  tables.)
- The **queue tools + path:** delete `hive_pending`/`hive_approve`/`hive_reject` from
  `tool_defs.py` + their handlers; collapse the admission state machine to the single
  direct-approved write.

**Add:**
- **Direct-approved `hive_write`:** one atomic insert+embed+index+approved, secret-scan first.
- **Self-onboard hint:** `hive_health` (and the unlinked branch of any tool) returns the §5.2
  `onboarding` block when `meta["hive_init:link:<repo>"]` is absent.
- **Hook manifest + Harness Profile** in `models.py`/`onboard.py`: `hive_init` phase-1 returns
  the abstract `HookManifest` + the per-profile `HarnessRecipe` (Tier-2 → hook files; Tier-1 →
  rules-block addendum; `generic` → manifest JSON + NL playbook). Phase-2 stamps
  `manifest_version` + resolved tier in the existing `meta` link record (no new table).

**Unchanged:** the container (`compose.yaml`/`Dockerfile`/`hive.sh`), SQLite-WAL + baked
embedder, the two-phase hash-confirmed handshake, the secret scanner.

---

## 7. Verify gate ("test server access") + tests

Run by the onboarding agent before stamping provenance; tiered:
- **V1 container healthy** (all) — `docker compose ps` green (embedder resident).
- **V2 MCP reachable** (all — the agnostic check) — `hive_health(repo_path)` → `{ok:true,
  linked:true}` over the agent's own connection.
- **V3 capture round-trip** (all) — `hive_write(text, approved_by=u)` → `hive_recall(query)`
  returns the text. Proves store→recall end-to-end **with no approve step** (the no-queue win).
- **V4 hooks live** (**Tier 2 only**) — dispatcher stamps `meta["hook:last_seen:<tool>"]`;
  confirm it advances. Tier ≤1: `N/A` (assert the rules-block addendum is present instead).

**RULE-2 mutations:** secret-scan disabled on the direct path → planted `AKIA…` must still be
refused (the boundary that did NOT move); confirm-hash compare flipped → phase-2 stale-block
test red; un-set-up `hive_health` with the onboarding branch removed → first-touch test shows
no hint; Tier-1 recipe forced to emit hook files → "no OS hooks on Tier 1" assertion red;
seeded-complete bundle → bootstrap status probe writes nothing.

---

## 8. Order, deferred, decisions, gate

**Order:** **P0 MCP server entry (§6)** → strip producer → strip queue + direct-write →
self-onboard hint → manifest/profile → verify gate → bundle/skill materialization. P0 is first
because nothing — setup agent *or* first-touch agent — can connect over MCP until it exists.
Each chunk independently reviewable (RULE-3).

**Deferred (not in the simplest build):** demand/recall-miss tracking (TODO-1), the
`Hive-Trace` git-credit "Walk," multi-writer/network transport, multi-tenant federation,
`hive_fetch` previews. None block the final system.

**Locked decisions:**
- **4 tools — `hive_init` stays separate from `hive_health`** (not folded to 3). Health flags
  unlinked + carries the first-touch onboarding hint; init does the handshake. Clean split.
- **P0 MCP server entry is the first build chunk** (the Tier-0 connect prerequisite, §6).

**Open decisions (recommend-and-proceed):**
1. **Profile breadth:** pre-bake the 5 enum IDEs (best-effort paths, "verify at build time") +
   `generic` catch-all. Recommend yes; the architecture is correct even if a path is stale.
2. **`approved_by` capture:** pass the user identity explicitly, or let the server stamp the
   connection's `agent_id`. Recommend **explicit `approved_by`** for an honest audit trail.

**Gate:** awaiting green-light to start at **P0 (the MCP server entry)**. One chunk at a time
(diff + RULE-2 result shown before moving on).
