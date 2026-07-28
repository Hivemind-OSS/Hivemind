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
