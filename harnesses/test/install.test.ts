// BUG-098's contract: the DOCUMENTED install route is a symlinked plugin root
// (`ln -sfn <repo>/harnesses ~/.claude/skills/hive-loop`), so the entry must run
// — and decide — when reached through the symlinked spelling, while importing the
// adapter as a module stays side-effect-free (the frozen suite imports it to read
// EVENT_MAP). The unfixed guard compared argv's as-invoked spelling against the
// loader's realpathed one, so every documented install was silently inert.
//
// A NEW file by design: the frozen contract.test.ts is never edited, so the
// install contract lands beside it. Authored red against the unfixed system;
// from then on it is never edited to make an implementation pass.

import { test } from "node:test"
import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  symlinkSync,
  writeFileSync,
} from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { fileURLToPath } from "node:url"

const HARNESS_ROOT = fileURLToPath(new URL("../", import.meta.url))
const ENTRY = join(HARNESS_ROOT, "adapters", "claude-code.ts")
const ENTRY_URL = new URL("../adapters/claude-code.ts", import.meta.url).href
const LIVE = (process.env["HIVE_LOOP_LIVE"] ?? "") !== ""

type Json = unknown

function requireEntry(): string {
  if (!existsSync(ENTRY)) assert.fail("harnesses/adapters/claude-code.ts not built yet")
  return ENTRY
}

/** The harness reached through a fresh symlinked root — the documented install shape. */
function symlinkedEntry(prefix: string): string {
  const base = mkdtempSync(join(tmpdir(), prefix))
  const root = join(base, "hive-loop")
  symlinkSync(HARNESS_ROOT, root, "dir")
  return join(root, "adapters", "claude-code.ts")
}

// ── a minimal process rig (the contract suite's shape, self-contained) ───────

interface Ctx {
  readonly stateDir: string
  readonly repo: string
  readonly home: string
}

let seq = 0

function ctx(): Ctx {
  const base = mkdtempSync(join(tmpdir(), `hive-loop-install-${seq++}-`))
  const repo = join(base, "repo")
  const home = join(base, "home")
  mkdirSync(join(repo, "src"), { recursive: true })
  mkdirSync(join(home, ".claude"), { recursive: true })
  writeFileSync(join(repo, "src", "app.ts"), "export const a = 1\n", "utf8")
  return { stateDir: join(base, "state"), repo, home }
}

interface Res {
  readonly code: number
  readonly stdout: string
  readonly stderr: string
}

function runAt(entry: string, c: Ctx, payload: Json): Res {
  const r = spawnSync(process.execPath, [entry], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    cwd: c.repo,
    timeout: 30_000,
    env: {
      PATH: process.env["PATH"],
      HOME: c.home,
      HIVE_LOOP__STATE_DIR: c.stateDir,
    },
  })
  return { code: r.status ?? -1, stdout: r.stdout ?? "", stderr: r.stderr ?? "" }
}

const SID = "sess-install-0001"
const SRC = "src/app.ts"

function armPayload(c: Ctx): Json {
  return {
    session_id: SID,
    hook_event_name: "PostToolUse",
    cwd: c.repo,
    tool_name: "Read",
    tool_input: { file_path: SRC },
    tool_response: { ok: true },
  }
}

function editPayload(c: Ctx): Json {
  return {
    session_id: SID,
    hook_event_name: "PreToolUse",
    cwd: c.repo,
    tool_name: "Edit",
    tool_input: { file_path: SRC },
  }
}

// ── I1 · the documented install decides ──────────────────────────────────────

test("spawn through a symlinked plugin root decides", () => {
  requireEntry()
  const entry = symlinkedEntry("hive-loop-symlink-")
  const c = ctx()
  const armed = runAt(entry, c, armPayload(c))
  assert.equal(armed.code, 0, `arming through the symlink crashed: ${armed.stderr}`)
  const edit = runAt(entry, c, editPayload(c))
  assert.equal(edit.code, 0, `the gate through the symlink crashed: ${edit.stderr}`)
  assert.match(
    edit.stdout,
    /"permissionDecision"\s*:\s*"deny"/,
    "the symlinked spelling is the documented install; byte-silence here is BUG-098 " +
      `(got stdout=${JSON.stringify(edit.stdout)})`,
  )
  assert.ok(
    existsSync(c.stateDir),
    "an armed session journals its ledger — a missing state dir means the entry never ran",
  )
})

test("the decision through the symlinked spelling equals the real-path one", () => {
  requireEntry()
  const linked = symlinkedEntry("hive-loop-samewire-")
  const cReal = ctx()
  runAt(ENTRY, cReal, armPayload(cReal))
  const real = runAt(ENTRY, cReal, editPayload(cReal))
  const cLink = ctx()
  runAt(linked, cLink, armPayload(cLink))
  const link = runAt(linked, cLink, editPayload(cLink))
  assert.equal(link.stdout, real.stdout, "one file, one decision — path spelling must not matter")
  assert.equal(link.code, real.code)
})

// ── import stays inert (the guard is load-bearing for the module import path) ─

test("importing the adapter module is side-effect-free", async () => {
  requireEntry()
  const before = {
    data: process.stdin.listenerCount("data"),
    end: process.stdin.listenerCount("end"),
    error: process.stdin.listenerCount("error"),
    exitCode: process.exitCode,
  }
  const mod = (await import(ENTRY_URL)) as Record<string, Json>
  assert.ok(mod["EVENT_MAP"] !== undefined, "sanity: the module really loaded")
  assert.equal(
    process.stdin.listenerCount("data"),
    before.data,
    "importing the adapter must attach no stdin data listener (main() ran at import)",
  )
  assert.equal(process.stdin.listenerCount("end"), before.end, "no stdin end listener on import")
  assert.equal(process.stdin.listenerCount("error"), before.error, "no stdin error listener on import")
  assert.equal(process.exitCode, before.exitCode, "importing the adapter must not stamp an exit code")
})

// ── the entry-identity predicate (unit table: equal / symlinked / unequal / throw) ─

test("the entry-identity predicate covers equal, symlinked, unequal and throw arms", async () => {
  requireEntry()
  const mod = (await import(ENTRY_URL)) as Record<string, Json>
  const isEntry = mod["isEntry"]
  assert.equal(
    typeof isEntry,
    "function",
    "the adapter must export its entry-identity predicate (isEntry)",
  )
  const f = isEntry as (argv1: string | undefined, moduleUrl: string) => boolean
  const linked = symlinkedEntry("hive-loop-predicate-")
  const cases: [string | undefined, boolean, string][] = [
    [ENTRY, true, "the real spelling is the entry"],
    [linked, true, "the symlinked spelling names the SAME file — BUG-098's arm"],
    [fileURLToPath(import.meta.url), false, "a different real file is not the entry"],
    [join(tmpdir(), "hive-loop-no-such-file.ts"), false, "an unresolvable argv is inert (throw arm)"],
    [undefined, false, "an absent argv is inert"],
  ]
  for (const [argv1, want, why] of cases) {
    assert.equal(f(argv1, ENTRY_URL), want, why)
  }
})

// ── the live tier's symlink-install probe (opt-in, HIVE_LOOP_LIVE=1) ──────────
//
// The always-on tests above drive the entry file directly; this one hands the
// SYMLINKED root to the real CLI, so the platform itself resolves the plugin —
// hooks manifest, matcher, spawn — through the documented install shape.

const EDIT_TASK =
  "Read src/app.ts, then use the Edit tool to change the value 1 to 2 in it. " +
  "Do not use Bash. When you are done, reply with one short sentence."

function ledgers(stateDir: string): Record<string, unknown>[] {
  if (!existsSync(stateDir)) return []
  const out: Record<string, unknown>[] = []
  for (const bucket of readdirSync(stateDir)) {
    const dir = join(stateDir, bucket)
    let names: string[] = []
    try {
      names = readdirSync(dir)
    } catch {
      continue
    }
    for (const name of names) {
      try {
        out.push(JSON.parse(readFileSync(join(dir, name), "utf8")) as Record<string, unknown>)
      } catch {
        // a half-written ledger is not a fixture; skip it
      }
    }
  }
  return out
}

test("a symlink-installed plugin arms and journals through the real CLI", { skip: !LIVE }, () => {
  const base = mkdtempSync(join(tmpdir(), "hive-loop-install-live-"))
  const root = join(base, "hive-loop")
  symlinkSync(HARNESS_ROOT, root, "dir")
  const repo = join(base, "repo")
  const stateDir = join(base, "state")
  mkdirSync(join(repo, "src"), { recursive: true })
  writeFileSync(join(repo, "src", "app.ts"), "export const answer = 1\n", "utf8")
  const r = spawnSync(
    "claude",
    [
      "-p",
      EDIT_TASK,
      "--setting-sources",
      "",
      "--strict-mcp-config",
      "--mcp-config",
      '{"mcpServers":{}}',
      "--plugin-dir",
      root,
    ],
    {
      encoding: "utf8",
      cwd: repo,
      timeout: 600_000,
      env: { ...process.env, HIVE_LOOP__STATE_DIR: stateDir },
    },
  )
  const found = ledgers(stateDir)
  assert.ok(
    found.length > 0,
    `no ledger through the symlinked root — the documented install is inert (BUG-098).\n` +
      `stdout: ${r.stdout}\nstderr: ${r.stderr}`,
  )
  assert.ok(
    found.some((l) => l["armed"] === true),
    `the symlink-installed session never armed: ${JSON.stringify(found)}`,
  )
})
