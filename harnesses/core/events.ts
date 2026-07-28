// The carriers the whole harness is written against, plus the total coercion
// primitives every parse goes through. PURE: no I/O, no runtime vocabulary.
//
// The two normalized vocabularies below are the portability contract. An adapter
// maps its own runtime's event names onto EventKind and its own tool names onto
// Role; nothing here learns what those names were. That is the entire cost of a
// second runtime.

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json }
export type JsonObject = { readonly [key: string]: Json }

export const EVENT_KINDS = ["SESSION_RESUMED", "TOOL_PRE", "TOOL_POST", "TURN_END"] as const
export type EventKind = (typeof EVENT_KINDS)[number]

export const ROLES = ["MUTATE", "READ", "OTHER"] as const
export type Role = (typeof ROLES)[number]

/**
 * One normalized observation. Every field is present and typed, so a rule can
 * never read a half-built event; an adapter that learned nothing about a field
 * hands over its safe default rather than omitting it.
 */
export interface Event {
  readonly kind: EventKind
  /** What the observed call does to the working tree. */
  readonly role: Role
  /** The memory verb this call names, or "" when it names none. */
  readonly verb: string
  /** The observed call's arguments. */
  readonly args: JsonObject
  /** The observed call's result, uninterpreted. */
  readonly result: Json
  /** The final message of the turn, when the runtime carries one. */
  readonly finalMessage: string
  readonly sessionId: string
  /** Distinct per nested agent; "" on the main thread. */
  readonly agentId: string
  /** The working root — the ledger's partition, never written into. */
  readonly scope: string
  /** A source file under the working root was read, searched or changed. */
  readonly touchedSource: boolean
  /** The runtime has a memory endpoint wired for this session. */
  readonly configured: boolean
  /** This event is the runtime re-entering a decision it already made. */
  readonly reentrant: boolean
}

/**
 * What the harness wants done about an event. Law 2 by type construction: every
 * non-inert arm carries its reason, so a silent block is a COMPILE error rather
 * than a runtime one. The adapter renders these into its runtime's wire format.
 */
export type Decision =
  | { readonly kind: "inert" }
  | { readonly kind: "deny"; readonly reason: string }
  | { readonly kind: "block"; readonly reason: string }
  | { readonly kind: "context"; readonly reason: string }
  | { readonly kind: "feedback"; readonly reason: string }

export const INERT: Decision = Object.freeze({ kind: "inert" as const })

export function deny(reason: string): Decision {
  return Object.freeze({ kind: "deny" as const, reason })
}
export function block(reason: string): Decision {
  return Object.freeze({ kind: "block" as const, reason })
}
export function context(reason: string): Decision {
  return Object.freeze({ kind: "context" as const, reason })
}
export function feedback(reason: string): Decision {
  return Object.freeze({ kind: "feedback" as const, reason })
}

/** Resolved once at the I/O edge and handed in; the rules never read the environment. */
export interface Env {
  readonly enabled: boolean
  readonly stateDir: string
  /** Degradations worth one line on the error channel — never a crash. */
  readonly warnings: readonly string[]
}

const EMPTY_OBJECT: JsonObject = Object.freeze({})
const EMPTY_ARRAY: readonly Json[] = Object.freeze([])

/** Any value as a plain record. Never throws; a non-record is an empty one. */
export function asRecord(value: unknown): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return EMPTY_OBJECT
  return value as JsonObject
}

/** Any value as a list. Never throws; a non-list is an empty one. */
export function asArray(value: unknown): readonly Json[] {
  return Array.isArray(value) ? (value as Json[]) : EMPTY_ARRAY
}

/** Any value as text. Never throws; a non-string is "". */
export function asText(value: unknown): string {
  return typeof value === "string" ? value : ""
}

/** Any value as a flag. Never throws; only a real true is true. */
export function asFlag(value: unknown): boolean {
  return value === true
}

/** Any value as a memory id. Never throws; anything but a finite integer is null. */
export function asId(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || !Number.isInteger(value)) return null
  return value
}

/** Parse text as JSON. Never throws; anything unparseable is undefined. */
export function readJson(text: unknown): Json | undefined {
  if (typeof text !== "string") return undefined
  try {
    return JSON.parse(text) as Json
  } catch {
    return undefined
  }
}

/**
 * Build a complete, frozen event from whatever an adapter managed to observe.
 * Total by construction: a missing or hostile field falls to its safe default,
 * so no caller can produce a partially-built event. The kind is a parameter
 * rather than a field because an unmapped event has no safe default — an
 * adapter drops it instead of guessing one.
 */
export function event(kind: EventKind, fields: Omit<Partial<Event>, "kind">): Event {
  const role = fields.role !== undefined && ROLES.includes(fields.role) ? fields.role : "OTHER"
  return Object.freeze({
    kind,
    role,
    verb: asText(fields.verb),
    args: asRecord(fields.args),
    result: fields.result === undefined ? null : fields.result,
    finalMessage: asText(fields.finalMessage),
    sessionId: asText(fields.sessionId),
    agentId: asText(fields.agentId),
    scope: asText(fields.scope),
    touchedSource: asFlag(fields.touchedSource),
    configured: asFlag(fields.configured),
    reentrant: asFlag(fields.reentrant),
  })
}
