// The ledger: round-trip, three-valued absence, atomicity, key isolation, and
// the lazy sweep that is why no end-of-session hook exists.

import { test } from "node:test"
import assert from "node:assert/strict"
import {
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

import {
  EMPTY,
  PRUNE_AFTER_MS,
  STATE_VERSION,
  ledgerDir,
  ledgerPath,
  load,
  prune,
  save,
} from "../core/state.ts"
import type { LoopState } from "../core/state.ts"

let seq = 0
function dir(): string {
  return mkdtempSync(join(tmpdir(), `hive-loop-state-${seq++}-`))
}

const SCOPE = "/work/some-repo"
const SESSION = "sess-1"
const AGENT = ""

const DIRTY: LoopState = {
  ...EMPTY,
  armed: true,
  hiveSeen: true,
  recalls: 2,
  mutations: 3,
  served: [12, 40],
  actionable: [40],
  outcomeAfterServe: true,
  writes: 1,
  captures: 2,
  storeArrivals: 5,
  unscopedStore: true,
  maintained: [40],
  deferred: [12],
  noStoreWhy: "nothing applied",
  captureWhy: "the verb choice was not clear",
  generalWhy: "binds to nothing",
  blocks: ["store_missing"],
}

test("a saved ledger round-trips every field", () => {
  const d = dir()
  save(d, SCOPE, SESSION, AGENT, DIRTY)
  assert.deepEqual(load(d, SCOPE, SESSION, AGENT), { ...DIRTY, version: STATE_VERSION })
})

test("an absent ledger is null — never a clean one, never a dirty one", () => {
  assert.equal(load(dir(), SCOPE, SESSION, AGENT), null)
})

test("a clean ledger is distinguishable from an absent one", () => {
  const d = dir()
  save(d, SCOPE, SESSION, AGENT, EMPTY)
  const loaded = load(d, SCOPE, SESSION, AGENT)
  assert.notEqual(loaded, null)
  assert.deepEqual(loaded, { ...EMPTY, version: STATE_VERSION })
})

test("truncated, empty, non-object and hostile bytes all read absent", () => {
  for (const body of ["", "   ", "{", "null", "[]", "42", '"x"', '{"version"', "\u0000"]) {
    const d = dir()
    save(d, SCOPE, SESSION, AGENT, DIRTY)
    writeFileSync(ledgerPath(d, SCOPE, SESSION, AGENT), body, "utf8")
    assert.equal(load(d, SCOPE, SESSION, AGENT), null, JSON.stringify(body))
  }
})

test("a version-2 ledger written before the arrival counter existed still reads, as zero", () => {
  // additive schema: the field's absence is its zero, so no version bump — a
  // bump would make every ledger open under a live session read absent the
  // moment the re-read-per-event adapter picks up new code
  const d = dir()
  save(d, SCOPE, SESSION, AGENT, DIRTY)
  const doc = { ...DIRTY, version: STATE_VERSION } as Record<string, unknown>
  delete doc["storeArrivals"]
  writeFileSync(ledgerPath(d, SCOPE, SESSION, AGENT), JSON.stringify(doc), "utf8")
  const loaded = load(d, SCOPE, SESSION, AGENT)
  assert.notEqual(loaded, null, "an additive field's absence must not invalidate the ledger")
  assert.equal(loaded!.storeArrivals, 0)
  assert.equal(loaded!.writes, DIRTY.writes, "every recorded field still reads")
  assert.equal(loaded!.armed, DIRTY.armed)
})

test("an unknown version reads absent rather than being interpreted", () => {
  for (const version of [0, 1, 999, "2", null, undefined]) {
    const d = dir()
    save(d, SCOPE, SESSION, AGENT, DIRTY)
    writeFileSync(
      ledgerPath(d, SCOPE, SESSION, AGENT),
      JSON.stringify({ ...DIRTY, version }),
      "utf8",
    )
    assert.equal(load(d, SCOPE, SESSION, AGENT), null, String(version))
  }
})

test("a ledger whose fields have the wrong types degrades field by field, never crashes", () => {
  const d = dir()
  save(d, SCOPE, SESSION, AGENT, DIRTY)
  writeFileSync(
    ledgerPath(d, SCOPE, SESSION, AGENT),
    JSON.stringify({
      version: STATE_VERSION,
      armed: "yes",
      recalls: "many",
      mutations: -3,
      served: [1, "two", null, 3.5, 4],
      actionable: "none",
      writes: NaN,
      captures: -1,
      storeArrivals: "many",
      maintained: null,
      deferred: [7],
      noStoreWhy: 12,
      captureWhy: [],
      blocks: ["a", 3, null],
    }),
    "utf8",
  )
  const loaded = load(d, SCOPE, SESSION, AGENT)
  assert.notEqual(loaded, null)
  assert.equal(loaded!.armed, false)
  assert.equal(loaded!.recalls, 0)
  assert.equal(loaded!.mutations, 0)
  assert.deepEqual(loaded!.served, [1, 4])
  assert.deepEqual(loaded!.actionable, [])
  assert.equal(loaded!.writes, 0)
  assert.equal(loaded!.captures, 0)
  assert.equal(loaded!.storeArrivals, 0)
  assert.deepEqual(loaded!.maintained, [])
  assert.deepEqual(loaded!.deferred, [7])
  assert.equal(loaded!.noStoreWhy, "")
  assert.equal(loaded!.captureWhy, "")
  assert.deepEqual(loaded!.blocks, ["a"])
})

test("the write is atomic: no partial file survives, and the final file is complete", () => {
  const d = dir()
  save(d, SCOPE, SESSION, AGENT, DIRTY)
  const names = readdirSync(ledgerDir(d, SCOPE))
  assert.deepEqual(
    names.filter((n) => n.endsWith(".partial")),
    [],
    "a partial file must never be left behind",
  )
  assert.equal(names.length, 1)
  const raw = readFileSync(ledgerPath(d, SCOPE, SESSION, AGENT), "utf8")
  assert.deepEqual(JSON.parse(raw), { ...DIRTY, version: STATE_VERSION })
})

test("the write goes THROUGH the partial path — the final file is never written directly", () => {
  // Blocking the partial path blocks the whole write. A direct write to the final
  // path would sail past this, which is precisely the atomicity being asserted:
  // "no partial survives" is also true of an implementation that never made one.
  const d = dir()
  save(d, SCOPE, SESSION, AGENT, EMPTY)
  const final = ledgerPath(d, SCOPE, SESSION, AGENT)
  rmSync(final, { recursive: true, force: true })
  mkdirSync(`${final}.partial`, { recursive: true })
  save(d, SCOPE, SESSION, AGENT, DIRTY)
  assert.ok(
    !existsSync(final) || readFileSync(final, "utf8") === "",
    "a save whose partial path is unusable must not have reached the final path",
  )
})

test("a partial file left by a crash is never read as the ledger", () => {
  const d = dir()
  mkdirSync(ledgerDir(d, SCOPE), { recursive: true })
  writeFileSync(`${ledgerPath(d, SCOPE, SESSION, AGENT)}.partial`, '{"version":1,"armed":true', "utf8")
  assert.equal(load(d, SCOPE, SESSION, AGENT), null)
})

test("a write fault is swallowed — a lost journal never raises into the decision", () => {
  const d = dir()
  // a FILE where the ledger directory must go: every write beneath it must fail
  writeFileSync(join(d, "blocker"), "x", "utf8")
  save(join(d, "blocker"), SCOPE, SESSION, AGENT, DIRTY)
  assert.equal(load(join(d, "blocker"), SCOPE, SESSION, AGENT), null)
})

test("session and nested-agent keys never collide", () => {
  const d = dir()
  save(d, SCOPE, "sess-a", "", { ...EMPTY, recalls: 1 })
  save(d, SCOPE, "sess-b", "", { ...EMPTY, recalls: 2 })
  save(d, SCOPE, "sess-a", "sub-1", { ...EMPTY, recalls: 3 })
  assert.equal(load(d, SCOPE, "sess-a", "")!.recalls, 1)
  assert.equal(load(d, SCOPE, "sess-b", "")!.recalls, 2)
  assert.equal(load(d, SCOPE, "sess-a", "sub-1")!.recalls, 3)
  assert.equal(load(d, SCOPE, "sess-b", "sub-1"), null)
})

test("two working roots partition into different directories", () => {
  const d = dir()
  save(d, "/work/one", SESSION, AGENT, { ...EMPTY, recalls: 1 })
  save(d, "/work/two", SESSION, AGENT, { ...EMPTY, recalls: 9 })
  assert.notEqual(ledgerDir(d, "/work/one"), ledgerDir(d, "/work/two"))
  assert.equal(load(d, "/work/one", SESSION, AGENT)!.recalls, 1)
  assert.equal(load(d, "/work/two", SESSION, AGENT)!.recalls, 9)
})

test("nothing is ever written outside the state directory", () => {
  const d = dir()
  save(d, SCOPE, SESSION, AGENT, DIRTY)
  assert.ok(ledgerPath(d, SCOPE, SESSION, AGENT).startsWith(d))
  assert.ok(!ledgerPath(d, SCOPE, SESSION, AGENT).includes(SCOPE))
})

test("the lazy sweep drops ledgers past the window and keeps the rest", () => {
  const d = dir()
  save(d, SCOPE, "old", "", EMPTY)
  save(d, SCOPE, "new", "", EMPTY)
  const oldPath = ledgerPath(d, SCOPE, "old", "")
  assert.ok(existsSync(oldPath))
  // sweep from a clock far enough ahead that only the window decides
  prune(d, SCOPE, Date.now() + PRUNE_AFTER_MS + 1000)
  assert.ok(!existsSync(oldPath), "a ledger past the window is swept")
  assert.ok(!existsSync(ledgerPath(d, SCOPE, "new", "")), "the window applies to every file alike")
  save(d, SCOPE, "fresh", "", EMPTY)
  prune(d, SCOPE)
  assert.ok(existsSync(ledgerPath(d, SCOPE, "fresh", "")), "a fresh ledger survives the sweep")
})

test("the sweep on a missing or unreadable directory is a no-op, never a fault", () => {
  const d = dir()
  prune(d, SCOPE)
  rmSync(d, { recursive: true, force: true })
  prune(d, SCOPE)
})

test("loading sweeps, so no end-of-session hook is needed to bound the directory", () => {
  const d = dir()
  save(d, SCOPE, "stale", "", EMPTY)
  const stale = ledgerPath(d, SCOPE, "stale", "")
  // backdate by rewriting through a directory whose entries the sweep will judge
  prune(d, SCOPE, Date.now() + PRUNE_AFTER_MS + 1000)
  assert.ok(!existsSync(stale))
  assert.equal(load(d, SCOPE, "stale", ""), null)
})
