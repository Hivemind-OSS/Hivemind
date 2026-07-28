#!/usr/bin/env node
// The Claude Code adapter: the ONLY module that does I/O and the only one that
// knows a single Claude Code name. It reads a hook payload from stdin, maps it
// onto the normalized event the core reasons about, asks the core, and renders
// the answer into the hook wire format.
//
// Fail-open throughout (Law 6): a side-channel must never break the turn, so
// every throw path exits 0 with no output. The harness under-blocking is the
// safe direction; the harness crashing a turn is not.
//
// Every import here is RELATIVE, so the directory this plugin is installed into
// can be named anything and the tree still runs from a copy.

import { existsSync, readFileSync } from "node:fs"
import { homedir, tmpdir } from "node:os"
import { join } from "node:path"
import { fileURLToPath } from "node:url"

import { asArray, asFlag, asRecord, asText, event, readJson } from "../core/events.ts"
import type { Decision, Env, Event, EventKind, Json, JsonObject, Role } from "../core/events.ts"
import { decide } from "../core/decide.ts"
import { load, save } from "../core/state.ts"
import { verbOf } from "../core/hive.ts"

// ── the event-name map: the whole of what this adapter knows about lifecycles ─
//
// hooks.json registers exactly these names, and a contract test compares the two
// in both directions so neither can drift. Anything unmapped is dropped.

export const EVENT_MAP: Readonly<Record<string, EventKind>> = Object.freeze({
  SessionStart: "SESSION_RESUMED",
  PreToolUse: "TOOL_PRE",
  PostToolUse: "TOOL_POST",
  Stop: "TURN_END",
  SubagentStop: "TURN_END",
})

// ── tool classification: the second half of the portability seam ─────────────

const MUTATING_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit"])
const READING_TOOLS = new Set(["Read", "Glob", "Grep", "NotebookRead"])
const SHELL_TOOL = "Bash"

/**
 * A shell command that changes the working tree. A matcher cannot express this
 * — it is a semantic, and a declarative file is where no test can reach it — so
 * the whole test lives here. It is deliberately generous: a narrower one would
 * leave in-place edits, redirections and checkouts ungated, which would make the
 * mutation gate decorative.
 */
const MUTATING_SHELL = new RegExp(
  [
    String.raw`\bsed\b[^|;&]*\s-[a-z]*i`,
    String.raw`\bperl\b[^|;&]*\s-[a-z]*i`,
    String.raw`\btee\b`,
    String.raw`(^|[^>&\d])>{1,2}(?!&)`,
    String.raw`\bmv\b`,
    String.raw`\bcp\b`,
    String.raw`\brm\b`,
    String.raw`\bmkdir\b`,
    String.raw`\btouch\b`,
    String.raw`\btruncate\b`,
    String.raw`\bchmod\b`,
    String.raw`\bchown\b`,
    String.raw`\bln\b`,
    String.raw`\bpatch\b`,
    String.raw`\bdd\b`,
    String.raw`\bgit\s+(?:apply|checkout|restore|reset|revert|stash|clean|rm|mv|merge|rebase|cherry-pick|am)\b`,
    String.raw`\bnpm\s+(?:install|i|ci|uninstall)\b`,
    String.raw`\b(?:uv|pip|pip3)\s+(?:install|uninstall|sync|add|remove)\b`,
  ].join("|"),
)

function roleOf(toolName: string, input: JsonObject): Role {
  if (MUTATING_TOOLS.has(toolName)) return "MUTATE"
  if (READING_TOOLS.has(toolName)) return "READ"
  if (toolName === SHELL_TOOL) {
    return MUTATING_SHELL.test(asText(input["command"])) ? "MUTATE" : "READ"
  }
  return "OTHER"
}

// ── the arming observation: what counts as engaging the codebase ─────────────

const SOURCE_SUFFIXES = [
  ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".pyi", ".go", ".rs", ".rb",
  ".java", ".kt", ".kts", ".scala", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift",
  ".m", ".mm", ".php", ".pl", ".sh", ".bash", ".zsh", ".sql", ".lua", ".ex", ".exs",
  ".erl", ".hs", ".ml", ".clj", ".vue", ".svelte", ".proto", ".tf", ".gradle",
]

/** Trees that are about the work rather than the work itself. */
const EXCLUDED_ROOTS = ["graphify-out", "CONTEXT", "docs", "node_modules", "vendor"]

const PATH_KEYS = ["file_path", "notebook_path", "path", "pattern"]

function looksLikeSource(candidate: string, root: string): boolean {
  if (candidate === "") return false
  let path = candidate.replace(/\\/g, "/")
  if (root !== "" && path.startsWith(root)) path = path.slice(root.length)
  path = path.replace(/^\/+/, "")
  if (path.startsWith("/") || path.includes("://")) return false
  const segments = path.split("/")
  for (const segment of segments.slice(0, -1)) {
    if (segment.startsWith(".") || EXCLUDED_ROOTS.includes(segment)) return false
  }
  const leaf = segments[segments.length - 1] ?? ""
  if (leaf.startsWith(".")) return false
  return SOURCE_SUFFIXES.some((suffix) => leaf.endsWith(suffix))
}

function touchedSource(toolName: string, input: JsonObject, root: string): boolean {
  if (!MUTATING_TOOLS.has(toolName) && !READING_TOOLS.has(toolName)) return false
  for (const key of PATH_KEYS) {
    if (looksLikeSource(asText(input[key]), root)) return true
  }
  for (const item of asArray(input["edits"])) {
    if (looksLikeSource(asText(asRecord(item)["file_path"]), root)) return true
  }
  return false
}

// ── the endpoint observation: is a memory server wired for this session ──────
//
// A local file read only. Probing the endpoint would be transport, which this
// harness does not have and must not acquire.

const PLUGIN_MANIFEST = fileURLToPath(new URL("../.claude-plugin/plugin.json", import.meta.url))

function declaresHive(value: Json | undefined): boolean {
  const servers = asRecord(asRecord(value)["mcpServers"])
  return Object.keys(servers).some((name) => name.toLowerCase().includes("hive"))
}

function readsAsHive(path: string): boolean {
  try {
    if (!existsSync(path)) return false
    return declaresHive(readJson(readFileSync(path, "utf8")))
  } catch {
    return false // one unreadable location never suppresses the others
  }
}

function configLocations(root: string): string[] {
  const home = safeHome()
  return [
    PLUGIN_MANIFEST,
    join(root, ".mcp.json"),
    join(root, ".claude", "settings.json"),
    join(root, ".claude", "settings.local.json"),
    join(home, ".claude.json"),
    join(home, ".claude", "settings.json"),
    "/etc/claude-code/managed-mcp.json",
    "/etc/claude-code/managed-settings.json",
    "/Library/Application Support/ClaudeCode/managed-mcp.json",
    "/Library/Application Support/ClaudeCode/managed-settings.json",
  ]
}

function safeHome(): string {
  try {
    return homedir()
  } catch {
    return ""
  }
}

function configured(root: string, verb: string): boolean {
  if (verb !== "") return true // an observed memory call is the strongest evidence
  return configLocations(root).some(readsAsHive)
}

// ── the environment, resolved once at the edge ───────────────────────────────

const ENABLED_VAR = "HIVE_LOOP__ENABLED"
const STATE_DIR_VAR = "HIVE_LOOP__STATE_DIR"
const DATA_DIR_VAR = "CLAUDE_PLUGIN_DATA"

const TRUTHY = new Set(["1", "true", "yes", "on"])
const FALSY = new Set(["0", "false", "no", "off"])

export function resolveEnv(vars: Record<string, string | undefined>): Env {
  const warnings: string[] = []
  const raw = (vars[ENABLED_VAR] ?? "").trim().toLowerCase()
  let enabled = true
  if (raw !== "") {
    if (FALSY.has(raw)) enabled = false
    else if (!TRUTHY.has(raw)) {
      warnings.push(`hive-loop: ${ENABLED_VAR}=${raw} is not a recognized value; staying enabled`)
    }
  }
  const stateDir =
    (vars[STATE_DIR_VAR] ?? "").trim() ||
    (vars[DATA_DIR_VAR] ?? "").trim() ||
    join(tmpdir(), "hive-loop")
  return { enabled, stateDir, warnings }
}

// ── payload → Event ──────────────────────────────────────────────────────────

export function toEvent(payload: Json): Event | null {
  const record = asRecord(payload)
  const kind = EVENT_MAP[asText(record["hook_event_name"])]
  if (kind === undefined) return null

  const root = asText(record["cwd"])
  const toolName = asText(record["tool_name"])
  const input = asRecord(record["tool_input"])
  const verb = verbOf(toolName)
  return event(kind, {
    role: roleOf(toolName, input),
    verb,
    args: input,
    result: record["tool_response"] ?? null,
    finalMessage: asText(record["last_assistant_message"]),
    sessionId: asText(record["session_id"]),
    agentId: asText(record["agent_id"]),
    scope: root,
    touchedSource: touchedSource(toolName, input, root),
    configured: configured(root, verb),
    reentrant: asFlag(record["stop_hook_active"]),
  })
}

// ── Decision → the hook wire format ──────────────────────────────────────────

const WIRE_EVENT: Readonly<Record<EventKind, string>> = Object.freeze({
  SESSION_RESUMED: "SessionStart",
  TOOL_PRE: "PreToolUse",
  TOOL_POST: "PostToolUse",
  TURN_END: "Stop",
})

export interface Emission {
  readonly stdout: string
  readonly stderr: string
  readonly code: number
}

const SILENT: Emission = Object.freeze({ stdout: "", stderr: "", code: 0 })

export function render(decision: Decision, kind: EventKind): Emission {
  if (decision.kind === "deny") {
    return {
      stdout: JSON.stringify({
        hookSpecificOutput: {
          hookEventName: WIRE_EVENT[kind],
          permissionDecision: "deny",
          permissionDecisionReason: decision.reason,
        },
      }),
      stderr: "",
      code: 0,
    }
  }
  if (decision.kind === "block") {
    return { stdout: JSON.stringify({ decision: "block", reason: decision.reason }), stderr: "", code: 0 }
  }
  if (decision.kind === "context") {
    return {
      stdout: JSON.stringify({
        hookSpecificOutput: {
          hookEventName: WIRE_EVENT[kind],
          additionalContext: decision.reason,
        },
      }),
      stderr: "",
      code: 0,
    }
  }
  if (decision.kind === "feedback") {
    // structured stdout, never stderr+exit-2: this rides a detached hook, and a
    // detached process has no turn left to feed stderr back into
    return {
      stdout: JSON.stringify({
        hookSpecificOutput: {
          hookEventName: WIRE_EVENT[kind],
          additionalContext: decision.reason,
        },
      }),
      stderr: "",
      code: 0,
    }
  }
  return SILENT
}

// ── the entry point ──────────────────────────────────────────────────────────

export function handle(raw: string, vars: Record<string, string | undefined>): Emission {
  const env = resolveEnv(vars)
  if (!env.enabled) return SILENT // byte-inert: no output, no ledger, no read

  const parsed = readJson(raw)
  if (parsed === undefined) return warned(SILENT, env)
  const observed = toEvent(parsed)
  if (observed === null) return warned(SILENT, env)

  const previous = load(env.stateDir, observed.scope, observed.sessionId, observed.agentId)
  const outcome = decide(observed, previous, env)
  if (outcome.next !== null) {
    save(env.stateDir, observed.scope, observed.sessionId, observed.agentId, outcome.next)
  }
  return warned(render(outcome.decision, observed.kind), env)
}

function warned(emission: Emission, env: Env): Emission {
  if (env.warnings.length === 0) return emission
  const prefix = `${env.warnings.join("\n")}\n`
  return { ...emission, stderr: `${prefix}${emission.stderr}` }
}

function emit(emission: Emission): void {
  if (emission.stdout !== "") process.stdout.write(emission.stdout)
  if (emission.stderr !== "") process.stderr.write(emission.stderr)
  process.exitCode = emission.code
}

function main(): void {
  let raw = ""
  process.stdin.setEncoding("utf8")
  process.stdin.on("data", (chunk: string) => {
    raw += chunk
  })
  process.stdin.on("end", () => {
    try {
      emit(handle(raw, process.env))
    } catch {
      emit(SILENT) // a side-channel never breaks the turn
    }
  })
  process.stdin.on("error", () => {
    emit(SILENT)
  })
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main()
