// The portability seam, scanned. Duplicated from the frozen contract suite so a
// core edit reds in milliseconds instead of after a process-driven run.
//
// Two clauses, deliberately different in scope. The VOCABULARY clause covers all
// of core/: no file there may name a runtime's events, tools or wire fields, or
// a second adapter would be forking the rules rather than mapping onto them. The
// IMPORT clause covers the four PURE modules: they reach for nothing outside the
// language. core/state.ts is the single named exemption — it owns the ledger's
// disk — and the exemption list is asserted to be exactly one entry long, so I/O
// cannot quietly drift into a module that is supposed to be pure.

import { test } from "node:test"
import assert from "node:assert/strict"
import { existsSync, readFileSync, readdirSync } from "node:fs"
import { join } from "node:path"
import { fileURLToPath } from "node:url"

const CORE = fileURLToPath(new URL("../core/", import.meta.url))
const ADAPTERS = fileURLToPath(new URL("../adapters/", import.meta.url))

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

const PURE_MODULES = ["decide.ts", "events.ts", "hive.ts", "hive-constants.ts"]
const IO_EXEMPT = ["state.ts"]

const BANNED_SEMANTICS = [
  "provisional",
  "quarantine",
  "established",
  "deprecated",
  "promotion",
  "promote",
  "demand",
  "vouch",
  "trust",
  "durable lesson",
  "non-obvious",
  "single-pointed",
  "outcome-verified",
]

const BANNED_TRANSPORT = [
  "node:http",
  "node:https",
  "node:net",
  "node:dgram",
  "node:tls",
  "child_process",
  "WebSocket",
  "fetch(",
]

function sources(dir: string): [string, string][] {
  assert.ok(existsSync(dir), `${dir} not built yet`)
  return readdirSync(dir)
    .filter((n) => n.endsWith(".ts"))
    .map((n) => [n, readFileSync(join(dir, n), "utf8")] as [string, string])
}

test("no file under core/ names a runtime's events, tools or wire fields", () => {
  for (const [name, body] of sources(CORE)) {
    for (const word of FRAMEWORK_WORDS) {
      assert.ok(!body.includes(word), `core/${name} names a framework concept: ${word}`)
    }
  }
})

test("the pure modules import nothing outside the language", () => {
  const present = sources(CORE).map(([name]) => name)
  for (const name of PURE_MODULES) {
    assert.ok(present.includes(name), `core/${name} not built yet`)
    const body = readFileSync(join(CORE, name), "utf8")
    assert.doesNotMatch(body, /from\s+["']node:/, `core/${name} must stay pure`)
    assert.doesNotMatch(body, /import\(\s*["']node:/, `core/${name} must stay pure`)
  }
})

test("the I/O exemption list is exactly one module long", () => {
  const importsNode = sources(CORE)
    .filter(([, body]) => /from\s+["']node:/.test(body))
    .map(([name]) => name)
  assert.deepEqual(importsNode.sort(), [...IO_EXEMPT].sort())
})

test("nothing under core/ depends outward on an adapter", () => {
  for (const [name, body] of sources(CORE)) {
    assert.doesNotMatch(body, /from\s+["'][^"']*adapters\//, `core/${name} depends outward`)
  }
})

test("no shipped runtime source states a hive semantic or opens a transport", () => {
  const shipped = [...sources(CORE), ...(existsSync(ADAPTERS) ? sources(ADAPTERS) : [])]
  assert.ok(shipped.length > 0)
  for (const [name, body] of shipped) {
    const lower = body.toLowerCase()
    for (const word of BANNED_SEMANTICS) {
      assert.ok(!lower.includes(word), `${name} states a hive semantic: ${word}`)
    }
    for (const word of BANNED_TRANSPORT) {
      assert.ok(!body.includes(word), `${name} reaches for transport: ${word}`)
    }
  }
})

// ── every emitting decision owes a delivery proof ────────────────────────────
//
// A decision that serializes correctly and is discarded by the platform is a
// green suite over an inert feature. The live tier proves each CHANNEL is
// actually received; this binds the two, so a new channel cannot be added
// without one. Both sides are read from source — nothing is hand-copied, or the
// registry itself becomes the thing that drifts.

const EVENTS = fileURLToPath(new URL("../core/events.ts", import.meta.url))
const LIVE_SUITE = fileURLToPath(new URL("./live.test.ts", import.meta.url))

/** the Decision union's members, straight from the type that declares them */
function decisionKinds(): string[] {
  const body = readFileSync(EVENTS, "utf8")
  const union = /export type Decision =([\s\S]*?)\n\n/.exec(body)
  assert.ok(union !== null, "core/events.ts declares no Decision union")
  return [...(union[1] ?? "").matchAll(/kind:\s*"([a-z]+)"/g)].map((m) => m[1] as string)
}

/** the channels the live tier actually drives end to end */
function probedChannels(): string[] {
  const body = readFileSync(LIVE_SUITE, "utf8")
  const table = /const CHANNELS:[\s\S]*?\n\]/.exec(body)
  assert.ok(table !== null, "the live tier declares no CHANNELS table")
  return [...table[0].matchAll(/kind:\s*"([a-z]+)"/g)].map((m) => m[1] as string)
}

test("every decision that EMITS has a live delivery probe", () => {
  // `inert` writes nothing, so it has nothing to deliver — it is the one
  // exemption, and naming it here is what keeps the exemption reviewable
  const emitting = decisionKinds().filter((k) => k !== "inert")
  const probed = probedChannels()
  assert.ok(emitting.length > 0, "no emitting decision kinds found")
  for (const kind of emitting) {
    assert.ok(
      probed.includes(kind),
      `the ${kind} decision emits but no live probe proves it is DELIVERED — ` +
        `add one to live.test.ts's CHANNELS table`,
    )
  }
})

test("no delivery probe outlives the decision it covers", () => {
  const kinds = decisionKinds()
  for (const kind of probedChannels()) {
    assert.ok(kinds.includes(kind), `a probe covers "${kind}", which is no longer a decision kind`)
  }
})
