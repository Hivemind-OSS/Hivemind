// Truth tables at every boundary of the pure reducer. These are the rules'
// own tests; the frozen contract suite drives the same rules through a process.

import { test } from "node:test"
import assert from "node:assert/strict"
import { existsSync, readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"

import { event } from "../core/events.ts"
import type { Decision, Env, Event, Json } from "../core/events.ts"
import { EMPTY } from "../core/state.ts"
import type { LoopState } from "../core/state.ts"
import {
  LOOP_KEYS,
  MAINTENANCE_MISSING,
  OUTCOME_MISSING,
  RECALL_MISSING,
  SCOPE_MISSING,
  STORE_MISSING,
  applySentinels,
  decide,
  intentSignals,
  openKeys,
  shapeSignals,
} from "../core/decide.ts"

const ON: Env = { enabled: true, stateDir: "/tmp/state", warnings: [] }
const OFF: Env = { enabled: false, stateDir: "/tmp/state", warnings: [] }

const ARMED: LoopState = { ...EMPTY, armed: true }

const FIXTURES = fileURLToPath(new URL("./fixtures/envelopes.json", import.meta.url))

function fixture(group: string, scenario: string): Json {
  if (!existsSync(FIXTURES)) assert.fail("envelope fixture not generated yet")
  const all = JSON.parse(readFileSync(FIXTURES, "utf8")) as Record<string, Record<string, Json>>
  const slot = all[group]?.[scenario]
  assert.ok(slot !== undefined, `fixture ${group}.${scenario} not generated yet`)
  return slot
}

/** The recorded capture result with the server's own refusal status substituted in. */
function refusedCapture(): Json {
  const result = JSON.parse(JSON.stringify(fixture("capture", "approved"))) as {
    content?: { text?: string }[]
  }
  const slot = result.content?.[0]
  assert.ok(slot !== undefined && typeof slot.text === "string", "recorded result has no content[].text")
  const refusedText = (fixture("write", "refused") as { content?: { text?: string }[] }).content?.[0]?.text
  assert.ok(typeof refusedText === "string", "the recorded refusal carries no envelope")
  const envelope = JSON.parse(slot.text) as Record<string, Json>
  envelope["status"] = (JSON.parse(refusedText) as Record<string, Json>)["status"] ?? null
  slot.text = JSON.stringify(envelope)
  return result as Json
}

function kindOf(d: Decision): string {
  return d.kind
}

function reasonOf(d: Decision): string {
  return "reason" in d ? d.reason : ""
}

function pre(fields: Omit<Partial<Event>, "kind">): Event {
  return event("TOOL_PRE", fields)
}

function post(fields: Omit<Partial<Event>, "kind">): Event {
  return event("TOOL_POST", fields)
}

function end(finalMessage = ""): Event {
  return event("TURN_END", { finalMessage })
}

// ── the kill switch and the unmapped event ───────────────────────────────────

test("disabled is inert on every event kind, and journals nothing", () => {
  for (const e of [pre({ role: "MUTATE" }), post({ role: "MUTATE" }), end(), event("SESSION_RESUMED", {})]) {
    const out = decide(e, ARMED, OFF)
    assert.equal(kindOf(out.decision), "inert")
    assert.equal(out.next, null)
  }
})

// ── G1 / G2: the two gates ───────────────────────────────────────────────────

test("G1 denies a mutation exactly when the session is armed and holds no recall", () => {
  const rows: [LoopState | null, string][] = [
    [null, "inert"],
    [EMPTY, "inert"],
    [ARMED, "deny"],
    [{ ...ARMED, recalls: 1 }, "inert"],
    [{ ...EMPTY, recalls: 0 }, "inert"],
  ]
  for (const [state, expected] of rows) {
    const out = decide(pre({ role: "MUTATE" }), state, ON)
    assert.equal(kindOf(out.decision), expected, JSON.stringify(state))
    assert.equal(out.next, null, "a gate never journals")
  }
})

test("G2 denies either store verb on the same predicate, and no other verb", () => {
  for (const verb of ["hive_write", "hive_capture"]) {
    assert.equal(kindOf(decide(pre({ verb }), ARMED, ON).decision), "deny", verb)
    assert.equal(kindOf(decide(pre({ verb }), { ...ARMED, recalls: 1 }, ON).decision), "inert", verb)
  }
  for (const verb of ["hive_recall", "hive_outcome", "hive_prune", "hive_health", ""]) {
    assert.equal(kindOf(decide(pre({ verb }), ARMED, ON).decision), "inert", verb)
  }
})

test("a READ role is never denied, at any recall count", () => {
  for (const recalls of [0, 1, 9]) {
    assert.equal(kindOf(decide(pre({ role: "READ" }), { ...ARMED, recalls }, ON).decision), "inert")
  }
})

test("both gate reasons name the recall and defer to the served contract", () => {
  for (const e of [pre({ role: "MUTATE" }), pre({ verb: "hive_write" })]) {
    const reason = reasonOf(decide(e, ARMED, ON).decision)
    assert.match(reason, /hive_recall/)
    assert.match(reason, /served hive contract/)
  }
})

test("the gate reason is a function of the gate, never of the call's text", () => {
  const a = reasonOf(decide(pre({ verb: "hive_write", args: { text: "alpha" } }), ARMED, ON).decision)
  const b = reasonOf(decide(pre({ verb: "hive_write", args: { text: "beta beta" } }), ARMED, ON).decision)
  assert.equal(a, b)
})

// ── arming ───────────────────────────────────────────────────────────────────

test("arming needs BOTH clauses, and is observed at post-tool time", () => {
  const rows: [boolean, boolean, boolean][] = [
    [false, false, false],
    [true, false, false],
    [false, true, false],
    [true, true, true],
  ]
  for (const [touchedSource, configured, armed] of rows) {
    const out = decide(post({ touchedSource, configured }), null, ON)
    assert.equal(out.next?.armed, armed, `${touchedSource}/${configured}`)
  }
})

test("arming never un-arms", () => {
  const out = decide(post({ touchedSource: false, configured: false }), ARMED, ON)
  assert.equal(out.next?.armed, true)
})

// ── journalling ──────────────────────────────────────────────────────────────

test("a mutation is counted after it ran, not when it was proposed", () => {
  assert.equal(decide(post({ role: "MUTATE" }), ARMED, ON).next?.mutations, 1)
  assert.equal(decide(pre({ role: "MUTATE" }), ARMED, ON).next, null)
})

test("a recall counts as an attempt whatever it answered", () => {
  for (const scenario of ["confident_multi", "abstained"]) {
    const out = decide(
      post({ verb: "hive_recall", args: { query: "x" }, result: fixture("recall", scenario) }),
      ARMED,
      ON,
    )
    assert.equal(out.next?.recalls, 1, scenario)
  }
})

test("a serving recall records its ids and re-opens the outcome question", () => {
  const out = decide(
    post({ verb: "hive_recall", args: { query: "x" }, result: fixture("recall", "drift_fresh") }),
    { ...ARMED, outcomeAfterServe: true },
    ON,
  )
  assert.ok((out.next?.served.length ?? 0) > 0)
  assert.equal(out.next?.outcomeAfterServe, false)
})

test("an abstained recall records nothing served and leaves the outcome question alone", () => {
  const out = decide(
    post({ verb: "hive_recall", args: { query: "x" }, result: fixture("recall", "abstained") }),
    { ...ARMED, outcomeAfterServe: true },
    ON,
  )
  assert.deepEqual(out.next?.served, [])
  assert.equal(out.next?.outcomeAfterServe, true)
})

test("each actionable signal is recorded and each non-signal is not", () => {
  for (const scenario of ["drift_anchor_missing", "drift_anchor_changed", "drift_blast_radius_changed"]) {
    const out = decide(
      post({ verb: "hive_recall", args: { query: "x" }, result: fixture("recall", scenario) }),
      ARMED,
      ON,
    )
    assert.ok((out.next?.actionable.length ?? 0) > 0, scenario)
  }
  for (const scenario of ["drift_fresh", "drift_unverifiable", "drift_branch_scoped", "drift_na"]) {
    const out = decide(
      post({ verb: "hive_recall", args: { query: "x" }, result: fixture("recall", scenario) }),
      ARMED,
      ON,
    )
    assert.deepEqual(out.next?.actionable, [], scenario)
  }
})

test("a landed store counts under ITS OWN verb and a refused one does not", () => {
  for (const [group, scenario, writes, captures] of [
    ["write", "approved", 1, 0],
    ["write", "anchored", 1, 0],
    ["capture", "approved", 0, 1],
    ["write", "refused", 0, 0],
    ["capture", "refused", 0, 0],
  ] as const) {
    const result =
      scenario === "refused" && group === "capture"
        ? refusedCapture()
        : fixture(group, scenario)
    const out = decide(
      post({ verb: group === "capture" ? "hive_capture" : "hive_write", args: { text: "x" }, result }),
      ARMED,
      ON,
    )
    assert.equal(out.next?.writes, writes, `${group}.${scenario}`)
    assert.equal(out.next?.captures, captures, `${group}.${scenario}`)
  }
})

test("only a LANDED store with no carrier opens the scope question", () => {
  const rows: [Record<string, Json>, string, boolean][] = [
    [{ text: "x" }, "approved", true],
    [{ text: "x", anchors: [{ repo: "a", anchor: "f.py::g" }] }, "anchored", false],
    [{ text: "x", repos: ["a"] }, "repos_only", false],
    [{ text: "x", anchors: [], repos: [] }, "approved", true],
    [{ text: "x" }, "refused", false],
  ]
  for (const [args, scenario, expected] of rows) {
    const out = decide(
      post({ verb: "hive_write", args, result: fixture("write", scenario) }),
      ARMED,
      ON,
    )
    assert.equal(out.next?.unscopedStore, expected, JSON.stringify(args))
  }
})

test("a maintenance call credits only what the server affirmed", () => {
  const rows: [string, string, string, Record<string, Json>, number[]][] = [
    ["hive_prune", "prune", "affirmed", { episode_id: 7 }, [7]],
    ["hive_prune", "prune", "noop", { episode_id: 7 }, []],
    ["hive_supersede", "supersede", "affirmed", { loser: 7, winner: 8 }, [7, 8]],
    ["hive_supersede", "supersede", "noop", { loser: 7, winner: 8 }, []],
    ["hive_flag", "flag", "recorded", { a: 7, b: 8, kind: "conflict" }, [7, 8]],
  ]
  for (const [verb, group, scenario, args, expected] of rows) {
    const out = decide(post({ verb, args, result: fixture(group, scenario) }), ARMED, ON)
    assert.deepEqual(out.next?.maintained, expected, `${verb}.${scenario}`)
  }
})

test("a store's retirement rider credits only when the envelope reports it landed", () => {
  const affirmedRider = decide(
    post({ verb: "hive_write", args: { text: "x", replaces: 3 }, result: fixture("write", "replaces_affirmed") }),
    ARMED,
    ON,
  )
  assert.deepEqual(affirmedRider.next?.maintained, [3])
  const noRider = decide(
    post({ verb: "hive_write", args: { text: "x" }, result: fixture("write", "approved") }),
    ARMED,
    ON,
  )
  assert.deepEqual(noRider.next?.maintained, [])
})

test("a store event's ARRIVAL is counted apart from its credit", () => {
  const rows: [string, Json, number, number][] = [
    // verb, result, storeArrivals, writes
    ["hive_write", fixture("write", "approved"), 1, 1],
    ["hive_write", fixture("write", "refused"), 1, 0],
    ["hive_write", "complete junk", 1, 0],
    ["hive_write", null, 1, 0],
    ["hive_capture", fixture("capture", "approved"), 1, 0],
  ]
  for (const [verb, result, arrivals, writes] of rows) {
    const out = decide(post({ verb, args: { text: "x" }, result }), ARMED, ON)
    assert.equal(out.next?.storeArrivals, arrivals, `${verb} ${JSON.stringify(result).slice(0, 40)}`)
    assert.equal(out.next?.writes, writes, "credit stays keyed on a parsed, non-refused envelope")
  }
})

test("no non-store event counts as a store arrival", () => {
  const rows: [string, Json][] = [
    ["hive_recall", fixture("recall", "confident_multi")],
    ["hive_outcome", fixture("outcome", "ok")],
    ["hive_supersede", fixture("supersede", "affirmed")],
    ["", { ok: true }],
  ]
  for (const [verb, result] of rows) {
    const out = decide(post({ verb, args: {}, result }), ARMED, ON)
    assert.equal(out.next?.storeArrivals, 0, verb || "(no verb)")
  }
})

test("the arrival count opens and closes NOTHING — every open set ignores it", () => {
  // arrival is diagnostic: crediting it would credit refused stores, so no
  // openKeys predicate may read it in either direction
  for (const patch of [
    { mutations: 1, storeArrivals: 3 },
    { mutations: 1, recalls: 1, storeArrivals: 3 },
  ] as Partial<LoopState>[]) {
    const withArrivals = openKeys({ ...ARMED, ...patch })
    const without = openKeys({ ...ARMED, ...patch, storeArrivals: 0 })
    assert.deepEqual(withArrivals, without, JSON.stringify(patch))
  }
})

test("an unreadable result records nothing but never loses the attempt", () => {
  const out = decide(post({ verb: "hive_recall", args: { query: "x" }, result: "garbage" }), ARMED, ON)
  assert.equal(out.next?.recalls, 1)
  assert.deepEqual(out.next?.served, [])
  const store = decide(post({ verb: "hive_write", args: { text: "x" }, result: null }), ARMED, ON)
  assert.equal(store.next?.writes, 0, "an unreadable store envelope never counts as landed")
})

// ── F1: the one feedback case ────────────────────────────────────────────────

test("feedback fires only on an abstain crossed with a bundled query", () => {
  const bundled = "how does the gate abstain; and what retires a memory?"
  const single = "what makes the gate abstain on a weak field"
  const rows: [string, string, string][] = [
    ["abstained", bundled, "feedback"],
    ["abstained", single, "inert"],
    ["confident_multi", bundled, "inert"],
    ["confident_multi", single, "inert"],
  ]
  for (const [scenario, query, expected] of rows) {
    const out = decide(
      post({ verb: "hive_recall", args: { query }, result: fixture("recall", scenario) }),
      ARMED,
      ON,
    )
    assert.equal(kindOf(out.decision), expected, `${scenario} / ${query}`)
    assert.notEqual(out.next, null, "feedback still journals the attempt")
  }
})

test("each bundling signal stands alone, and a single-pointed query trips none", () => {
  assert.deepEqual(intentSignals("what makes the gate abstain on a weak field"), [])
  assert.deepEqual(intentSignals(""), [])
  assert.ok(intentSignals("is it a? is it b?").length > 0, "two question marks")
  assert.ok(intentSignals("first clause; second clause").length > 0, "a joined clause pair")
  assert.ok(intentSignals("what retires it and why").length > 0, "two interrogatives")
  assert.ok(intentSignals("tell me about drift and how it is computed").length > 0, "a conjunction")
})

test("a non-string or empty query never trips the feedback", () => {
  for (const query of ["", null, 42, ["x"], { a: 1 }] as Json[]) {
    const out = decide(
      post({ verb: "hive_recall", args: { query }, result: fixture("recall", "abstained") }),
      ARMED,
      ON,
    )
    assert.equal(kindOf(out.decision), "inert", JSON.stringify(query))
  }
})

// ── F2: the store-shape observation ──────────────────────────────────────────

test("each shape signal stands alone, and the case just under each floor trips none", () => {
  const anchor = { repo: "alpha", anchor: "app.py::greet" }
  const other = { repo: "alpha", anchor: "b.py::helper" }
  const tripping: [string, Record<string, Json>][] = [
    ["no binding at all", { text: "a flat lesson", repos: ["alpha"] }],
    ["an empty binding list", { text: "a flat lesson", anchors: [] }],
    ["two bindings", { text: "a flat lesson", anchors: [anchor, other] }],
    ["two list markers", { text: "a lesson\n- one\n- two", anchors: [anchor] }],
    ["two symbol separators", { text: "a.py::g and b.py::h", anchors: [anchor] }],
    ["three terminators", { text: "X breaks Y. Use Z. And W too.", anchors: [anchor] }],
  ]
  for (const [label, args] of tripping) {
    assert.ok(shapeSignals(args).length > 0, label)
  }
  const silent: [string, Record<string, Json>][] = [
    ["one binding and flat prose", { text: "a flat lesson", anchors: [anchor] }],
    ["one list marker", { text: "a lesson\n- the only one", anchors: [anchor] }],
    ["one symbol separator", { text: "the fix is in a.py::g", anchors: [anchor] }],
    ["two terminators — one dense fact", { text: "X breaks Y. Use Z.", anchors: [anchor] }],
  ]
  for (const [label, args] of silent) {
    assert.deepEqual([...shapeSignals(args)], [], label)
  }
  // a hostile value coerces rather than throwing: an unreadable binding list is
  // no binding, which is exactly the absence the first signal reports
  assert.deepEqual([...shapeSignals({ text: 42, anchors: "not a list" })], ["no code site named"])
  assert.deepEqual([...shapeSignals({})], ["no code site named"])
})

test("the observation is spent once per session and never on a store that did not land", () => {
  const loud = {
    text: "a lesson\n- one\n- two. It bites here. And here.",
    anchors: [{ repo: "alpha", anchor: "app.py::greet" }, { repo: "alpha", anchor: "b.py::helper" }],
  }
  const first = decide(
    post({ verb: "hive_write", args: loud, result: fixture("write", "approved") }),
    ARMED,
    ON,
  )
  assert.equal(kindOf(first.decision), "feedback")
  assert.equal(first.next?.shapeObserved, true)
  const second = decide(
    post({ verb: "hive_capture", args: loud, result: fixture("capture", "approved") }),
    first.next,
    ON,
  )
  assert.equal(kindOf(second.decision), "inert", "the budget is one observation per session")
  const refusedStore = decide(
    post({ verb: "hive_write", args: loud, result: fixture("write", "refused") }),
    ARMED,
    ON,
  )
  assert.equal(kindOf(refusedStore.decision), "inert", "a refused store landed nothing to observe")
  const unarmed = decide(
    post({ verb: "hive_write", args: loud, result: fixture("write", "approved") }),
    EMPTY,
    ON,
  )
  assert.equal(kindOf(unarmed.decision), "inert", "an unarmed session is silent")
})

// ── the five loop items ──────────────────────────────────────────────────────

test("each loop item opens on exactly its own predicate", () => {
  const rows: [Partial<LoopState>, string[]][] = [
    [{}, []],
    [{ mutations: 1 }, [RECALL_MISSING, STORE_MISSING]],
    [{ mutations: 1, recalls: 1 }, [STORE_MISSING]],
    [{ mutations: 1, recalls: 1, writes: 1 }, []],
    [{ mutations: 1, recalls: 1, captures: 1 }, [STORE_MISSING]],
    [{ mutations: 1, recalls: 1, captures: 1, captureWhy: "the choice was not clear" }, []],
    [{ mutations: 1, recalls: 1, captureWhy: "the choice was not clear" }, [STORE_MISSING]],
    [{ mutations: 1, recalls: 1, noStoreWhy: "nothing applied" }, []],
    [{ served: [1] }, [OUTCOME_MISSING]],
    [{ served: [1], outcomeAfterServe: true }, []],
    [{ served: [1, 2], actionable: [2], outcomeAfterServe: true }, [MAINTENANCE_MISSING]],
    [{ served: [1, 2], actionable: [2], maintained: [2], outcomeAfterServe: true }, []],
    [{ served: [1, 2], actionable: [2], deferred: [2], outcomeAfterServe: true }, []],
    [{ unscopedStore: true }, [SCOPE_MISSING]],
    [{ unscopedStore: true, generalWhy: "binds to nothing" }, []],
  ]
  for (const [patch, expected] of rows) {
    assert.deepEqual([...openKeys({ ...ARMED, ...patch })], expected, JSON.stringify(patch))
  }
})

test("every loop item is reachable and none is dead", () => {
  const reachable = new Set<string>()
  const states: Partial<LoopState>[] = [
    { mutations: 1 },
    { served: [1] },
    { served: [1], actionable: [1], outcomeAfterServe: true },
    { unscopedStore: true },
  ]
  for (const patch of states) for (const key of openKeys({ ...ARMED, ...patch })) reachable.add(key)
  assert.deepEqual([...reachable].sort(), [...LOOP_KEYS].sort())
})

test("one mutually exclusive pair is left, so the largest open set is four", () => {
  // recall_missing needs zero recalls while outcome_missing and maintenance_missing
  // each need one — the only pair exclusive by construction. store_missing and
  // scope_missing are NOT: a carrierless capture with no write opens both at once.
  let largest = 0
  for (const mutations of [0, 1]) {
    for (const recalls of [0, 1]) {
      for (const writes of [0, 1]) {
        for (const captures of [0, 1]) {
          for (const served of [[], [1]]) {
            for (const actionable of [[], [1]]) {
              for (const unscopedStore of [false, true]) {
                const consistent =
                  (recalls > 0 || served.length === 0) &&
                  (recalls > 0 || actionable.length === 0) &&
                  (actionable.length === 0 || served.length > 0) &&
                  (!unscopedStore || writes + captures > 0)
                if (!consistent) continue
                const keys = openKeys({
                  ...ARMED,
                  mutations,
                  recalls,
                  writes,
                  captures,
                  served,
                  actionable,
                  unscopedStore,
                })
                largest = Math.max(largest, keys.length)
              }
            }
          }
        }
      }
    }
  }
  assert.equal(largest, 4)
})

// ── TURN_END ─────────────────────────────────────────────────────────────────

const DIRTY: LoopState = { ...ARMED, mutations: 1, served: [12], actionable: [12] }

test("one turn-end emits ONE block naming every open item, and never blocks twice", () => {
  const first = decide(end(), DIRTY, ON)
  assert.equal(kindOf(first.decision), "block")
  for (const key of [OUTCOME_MISSING, MAINTENANCE_MISSING, STORE_MISSING]) {
    assert.match(reasonOf(first.decision), new RegExp(key))
  }
  assert.ok(first.next !== null)
  const second = decide(end(), first.next, ON)
  assert.equal(kindOf(second.decision), "inert")
})

test("an item that first opens AFTER a block may block once more, and no more", () => {
  const first = decide(end(), { ...ARMED, mutations: 1 }, ON)
  assert.equal(kindOf(first.decision), "block")
  assert.ok(first.next !== null)
  const later = { ...first.next, unscopedStore: true, writes: 1 }
  const second = decide(end(), later, ON)
  assert.equal(kindOf(second.decision), "block")
  assert.match(reasonOf(second.decision), new RegExp(SCOPE_MISSING))
  assert.ok(second.next !== null)
  assert.equal(kindOf(decide(end(), second.next, ON).decision), "inert")
})

test("a re-entrant turn-end short-circuits before anything else", () => {
  const out = decide(event("TURN_END", { reentrant: true }), DIRTY, ON)
  assert.equal(kindOf(out.decision), "inert")
  assert.equal(out.next, null)
})

test("an unarmed or absent session never blocks", () => {
  assert.equal(kindOf(decide(end(), null, ON).decision), "inert")
  assert.equal(kindOf(decide(end(), { ...DIRTY, armed: false }, ON).decision), "inert")
})

test("the block reason names both store verbs and no hive semantics", () => {
  const reason = reasonOf(decide(end(), DIRTY, ON).decision)
  assert.match(reason, /hive_write/)
  assert.match(reason, /hive_capture/)
  assert.match(reason, /served hive contract/)
  for (const word of ["provisional", "quarantine", "established", "promote", "demand", "trust"]) {
    assert.ok(!reason.toLowerCase().includes(word), word)
  }
})

// ── the three sentinels ──────────────────────────────────────────────────────

test("each sentinel closes exactly its own item when it carries a rationale", () => {
  const noStore = decide(end("HIVE-LOOP: no-store — a one-line typo fix"), { ...ARMED, mutations: 1, recalls: 1 }, ON)
  assert.equal(kindOf(noStore.decision), "inert")
  const captured = decide(
    end("HIVE-LOOP: capture — the lesson may not generalize past this branch"),
    { ...ARMED, mutations: 1, recalls: 1, captures: 1 },
    ON,
  )
  assert.equal(kindOf(captured.decision), "inert")
  const general = decide(end("HIVE-LOOP: general — a language-level fact"), { ...ARMED, unscopedStore: true }, ON)
  assert.equal(kindOf(general.decision), "inert")
  const defer = decide(
    end("HIVE-LOOP: defer 12 — the successor lands next change"),
    { ...ARMED, served: [12], actionable: [12], outcomeAfterServe: true },
    ON,
  )
  assert.equal(kindOf(defer.decision), "inert")
})

test("a sentinel with an empty rationale closes nothing", () => {
  for (const line of ["HIVE-LOOP: no-store — ", "HIVE-LOOP: no-store —", "HIVE-LOOP: no-store"]) {
    const out = decide(end(line), { ...ARMED, mutations: 1, recalls: 1 }, ON)
    assert.equal(kindOf(out.decision), "block", JSON.stringify(line))
  }
})

test("a sentinel is read off its own line, wherever it sits in the reply", () => {
  const message = "I did the work.\n\nHIVE-LOOP: no-store — nothing reusable\n\nThanks."
  const out = decide(end(message), { ...ARMED, mutations: 1, recalls: 1 }, ON)
  assert.equal(kindOf(out.decision), "inert")
})

test("a defer sentinel takes every id it names and no id it does not", () => {
  const next = applySentinels({ ...ARMED }, "HIVE-LOOP: defer 12, 40 and 7 — later")
  assert.deepEqual([...next.deferred], [12, 40, 7])
  assert.deepEqual([...applySentinels({ ...ARMED }, "HIVE-LOOP: defer — later").deferred], [])
})

test("a partial deferral leaves the remainder open", () => {
  const out = decide(
    end("HIVE-LOOP: defer 12 — later"),
    { ...ARMED, served: [12, 40], actionable: [12, 40], outcomeAfterServe: true },
    ON,
  )
  assert.equal(kindOf(out.decision), "block")
  assert.match(reasonOf(out.decision), /\b40\b/)
})

test("a look-alike line is not a sentinel — the grammar is a whole line, exactly", () => {
  for (const line of [
    "hive-loop: no-store — lowercase",
    "HIVE-LOOP no-store — no colon",
    "HIVE-LOOP: nostore — hyphen dropped",
    "HIVE-LOOP: no-store - an ASCII hyphen, not the dash the grammar names",
    "the agent wrote HIVE-LOOP: no-store — inside a sentence",
  ]) {
    const out = decide(end(line), { ...ARMED, mutations: 1, recalls: 1 }, ON)
    assert.equal(kindOf(out.decision), "block", JSON.stringify(line))
  }
})

// ── SESSION_RESUMED ──────────────────────────────────────────────────────────

test("a resumed session re-states open items as context and journals nothing", () => {
  const out = decide(event("SESSION_RESUMED", {}), DIRTY, ON)
  assert.equal(kindOf(out.decision), "context")
  assert.match(reasonOf(out.decision), new RegExp(STORE_MISSING))
  assert.equal(out.next, null)
})

test("a resumed session with nothing open, unarmed, or absent emits nothing", () => {
  for (const state of [ARMED, { ...DIRTY, armed: false }, null]) {
    assert.equal(kindOf(decide(event("SESSION_RESUMED", {}), state, ON).decision), "inert")
  }
})

test("a resumed session can never block", () => {
  const out = decide(event("SESSION_RESUMED", {}), DIRTY, ON)
  assert.notEqual(kindOf(out.decision), "block")
})
