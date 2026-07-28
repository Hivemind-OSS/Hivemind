// The live tier: the real CLI, in a real scratch repo, because a whole bug
// family in this project is real-CLI-only — an offline double cannot see a hook
// that never loaded, a matcher that never matched, or an operator-scope hook
// silently replacing an answer.
//
// Opt-in and self-skipping: without HIVE_LOOP_LIVE these are skipped, so the
// canonical gate needs no marker registry and no deselect expression.

import { test } from "node:test"
import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { fileURLToPath } from "node:url"

const HARNESS_ROOT = fileURLToPath(new URL("../", import.meta.url))
const LIVE = (process.env["HIVE_LOOP_LIVE"] ?? "") !== ""

interface Scratch {
  readonly repo: string
  readonly stateDir: string
}

let seq = 0

function scratch(): Scratch {
  const base = mkdtempSync(join(tmpdir(), `hive-loop-live-${seq++}-`))
  const repo = join(base, "repo")
  mkdirSync(join(repo, "src"), { recursive: true })
  writeFileSync(join(repo, "src", "app.ts"), "export const answer = 1\n", "utf8")
  writeFileSync(
    join(repo, ".mcp.json"),
    JSON.stringify({ mcpServers: { hive: { type: "http", url: "http://127.0.0.1:8765/mcp" } } }),
    "utf8",
  )
  return { repo, stateDir: join(base, "state") }
}

/**
 * One headless session. Isolated from the operator's own configuration on every
 * axis a nested run can leak through — settings sources, MCP config — because a
 * leaked operator hook silently replacing the measured answer is exactly the
 * failure this tier exists to catch.
 */
function session(
  s: Scratch,
  prompt: string,
  opts: { plugin?: boolean; enabled?: string } = {},
): { code: number; stdout: string; stderr: string } {
  const argv = [
    "-p",
    prompt,
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
  ]
  if (opts.plugin !== false) argv.push("--plugin-dir", HARNESS_ROOT)
  const env: Record<string, string | undefined> = {
    ...process.env,
    HIVE_LOOP__STATE_DIR: s.stateDir,
  }
  if (opts.enabled !== undefined) env["HIVE_LOOP__ENABLED"] = opts.enabled
  const r = spawnSync("claude", argv, {
    encoding: "utf8",
    cwd: s.repo,
    timeout: 600_000,
    env,
  })
  return { code: r.status ?? -1, stdout: r.stdout ?? "", stderr: r.stderr ?? "" }
}

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

const EDIT_TASK =
  "Read src/app.ts, then use the Edit tool to change the value 1 to 2 in it. " +
  "Do not use Bash. When you are done, reply with one short sentence."

test("a governed session arms on a source read and journals its own loop", { skip: !LIVE }, () => {
  const s = scratch()
  const run = session(s, EDIT_TASK)
  const found = ledgers(s.stateDir)
  assert.ok(
    found.length > 0,
    `no ledger was written — the plugin did not load.\nstdout: ${run.stdout}\nstderr: ${run.stderr}`,
  )
  assert.ok(
    found.some((l) => l["armed"] === true),
    `the session never armed: ${JSON.stringify(found)}`,
  )
})

test("the same repo with the plugin ABSENT stays byte-inert", { skip: !LIVE }, () => {
  const s = scratch()
  session(s, EDIT_TASK, { plugin: false })
  assert.deepEqual(ledgers(s.stateDir), [], "an ungoverned session must write nothing")
  assert.ok(!existsSync(s.stateDir), "an ungoverned session must not even create the directory")
})

test("the kill switch makes a governed session byte-inert", { skip: !LIVE }, () => {
  const s = scratch()
  session(s, EDIT_TASK, { enabled: "0" })
  assert.deepEqual(ledgers(s.stateDir), [], "the kill switch must write nothing")
  assert.ok(!existsSync(s.stateDir), "the kill switch must not even create the directory")
})

test("a session that touches no source never arms", { skip: !LIVE }, () => {
  const s = scratch()
  const run = session(s, "Reply with exactly: ok. Do not read or write any file.")
  assert.equal(run.code, 0, run.stderr)
  for (const ledger of ledgers(s.stateDir)) {
    assert.notEqual(ledger["armed"], true, "a conversational turn must stay unarmed")
  }
})
