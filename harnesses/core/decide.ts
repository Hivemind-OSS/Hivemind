// Every rule in the system, as one pure reducer over three readonly inputs.
// No I/O, no runtime vocabulary, no reachable path that mutates its arguments.
//
//   event kind          what happens here
//   ─────────────────   ────────────────────────────────────────────────────────
//   SESSION_RESUMED     re-state open loop items as context. Cannot block.
//   TOOL_PRE            two gates. Deny with a reason, or nothing.
//   TOOL_POST           arming, journalling, two feedback cases. Never blocks.
//   TURN_END            the five loop items and the four sentinels. Blocks once.
//   anything else       inert.
//
// The wedge-proofs are structural rather than remembered: a missing ledger reads
// absent, a gate is inert unless the session armed, a re-entrant turn-end
// short-circuits, and one turn-end emits ONE block naming every open item and
// marks all of them in the same write — so a session's dirt costs one turn, not
// one turn per item.

import { INERT, block, context, deny, feedback } from "./events.ts"
import type { Decision, Env, Event, JsonObject } from "./events.ts"
import { EMPTY } from "./state.ts"
import type { LoopState } from "./state.ts"
import {
  CAPTURE,
  OUTCOME,
  RECALL,
  WRITE,
  abstained,
  actionableIds,
  affirmed,
  anchorsOf,
  carrierless,
  isMaintenance,
  isStore,
  namedIds,
  queryOf,
  readEnvelope,
  refused,
  retiredByRider,
  riderTarget,
  servedIds,
  textOf,
} from "./hive.ts"

/** What the reducer decided, and what to journal. A null ledger writes nothing. */
export interface Outcome {
  readonly decision: Decision
  readonly next: LoopState | null
}

export const RECALL_MISSING = "recall_missing"
export const OUTCOME_MISSING = "outcome_missing"
export const MAINTENANCE_MISSING = "maintenance_missing"
export const STORE_MISSING = "store_missing"
export const SCOPE_MISSING = "scope_missing"

/** The five loop items, in the order a block names them. */
export const LOOP_KEYS = [
  RECALL_MISSING,
  OUTCOME_MISSING,
  MAINTENANCE_MISSING,
  STORE_MISSING,
  SCOPE_MISSING,
] as const

const SENTINEL = /^\s*HIVE-LOOP:\s*(no-store|capture|defer|general)\b([^—]*)—(.*)$/

// ── the one place every emitted string lives ─────────────────────────────────
//
// Each states only what was observed, which step is still unmade, and that the
// authority for what to do about it is the served contract. Nothing here
// restates what the server means by anything.

const AUTHORITY = "is defined by your served hive contract"

function unresolved(what: string): string {
  return (
    `hive-loop: this call ${what} and no ${RECALL} call is journaled for this ` +
    `session. The recall is the step still unmade — what to ask and how to scope ` +
    `it ${AUTHORITY}.`
  )
}

const GATE_MUTATION = unresolved("changes the working tree")
const GATE_STORE = unresolved("stores a memory")

const SENTINEL_HELP =
  "Close an item by making the call, or end your reply with one of these lines:\n" +
  "  HIVE-LOOP: no-store — <why nothing applied>\n" +
  "  HIVE-LOOP: capture — <why the verb choice was not clear>\n" +
  "  HIVE-LOOP: defer <ids> — <why they stay open>\n" +
  "  HIVE-LOOP: general — <why this binds to no repo or path>"

function bundledFeedback(query: string): string {
  return (
    `hive-loop: this ${RECALL} abstained, and its query carried two or more ` +
    `separate intents (observed: ${intentSignals(query).join(", ")}). Whether to ` +
    `re-ask it as separate calls is your decision, and what a well-scoped query ` +
    `looks like ${AUTHORITY}.`
  )
}

function shapeFeedback(verb: string, signals: readonly string[]): string {
  return (
    `hive-loop: this ${verb} landed carrying ${signals.length} structural ` +
    `observation(s) (observed: ${signals.join(", ")}). What to do about that — ` +
    `whether it is one memory or several, and what it should bind to — is your ` +
    `decision, and what a well-formed one looks like ${AUTHORITY}.`
  )
}

function itemLine(key: string, state: LoopState, open: readonly number[]): string {
  if (key === RECALL_MISSING) {
    return (
      `- ${RECALL_MISSING}: ${state.mutations} tree-changing call(s) observed, ` +
      `0 ${RECALL} call(s) journaled.`
    )
  }
  if (key === OUTCOME_MISSING) {
    return (
      `- ${OUTCOME_MISSING}: ${RECALL} served episode id(s) ${state.served.join(", ")} ` +
      `and no ${OUTCOME} call followed.`
    )
  }
  if (key === MAINTENANCE_MISSING) {
    return (
      `- ${MAINTENANCE_MISSING}: the server marked episode id(s) ${open.join(", ")} as ` +
      `needing an answer on this session's recalls, and no maintenance call it ` +
      `affirmed named them. Which call applies ${AUTHORITY}.`
    )
  }
  if (key === STORE_MISSING) {
    return (
      `- ${STORE_MISSING}: ${state.mutations} tree-changing call(s) observed, ` +
      `${state.writes} landed ${WRITE} call(s) and ${state.captures} landed ` +
      `${CAPTURE} call(s) journaled. Which one applies, or neither, ${AUTHORITY}. ` +
      `A landed ${CAPTURE} closes this item alongside the declaration line below.`
    )
  }
  return (
    `- ${SCOPE_MISSING}: a store landed carrying neither anchors nor repos, so it ` +
    `names no code and no repo.`
  )
}

function report(header: string, keys: readonly string[], state: LoopState, open: readonly number[]): string {
  const lines = keys.map((key) => itemLine(key, state, open))
  return `${header}\n${lines.join("\n")}\n${SENTINEL_HELP}`
}

// ── the bundled-query measurement (counting, never classifying) ──────────────

const INTERROGATIVES = ["what", "why", "how", "when", "where", "which", "who", "whether"]

function distinctInterrogatives(query: string): string[] {
  const lower = query.toLowerCase()
  return INTERROGATIVES.filter((word) => new RegExp(`\\b${word}\\b`).test(lower))
}

/** The observations that make a query read as more than one ask. Never a verdict. */
export function intentSignals(query: string): string[] {
  const out: string[] = []
  const marks = (query.match(/\?/g) ?? []).length
  if (marks >= 2) out.push(`${marks} question marks`)
  const clauses = query
    .split(/[;\n]/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0)
  if (clauses.length >= 2) out.push(`${clauses.length} joined clauses`)
  const words = distinctInterrogatives(query)
  if (words.length >= 2) out.push(`the interrogatives ${words.join(" and ")}`)
  const conjoined = /\b(?:and|or)\s+(?:what|why|how|when|where|which|who|whether)\b/i
  if (conjoined.test(query)) out.push("a conjunction joining a second question")
  return out
}

function bundled(query: string): boolean {
  return intentSignals(query).length > 0
}

// ── the store-shape measurement (counting, never classifying) ────────────────
//
// Every member is a tally or an absence a parser can settle. None of them reads
// what the text MEANS, which is the whole reason this can be observed at all:
// the harness reports the count and leaves the verdict where it belongs.
//
// The floors are chosen against the false positive each one has. Two sentences
// ("X breaks Y. Use Z instead.") is one dense fact, so the terminator floor is
// THREE and not two; a single list marker or a single symbol separator is prose,
// so both of those are two.
//
// A terminator counts only where it ENDS a word — the dot inside `recall.py` or
// `0.7` terminates nothing. Without that clause the population this observes is
// exactly the one it misreads: a single-fact lesson about code names files, and
// two file names plus a real full stop would trip a floor meant for three
// sentences, spending the session's one observation on prose that carries one fact.

const LIST_MARKER = /^\s*(?:[-*+]|\d+[.)])\s+/
const SENTENCE_END = /[.!?](?:\s|$)/g
const SYMBOL_SEPARATOR = "::"

const LIST_FLOOR = 2
const SYMBOL_FLOOR = 2
const SENTENCE_FLOOR = 3
const BINDING_FLOOR = 2

/** The observations that make a landed store read as more than one thing. Never a verdict. */
export function shapeSignals(args: JsonObject): readonly string[] {
  const out: string[] = []
  const bindings = anchorsOf(args).length
  if (bindings === 0) out.push("no code site named")
  else if (bindings >= BINDING_FLOOR) out.push(`${bindings} code sites named`)
  const body = textOf(args)
  const markers = body.split("\n").filter((line) => LIST_MARKER.test(line)).length
  if (markers >= LIST_FLOOR) out.push(`${markers} list markers`)
  const separators = body.split(SYMBOL_SEPARATOR).length - 1
  if (separators >= SYMBOL_FLOOR) out.push(`${separators} ${SYMBOL_SEPARATOR} separators`)
  const terminators = (body.match(SENTENCE_END) ?? []).length
  if (terminators >= SENTENCE_FLOOR) out.push(`${terminators} sentence terminators`)
  return out
}

// ── the loop items ───────────────────────────────────────────────────────────

/** Actionable ids the session has neither resolved nor deferred. */
function openIds(state: LoopState): readonly number[] {
  return state.actionable.filter(
    (id) => !state.maintained.includes(id) && !state.deferred.includes(id),
  )
}

/** The loop items open right now. Order is LOOP_KEYS order, so a block reads the same way twice. */
export function openKeys(state: LoopState): readonly string[] {
  const out: string[] = []
  if (state.mutations > 0 && state.recalls === 0) out.push(RECALL_MISSING)
  if (state.served.length > 0 && !state.outcomeAfterServe) out.push(OUTCOME_MISSING)
  if (openIds(state).length > 0) out.push(MAINTENANCE_MISSING)
  if (
    state.mutations > 0 &&
    state.writes === 0 &&
    state.noStoreWhy === "" &&
    !(state.captures > 0 && state.captureWhy !== "")
  ) {
    out.push(STORE_MISSING)
  }
  if (state.unscopedStore && state.generalWhy === "") out.push(SCOPE_MISSING)
  return out
}

// ── the sentinels ────────────────────────────────────────────────────────────

/**
 * Read the four declarations out of the turn's final message. A declaration
 * with an EMPTY rationale closes nothing — that is what keeps it a decision
 * rather than a way past the gate.
 */
export function applySentinels(state: LoopState, finalMessage: string): LoopState {
  let next = state
  for (const line of finalMessage.split("\n")) {
    const match = SENTINEL.exec(line)
    if (match === null) continue
    const why = (match[3] ?? "").trim()
    if (why === "") continue
    const kind = match[1]
    if (kind === "no-store") {
      next = { ...next, noStoreWhy: why }
    } else if (kind === "capture") {
      next = { ...next, captureWhy: why }
    } else if (kind === "general") {
      next = { ...next, generalWhy: why }
    } else {
      const ids = (match[2] ?? "").match(/\d+/g) ?? []
      const deferred = [...next.deferred]
      for (const raw of ids) {
        const id = Number.parseInt(raw, 10)
        if (Number.isInteger(id) && !deferred.includes(id)) deferred.push(id)
      }
      next = { ...next, deferred }
    }
  }
  return next
}

// ── journalling ──────────────────────────────────────────────────────────────

function extend(existing: readonly number[], added: readonly number[]): readonly number[] {
  const out = [...existing]
  for (const id of added) if (!out.includes(id)) out.push(id)
  return out
}

function extendKeys(existing: readonly string[], added: readonly string[]): readonly string[] {
  const out = [...existing]
  for (const key of added) if (!out.includes(key)) out.push(key)
  return out
}

function journal(state: LoopState, event: Event): LoopState {
  let next = state
  if (event.touchedSource && event.configured) next = { ...next, armed: true }
  if (event.role === "MUTATE") next = { ...next, mutations: next.mutations + 1 }
  if (event.verb === "") return next

  next = { ...next, hiveSeen: true }
  const envelope: JsonObject | undefined = readEnvelope(event.result)

  if (event.verb === RECALL) {
    next = { ...next, recalls: next.recalls + 1 }
    if (envelope === undefined) return next
    const served = servedIds(envelope)
    if (served.length > 0) {
      next = {
        ...next,
        served: extend(next.served, served),
        actionable: extend(next.actionable, actionableIds(envelope)),
        outcomeAfterServe: false,
      }
    }
    return next
  }

  if (event.verb === OUTCOME) return { ...next, outcomeAfterServe: true }

  if (isStore(event.verb)) {
    if (envelope === undefined || refused(envelope)) return next
    next =
      event.verb === WRITE
        ? { ...next, writes: next.writes + 1 }
        : { ...next, captures: next.captures + 1 }
    if (carrierless(event.args)) next = { ...next, unscopedStore: true }
    // a store's own retirement rider routes through the one retirement owner
    // after the write lands, so its outcome is reported in this same envelope
    const target = riderTarget(event.args)
    if (target !== null && retiredByRider(envelope) !== null) {
      next = { ...next, maintained: extend(next.maintained, [target]) }
    }
    return next
  }

  if (isMaintenance(event.verb)) {
    if (envelope === undefined || !affirmed(envelope)) return next
    return { ...next, maintained: extend(next.maintained, namedIds(event.verb, event.args)) }
  }

  return next
}

// ── the reducer ──────────────────────────────────────────────────────────────

const NOTHING: Outcome = Object.freeze({ decision: INERT, next: null })

function resolved(decision: Decision, next: LoopState | null): Outcome {
  return Object.freeze({ decision, next })
}

export function decide(event: Event, state: LoopState | null, env: Env): Outcome {
  if (!env.enabled) return NOTHING

  if (event.kind === "SESSION_RESUMED") {
    if (state === null || !state.armed) return NOTHING
    const keys = openKeys(state)
    if (keys.length === 0) return NOTHING
    const header =
      `hive-loop: this session carries ${keys.length} open loop item(s) recorded ` +
      `before its context was compacted.`
    return resolved(context(report(header, keys, state, openIds(state))), null)
  }

  if (event.kind === "TOOL_PRE") {
    // inert unless armed: there is no arming precondition a caller could forget
    if (state === null || !state.armed || state.recalls > 0) return NOTHING
    if (event.role === "MUTATE") return resolved(deny(GATE_MUTATION), null)
    if (isStore(event.verb)) return resolved(deny(GATE_STORE), null)
    return NOTHING
  }

  if (event.kind === "TOOL_POST") {
    const next = journal(state ?? EMPTY, event)
    if (event.verb === RECALL) {
      const envelope = readEnvelope(event.result)
      const query = queryOf(event.args)
      if (envelope !== undefined && abstained(envelope) && bundled(query)) {
        return resolved(feedback(bundledFeedback(query)), next)
      }
    }
    // one observation per session, spent on the first landed store that trips a
    // signal: the budget is what keeps a channel with no server-side gate credible
    if (isStore(event.verb) && next.armed && !next.shapeObserved) {
      const envelope = readEnvelope(event.result)
      const signals = envelope === undefined || refused(envelope) ? [] : shapeSignals(event.args)
      if (signals.length > 0) {
        return resolved(feedback(shapeFeedback(event.verb, signals)), { ...next, shapeObserved: true })
      }
    }
    return resolved(INERT, next)
  }

  if (event.kind === "TURN_END") {
    if (state === null || !state.armed || event.reentrant) return NOTHING
    const next = applySentinels(state, event.finalMessage)
    const keys = openKeys(next)
    if (keys.length === 0) return resolved(INERT, next)
    if (keys.every((key) => next.blocks.includes(key))) return resolved(INERT, next)
    const header =
      `hive-loop: this turn is ending with ${keys.length} open loop item(s) ` +
      `recorded for this session. Each is named once and will not block again.`
    const reason = report(header, keys, next, openIds(next))
    return resolved(block(reason), { ...next, blocks: extendKeys(next.blocks, keys) })
  }

  return NOTHING
}

