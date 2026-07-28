// The ledger: the only module under core/ that owns disk, and the only one that
// may import node:*. Everything about the schema lives here and nowhere else —
// the JSON on disk IS the LoopState shape, so there is no serializer to drift.
//
// Three-valued by design: missing is not the same as recorded-clean, which is
// not the same as recorded-with-debt. A missing, truncated or unknown-version
// ledger reads ABSENT, never as an open debt — a first turn can never be blocked
// over dirt that predates the ledger.

import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs"
import { join } from "node:path"
import { createHash } from "node:crypto"

/** Bumped when the shape changes. An unknown version reads absent (silence, never a crash). */
export const STATE_VERSION = 2

/** Ledger files older than this are swept on load, which is why no end-of-session hook exists. */
export const PRUNE_AFTER_MS = 7 * 24 * 60 * 60 * 1000

export interface LoopState {
  readonly version: number
  /** Both arming clauses were satisfied at some point in this session. */
  readonly armed: boolean
  /** A memory call was observed. */
  readonly hiveSeen: boolean
  /** Recall calls observed — a COUNT OF ATTEMPTS, never the query text and never the answer. */
  readonly recalls: number
  /** Tree-changing calls observed. */
  readonly mutations: number
  /** Episode ids the server returned. */
  readonly served: readonly number[]
  /** Served ids the SERVER itself marked actionable. */
  readonly actionable: readonly number[]
  /** An outcome call followed the last serving recall. */
  readonly outcomeAfterServe: boolean
  /**
   * Landed store calls, counted PER VERB. Two counters rather than one because
   * the two verbs close different things — a single total could not tell a
   * session that wrote from one that captured.
   */
  readonly writes: number
  readonly captures: number
  /** A landed store carried neither binding nor scope. */
  readonly unscopedStore: boolean
  /** The one-per-session shape observation has been spent. */
  readonly shapeObserved: boolean
  /** Ids in a maintenance call the server AFFIRMED. */
  readonly maintained: readonly number[]
  /** Ids deferred by a final-message sentinel. */
  readonly deferred: readonly number[]
  /** The recorded "nothing applies" rationale. */
  readonly noStoreWhy: string
  /** The recorded "the verb choice was not clear" rationale. */
  readonly captureWhy: string
  /** The recorded "this binds to nothing" rationale. */
  readonly generalWhy: string
  /** Loop keys that already blocked once. */
  readonly blocks: readonly string[]
}

export const EMPTY: LoopState = Object.freeze({
  version: STATE_VERSION,
  armed: false,
  hiveSeen: false,
  recalls: 0,
  mutations: 0,
  served: Object.freeze([]) as readonly number[],
  actionable: Object.freeze([]) as readonly number[],
  outcomeAfterServe: false,
  writes: 0,
  captures: 0,
  unscopedStore: false,
  shapeObserved: false,
  maintained: Object.freeze([]) as readonly number[],
  deferred: Object.freeze([]) as readonly number[],
  noStoreWhy: "",
  captureWhy: "",
  generalWhy: "",
  blocks: Object.freeze([]) as readonly string[],
})

function digest(value: string): string {
  return createHash("sha1").update(value).digest("hex").slice(0, 16)
}

/** One directory per working root, one file per (session, nested agent). */
export function ledgerDir(stateDir: string, scope: string): string {
  return join(stateDir, digest(scope))
}

export function ledgerPath(
  stateDir: string,
  scope: string,
  sessionId: string,
  agentId: string,
): string {
  const key = `${digest(sessionId)}-${digest(agentId)}.json`
  return join(ledgerDir(stateDir, scope), key)
}

function numbers(value: unknown): readonly number[] {
  if (!Array.isArray(value)) return []
  const out: number[] = []
  for (const item of value) {
    if (typeof item === "number" && Number.isFinite(item) && Number.isInteger(item)) out.push(item)
  }
  return Object.freeze(out)
}

function strings(value: unknown): readonly string[] {
  if (!Array.isArray(value)) return []
  return Object.freeze(value.filter((v): v is string => typeof v === "string"))
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0
}

function text(value: unknown): string {
  return typeof value === "string" ? value : ""
}

/**
 * Read the ledger, or null when there is nothing sound to read. Missing,
 * truncated, non-object and unknown-version all collapse to null — the safe
 * direction, since a fabricated ledger would manufacture a debt.
 */
export function load(
  stateDir: string,
  scope: string,
  sessionId: string,
  agentId: string,
): LoopState | null {
  prune(stateDir, scope)
  let raw = ""
  try {
    raw = readFileSync(ledgerPath(stateDir, scope, sessionId, agentId), "utf8")
  } catch {
    return null
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(raw) as unknown
  } catch {
    return null
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null
  const doc = parsed as Record<string, unknown>
  if (doc["version"] !== STATE_VERSION) return null
  return Object.freeze({
    version: STATE_VERSION,
    armed: doc["armed"] === true,
    hiveSeen: doc["hiveSeen"] === true,
    recalls: count(doc["recalls"]),
    mutations: count(doc["mutations"]),
    served: numbers(doc["served"]),
    actionable: numbers(doc["actionable"]),
    outcomeAfterServe: doc["outcomeAfterServe"] === true,
    writes: count(doc["writes"]),
    captures: count(doc["captures"]),
    unscopedStore: doc["unscopedStore"] === true,
    shapeObserved: doc["shapeObserved"] === true,
    maintained: numbers(doc["maintained"]),
    deferred: numbers(doc["deferred"]),
    noStoreWhy: text(doc["noStoreWhy"]),
    captureWhy: text(doc["captureWhy"]),
    generalWhy: text(doc["generalWhy"]),
    blocks: strings(doc["blocks"]),
  })
}

/**
 * Write the ledger atomically: a partial file plus a rename, so a crash mid-write
 * can never leave truncated bytes that a later read would have to interpret. A
 * write fault is swallowed — a lost journal under-blocks, which is the safe
 * direction, and the decision it belonged to is still emitted.
 */
export function save(
  stateDir: string,
  scope: string,
  sessionId: string,
  agentId: string,
  next: LoopState,
): void {
  const final = ledgerPath(stateDir, scope, sessionId, agentId)
  const partial = `${final}.partial`
  try {
    mkdirSync(ledgerDir(stateDir, scope), { recursive: true })
    writeFileSync(partial, JSON.stringify({ ...next, version: STATE_VERSION }), "utf8")
    renameSync(partial, final)
  } catch {
    try {
      rmSync(partial, { recursive: true, force: true })
    } catch {
      // nothing left to do; the journal is lost and the decision still stands
    }
  }
}

/** Sweep this working root's stale ledgers. Session ids never repeat, so age is the only signal. */
export function prune(stateDir: string, scope: string, nowMs: number = Date.now()): void {
  const dir = ledgerDir(stateDir, scope)
  if (!existsSync(dir)) return
  let names: string[] = []
  try {
    names = readdirSync(dir)
  } catch {
    return
  }
  for (const name of names) {
    const path = join(dir, name)
    try {
      if (nowMs - statSync(path).mtimeMs > PRUNE_AFTER_MS) rmSync(path, { recursive: true, force: true })
    } catch {
      // an unreadable entry is left alone; sweeping is housekeeping, never a gate
    }
  }
}
