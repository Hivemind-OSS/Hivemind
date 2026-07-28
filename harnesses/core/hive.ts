// The single owner of the memory-server coupling: what a result envelope looks
// like and what the server already decided about it. Every name it matches on
// comes from the generated constants module, never from a literal here — a
// hand-copied name would be a second copy of the server's vocabulary.
//
// PURE, and a fail-open side-channel throughout: the parse SHELL is guarded, not
// just the handlers, so a hostile or unrecognized result records nothing and
// never breaks the read. This module reads verdicts; it never computes one.

import {
  AFFIRMATIVE_STATUS,
  QUALIFYING_DRIFT,
  STATUS_REFUSED,
  VERBS,
  VERB_CAPTURE,
  VERB_FLAG,
  VERB_OUTCOME,
  VERB_PRUNE,
  VERB_RECALL,
  VERB_SUPERSEDE,
  VERB_WRITE,
} from "./hive-constants.ts"
import { asArray, asId, asRecord, asText, readJson } from "./events.ts"
import type { Json, JsonObject } from "./events.ts"

const VERB_SET: ReadonlySet<string> = new Set<string>(VERBS)
const QUALIFYING: ReadonlySet<string> = new Set<string>(QUALIFYING_DRIFT)
const AFFIRMED: ReadonlySet<string> = new Set<string>(AFFIRMATIVE_STATUS)

const STORE_VERBS: ReadonlySet<string> = new Set<string>([VERB_WRITE, VERB_CAPTURE])
const MAINTENANCE_VERBS: ReadonlySet<string> = new Set<string>([
  VERB_SUPERSEDE,
  VERB_PRUNE,
  VERB_FLAG,
])

export const RECALL = VERB_RECALL
export const OUTCOME = VERB_OUTCOME
export const WRITE = VERB_WRITE
export const CAPTURE = VERB_CAPTURE

export function isStore(verb: string): boolean {
  return STORE_VERBS.has(verb)
}

export function isMaintenance(verb: string): boolean {
  return MAINTENANCE_VERBS.has(verb)
}

/**
 * The memory verb a runtime tool name denotes, or "" when it denotes none.
 *
 * The verb is the segment after the LAST separator, never a prefix match: the
 * namespace a runtime wraps a server in is its own business and changes with how
 * the server was declared, so matching on it would make every rule depend on an
 * install detail.
 */
export function verbOf(toolName: string): string {
  const name = asText(toolName)
  const cut = name.lastIndexOf("__")
  const tail = cut < 0 ? name : name.slice(cut + 2)
  return VERB_SET.has(tail) ? tail : ""
}

const ENVELOPE_KEYS = ["reference_context", "abstained", "status", "conflicts", "trace_id"]

function looksLikeEnvelope(value: JsonObject): boolean {
  return ENVELOPE_KEYS.some((key) => key in value)
}

/**
 * The server envelope inside whatever the runtime handed over, or undefined.
 *
 * Three shapes are accepted because the wrapping is the runtime's choice, not
 * the server's: the result frame carrying the envelope as text, the envelope
 * itself, and either of those as a JSON string. Anything else yields undefined,
 * which records nothing — an unreadable serve must never manufacture a debt.
 */
export function readEnvelope(result: Json | undefined): JsonObject | undefined {
  if (typeof result === "string") {
    const parsed = readJson(result)
    return parsed === undefined ? undefined : readEnvelope(parsed)
  }
  const record = asRecord(result)
  for (const item of asArray(record["content"])) {
    const parsed = readJson(asRecord(item)["text"])
    if (parsed === undefined) continue
    const inner = asRecord(parsed)
    if (looksLikeEnvelope(inner)) return inner
  }
  return looksLikeEnvelope(record) ? record : undefined
}

function hits(envelope: JsonObject): readonly JsonObject[] {
  return asArray(envelope["reference_context"]).map((hit) => asRecord(hit))
}

/** The episode ids the server actually served. */
export function servedIds(envelope: JsonObject): readonly number[] {
  const out: number[] = []
  for (const hit of hits(envelope)) {
    const id = asId(hit["episode_id"])
    if (id !== null && !out.includes(id)) out.push(id)
  }
  return out
}

/**
 * The served ids the SERVER itself marked as needing an answer, by any of the
 * three signals it emits: a drift verdict on the tier it acts on, its own
 * per-hit rider, or membership of the conflicts list. Never a fourth signal and
 * never a computed one — a verdict the server declines to act on is not one the
 * harness may act on either.
 */
export function actionableIds(envelope: JsonObject): readonly number[] {
  const served = servedIds(envelope)
  const out: number[] = []
  const mark = (id: number | null): void => {
    if (id !== null && served.includes(id) && !out.includes(id)) out.push(id)
  }
  for (const hit of hits(envelope)) {
    const verdict = asText(asRecord(hit["drift"])["type"])
    if (QUALIFYING.has(verdict) || "remediation" in hit) mark(asId(hit["episode_id"]))
  }
  for (const note of asArray(envelope["conflicts"])) {
    const record = asRecord(note)
    mark(asId(record["a_id"]))
    mark(asId(record["b_id"]))
  }
  return out
}

/** The server declined to answer. */
export function abstained(envelope: JsonObject): boolean {
  return envelope["abstained"] === true
}

/** The store call was refused, so nothing landed and no decision was closed by it. */
export function refused(envelope: JsonObject): boolean {
  return asText(envelope["status"]) === STATUS_REFUSED
}

/**
 * The server honored the maintenance call. An ALLOW-LIST: an unrecognized future
 * status is not credited, so the loop item stays open. Under-closing is the safe
 * direction — crediting a call the server treated as a benign no-op would teach
 * agents to fire ritual no-ops at a client-side gate.
 */
export function affirmed(envelope: JsonObject): boolean {
  return AFFIRMED.has(asText(envelope["status"]))
}

/**
 * The id a store call retired through its own rider, when the server reports it
 * did. The rider's outcome rides the same envelope as the write it accompanied.
 */
export function retiredByRider(envelope: JsonObject): number | null {
  return asId(envelope["superseded"])
}
