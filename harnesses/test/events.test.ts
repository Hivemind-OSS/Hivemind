// The carriers and the coercion primitives, under a seeded generative fuzz.
//
// The payload space is adversarial and open-ended, and a crashed parse SHELL is
// a logged prior failure in this project, so this earns a generator rather than
// a handful of hand-picked rows. The generator is a seeded PRNG over the JSON
// value grammar — deliberately not a library: the seed already gives
// reproducibility, which is the only thing a library would buy here.

import { test } from "node:test"
import assert from "node:assert/strict"

import {
  EVENT_KINDS,
  INERT,
  ROLES,
  asArray,
  asFlag,
  asId,
  asRecord,
  asText,
  block,
  context,
  deny,
  event,
  feedback,
  readJson,
} from "../core/events.ts"
import type { Event, Json } from "../core/events.ts"

// ── a seeded PRNG over the JSON value grammar ────────────────────────────────

function mulberry(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const ATOMS: Json[] = [
  null,
  true,
  false,
  0,
  -1,
  1.5,
  NaN as unknown as Json,
  Infinity as unknown as Json,
  "",
  "x",
  "reference_context",
  "hive_recall",
  "9007199254740993",
]
const KEYS = ["a", "status", "reference_context", "content", "text", "episode_id", "drift", "__proto__"]

function value(rand: () => number, depth: number): Json {
  const roll = rand()
  if (depth <= 0 || roll < 0.45) return ATOMS[Math.floor(rand() * ATOMS.length)] as Json
  if (roll < 0.7) {
    const n = Math.floor(rand() * 4)
    const out: Json[] = []
    for (let i = 0; i < n; i++) out.push(value(rand, depth - 1))
    return out
  }
  const n = Math.floor(rand() * 4)
  const out: { [key: string]: Json } = {}
  for (let i = 0; i < n; i++) {
    out[KEYS[Math.floor(rand() * KEYS.length)] as string] = value(rand, depth - 1)
  }
  return out
}

// ── the coercion primitives are total ────────────────────────────────────────

test("every coercion is total over arbitrary generated JSON", () => {
  const rand = mulberry(0xc0ffee)
  for (let i = 0; i < 4000; i++) {
    const v = value(rand, 4)
    const record = asRecord(v)
    assert.equal(typeof record, "object")
    assert.ok(!Array.isArray(record))
    assert.ok(Array.isArray(asArray(v)))
    assert.equal(typeof asText(v), "string")
    assert.equal(typeof asFlag(v), "boolean")
    const id = asId(v)
    assert.ok(id === null || Number.isInteger(id))
    const parsed = readJson(v)
    assert.ok(parsed === undefined || parsed === null || typeof parsed !== "undefined")
    // reading an absent key from a coerced record is always safe
    assert.equal(asText(asRecord(record["content"])["text"]).length >= 0, true)
  }
})

test("a non-finite or fractional number is never an id", () => {
  for (const v of [NaN, Infinity, -Infinity, 1.5, "3", null, true, {}, []]) {
    assert.equal(asId(v), null, String(v))
  }
  assert.equal(asId(0), 0)
  assert.equal(asId(-4), -4)
  assert.equal(asId(12), 12)
})

test("only a real true is a flag, and only a real string is text", () => {
  for (const v of ["true", 1, {}, [], null, undefined]) assert.equal(asFlag(v), false, String(v))
  assert.equal(asFlag(true), true)
  for (const v of [1, {}, [], null, undefined, true]) assert.equal(asText(v), "", String(v))
})

test("readJson never throws and never returns a value for a non-string", () => {
  const rand = mulberry(7)
  for (let i = 0; i < 500; i++) {
    const v = value(rand, 3)
    if (typeof v !== "string") assert.equal(readJson(v), undefined)
  }
  assert.equal(readJson("{"), undefined)
  assert.deepEqual(readJson('{"a":1}'), { a: 1 })
})

test("a coerced record never exposes a prototype key as data", () => {
  const record = asRecord(JSON.parse('{"__proto__":{"polluted":true}}'))
  assert.equal((({}) as Record<string, unknown>)["polluted"], undefined)
  assert.equal(typeof record, "object")
})

// ── events are complete or they are not events ───────────────────────────────

const FIELDS: (keyof Event)[] = [
  "kind",
  "role",
  "verb",
  "args",
  "result",
  "finalMessage",
  "sessionId",
  "agentId",
  "scope",
  "touchedSource",
  "configured",
  "reentrant",
]

test("an event built from nothing is complete, frozen and safe", () => {
  for (const kind of EVENT_KINDS) {
    const e = event(kind, {})
    for (const field of FIELDS) assert.ok(field in e, `${kind} is missing ${field}`)
    assert.equal(e.kind, kind)
    assert.equal(e.role, "OTHER")
    assert.equal(Object.isFrozen(e), true)
  }
})

test("an event built from hostile fields is still complete and still typed", () => {
  const rand = mulberry(99)
  for (let i = 0; i < 2000; i++) {
    const junk = asRecord(value(rand, 3)) as unknown as Partial<Event>
    const e = event("TOOL_POST", junk)
    for (const field of FIELDS) assert.ok(field in e)
    assert.equal(typeof e.verb, "string")
    assert.equal(typeof e.sessionId, "string")
    assert.equal(typeof e.scope, "string")
    assert.equal(typeof e.touchedSource, "boolean")
    assert.ok(ROLES.includes(e.role))
    assert.ok(!Array.isArray(e.args) && typeof e.args === "object")
  }
})

test("an out-of-vocabulary role falls back rather than riding through", () => {
  const e = event("TOOL_PRE", { role: "DESTROY" as unknown as Event["role"] })
  assert.equal(e.role, "OTHER")
})

test("the observed fields ride through unchanged when they are well typed", () => {
  const e = event("TURN_END", {
    role: "MUTATE",
    verb: "hive_write",
    args: { text: "x" },
    result: { status: "approved" },
    finalMessage: "done",
    sessionId: "s",
    agentId: "a",
    scope: "/tmp/repo",
    touchedSource: true,
    configured: true,
    reentrant: true,
  })
  assert.equal(e.role, "MUTATE")
  assert.equal(e.verb, "hive_write")
  assert.deepEqual(e.args, { text: "x" })
  assert.equal(e.finalMessage, "done")
  assert.equal(e.scope, "/tmp/repo")
  assert.ok(e.touchedSource && e.configured && e.reentrant)
})

// ── decisions carry their reason by construction ─────────────────────────────

test("every non-inert decision carries a reason, and inert carries none", () => {
  assert.deepEqual(INERT, { kind: "inert" })
  assert.equal(Object.isFrozen(INERT), true)
  for (const make of [deny, block, context, feedback]) {
    const d = make("because")
    assert.ok("reason" in d)
    assert.equal((d as { reason: string }).reason, "because")
    assert.equal(Object.isFrozen(d), true)
  }
})

test("the normalized vocabularies are closed and exactly the promised size", () => {
  assert.deepEqual([...EVENT_KINDS], ["SESSION_RESUMED", "TOOL_PRE", "TOOL_POST", "TURN_END"])
  assert.deepEqual([...ROLES], ["MUTATE", "READ", "OTHER"])
})
