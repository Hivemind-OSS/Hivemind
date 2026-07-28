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

// ═════════════════════════════════════════════════════════════════════════════
// Channel delivery — that an emission is RECEIVED, not merely that it was sent
// ═════════════════════════════════════════════════════════════════════════════
//
// Emitting and delivering are claims about two different systems, and only the
// first is cheap to test. A decision can serialize perfectly and be discarded by
// the platform, which is a green suite over an inert feature: the bundled-recall
// observation shipped that way, undeliverable in every session from the day it
// was written, because it rode an error channel out of a DETACHED hook and a
// detached process has no turn left to write back into.
//
// So the probe is per CHANNEL, not per rule. Channels are few and fixed; rules
// are many and grow, and driving every rule end-to-end needs the model to choose
// an exact call, which is neither cheap nor deterministic. A synthetic marker
// through each channel is both.
//
// Each case carries the async flag READ FROM the shipped manifest, so flipping
// async on an event that carries a channel reds here rather than in production.
// The evidence predicates below are MEASURED against the platform, not assumed —
// re-measure before changing one.

interface Channel {
  readonly kind: string
  readonly event: string
  /** the matcher the PROBE registers, chosen to be easy to trigger in a scratch repo */
  readonly matcher?: string
  /**
   * the matcher of the SHIPPED group that carries this channel — the probe takes
   * its async flag from there, so this channel is driven the way it really runs.
   * An event with one group leaves it undefined.
   */
  readonly shippedMatcher?: string
  readonly emit: (marker: string) => string
  /** did the platform PARSE and honor it — as opposed to merely running us? */
  readonly honored: (marker: string, records: Record<string, unknown>[], result: Json) => boolean
}

type Json = Record<string, unknown>

function attachments(records: Record<string, unknown>[], marker: string): Json[] {
  return records
    .filter((r) => JSON.stringify(r).includes(marker))
    .map((r) => r["attachment"])
    .filter((a): a is Json => typeof a === "object" && a !== null)
}

const CHANNELS: readonly Channel[] = [
  {
    kind: "context",
    event: "SessionStart",
    emit: (m) =>
      JSON.stringify({ hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: m } }),
    honored: (m, recs) => attachments(recs, m).some((a) => a["type"] === "hook_additional_context"),
  },
  {
    kind: "deny",
    event: "PreToolUse",
    matcher: "Read",
    emit: (m) =>
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "deny",
          permissionDecisionReason: m,
        },
      }),
    // a denied call is recorded by the platform as a refusal, not as an attachment
    honored: (_m, _recs, result) => Array.isArray(result["permission_denials"])
      && (result["permission_denials"] as unknown[]).length > 0,
  },
  {
    kind: "block",
    event: "Stop",
    emit: (m) => JSON.stringify({ decision: "block", reason: m }),
    honored: (m, recs) => attachments(recs, m).some((a) => a["type"] === "hook_blocking_error"),
  },
  {
    kind: "feedback",
    event: "PostToolUse",
    matcher: "Read",
    // the hive group, which is SYNCHRONOUS: a detached hook's output has no turn
    // held open for it, so it is collected only if another turn happens to follow
    shippedMatcher: "mcp__.*__hive_.*",
    emit: (m) =>
      JSON.stringify({ hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: m } }),
    honored: (m, recs) =>
      attachments(recs, m).some(
        (a) =>
          a["type"] === "hook_additional_context" ||
          // detached delivery, kept so a regression to async is diagnosed rather
          // than merely failing: an EMPTY response is what a dropped one looks like
          (a["type"] === "async_hook_response" &&
            typeof a["response"] === "object" &&
            a["response"] !== null &&
            Object.keys(a["response"] as Json).length > 0),
      ),
  },
]

/** the async flag the SHIPPED manifest carries for this channel — never a literal */
function shippedAsync(event: string, shippedMatcher?: string): boolean {
  const manifest = JSON.parse(readFileSync(join(HARNESS_ROOT, "hooks", "hooks.json"), "utf8")) as Json
  const groups = (manifest["hooks"] as Json)[event] as { matcher?: string; hooks?: Json[] }[] | undefined
  assert.ok(groups !== undefined, `the shipped manifest registers no ${event}`)
  const group =
    shippedMatcher === undefined
      ? groups[0]
      : groups.find((g) => g.matcher === shippedMatcher)
  assert.ok(
    group !== undefined,
    `the shipped manifest has no ${event} group matching ${shippedMatcher} — ` +
      `the probe would measure a channel the harness does not actually run`,
  )
  return group.hooks?.[0]?.["async"] === true
}

function transcriptOf(sessionId: string): Record<string, unknown>[] {
  const root = join(process.env["HOME"] ?? "", ".claude", "projects")
  const stack = [root]
  while (stack.length > 0) {
    const dir = stack.pop() as string
    let names: string[] = []
    try {
      names = readdirSync(dir)
    } catch {
      continue
    }
    for (const name of names) {
      const full = join(dir, name)
      if (name === `${sessionId}.jsonl`) {
        return readFileSync(full, "utf8")
          .split("\n")
          .filter((l) => l.trim().startsWith("{"))
          .map((l) => JSON.parse(l) as Record<string, unknown>)
      }
      if (!name.endsWith(".jsonl")) stack.push(full)
    }
  }
  assert.fail(`no transcript found for session ${sessionId}`)
}

for (const channel of CHANNELS) {
  test(`the ${channel.kind} channel is DELIVERED, not merely emitted`, { skip: !LIVE }, () => {
    const marker = `HIVE-LOOP-PROBE-${channel.kind.toUpperCase()}`
    const base = mkdtempSync(join(tmpdir(), `hive-loop-chan-${channel.kind}-`))
    const plug = join(base, "plug")
    const work = join(base, "work")
    mkdirSync(join(plug, ".claude-plugin"), { recursive: true })
    mkdirSync(join(plug, "hooks"), { recursive: true })
    mkdirSync(work, { recursive: true })
    writeFileSync(join(work, "f.txt"), "probe target\n", "utf8")
    writeFileSync(
      join(plug, ".claude-plugin", "plugin.json"),
      JSON.stringify({
        name: `chan-${channel.kind}`,
        description: "channel delivery probe",
        version: "0.0.1",
        license: "Apache-2.0",
        author: { name: "hive-loop" },
      }),
      "utf8",
    )
    // a Stop hook that blocks unconditionally would wedge the probe; the
    // continuation carries the re-entrancy flag, exactly as the harness relies on
    writeFileSync(
      join(plug, "hooks", "probe.js"),
      `let raw="";process.stdin.on("data",c=>raw+=c);process.stdin.on("end",()=>{\n` +
        `let d={};try{d=JSON.parse(raw)}catch{}\n` +
        `if(d.stop_hook_active){process.exit(0)}\n` +
        `process.stdout.write(${JSON.stringify(channel.emit(marker))});process.exit(0)})\n`,
      "utf8",
    )
    const entry: Json = {
      type: "command",
      command: 'node "${CLAUDE_PLUGIN_ROOT}/hooks/probe.js"',
      timeout: 10,
    }
    if (shippedAsync(channel.event, channel.shippedMatcher)) entry["async"] = true
    const group: Json = { hooks: [entry] }
    if (channel.matcher !== undefined) group["matcher"] = channel.matcher
    writeFileSync(
      join(plug, "hooks", "hooks.json"),
      JSON.stringify({ hooks: { [channel.event]: [group] } }),
      "utf8",
    )

    const r = spawnSync(
      "claude",
      [
        "-p",
        "Read f.txt then reply with one short sentence.",
        "--output-format",
        "json",
        "--plugin-dir",
        plug,
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--allowedTools",
        "Read",
      ],
      { encoding: "utf8", cwd: work, timeout: 600_000 },
    )
    const result = JSON.parse(r.stdout || "{}") as Json
    const sessionId = String(result["session_id"] ?? "")
    assert.ok(sessionId !== "", `no session id returned:\n${r.stdout}\n${r.stderr}`)

    assert.ok(
      channel.honored(marker, transcriptOf(sessionId), result),
      `the ${channel.kind} channel emitted but the platform did not honor it — ` +
        `the decision would be inert in production with a green suite. ` +
        `event=${channel.event} async=${shippedAsync(channel.event, channel.shippedMatcher)}`,
    )
  })
}
