// The memory coupling, asserted against envelopes a REAL server actually
// emitted (recorded by tests/harness/test_envelope_fixture.py). Hand-authored
// JSON would only prove the parser agrees with itself.

import { test } from "node:test"
import assert from "node:assert/strict"
import { existsSync, readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"

import {
  abstained,
  actionableIds,
  affirmed,
  isMaintenance,
  isStore,
  readEnvelope,
  refused,
  retiredByRider,
  servedIds,
  verbOf,
} from "../core/hive.ts"
import type { Json, JsonObject } from "../core/events.ts"

const FIXTURES = fileURLToPath(new URL("./fixtures/envelopes.json", import.meta.url))

function fixtures(): Record<string, Record<string, Json>> {
  if (!existsSync(FIXTURES)) assert.fail("envelope fixture not generated yet")
  return JSON.parse(readFileSync(FIXTURES, "utf8")) as Record<string, Record<string, Json>>
}

function envelope(group: string, scenario: string): JsonObject {
  const slot = fixtures()[group]?.[scenario]
  assert.ok(slot !== undefined, `fixture ${group}.${scenario} not generated yet`)
  const parsed = readEnvelope(slot)
  assert.ok(parsed !== undefined, `${group}.${scenario} did not parse as an envelope`)
  return parsed
}

function reshape(group: string, scenario: string, mutate: (env: Record<string, Json>) => void): Json {
  const result = JSON.parse(JSON.stringify(fixtures()[group]?.[scenario])) as {
    content: { text: string }[]
  }
  const inner = JSON.parse(result.content[0]!.text) as Record<string, Json>
  mutate(inner)
  result.content[0]!.text = JSON.stringify(inner)
  return result as unknown as Json
}

// ── the verb, read off the tail and never off the namespace ──────────────────

test("the verb is the segment after the last separator, whatever the namespace", () => {
  for (const prefix of ["mcp__hive__", "mcp__plugin_hive-loop_hive__", "mcp__anything_at_all__", ""]) {
    assert.equal(verbOf(`${prefix}hive_recall`), "hive_recall", prefix)
    assert.equal(verbOf(`${prefix}hive_write`), "hive_write", prefix)
  }
})

test("a name that is not an advertised verb resolves to nothing", () => {
  for (const name of ["Read", "mcp__hive__hive_nonsense", "hive_recall_extra", "", "__"]) {
    assert.equal(verbOf(name), "", name)
  }
})

test("a hive-verb SUFFIX on a foreign tool still resolves — over-inclusion is the safe direction", () => {
  assert.equal(verbOf("mcp__other_server__hive_recall"), "hive_recall")
})

test("the verb roles partition the way the rules need them", () => {
  assert.ok(isStore("hive_write") && isStore("hive_capture"))
  assert.ok(!isStore("hive_recall") && !isStore("hive_prune"))
  assert.ok(isMaintenance("hive_prune") && isMaintenance("hive_supersede") && isMaintenance("hive_flag"))
  assert.ok(!isMaintenance("hive_write") && !isMaintenance("hive_outcome"))
})

// ── envelope extraction, over every shape a runtime might hand over ──────────

test("a real result frame yields its envelope", () => {
  const env = envelope("recall", "confident_multi")
  assert.equal(env["abstained"], false)
  assert.ok(servedIds(env).length >= 2)
})

test("the envelope itself, and either shape as a JSON string, all parse", () => {
  const frame = fixtures()["recall"]!["confident_multi"]!
  const direct = readEnvelope(frame)
  assert.ok(direct !== undefined)
  assert.deepEqual(readEnvelope(direct as unknown as Json), direct)
  assert.deepEqual(readEnvelope(JSON.stringify(frame)), direct)
  assert.deepEqual(readEnvelope(JSON.stringify(direct)), direct)
})

test("a malformed, absent or hostile result records nothing and never throws", () => {
  const hostile: Json[] = [
    null,
    "",
    "not json",
    "[]",
    "42",
    [],
    {},
    { content: "not-an-array" },
    { content: [] },
    { content: [{ type: "text" }] },
    { content: [{ type: "text", text: "not json" }] },
    { content: [{ type: "text", text: "[1,2,3]" }] },
    { content: [{ type: "text", text: "{}" }] },
    { content: [null, 7, "x"] },
    { reference_context: "not-a-list" },
  ]
  for (const value of hostile) {
    const env = readEnvelope(value)
    if (env === undefined) continue
    assert.deepEqual(servedIds(env), [], JSON.stringify(value))
    assert.deepEqual(actionableIds(env), [], JSON.stringify(value))
    assert.equal(refused(env), false)
    assert.equal(affirmed(env), false)
  }
  assert.equal(readEnvelope(undefined), undefined)
})

// ── the three actionable signals, and every verdict that is not one ──────────

const QUALIFYING = ["drift_anchor_missing", "drift_anchor_changed", "drift_blast_radius_changed"]
const NOT_QUALIFYING = ["drift_fresh", "drift_unverifiable", "drift_branch_scoped", "drift_na"]

test("every verdict on the tier the server acts on is actionable", () => {
  for (const scenario of QUALIFYING) {
    const env = envelope("recall", scenario)
    assert.deepEqual(actionableIds(env), servedIds(env), scenario)
  }
})

test("no verdict off that tier is actionable, even though the hit was served", () => {
  for (const scenario of NOT_QUALIFYING) {
    const env = envelope("recall", scenario)
    assert.ok(servedIds(env).length > 0, scenario)
    assert.deepEqual(actionableIds(env), [], scenario)
  }
})

test("the qualifying verdict ALONE is actionable, with the server's rider stripped", () => {
  const stripped = reshape("recall", "drift_anchor_missing", (env) => {
    for (const hit of env["reference_context"] as Record<string, Json>[]) delete hit["remediation"]
  })
  const env = readEnvelope(stripped)
  assert.ok(env !== undefined)
  assert.deepEqual(actionableIds(env), servedIds(env))
})

test("the server's rider ALONE is actionable, with the verdict off the tier", () => {
  const rider = reshape("recall", "drift_anchor_missing", (env) => {
    for (const hit of env["reference_context"] as Record<string, Json>[]) {
      hit["drift"] = { type: "unverifiable", detail: { per_anchor: [] } }
    }
  })
  const env = readEnvelope(rider)
  assert.ok(env !== undefined)
  assert.deepEqual(actionableIds(env), servedIds(env))
})

test("a conflicted id that was served is actionable", () => {
  const env = envelope("recall", "conflicts")
  const served = servedIds(env)
  assert.ok(served.length > 0)
  assert.ok(actionableIds(env).length > 0)
  for (const id of actionableIds(env)) assert.ok(served.includes(id))
})

test("a conflicted id that was NOT served is not actionable — the set stays a subset", () => {
  const foreign = reshape("recall", "conflicts", (env) => {
    env["conflicts"] = [{ a_id: 99_001, b_id: 99_002, relation: "contradiction" }]
  })
  const env = readEnvelope(foreign)
  assert.ok(env !== undefined)
  assert.deepEqual(actionableIds(env), [])
})

test("two actionable hits in one envelope both surface", () => {
  const env = envelope("recall", "multi_actionable")
  assert.ok(servedIds(env).length >= 2)
  assert.deepEqual(actionableIds(env), servedIds(env))
})

// ── abstain, refusal, affirmation ────────────────────────────────────────────

test("an abstained envelope serves nothing and says so", () => {
  const env = envelope("recall", "abstained")
  assert.equal(abstained(env), true)
  assert.deepEqual(servedIds(env), [])
})

test("a confident envelope does not claim to have abstained", () => {
  assert.equal(abstained(envelope("recall", "confident_multi")), false)
})

test("a refused store is refused and every landed one is not", () => {
  assert.equal(refused(envelope("write", "refused")), true)
  for (const [group, scenario] of [
    ["write", "approved"],
    ["write", "anchored"],
    ["write", "repos_only"],
    ["capture", "approved"],
  ] as const) {
    assert.equal(refused(envelope(group, scenario)), false, `${group}.${scenario}`)
  }
})

test("only an affirmed maintenance envelope is credited", () => {
  assert.equal(affirmed(envelope("prune", "affirmed")), true)
  assert.equal(affirmed(envelope("supersede", "affirmed")), true)
  assert.equal(affirmed(envelope("flag", "recorded")), true)
  assert.equal(affirmed(envelope("prune", "noop")), false)
  assert.equal(affirmed(envelope("supersede", "noop")), false)
})

test("an unrecognized future status is not credited — the loop item stays open", () => {
  for (const status of ["retired", "ok", "done", "", "superseded_maybe"]) {
    const invented = reshape("prune", "affirmed", (env) => {
      env["status"] = status
    })
    const env = readEnvelope(invented)
    assert.ok(env !== undefined)
    assert.equal(affirmed(env), false, status)
  }
})

test("a store's own retirement rider is read from the envelope, not assumed", () => {
  const affirmedRider = envelope("write", "replaces_affirmed")
  assert.ok(typeof retiredByRider(affirmedRider) === "number")
  assert.equal(retiredByRider(envelope("write", "approved")), null)
  const declined = reshape("write", "replaces_affirmed", (env) => {
    env["superseded"] = null
    env["supersede_noop"] = "no qualifying machine signal"
  })
  const env = readEnvelope(declined)
  assert.ok(env !== undefined)
  assert.equal(retiredByRider(env), null)
})
