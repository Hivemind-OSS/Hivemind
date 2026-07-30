// BUG-101's contract: the journalling pipeline reads the DELIVERED wrapping of a
// memory call's result, not just the wrappings the server-side fixtures record.
// The platform (measured 2026-07-29, claude 2.1.220, probe session 121c1fb9…)
// hands the PostToolUse `tool_response` for an MCP tool to the hook as the BARE
// content-block array — `[{"type":"text","text":"<envelope json>"}]` — a fourth
// wrapping `readEnvelope` did not accept, so every landed store journaled as
// absent and `store_missing` fired spuriously while recalls (envelope-free)
// counted normally.
//
// A NEW file by design: the frozen contract.test.ts is never edited, so the
// delivered-wrapping contract lands beside it. Authored red against the unfixed
// system (the array-wrapping rows fail exactly as the diagnosis replay did);
// from then on it is FROZEN — never edited to make an implementation pass.
//
// Every test drives the real entry point AS A PROCESS with a real payload on
// stdin and asserts the LEDGER it wrote (the pipeline's terminal state). All
// envelope bytes come from the GENERATED fixture file; only the WRAPPING is
// authored here, because the wrapping is platform truth, not server truth.

import { test } from "node:test"
import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { fileURLToPath } from "node:url"

const HARNESS_ROOT = fileURLToPath(new URL("../", import.meta.url))
const ENTRY = join(HARNESS_ROOT, "adapters", "claude-code.ts")
const FIXTURES = join(HARNESS_ROOT, "test", "fixtures", "envelopes.json")

type Json = unknown

function requireEntry(): string {
  if (!existsSync(ENTRY)) assert.fail("harnesses/adapters/claude-code.ts not built yet")
  return ENTRY
}

function requireFixtures(): Record<string, Record<string, Json>> {
  if (!existsSync(FIXTURES)) assert.fail("envelope fixture not generated yet")
  return JSON.parse(readFileSync(FIXTURES, "utf8")) as Record<string, Record<string, Json>>
}

/** A recorded server result frame — `{"content":[…],"isError":…}` — verbatim. */
function fixture(group: string, scenario: string): Json {
  const slot = requireFixtures()[group]?.[scenario]
  assert.ok(slot !== undefined, `envelope fixture ${group}.${scenario} not generated yet`)
  return JSON.parse(JSON.stringify(slot)) as Json
}

// ── the delivered wrappings: test-local consts, provenance stated ────────────
//
// Recorded 2026-07-29 against claude 2.1.220 with a tee hook in an isolated
// session over a scratch stdio MCP server: PostToolUse delivered every hive
// verb's `tool_response` as the frame's content list BARE, type `list`, tool
// names unaltered (`mcp__hive__hive_write`). The three previously-accepted
// wrappings (frame object, bare envelope, either as a JSON string) remain
// runtime possibilities and are pinned below as regression rows.

/** The shape the platform actually delivers: the frame's content list, bare. */
function deliveredArray(frame: Json): Json {
  const content = (frame as { content?: Json[] }).content
  assert.ok(Array.isArray(content) && content.length > 0, "recorded frame has no content list")
  return JSON.parse(JSON.stringify(content)) as Json
}

/** The envelope the fixture's frame carries, read for expected ids. */
function innerEnvelope(frame: Json): Record<string, Json> {
  const text = (frame as { content?: { text?: string }[] }).content?.[0]?.text
  assert.ok(typeof text === "string", "recorded frame carries no content[].text")
  return JSON.parse(text) as Record<string, Json>
}

function expectedServed(frame: Json): number[] {
  const hits = (innerEnvelope(frame)["reference_context"] ?? []) as { episode_id?: number }[]
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
  const base = mkdtempSync(join(tmpdir(), `hive-loop-delivered-${seq++}-`))
  const repo = join(base, "repo")
  const home = join(base, "home")
  mkdirSync(join(repo, "src"), { recursive: true })
  mkdirSync(join(home, ".claude"), { recursive: true })
  writeFileSync(join(repo, "src", "app.ts"), "export const a = 1\n", "utf8")
  writeFileSync(
    join(repo, ".mcp.json"),
    JSON.stringify({ mcpServers: { hive: { type: "http", url: "http://127.0.0.1:8765/mcp" } } }),
    "utf8",
  )
  return { stateDir: join(base, "state"), repo, home }
}

interface Res {
  readonly code: number
  readonly stdout: string
  readonly stderr: string
}

function run(c: Ctx, payload: Json): Res {
  const r = spawnSync(process.execPath, [requireEntry()], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    cwd: c.repo,
    timeout: 30_000,
    env: {
      PATH: process.env["PATH"],
      HOME: c.home,
      HIVE_LOOP__STATE_DIR: c.stateDir,
    },
  })
  return { code: r.status ?? -1, stdout: r.stdout ?? "", stderr: r.stderr ?? "" }
}

function drive(c: Ctx, payloads: Json[]): Res[] {
  return payloads.map((p) => run(c, p))
}

const SID = "sess-delivered-0001"
const SRC = "src/app.ts"

function post(c: Ctx, tool: string, input: Json, response: Json): Json {
  return {
    session_id: SID,
    hook_event_name: "PostToolUse",
    cwd: c.repo,
    tool_name: tool,
    tool_input: input,
    tool_response: response,
  }
}

/** Observing a source read is what arms the session (arming is observed, never predicted). */
function arm(c: Ctx): Json {
  return post(c, "Read", { file_path: SRC }, { ok: true })
}

/** A tree-changing call, observed after it ran — what the store loop hangs off. */
function edit(c: Ctx): Json {
  return post(c, "Edit", { file_path: SRC }, { ok: true })
}

function stop(c: Ctx): Json {
  return {
    session_id: SID,
    hook_event_name: "Stop",
    cwd: c.repo,
    stop_hook_active: false,
    last_assistant_message: "done.",
  }
}

/** The one ledger this context's session wrote, read back from disk. */
function theLedger(c: Ctx): Record<string, unknown> {
  assert.ok(existsSync(c.stateDir), "no ledger directory was written — the entry never journaled")
  const out: Record<string, unknown>[] = []
  for (const bucket of readdirSync(c.stateDir)) {
    const dir = join(c.stateDir, bucket)
    let names: string[] = []
    try {
      names = readdirSync(dir)
    } catch {
      continue
    }
    for (const name of names) {
      out.push(JSON.parse(readFileSync(join(dir, name), "utf8")) as Record<string, unknown>)
    }
  }
  assert.equal(out.length, 1, `expected exactly one ledger, found ${out.length}`)
  return out[0] as Record<string, unknown>
}

function blockReason(res: Res): string {
  const trimmed = res.stdout.trim()
  if (trimmed === "") return ""
  const parsed = JSON.parse(trimmed) as Record<string, Json>
  return parsed["decision"] === "block" ? String(parsed["reason"] ?? "") : ""
}

function contextText(res: Res): string {
  const trimmed = res.stdout.trim()
  if (trimmed === "") return ""
  const parsed = JSON.parse(trimmed) as Record<string, Json>
  const hso = parsed["hookSpecificOutput"] as Record<string, Json> | undefined
  const text = hso?.["additionalContext"]
  return typeof text === "string" ? text : ""
}

const HIVE = "mcp__hive__"

// ═════════════════════════════════════════════════════════════════════════════
// I1 — a landed store delivered with the platform's array wrapping journals
// ═════════════════════════════════════════════════════════════════════════════

test("I1 · a store delivered as the bare content-block array journals under its verb", () => {
  const rows: [string, string, string][] = [
    ["hive_write", "write", "writes"],
    ["hive_capture", "capture", "captures"],
  ]
  for (const [verb, group, counter] of rows) {
    const c = ctx()
    drive(c, [
      arm(c),
      post(
        c,
        `${HIVE}${verb}`,
        { text: "a lesson", anchors: [{ repo: "r", anchor: "f.py::g" }] },
        deliveredArray(fixture(group, "approved")),
      ),
    ])
    const ledger = theLedger(c)
    assert.equal(
      ledger[counter],
      1,
      `a landed ${verb} delivered as the content-block array must journal ` +
        `(${counter}=${JSON.stringify(ledger[counter])}) — the shape the platform actually hands over`,
    )
  }
})

test("I1 · the namespace the runtime wraps the server in does not matter for the delivered array", () => {
  for (const ns of ["mcp__hive__", "mcp__plugin_hive-loop_hive__"]) {
    for (const [verb, group, counter] of [
      ["hive_write", "write", "writes"],
      ["hive_capture", "capture", "captures"],
    ] as const) {
      const c = ctx()
      drive(c, [
        arm(c),
        post(c, `${ns}${verb}`, { text: "a lesson" }, deliveredArray(fixture(group, "approved"))),
      ])
      assert.equal(theLedger(c)[counter], 1, `${ns}${verb} must journal from the delivered array`)
    }
  }
})

test("I1 · the recorded frame object still journals (no regression)", () => {
  const c = ctx()
  drive(c, [arm(c), post(c, `${HIVE}hive_write`, { text: "a lesson" }, fixture("write", "approved"))])
  assert.equal(theLedger(c)["writes"], 1, "the result-frame wrapping must keep journaling")
})

test("I1 · the frame as a JSON string still journals (no regression)", () => {
  const c = ctx()
  drive(c, [
    arm(c),
    post(c, `${HIVE}hive_write`, { text: "a lesson" }, JSON.stringify(fixture("write", "approved"))),
  ])
  assert.equal(theLedger(c)["writes"], 1, "the string-of-frame wrapping must keep journaling")
})

test("I1 · the delivered array as a JSON string journals", () => {
  const c = ctx()
  drive(c, [
    arm(c),
    post(
      c,
      `${HIVE}hive_write`,
      { text: "a lesson" },
      JSON.stringify(deliveredArray(fixture("write", "approved"))),
    ),
  ])
  assert.equal(
    theLedger(c)["writes"],
    1,
    "a JSON string carrying the content-block array is the same delivery one hop stringified",
  )
})

// ═════════════════════════════════════════════════════════════════════════════
// I2 — every envelope-fed behavior reads the delivered payload
// ═════════════════════════════════════════════════════════════════════════════

test("I2 · a serving recall's ids populate from the delivered array", () => {
  const frame = fixture("recall", "confident_multi")
  const c = ctx()
  drive(c, [arm(c), post(c, `${HIVE}hive_recall`, { query: "one question" }, deliveredArray(frame))])
  const ledger = theLedger(c)
  assert.equal(ledger["recalls"], 1)
  assert.deepEqual(
    ledger["served"],
    expectedServed(frame),
    "the served ids must be read out of the delivered array wrapping",
  )
})

test("I2 · a server-marked actionable id populates from the delivered array", () => {
  const frame = fixture("recall", "drift_anchor_missing")
  const c = ctx()
  drive(c, [arm(c), post(c, `${HIVE}hive_recall`, { query: "one question" }, deliveredArray(frame))])
  const ledger = theLedger(c)
  assert.deepEqual(
    ledger["actionable"],
    expectedServed(frame),
    "the server's own drift marks must be read out of the delivered array wrapping",
  )
})

test("I2 · an abstained bundled recall earns its observation through the delivered array", () => {
  const c = ctx()
  const results = drive(c, [
    arm(c),
    post(
      c,
      `${HIVE}hive_recall`,
      { query: "how does the gate abstain; and what retires a memory?" },
      deliveredArray(fixture("recall", "abstained")),
    ),
  ])
  const text = contextText(results[1] as Res)
  assert.ok(
    text.length > 0,
    "the abstain-crossed-with-a-bundled-query observation needs the envelope's own abstain, " +
      `which rides the delivered array (got stdout=${JSON.stringify((results[1] as Res).stdout)})`,
  )
  const ledger = theLedger(c)
  assert.equal(ledger["recalls"], 1)
  assert.deepEqual(ledger["served"], [], "an abstained recall serves nothing")
})

test("I2 · an affirmed maintenance call credits its named ids through the delivered array", () => {
  const c = ctx()
  drive(c, [
    arm(c),
    post(
      c,
      `${HIVE}hive_supersede`,
      { loser: 7, winner: 8 },
      deliveredArray(fixture("supersede", "affirmed")),
    ),
  ])
  assert.deepEqual(
    theLedger(c)["maintained"],
    [7, 8],
    "an affirmed maintenance envelope must credit through the delivered array wrapping",
  )
})

test("I2 · a store's retirement rider credits through the delivered array", () => {
  const c = ctx()
  drive(c, [
    arm(c),
    post(
      c,
      `${HIVE}hive_write`,
      { text: "a successor lesson", replaces: 3 },
      deliveredArray(fixture("write", "replaces_affirmed")),
    ),
  ])
  const ledger = theLedger(c)
  assert.equal(ledger["writes"], 1)
  assert.deepEqual(
    ledger["maintained"],
    [3],
    "the rider's reported outcome rides the same delivered envelope as the store it accompanied",
  )
})

test("I2 · a refused store delivered as the array never credits, and store_missing still opens", () => {
  const c = ctx()
  const results = drive(c, [
    arm(c),
    edit(c),
    post(c, `${HIVE}hive_write`, { text: "a lesson" }, deliveredArray(fixture("write", "refused"))),
    stop(c),
  ])
  const ledger = theLedger(c)
  assert.equal(ledger["writes"], 0, "a refused store landed nothing — parsing it must not credit it")
  assert.match(
    blockReason(results[3] as Res),
    /store_missing/,
    "the store loop item must still open on a refused store",
  )
})

// ═════════════════════════════════════════════════════════════════════════════
// I3 — arrival-without-parse is ledger-visible, and closes nothing
// ═════════════════════════════════════════════════════════════════════════════

test("I3 · an arrived store whose result yields no envelope is counted, and closes nothing", () => {
  const c = ctx()
  const results = drive(c, [
    arm(c),
    edit(c),
    post(c, `${HIVE}hive_write`, { text: "a lesson" }, [{ type: "text", text: "oops — not json" }]),
    post(c, `${HIVE}hive_capture`, { text: "another" }, "complete junk"),
    stop(c),
  ])
  const ledger = theLedger(c)
  assert.equal(
    ledger["storeArrivals"],
    2,
    "both store events ARRIVED; a ledger where arrival is invisible reads exactly like one " +
      `where the platform never fired (got ${JSON.stringify(ledger["storeArrivals"])})`,
  )
  assert.equal(ledger["writes"], 0, "an unreadable result must never credit a write")
  assert.equal(ledger["captures"], 0, "an unreadable result must never credit a capture")
  assert.match(
    blockReason(results[4] as Res),
    /store_missing/,
    "arrival alone closes nothing — the store loop item still opens",
  )
})

test("I3 · a refused store counts as arrived, never as landed", () => {
  const c = ctx()
  drive(c, [
    arm(c),
    post(c, `${HIVE}hive_write`, { text: "a lesson" }, deliveredArray(fixture("write", "refused"))),
  ])
  const ledger = theLedger(c)
  assert.equal(ledger["storeArrivals"], 1, "the refused store's event arrived and must be counted")
  assert.equal(ledger["writes"], 0, "the refusal must still never credit")
})

test("I3 · with no store event, the arrival count journals as zero", () => {
  const c = ctx()
  drive(c, [
    arm(c),
    post(c, `${HIVE}hive_recall`, { query: "one question" }, fixture("recall", "confident_multi")),
    post(c, `${HIVE}hive_supersede`, { loser: 7, winner: 8 }, fixture("supersede", "affirmed")),
    edit(c),
  ])
  const ledger = theLedger(c)
  assert.equal(
    ledger["storeArrivals"],
    0,
    "recall and maintenance events are not store arrivals — silence must journal as a countable zero",
  )
  assert.equal(ledger["recalls"], 1)
})
