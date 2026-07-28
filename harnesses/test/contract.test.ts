// FROZEN contract suite. Authored before the harness existed and observed red as
// named assertions; from that point it is never edited to make an implementation
// pass. A genuine defect in it is an escalation to a human, never a silent edit.
//
// Every test drives the real entry point AS A PROCESS with a real payload on stdin
// and asserts the exit code and the emitted JSON. Never an in-process call into the
// entry function: the shipped artifact is a process, and only running it as one
// proves the erasable-TypeScript constraint and the fail-open exit paths hold. The
// one import of a shipped module reads a declarative table (the event-name map) so
// it can be compared against the manifest in both directions.
//
// The suite reaches the harness only through existence guards, so "not built yet"
// is a readable failing assertion rather than a crashed runner.

import { test } from "node:test"
import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { fileURLToPath } from "node:url"

// ── locations ────────────────────────────────────────────────────────────────

const HARNESS_ROOT = fileURLToPath(new URL("../", import.meta.url))
const ENTRY = join(HARNESS_ROOT, "adapters", "claude-code.ts")
const ENTRY_URL = new URL("../adapters/claude-code.ts", import.meta.url).href
const CORE_DIR = join(HARNESS_ROOT, "core")
const ADAPTER_DIR = join(HARNESS_ROOT, "adapters")
const FIXTURES = join(HARNESS_ROOT, "test", "fixtures", "envelopes.json")
const PLUGIN_MANIFEST = join(HARNESS_ROOT, ".claude-plugin", "plugin.json")
const HOOKS_MANIFEST = join(HARNESS_ROOT, "hooks", "hooks.json")

/** The four normalized event kinds the core owns (the portability contract). */
const EVENT_KINDS = ["SESSION_RESUMED", "TOOL_PRE", "TOOL_POST", "TURN_END"]

/** The five turn-end debt keys. A block reason that stops naming one is a behavior change. */
const DEBT_KEYS = [
  "recall_missing",
  "outcome_missing",
  "maintenance_missing",
  "store_missing",
  "scope_missing",
]

/** Trust-lifecycle / storage-bar vocabulary the harness may never state (Law H1). */
const BANNED_SEMANTICS = [
  "provisional",
  "quarantine",
  "quarantined",
  "established",
  "deprecated",
  "promotion",
  "promote",
  "demand",
  "vouch",
  "stigmergic",
  "trust",
  "clears the bar",
  "storage bar",
  "durable lesson",
  "non-obvious",
  "single-pointed",
  "outcome-verified",
  "anti-gaming",
]

/** Law H2: no transport of any kind in the shipped runtime surface. */
const BANNED_TRANSPORT = [
  "node:http",
  "node:https",
  "node:net",
  "node:dgram",
  "node:tls",
  "child_process",
  "WebSocket",
  "fetch(",
  "XMLHttpRequest",
]
const CREDENTIAL_SHAPED = /\b(?:TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL|BEARER)\b/

/** Framework vocabulary that may not appear anywhere under core/ (the §4e seam). */
const FRAMEWORK_WORDS = [
  "hook_",
  "Stop",
  "PreToolUse",
  "PostToolUse",
  "SessionStart",
  "tool_name",
  "tool_input",
  "tool_response",
  "Edit",
  "NotebookEdit",
  "permissionDecision",
  "additionalContext",
  "settings.json",
]

/**
 * The pure modules: no node:* import may appear in any of them. core/state.ts is
 * the single named exemption (it owns the atomic ledger write) and the exemption
 * list is asserted to be exactly one entry long, so I/O cannot drift elsewhere.
 */
const PURE_MODULES = ["decide.ts", "events.ts", "hive.ts", "hive-constants.ts"]
const IO_EXEMPT_MODULES = ["state.ts"]

// ── guards: "not built yet" must be a readable assertion, never a crash ──────

function requireEntry(): string {
  if (!existsSync(ENTRY)) {
    assert.fail("harnesses/adapters/claude-code.ts not built yet")
  }
  return ENTRY
}

function requireDir(dir: string, label: string): string[] {
  if (!existsSync(dir)) assert.fail(`${label} not built yet`)
  const names = readdirSync(dir).filter((n) => n.endsWith(".ts"))
  if (names.length === 0) assert.fail(`${label} not built yet`)
  return names
}

type Json = unknown

function requireFixtures(): Record<string, Record<string, Json>> {
  if (!existsSync(FIXTURES)) assert.fail("envelope fixture not generated yet")
  return JSON.parse(readFileSync(FIXTURES, "utf8")) as Record<string, Record<string, Json>>
}

function requireManifest(path: string, label: string): Record<string, Json> {
  if (!existsSync(path)) assert.fail(`${label} not built yet`)
  return JSON.parse(readFileSync(path, "utf8")) as Record<string, Json>
}

async function requireAdapterModule(): Promise<Record<string, Json>> {
  requireEntry()
  try {
    return (await import(ENTRY_URL)) as Record<string, Json>
  } catch (e) {
    assert.fail(`harnesses/adapters/claude-code.ts is not importable yet: ${String(e)}`)
  }
}

function fixture(group: string, scenario: string): Json {
  const all = requireFixtures()
  const g = all[group]
  if (g === undefined || !(scenario in g)) {
    assert.fail(`envelope fixture ${group}.${scenario} not generated yet`)
  }
  return g[scenario]
}

/** A deep copy of a recorded tool result with its inner hive envelope rewritten. */
function variant(
  group: string,
  scenario: string,
  mutate: (env: Record<string, Json>) => void,
): Json {
  const result = JSON.parse(JSON.stringify(fixture(group, scenario))) as {
    content?: { text?: string }[]
  }
  const slot = result.content?.[0]
  assert.ok(slot !== undefined && typeof slot.text === "string", "recorded result has no content[].text")
  const env = JSON.parse(slot.text) as Record<string, Json>
  mutate(env)
  slot.text = JSON.stringify(env)
  return result
}

function envelopeOf(toolResult: Json): Record<string, Json> {
  const content = (toolResult as { content?: { text?: string }[] })?.content
  const text = content?.[0]?.text
  assert.ok(typeof text === "string", "recorded tool result carries no content[].text")
  return JSON.parse(text) as Record<string, Json>
}

function servedIdsOf(toolResult: Json): number[] {
  const hits = (envelopeOf(toolResult)["reference_context"] ?? []) as { episode_id?: number }[]
  return hits.map((h) => Number(h.episode_id))
}

// ── the process rig ──────────────────────────────────────────────────────────

interface Ctx {
  readonly stateDir: string
  readonly repo: string
  readonly home: string
}

let seq = 0

function ctx(): Ctx {
  const base = mkdtempSync(join(tmpdir(), `hive-loop-ct-${seq++}-`))
  const repo = join(base, "repo")
  const home = join(base, "home")
  mkdirSync(join(repo, "src"), { recursive: true })
  mkdirSync(join(home, ".claude"), { recursive: true })
  writeFileSync(join(repo, "src", "app.ts"), "export const a = 1\n", "utf8")
  writeFileSync(
    join(repo, ".mcp.json"),
    JSON.stringify({
      mcpServers: { hive: { type: "http", url: "http://127.0.0.1:8765/mcp" } },
    }),
    "utf8",
  )
  return { stateDir: join(base, "state"), repo, home }
}

interface Res {
  readonly code: number
  readonly stdout: string
  readonly stderr: string
  readonly json: Record<string, Json> | null
}

function runAt(entry: string, c: Ctx, payload: Json, extraEnv: Record<string, string>): Res {
  const r = spawnSync(process.execPath, [entry], {
    input: typeof payload === "string" ? payload : JSON.stringify(payload),
    encoding: "utf8",
    cwd: c.repo,
    timeout: 30_000,
    env: {
      PATH: process.env["PATH"],
      HOME: c.home,
      HIVE_LOOP__STATE_DIR: c.stateDir,
      ...extraEnv,
    },
  })
  const stdout = r.stdout ?? ""
  let json: Record<string, Json> | null = null
  if (stdout.trim().length > 0) {
    try {
      json = JSON.parse(stdout) as Record<string, Json>
    } catch {
      json = null
    }
  }
  return { code: r.status ?? -1, stdout, stderr: r.stderr ?? "", json }
}

function run(c: Ctx, payload: Json, extraEnv: Record<string, string> = {}): Res {
  return runAt(requireEntry(), c, payload, extraEnv)
}

const SID = "sess-ct-0001"

function pre(tool: string, input: Json, extra: Record<string, Json> = {}): Json {
  return {
    session_id: SID,
    hook_event_name: "PreToolUse",
    cwd: "REPO",
    tool_name: tool,
    tool_input: input,
    ...extra,
  }
}

function post(tool: string, input: Json, response: Json, extra: Record<string, Json> = {}): Json {
  return {
    session_id: SID,
    hook_event_name: "PostToolUse",
    cwd: "REPO",
    tool_name: tool,
    tool_input: input,
    tool_response: response,
    ...extra,
  }
}

function stop(finalMessage = "", extra: Record<string, Json> = {}): Json {
  return {
    session_id: SID,
    hook_event_name: "Stop",
    cwd: "REPO",
    stop_hook_active: false,
    last_assistant_message: finalMessage,
    ...extra,
  }
}

function resumed(extra: Record<string, Json> = {}): Json {
  return {
    session_id: SID,
    hook_event_name: "SessionStart",
    cwd: "REPO",
    source: "compact",
    ...extra,
  }
}

/** Rewrite the "REPO" placeholder to this context's real repo path. */
function withCwd(c: Ctx, payload: Json): Json {
  if (typeof payload !== "object" || payload === null) return payload
  const p = { ...(payload as Record<string, Json>) }
  if (p["cwd"] === "REPO") p["cwd"] = c.repo
  return p
}

function drive(c: Ctx, payloads: Json[], extraEnv: Record<string, string> = {}): Res[] {
  return payloads.map((p) => run(c, withCwd(c, p), extraEnv))
}

function last(results: Res[]): Res {
  const r = results[results.length - 1]
  assert.ok(r !== undefined, "no result")
  return r
}

// ── reusable payload fragments ───────────────────────────────────────────────

const HIVE = "mcp__hive__"
const SRC = "src/app.ts"

/** Observing a source read is what arms the session (arming is observed, never predicted). */
function armPayload(): Json {
  return post("Read", { file_path: SRC }, { ok: true })
}

function recallPost(scenario: string, query = "how does the recall gate abstain"): Json {
  return post(`${HIVE}hive_recall`, { query }, fixture("recall", scenario))
}

function outcomePost(): Json {
  return post(`${HIVE}hive_outcome`, { helped: [], hurt: [] }, fixture("outcome", "ok"))
}

function denyReason(res: Res): string {
  const hso = res.json?.["hookSpecificOutput"] as Record<string, Json> | undefined
  return String(hso?.["permissionDecisionReason"] ?? "")
}

function isDeny(res: Res): boolean {
  const hso = res.json?.["hookSpecificOutput"] as Record<string, Json> | undefined
  return hso?.["permissionDecision"] === "deny"
}

function isBlock(res: Res): boolean {
  return res.json?.["decision"] === "block"
}

function blockReason(res: Res): string {
  return String(res.json?.["reason"] ?? "")
}

/**
 * The text an observation actually delivers to the agent. Reads the emission the
 * way the platform does, so the assertion names the outcome and not the wire.
 */
function feedbackText(res: Res): string {
  const trimmed = res.stdout.trim()
  if (trimmed === "") return ""
  const parsed = JSON.parse(trimmed) as Record<string, Json>
  const hso = parsed["hookSpecificOutput"] as Record<string, Json> | undefined
  const text = hso?.["additionalContext"]
  return typeof text === "string" ? text : ""
}

function assertInert(res: Res, what: string): void {
  assert.equal(res.code, 0, `${what}: expected exit 0, got ${res.code} — ${res.stderr}`)
  assert.equal(res.stdout.trim(), "", `${what}: expected no stdout, got ${res.stdout}`)
}

// ═════════════════════════════════════════════════════════════════════════════
// CT-H1 · I1 — a mutation in an armed session with no journaled recall is denied
// ═════════════════════════════════════════════════════════════════════════════

test("CT-H1 · armed with no recall, a mutating call is denied and the reason names the missing recall", () => {
  const edit = last(drive(ctx(), [armPayload(), pre("Edit", { file_path: SRC })]))
  assert.ok(isDeny(edit), `expected a deny, got ${edit.stdout || "(silence)"}`)
  assert.equal(edit.code, 0, "a deny is exit 0 with JSON on stdout")
  assert.match(denyReason(edit), /hive_recall/)
})

test("CT-H1 · armed with a journaled recall, the same mutating call is allowed", () => {
  const edit = last(
    drive(ctx(), [armPayload(), recallPost("confident_multi"), pre("Edit", { file_path: SRC })]),
  )
  assertInert(edit, "a recall was journaled")
})

test("CT-H1 · an unarmed session never denies a mutating call", () => {
  const edit = last(drive(ctx(), [pre("Edit", { file_path: SRC })]))
  assertInert(edit, "no source was ever observed")
})

test("CT-H1 · a session that only touched excluded paths never arms", () => {
  const edit = last(
    drive(ctx(), [
      post("Read", { file_path: "CONTEXT/THEORY.md" }, { ok: true }),
      post("Read", { file_path: "docs/notes.md" }, { ok: true }),
      pre("Edit", { file_path: SRC }),
    ]),
  )
  assertInert(edit, "only non-source paths were observed")
})

test("CT-H1 · HIVE_LOOP__ENABLED=0 makes the deny path byte-inert", () => {
  const edit = last(
    drive(ctx(), [armPayload(), pre("Edit", { file_path: SRC })], { HIVE_LOOP__ENABLED: "0" }),
  )
  assertInert(edit, "disabled")
  assert.equal(edit.stderr, "", "disabled means byte-inert, stderr included")
})

test("CT-H1 · a mutating Bash call is denied and a read-only one is not", () => {
  const results = drive(ctx(), [
    armPayload(),
    pre("Bash", { command: "sed -i 's/a/b/' src/app.ts" }),
    pre("Bash", { command: "git status --short" }),
  ])
  const mutating = results[1]
  const readOnly = results[2]
  assert.ok(mutating !== undefined && readOnly !== undefined)
  assert.ok(isDeny(mutating), "sed -i mutates the tree")
  assertInert(readOnly, "git status mutates nothing")
})

test("CT-H1 · the mutating-Bash classification covers the whole named family", () => {
  const commands = [
    "sed -i 's/a/b/' src/app.ts",
    "echo hi | tee src/app.ts",
    "echo hi > src/app.ts",
    "mv src/app.ts src/b.ts",
    "rm -f src/app.ts",
    "patch -p1 < fix.diff",
    "git apply fix.diff",
    "git checkout -- src/app.ts",
    "git restore src/app.ts",
  ]
  for (const command of commands) {
    const res = last(drive(ctx(), [armPayload(), pre("Bash", { command })]))
    assert.ok(isDeny(res), `expected ${command} to be classified as a mutation`)
  }
})

test("CT-H1 · MultiEdit, NotebookEdit and Write are mutations", () => {
  for (const tool of ["MultiEdit", "NotebookEdit", "Write"]) {
    const res = last(drive(ctx(), [armPayload(), pre(tool, { file_path: SRC })]))
    assert.ok(isDeny(res), `${tool} must be classified as a mutation`)
  }
})

test("CT-H1 · a READ payload is NEVER denied, however many arrive", () => {
  const payloads: Json[] = [armPayload()]
  for (let i = 0; i < 6; i++) {
    payloads.push(pre("Read", { file_path: SRC }))
    payloads.push(pre("Grep", { pattern: "abstain" }))
    payloads.push(pre("Glob", { pattern: "**/*.ts" }))
  }
  const results = drive(ctx(), payloads)
  for (const res of results.slice(1)) {
    assertInert(res, "no read is ever denied — there is no orientation budget")
  }
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H2 · I2 — a bundled recall that ABSTAINED earns feedback, never a deny
// ═════════════════════════════════════════════════════════════════════════════

const BUNDLED = "how does the recall gate abstain; and what retires a memory?"
const SINGLE = "what makes the absolute relevance gate abstain on a weak field"

test("CT-H2 · an abstained recall carrying two intents produces feedback", () => {
  const res = last(
    drive(ctx(), [
      armPayload(),
      post(`${HIVE}hive_recall`, { query: BUNDLED }, fixture("recall", "abstained")),
    ]),
  )
  // the OBSERVATION must reach the agent; the transport is the adapter's choice.
  // it rides structured stdout because this hook is detached (`async`), and a
  // detached process's stderr has no turn left to feed back into
  assert.equal(res.code, 0, "feedback never signals a block")
  assert.ok(feedbackText(res).trim().length > 0, "feedback must carry text")
  assert.equal(res.stderr.trim(), "", "the observation never rides stderr")
})

test("CT-H2 · the feedback reports BOTH observations and never blocks", () => {
  const res = last(
    drive(ctx(), [
      armPayload(),
      post(`${HIVE}hive_recall`, { query: BUNDLED }, fixture("recall", "abstained")),
    ]),
  )
  assert.match(feedbackText(res), /abstain/i, "the server's own abstain is the evidence")
  assert.match(feedbackText(res), /intent/i, "the second observation is the bundled query")
  assert.doesNotMatch(res.stdout, /"decision"\s*:\s*"block"/)
  assert.doesNotMatch(res.stdout, /permissionDecision/)
})

test("CT-H2 · an abstained single-intent recall is silent", () => {
  const res = last(
    drive(ctx(), [
      armPayload(),
      post(`${HIVE}hive_recall`, { query: SINGLE }, fixture("recall", "abstained")),
    ]),
  )
  assertInert(res, "abstained but not bundled")
  assert.equal(res.stderr, "")
})

test("CT-H2 · a CONFIDENT bundled recall is silent — the abstain is the only trigger", () => {
  const res = last(
    drive(ctx(), [
      armPayload(),
      post(`${HIVE}hive_recall`, { query: BUNDLED }, fixture("recall", "confident_multi")),
    ]),
  )
  assertInert(res, "a bundle that answered is not a problem to report")
  assert.equal(res.stderr, "")
})

test("CT-H2 · a confident single-intent recall is silent", () => {
  const res = last(
    drive(ctx(), [
      armPayload(),
      post(`${HIVE}hive_recall`, { query: SINGLE }, fixture("recall", "confident_multi")),
    ]),
  )
  assertInert(res, "confident and not bundled")
  assert.equal(res.stderr, "")
})

test("CT-H2 · an empty or non-string query never produces feedback", () => {
  for (const query of ["", null, 42, { a: 1 }, ["x"]]) {
    const res = last(
      drive(ctx(), [
        armPayload(),
        post(`${HIVE}hive_recall`, { query }, fixture("recall", "abstained")),
      ]),
    )
    assertInert(res, `query=${JSON.stringify(query)}`)
    assert.equal(res.stderr, "", `query=${JSON.stringify(query)} must stay silent`)
  }
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H3 · I3 — the store decision is made, or the turn does not end quietly
// ═════════════════════════════════════════════════════════════════════════════

function mutatedSession(): Json[] {
  return [armPayload(), recallPost("confident_multi"), post("Edit", { file_path: SRC }, { ok: true })]
}

test("CT-H3 · mutations with no landed store block the turn naming store_missing", () => {
  const end = last(drive(ctx(), [...mutatedSession(), stop("done")]))
  assert.ok(isBlock(end), `expected a block, got ${end.stdout || "(silence)"}`)
  assert.equal(end.code, 0, "a block is exit 0 with JSON on stdout")
  assert.match(blockReason(end), /store_missing/)
})

test("CT-H3 · a landed hive_write closes store_missing", () => {
  const end = last(
    drive(ctx(), [
      ...mutatedSession(),
      post(
        `${HIVE}hive_write`,
        { text: "a lesson", anchors: [{ repo: "alpha", anchor: "app.py::greet" }] },
        fixture("write", "anchored"),
      ),
      stop("done"),
    ]),
  )
  assert.doesNotMatch(blockReason(end), /store_missing/)
})

test("CT-H3 · a landed hive_capture closes store_missing", () => {
  const end = last(
    drive(ctx(), [
      ...mutatedSession(),
      post(`${HIVE}hive_capture`, { text: "a lesson", repos: ["alpha"] }, fixture("capture", "approved")),
      stop("done"),
    ]),
  )
  assert.doesNotMatch(blockReason(end), /store_missing/)
})

test("CT-H3 · a no-store sentinel WITH a rationale closes store_missing", () => {
  const end = last(
    drive(ctx(), [
      ...mutatedSession(),
      stop("Work done.\nHIVE-LOOP: no-store — the change was a one-line typo fix, nothing reusable"),
    ]),
  )
  assert.doesNotMatch(blockReason(end), /store_missing/)
})

test("CT-H3 · a no-store sentinel with an EMPTY rationale still blocks", () => {
  const end = last(drive(ctx(), [...mutatedSession(), stop("HIVE-LOOP: no-store — ")]))
  assert.ok(isBlock(end), "an empty rationale is a bypass, not a decision")
  assert.match(blockReason(end), /store_missing/)
})

test("CT-H3 · a REFUSED store never closes store_missing", () => {
  const end = last(
    drive(ctx(), [
      ...mutatedSession(),
      post(`${HIVE}hive_write`, { text: "a lesson" }, fixture("write", "refused")),
      stop("done"),
    ]),
  )
  assert.ok(isBlock(end), "a refused write did not land")
  assert.match(blockReason(end), /store_missing/)
})

test("CT-H3 · a session with zero mutations never opens store_missing", () => {
  const end = last(drive(ctx(), [armPayload(), recallPost("abstained"), stop("done")]))
  assert.doesNotMatch(blockReason(end), /store_missing/)
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H4 · I4 — the store block names BOTH verbs and defers the choice
// ═════════════════════════════════════════════════════════════════════════════

test("CT-H4 · the store_missing reason names both store verbs and defers to the served contract", () => {
  const end = last(drive(ctx(), [...mutatedSession(), stop("done")]))
  const reason = blockReason(end)
  assert.match(reason, /hive_write/)
  assert.match(reason, /hive_capture/)
  assert.match(reason, /served[^.]*contract/i, "authority lives in the served contract")
})

test("CT-H4 · the store_missing reason states no hive semantics", () => {
  const reason = blockReason(last(drive(ctx(), [...mutatedSession(), stop("done")]))).toLowerCase()
  assert.ok(reason.length > 0, "there must be a reason to scan")
  for (const word of BANNED_SEMANTICS) {
    assert.ok(!reason.includes(word), `the block reason states hive semantics: ${word}`)
  }
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H5 · I5 — recall precedes every store
// ═════════════════════════════════════════════════════════════════════════════

test("CT-H5 · a store in an armed session with no journaled recall is denied, both verbs", () => {
  for (const verb of ["hive_write", "hive_capture"]) {
    const res = last(drive(ctx(), [armPayload(), pre(`${HIVE}${verb}`, { text: "a lesson" })]))
    assert.ok(isDeny(res), `${verb} must be denied before any recall`)
    assert.match(denyReason(res), /hive_recall/)
  }
})

test("CT-H5 · a journaled recall allows the store", () => {
  const res = last(
    drive(ctx(), [armPayload(), recallPost("abstained"), pre(`${HIVE}hive_write`, { text: "a lesson" })]),
  )
  assertInert(res, "a recall is journaled")
})

test("CT-H5 · an unarmed session never denies a store", () => {
  const res = last(drive(ctx(), [pre(`${HIVE}hive_write`, { text: "a lesson" })]))
  assertInert(res, "unarmed")
})

test("CT-H5 · the deny never inspects the store text", () => {
  const argSets: Json[] = [
    { text: "how does the recall gate abstain" },
    { text: "a completely unrelated sentence about penguins" },
    { text: "" },
    {},
  ]
  const reasons = new Set<string>()
  for (const args of argSets) {
    const res = last(drive(ctx(), [armPayload(), pre(`${HIVE}hive_write`, args)]))
    assert.ok(isDeny(res), `args=${JSON.stringify(args)} must still deny`)
    reasons.add(denyReason(res))
  }
  assert.equal(reasons.size, 1, "the deny reason is text-independent — no overlap rule exists")
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H6 · I6 — a hit the SERVER marked actionable is resolved or deferred
// ═════════════════════════════════════════════════════════════════════════════

/** Only the drift verdict survives — the server's stale rider is stripped. */
function driftOnly(): Json {
  return variant("recall", "drift_anchor_missing", (env) => {
    for (const hit of (env["reference_context"] ?? []) as Record<string, Json>[]) {
      delete hit["remediation"]
    }
  })
}

/** Only the stale rider survives — the drift verdict is downgraded off the tier. */
function riderOnly(): Json {
  return variant("recall", "drift_anchor_missing", (env) => {
    for (const hit of (env["reference_context"] ?? []) as Record<string, Json>[]) {
      hit["drift"] = { type: "unverifiable", detail: { per_anchor: [] } }
    }
  })
}

test("CT-H6 · the qualifying drift verdict ALONE opens the maintenance debt", () => {
  const end = last(
    drive(ctx(), [
      armPayload(),
      post(`${HIVE}hive_recall`, { query: SINGLE }, driftOnly()),
      outcomePost(),
      stop("done"),
    ]),
  )
  assert.ok(isBlock(end), "a moved anchor the server itself flagged is actionable")
  assert.match(blockReason(end), /maintenance_missing/)
})

test("CT-H6 · the server's stale rider ALONE opens the maintenance debt", () => {
  const end = last(
    drive(ctx(), [
      armPayload(),
      post(`${HIVE}hive_recall`, { query: SINGLE }, riderOnly()),
      outcomePost(),
      stop("done"),
    ]),
  )
  assert.ok(isBlock(end), "the rider is the server's own per-hit signal")
  assert.match(blockReason(end), /maintenance_missing/)
})

test("CT-H6 · the conflicts list ALONE opens the maintenance debt", () => {
  const end = last(
    drive(ctx(), [armPayload(), recallPost("conflicts"), outcomePost(), stop("done")]),
  )
  assert.ok(isBlock(end), "a surfaced conflict is the server's own signal")
  assert.match(blockReason(end), /maintenance_missing/)
})

test("CT-H6 · every member of the qualifying tier opens the debt", () => {
  for (const scenario of ["drift_anchor_missing", "drift_anchor_changed", "drift_blast_radius_changed"]) {
    const end = last(drive(ctx(), [armPayload(), recallPost(scenario), outcomePost(), stop("done")]))
    assert.ok(isBlock(end), `${scenario} must open maintenance_missing`)
    assert.match(blockReason(end), /maintenance_missing/, scenario)
  }
})

test("CT-H6 · a verdict the server refuses to act on opens NO debt", () => {
  for (const scenario of ["drift_unverifiable", "drift_branch_scoped", "drift_fresh", "drift_na"]) {
    const end = last(drive(ctx(), [armPayload(), recallPost(scenario), outcomePost(), stop("done")]))
    assert.doesNotMatch(blockReason(end), /maintenance_missing/, scenario)
  }
})

test("CT-H6 · an AFFIRMED maintenance verb closes the debt, for each affirming shape", () => {
  const ids = servedIdsOf(fixture("recall", "drift_anchor_missing"))
  assert.ok(ids.length > 0, "the actionable fixture must serve at least one id")
  const closers: [string, Json, (id: number) => Json][] = [
    [`${HIVE}hive_prune`, fixture("prune", "affirmed"), (id) => ({ episode_id: id })],
    [`${HIVE}hive_supersede`, fixture("supersede", "affirmed"), (id) => ({ loser: id, winner: id + 1 })],
    [`${HIVE}hive_flag`, fixture("flag", "recorded"), (id) => ({ a: id, b: id + 1, kind: "contradiction" })],
    [`${HIVE}hive_write`, fixture("write", "replaces_affirmed"), (id) => ({ text: "a successor", replaces: id })],
  ]
  for (const [tool, envelope, args] of closers) {
    const end = last(
      drive(ctx(), [
        armPayload(),
        recallPost("drift_anchor_missing"),
        outcomePost(),
        ...ids.map((id) => post(tool, args(id), envelope)),
        stop("done"),
      ]),
    )
    assert.doesNotMatch(blockReason(end), /maintenance_missing/, tool)
  }
})

test("CT-H6 · a NOOP maintenance call does NOT close the debt", () => {
  const ids = servedIdsOf(fixture("recall", "drift_anchor_missing"))
  const end = last(
    drive(ctx(), [
      armPayload(),
      recallPost("drift_anchor_missing"),
      outcomePost(),
      ...ids.map((id) => post(`${HIVE}hive_prune`, { episode_id: id }, fixture("prune", "noop"))),
      stop("done"),
    ]),
  )
  assert.ok(isBlock(end), "a benign no-op retired nothing")
  assert.match(blockReason(end), /maintenance_missing/)
})

test("CT-H6 · a defer sentinel naming the ids closes the debt, and an empty rationale does not", () => {
  const ids = servedIdsOf(fixture("recall", "drift_anchor_missing"))
  const withWhy = last(
    drive(ctx(), [
      armPayload(),
      recallPost("drift_anchor_missing"),
      outcomePost(),
      stop(`HIVE-LOOP: defer ${ids.join(",")} — the successor lands in the next change`),
    ]),
  )
  assert.doesNotMatch(blockReason(withWhy), /maintenance_missing/)

  const noWhy = last(
    drive(ctx(), [
      armPayload(),
      recallPost("drift_anchor_missing"),
      outcomePost(),
      stop(`HIVE-LOOP: defer ${ids.join(",")} — `),
    ]),
  )
  assert.ok(isBlock(noWhy))
  assert.match(blockReason(noWhy), /maintenance_missing/)
})

test("CT-H6 · a partial close blocks on the remainder only", () => {
  const ids = servedIdsOf(fixture("recall", "multi_actionable"))
  assert.ok(ids.length >= 2, "the multi-actionable fixture must serve at least two ids")
  const kept = ids[0] as number
  const closed = ids[1] as number
  const end = last(
    drive(ctx(), [
      armPayload(),
      recallPost("multi_actionable"),
      outcomePost(),
      post(`${HIVE}hive_prune`, { episode_id: closed }, fixture("prune", "affirmed")),
      stop("done"),
    ]),
  )
  assert.ok(isBlock(end))
  const reason = blockReason(end)
  assert.match(reason, /maintenance_missing/)
  assert.match(reason, new RegExp(`\\b${kept}\\b`), "the remaining id is named")
  const tail = reason.slice(reason.indexOf("maintenance_missing"))
  assert.doesNotMatch(
    tail.split("\n")[0] ?? "",
    new RegExp(`\\b${closed}\\b`),
    "a closed id is not re-named",
  )
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H7 · I7 — served evidence earns an outcome call
// ═════════════════════════════════════════════════════════════════════════════

test("CT-H7 · a serving recall with no outcome call blocks, naming the served ids", () => {
  const end = last(drive(ctx(), [armPayload(), recallPost("drift_fresh"), stop("done")]))
  assert.ok(isBlock(end))
  const reason = blockReason(end)
  assert.match(reason, /outcome_missing/)
  for (const id of servedIdsOf(fixture("recall", "drift_fresh"))) {
    assert.match(reason, new RegExp(`\\b${id}\\b`), `served id ${id} must be named`)
  }
})

test("CT-H7 · an outcome call after the serving recall closes it", () => {
  const end = last(
    drive(ctx(), [armPayload(), recallPost("drift_fresh"), outcomePost(), stop("done")]),
  )
  assert.doesNotMatch(blockReason(end), /outcome_missing/)
})

test("CT-H7 · an ABSTAINED recall serves nothing, so it opens no outcome debt", () => {
  const end = last(drive(ctx(), [armPayload(), recallPost("abstained"), stop("done")]))
  assert.doesNotMatch(blockReason(end), /outcome_missing/)
})

test("CT-H7 · an outcome call BEFORE the serving recall does not close it", () => {
  const end = last(
    drive(ctx(), [armPayload(), outcomePost(), recallPost("drift_fresh"), stop("done")]),
  )
  assert.ok(isBlock(end))
  assert.match(blockReason(end), /outcome_missing/)
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H8a · I8 — a stored memory carries a binding, a scope, or a declaration
// ═════════════════════════════════════════════════════════════════════════════

function storedWith(args: Record<string, Json>, scenario: string): Json[] {
  return [
    armPayload(),
    recallPost("abstained"),
    post("Edit", { file_path: SRC }, { ok: true }),
    post(`${HIVE}hive_write`, { text: "a lesson", ...args }, fixture("write", scenario)),
  ]
}

test("CT-H8a · a store carrying anchors, repos, or both never opens scope_missing", () => {
  const cases: [Record<string, Json>, string][] = [
    [{ anchors: [{ repo: "alpha", anchor: "app.py::greet" }] }, "anchored"],
    [{ repos: ["alpha"] }, "repos_only"],
    [{ anchors: [{ repo: "alpha", anchor: "app.py::greet" }], repos: ["alpha"] }, "anchored"],
  ]
  for (const [args, scenario] of cases) {
    const end = last(drive(ctx(), [...storedWith(args, scenario), stop("done")]))
    assert.doesNotMatch(blockReason(end), /scope_missing/, JSON.stringify(args))
  }
})

test("CT-H8a · a store carrying NEITHER opens scope_missing", () => {
  const end = last(drive(ctx(), [...storedWith({}, "approved"), stop("done")]))
  assert.ok(isBlock(end))
  assert.match(blockReason(end), /scope_missing/)
})

test("CT-H8a · a general sentinel WITH a rationale closes scope_missing", () => {
  const end = last(
    drive(ctx(), [
      ...storedWith({}, "approved"),
      stop("HIVE-LOOP: general — this is a language-level fact, it binds to no repo"),
    ]),
  )
  assert.doesNotMatch(blockReason(end), /scope_missing/)
})

test("CT-H8a · a general sentinel with an EMPTY rationale still blocks", () => {
  const end = last(drive(ctx(), [...storedWith({}, "approved"), stop("HIVE-LOOP: general — ")]))
  assert.ok(isBlock(end))
  assert.match(blockReason(end), /scope_missing/)
})

test("CT-H8a · a REFUSED store never opens scope_missing", () => {
  const end = last(drive(ctx(), [...storedWith({}, "refused"), stop("done")]))
  assert.doesNotMatch(blockReason(end), /scope_missing/)
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H10 · H1 / H2 — no second contract, no transport, no credential read
// ═════════════════════════════════════════════════════════════════════════════

function shippedSources(): [string, string][] {
  const out: [string, string][] = []
  const groups: [string, string][] = [
    [CORE_DIR, "harnesses/core/"],
    [ADAPTER_DIR, "harnesses/adapters/"],
  ]
  for (const [dir, label] of groups) {
    for (const name of requireDir(dir, label)) {
      out.push([`${label}${name}`, readFileSync(join(dir, name), "utf8")])
    }
  }
  return out
}

test("CT-H10 · no shipped runtime source states a hive semantic", () => {
  for (const [name, body] of shippedSources()) {
    const lower = body.toLowerCase()
    for (const word of BANNED_SEMANTICS) {
      assert.ok(!lower.includes(word), `${name} states a hive semantic: ${word}`)
    }
  }
})

test("CT-H10 · no shipped runtime source opens a transport", () => {
  for (const [name, body] of shippedSources()) {
    for (const word of BANNED_TRANSPORT) {
      assert.ok(!body.includes(word), `${name} reaches for transport: ${word}`)
    }
  }
})

test("CT-H10 · no shipped runtime source reads a credential-shaped environment variable", () => {
  for (const [name, body] of shippedSources()) {
    for (const line of body.split("\n")) {
      if (!line.includes("env")) continue
      assert.ok(
        !CREDENTIAL_SHAPED.test(line),
        `${name} reads a credential-shaped variable: ${line.trim()}`,
      )
    }
  }
})

test("CT-H10 · every reason the harness emits states no hive semantic", () => {
  const reasons: string[] = []
  const collect = (res: Res | undefined): void => {
    if (res === undefined) return
    if (res.stderr.trim().length > 0) reasons.push(res.stderr)
    const d = denyReason(res)
    if (d) reasons.push(d)
    const b = blockReason(res)
    if (b) reasons.push(b)
    const hso = res.json?.["hookSpecificOutput"] as Record<string, Json> | undefined
    const ctxText = hso?.["additionalContext"]
    if (typeof ctxText === "string") reasons.push(ctxText)
  }

  collect(drive(ctx(), [armPayload(), pre("Edit", { file_path: SRC })])[1])
  collect(drive(ctx(), [armPayload(), pre(`${HIVE}hive_write`, { text: "x" })])[1])
  collect(
    drive(ctx(), [
      armPayload(),
      post(`${HIVE}hive_recall`, { query: BUNDLED }, fixture("recall", "abstained")),
    ])[1],
  )
  const full = drive(ctx(), [
    armPayload(),
    recallPost("drift_anchor_missing"),
    post("Edit", { file_path: SRC }, { ok: true }),
    post(`${HIVE}hive_write`, { text: "a lesson" }, fixture("write", "approved")),
    stop("done"),
    resumed(),
  ])
  collect(full[4])
  collect(full[5])

  assert.ok(reasons.length >= 5, "the collector must have driven every emitting path")
  for (const reason of reasons) {
    const lower = reason.toLowerCase()
    for (const word of BANNED_SEMANTICS) {
      assert.ok(!lower.includes(word), `an emitted reason states a hive semantic (${word}): ${reason}`)
    }
  }
})

test("CT-H10 · the plugin manifest declares exactly one server and carries no credential literal", () => {
  const manifest = requireManifest(PLUGIN_MANIFEST, "harnesses/.claude-plugin/plugin.json")
  const servers = manifest["mcpServers"] as Record<string, Json> | undefined
  assert.ok(servers !== undefined, "the manifest must declare the endpoint")
  assert.equal(Object.keys(servers).length, 1, "exactly one server is declared")
  const raw = readFileSync(PLUGIN_MANIFEST, "utf8")
  assert.doesNotMatch(raw, /(sk-[A-Za-z0-9]|ghp_|Bearer\s+[A-Za-z0-9])/, "no credential literal")
  for (const value of JSON.stringify(servers).split('"')) {
    assert.ok(
      !/^[A-Za-z0-9]{24,}$/.test(value),
      `a credential-shaped literal rides the manifest: ${value}`,
    )
  }
})

test("CT-H10 · no hooks.json command references the test tree", () => {
  requireManifest(HOOKS_MANIFEST, "harnesses/hooks/hooks.json")
  const raw = readFileSync(HOOKS_MANIFEST, "utf8")
  assert.doesNotMatch(raw, /test\//, "the scan's scoping is only sound while no hook runs the test tree")
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H11 · I9 — the shipped plugin validates, registers, and runs from anywhere
// ═════════════════════════════════════════════════════════════════════════════

test("CT-H11 · the manifest carries every field --strict requires", () => {
  const manifest = requireManifest(PLUGIN_MANIFEST, "harnesses/.claude-plugin/plugin.json")
  for (const field of ["name", "description", "version", "author"]) {
    assert.ok(manifest[field] !== undefined, `plugin.json is missing ${field}`)
  }
  assert.equal(manifest["name"], "hive-loop")
})

test("CT-H11 · claude plugin validate --strict exits 0", () => {
  requireManifest(PLUGIN_MANIFEST, "harnesses/.claude-plugin/plugin.json")
  const probe = spawnSync("claude", ["--version"], { encoding: "utf8", timeout: 60_000 })
  if (probe.status !== 0) return // the CLI leg needs the CLI; the structural leg above always runs
  const r = spawnSync("claude", ["plugin", "validate", "--strict", HARNESS_ROOT], {
    encoding: "utf8",
    timeout: 120_000,
  })
  assert.equal(r.status, 0, `validate --strict failed:\n${r.stdout}\n${r.stderr}`)
})

test("CT-H11 · hooks.json registers exactly the handled events, and the adapter agrees both ways", async () => {
  // the platform reads registrations from a top-level `hooks` record, not from the
  // file root — the event map is one level in
  const hooks = requireManifest(HOOKS_MANIFEST, "harnesses/hooks/hooks.json")[
    "hooks"
  ] as Record<string, Json>
  const mod = await requireAdapterModule()
  const map = mod["EVENT_MAP"] as Record<string, string> | undefined
  assert.ok(map !== undefined, "the adapter must export its event-name map")
  assert.deepEqual(
    Object.keys(hooks).sort(),
    Object.keys(map).sort(),
    "hooks.json and the adapter's event map must not drift",
  )
  assert.deepEqual(
    [...new Set(Object.values(map))].sort(),
    [...EVENT_KINDS].sort(),
    "the map normalizes onto exactly the four event kinds",
  )
})

test("CT-H11 · every hook command is the one adapter under the plugin root", () => {
  // the platform reads registrations from a top-level `hooks` record, not from the
  // file root — the event map is one level in
  const hooks = requireManifest(HOOKS_MANIFEST, "harnesses/hooks/hooks.json")[
    "hooks"
  ] as Record<string, Json>
  const commands: string[] = []
  for (const groups of Object.values(hooks)) {
    for (const group of groups as { hooks?: { command?: string }[] }[]) {
      for (const hook of group.hooks ?? []) commands.push(String(hook.command))
    }
  }
  assert.ok(commands.length > 0, "no hook command is registered")
  assert.equal(
    new Set(commands).size,
    1,
    `one command string serves every event, got ${JSON.stringify([...new Set(commands)])}`,
  )
  assert.equal(commands[0], 'node "${CLAUDE_PLUGIN_ROOT}/adapters/claude-code.ts"')
})

test("CT-H11 · the blocking matcher carries no read tool and no literal MCP prefix", () => {
  // the platform reads registrations from a top-level `hooks` record, not from the
  // file root — the event map is one level in
  const hooks = requireManifest(HOOKS_MANIFEST, "harnesses/hooks/hooks.json")[
    "hooks"
  ] as Record<string, Json>
  const preGroups = hooks["PreToolUse"] as { matcher?: string }[] | undefined
  assert.ok(preGroups !== undefined && preGroups.length > 0, "the blocking hook must be registered")
  for (const group of preGroups) {
    const matcher = String(group.matcher ?? "")
    for (const readTool of ["Read", "Glob", "Grep"]) {
      assert.doesNotMatch(matcher, new RegExp(`\\b${readTool}\\b`), "no read tool blocks")
    }
    assert.doesNotMatch(matcher, /mcp__hive__|mcp__plugin_/, "matchers stay prefix-agnostic")
  }
  const postGroups = hooks["PostToolUse"] as { matcher?: string }[] | undefined
  assert.ok(postGroups !== undefined && postGroups.length > 0)
  for (const group of postGroups) {
    assert.doesNotMatch(String(group.matcher ?? ""), /mcp__hive__|mcp__plugin_/)
  }
})

test("CT-H11 · no state is ever written under the ephemeral plugin root", () => {
  for (const [name, body] of shippedSources()) {
    assert.ok(
      !body.includes("CLAUDE_PLUGIN_ROOT"),
      `${name} reads the ephemeral plugin root; the ledger's home is the persistent data dir`,
    )
  }
})

test("CT-H11 · the plugin root copied to a differently-named dir with node_modules deleted still decides", () => {
  requireEntry()
  const base = mkdtempSync(join(tmpdir(), "hive-loop-renamed-"))
  const copied = join(base, "some-other-plugin-name")
  cpSync(HARNESS_ROOT, copied, { recursive: true })
  rmSync(join(copied, "node_modules"), { recursive: true, force: true })
  assert.ok(!existsSync(join(copied, "node_modules")), "the copy must have no node_modules")

  const c = ctx()
  const entry = join(copied, "adapters", "claude-code.ts")
  const results = [armPayload(), pre("Edit", { file_path: SRC })].map((p) =>
    runAt(entry, c, withCwd(c, p), {}),
  )
  const edit = results[1]
  assert.ok(edit !== undefined)
  assert.equal(edit.code, 0, `renamed copy failed: ${edit.stderr}`)
  assert.match(edit.stdout, /"permissionDecision"\s*:\s*"deny"/, "the renamed copy still decides")
  rmSync(base, { recursive: true, force: true })
})

test("CT-H11 · no build output is committed and no runtime dependency is declared", () => {
  requireEntry() // the claim is about a BUILT harness; there is nothing to assert before one exists
  const pkg = JSON.parse(readFileSync(join(HARNESS_ROOT, "package.json"), "utf8")) as Record<string, Json>
  assert.equal(pkg["private"], true)
  assert.equal(pkg["dependencies"], undefined, "a dependencies key here is a defect, not a choice")
  const dev = pkg["devDependencies"] as Record<string, Json>
  assert.deepEqual(Object.keys(dev), ["typescript"], "the dev budget is exactly one package")
  assert.ok(!existsSync(join(HARNESS_ROOT, "dist")), "there is no build, so there is no dist/")
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H12 · I10 — a session can never wedge
// ═════════════════════════════════════════════════════════════════════════════

/**
 * The maximal simultaneously-open debt set is THREE, not five: recall_missing needs
 * zero recalls while outcome_missing and maintenance_missing each need one, and
 * store_missing needs no landed store while scope_missing needs one. Two of the five
 * pairs are mutually exclusive by construction, so "every open debt in one block" is
 * asserted over both maximal sets that can actually co-exist.
 */
function maximalOpenSet(): Json[] {
  return [armPayload(), recallPost("drift_anchor_missing"), post("Edit", { file_path: SRC }, { ok: true })]
}

test("CT-H12 · every currently-open debt is named in ONE block, and the next turn-end passes", () => {
  const results = drive(ctx(), [...maximalOpenSet(), stop("done"), stop("done again")])
  const first = results[results.length - 2]
  assert.ok(first !== undefined)
  assert.ok(isBlock(first), "the first turn-end blocks once")
  const reason = blockReason(first)
  for (const key of ["outcome_missing", "maintenance_missing", "store_missing"]) {
    assert.match(reason, new RegExp(key), `${key} must be named in the same block`)
  }
  assertInert(last(results), "a debt blocks at most once per session")
})

test("CT-H12 · the other maximal open set also blocks exactly once, naming all of it", () => {
  const results = drive(ctx(), [
    ...maximalOpenSet(),
    post(`${HIVE}hive_write`, { text: "a lesson" }, fixture("write", "approved")),
    stop("done"),
    stop("done again"),
  ])
  const first = results[results.length - 2]
  assert.ok(first !== undefined)
  assert.ok(isBlock(first))
  const reason = blockReason(first)
  for (const key of ["outcome_missing", "maintenance_missing", "scope_missing"]) {
    assert.match(reason, new RegExp(key), `${key} must be named in the same block`)
  }
  assert.doesNotMatch(reason, /store_missing/, "a landed store closed it")
  assertInert(last(results), "a debt blocks at most once per session")
})

test("CT-H12 · every debt key is reachable, so none is dead", () => {
  const seen = new Set<string>()
  const runs = [
    drive(ctx(), [armPayload(), post("Edit", { file_path: SRC }, { ok: true }), stop("x")]),
    drive(ctx(), [...maximalOpenSet(), stop("x")]),
    drive(ctx(), [
      armPayload(),
      recallPost("abstained"),
      post("Edit", { file_path: SRC }, { ok: true }),
      post(`${HIVE}hive_write`, { text: "a lesson" }, fixture("write", "approved")),
      stop("x"),
    ]),
  ]
  for (const results of runs) {
    const reason = blockReason(last(results))
    for (const key of DEBT_KEYS) if (reason.includes(key)) seen.add(key)
  }
  assert.deepEqual([...seen].sort(), [...DEBT_KEYS].sort(), "every debt key must be reachable")
})

test("CT-H12 · stop_hook_active short-circuits the turn-end entirely", () => {
  const end = last(drive(ctx(), [...maximalOpenSet(), stop("done", { stop_hook_active: true })]))
  assertInert(end, "re-entrant turn-end")
})

test("CT-H12 · HIVE_LOOP__ENABLED=0 makes every hook byte-inert", () => {
  const c = ctx()
  const results = drive(
    c,
    [...maximalOpenSet(), stop("done"), pre("Edit", { file_path: SRC }), resumed()],
    { HIVE_LOOP__ENABLED: "0" },
  )
  for (const res of results) {
    assert.equal(res.code, 0)
    assert.equal(res.stdout, "", "byte-inert means no stdout")
    assert.equal(res.stderr, "", "byte-inert means no stderr")
  }
  assert.ok(!existsSync(c.stateDir), "byte-inert means no ledger is written")
})

test("CT-H12 · a missing ledger is absent, never a debt", () => {
  assertInert(last(drive(ctx(), [stop("done")])), "no ledger exists")
})

test("CT-H12 · a corrupt or unknown-version ledger reads as absent", () => {
  for (const body of ["{not json at all", "", "null", '{"version":9999,"armed":true}', "[]"]) {
    const c = ctx()
    drive(c, maximalOpenSet())
    const files = ledgerFiles(c.stateDir)
    assert.ok(files.length > 0, "the session must have written a ledger")
    for (const f of files) writeFileSync(f, body, "utf8")
    assertInert(last(drive(c, [stop("done")])), `ledger body ${JSON.stringify(body)}`)
  }
})

function ledgerFiles(dir: string): string[] {
  const out: string[] = []
  const walk = (p: string): void => {
    let entries: string[] | null = null
    try {
      entries = readdirSync(p)
    } catch {
      entries = null
    }
    if (entries === null) {
      out.push(p)
      return
    }
    for (const name of entries) walk(join(p, name))
  }
  if (existsSync(dir)) walk(dir)
  return out
}

test("CT-H12 · a malformed env value degrades to the default with one warning, never a crash", () => {
  const results = drive(
    ctx(),
    [armPayload(), pre("Edit", { file_path: SRC })],
    { HIVE_LOOP__ENABLED: "maybe" },
  )
  const edit = last(results)
  assert.equal(edit.code, 0)
  assert.ok(isDeny(edit), "a malformed value falls back to the enabled default")
  const warnLines = edit.stderr.split("\n").filter((l) => l.trim().length > 0)
  assert.equal(warnLines.length, 1, `expected exactly one warning line, got ${edit.stderr}`)
})

test("CT-H12 · a subagent's ledger never reads the parent's", () => {
  const c = ctx()
  drive(c, [armPayload(), recallPost("confident_multi")])
  run(c, withCwd(c, post("Read", { file_path: SRC }, { ok: true }, { agent_id: "sub-1" })))
  const subEdit = run(c, withCwd(c, pre("Edit", { file_path: SRC }, { agent_id: "sub-1" })))
  assert.ok(isDeny(subEdit), "the subagent holds no recall of its own")
  assertInert(run(c, withCwd(c, pre("Edit", { file_path: SRC }))), "the parent's own recall stands")
})

test("CT-H12 · hostile stdin is inert and never crashes", () => {
  const hostile = [
    "",
    "   ",
    "not json",
    "null",
    "[]",
    "42",
    '"a string"',
    "{}",
    '{"hook_event_name":"NotAnEvent"}',
    '{"hook_event_name":123,"session_id":{"a":1}}',
    '{"hook_event_name":"PreToolUse"}',
    '{"hook_event_name":"PostToolUse","tool_response":{"content":"not-an-array"}}',
    '{"hook_event_name":"PostToolUse","tool_name":null,"tool_input":[]}',
  ]
  for (const body of hostile) {
    const res = run(ctx(), body)
    assert.equal(res.code, 0, `stdin ${JSON.stringify(body)} must exit 0, got ${res.stderr}`)
    assert.equal(res.stdout.trim(), "", `stdin ${JSON.stringify(body)} must emit nothing`)
  }
})

test("CT-H12 · an unreadable or malformed config file never crashes", () => {
  const c = ctx()
  writeFileSync(join(c.repo, ".mcp.json"), "{ this is not json", "utf8")
  const edit = last(drive(c, [armPayload(), pre("Edit", { file_path: SRC })]))
  assert.equal(edit.code, 0, "a malformed config is not a crash")
})

test("CT-H12 · a resumed session inherits its open debt as context, never as a block", () => {
  const res = last(drive(ctx(), [...maximalOpenSet(), resumed()]))
  assert.equal(res.code, 0)
  const hso = res.json?.["hookSpecificOutput"] as Record<string, Json> | undefined
  assert.ok(typeof hso?.["additionalContext"] === "string", "open debt is re-stated after compaction")
  assert.ok(!isBlock(res), "a resumed session cannot block")
})

test("CT-H12 · a clean resumed session emits nothing", () => {
  assertInert(last(drive(ctx(), [armPayload(), resumed()])), "no debt to inherit")
})

// ═════════════════════════════════════════════════════════════════════════════
// CT-H13 · I11 — the core knows no framework word and the pure modules no I/O
// ═════════════════════════════════════════════════════════════════════════════

test("CT-H13 · no file under core/ carries framework vocabulary", () => {
  for (const name of requireDir(CORE_DIR, "harnesses/core/")) {
    const body = readFileSync(join(CORE_DIR, name), "utf8")
    for (const word of FRAMEWORK_WORDS) {
      assert.ok(!body.includes(word), `core/${name} names a framework concept: ${word}`)
    }
  }
})

test("CT-H13 · the pure modules import nothing from node:* and nothing from adapters/", () => {
  const names = requireDir(CORE_DIR, "harnesses/core/")
  for (const name of PURE_MODULES) {
    assert.ok(names.includes(name), `core/${name} not built yet`)
    const body = readFileSync(join(CORE_DIR, name), "utf8")
    assert.doesNotMatch(body, /from\s+["']node:/, `core/${name} must stay pure`)
    assert.doesNotMatch(body, /import\(\s*["']node:/, `core/${name} must stay pure`)
    assert.doesNotMatch(body, /from\s+["'][^"']*adapters\//, `core/${name} must not depend outward`)
  }
})

test("CT-H13 · the I/O exemption list is exactly one module long", () => {
  const names = requireDir(CORE_DIR, "harnesses/core/")
  const importsNode = names.filter((name) =>
    /from\s+["']node:/.test(readFileSync(join(CORE_DIR, name), "utf8")),
  )
  assert.deepEqual(
    importsNode.sort(),
    [...IO_EXEMPT_MODULES].sort(),
    "exactly one module under core/ owns disk; anything else is I/O drifting into the pure core",
  )
})

test("CT-H13 · nothing under core/ imports the adapter layer", () => {
  for (const name of requireDir(CORE_DIR, "harnesses/core/")) {
    const body = readFileSync(join(CORE_DIR, name), "utf8")
    assert.doesNotMatch(body, /from\s+["'][^"']*adapters\//, `core/${name} depends outward`)
  }
})
