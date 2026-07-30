// I4: the platform's inbound store→ledger delivery, pinned by a live probe.
//
// The fast delivered.test.ts drives the entry with the wrapping the platform was
// MEASURED to use (2026-07-29, claude 2.1.220); this file is what keeps that
// measurement honest on the platform actually installed here. One real CLI
// session with the REAL plugin and a scratch stdio MCP server named `hive`
// (zero credentials, fixture-shaped envelopes, everything under a tmpdir) makes
// a recall, a write and a capture — and the assertion reads the LEDGER, the
// journalling pipeline's terminal state on the consumer side. Never transport
// details: no exit codes, no hook stdout (the outbound channels' delivery
// predicates live in live.test.ts).
//
// Opt-in and self-skipping (HIVE_LOOP_LIVE=1), per the live tier's precedent:
// the canonical gate needs no marker registry and no deselect expression. A
// future platform version that changes the PostToolUse tool_response wrapping
// reds THIS file instead of shipping silence.
//
// Authored with the BUG-101 contract suite and frozen with it: never edited to
// make an implementation pass.

import { test } from "node:test"
import assert from "node:assert/strict"
import { spawnSync } from "node:child_process"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { fileURLToPath } from "node:url"

const HARNESS_ROOT = fileURLToPath(new URL("../", import.meta.url))
const FIXTURES = join(HARNESS_ROOT, "test", "fixtures", "envelopes.json")
const LIVE = (process.env["HIVE_LOOP_LIVE"] ?? "") !== ""

/** The episode id the scratch server serves, asserted back out of the ledger. */
const PROBE_ID = 424242

type Json = unknown

function requireFixtures(): Record<string, Record<string, Json>> {
  if (!existsSync(FIXTURES)) assert.fail("envelope fixture not generated yet")
  return JSON.parse(readFileSync(FIXTURES, "utf8")) as Record<string, Record<string, Json>>
}

function innerText(frame: Json): string {
  const text = (frame as { content?: { text?: string }[] }).content?.[0]?.text
  assert.ok(typeof text === "string", "recorded frame carries no content[].text")
  return text
}

/**
 * The three envelopes the scratch server serves, sourced from the GENERATED
 * fixtures (real server results) — the only edit is stamping the probe id onto
 * the recall's first hit so the ledger assertion has a known id to find.
 */
function probeEnvelopes(): Record<string, string> {
  const all = requireFixtures()
  const recall = JSON.parse(innerText(all["recall"]?.["confident_multi"])) as {
    reference_context?: { episode_id?: number }[]
  }
  const hits = recall.reference_context ?? []
  assert.ok(hits.length > 0, "the recall fixture serves no hits")
  ;(hits[0] as { episode_id?: number }).episode_id = PROBE_ID
  return {
    hive_recall: JSON.stringify(recall),
    hive_write: innerText(all["write"]?.["approved"]),
    hive_capture: innerText(all["capture"]?.["approved"]),
  }
}

/**
 * A scratch stdio MCP server named `hive`: newline-delimited JSON-RPC, three
 * tools, fixture-shaped results, no credentials, no state. Lives in the test
 * tree by design — the SHIPPED runtime opens no transport and spawns nothing.
 */
function serverSource(): string {
  return (
    `const ENVELOPES = ${JSON.stringify(probeEnvelopes())};\n` +
    `const TOOLS = [\n` +
    `  { name: "hive_recall", description: "retrieve memories", inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } },\n` +
    `  { name: "hive_write", description: "store a memory", inputSchema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] } },\n` +
    `  { name: "hive_capture", description: "capture a memory", inputSchema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] } },\n` +
    `];\n` +
    `function reply(id, result) { process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\\n"); }\n` +
    `function handle(msg) {\n` +
    `  if (msg.id === undefined || msg.id === null) return;\n` +
    `  if (msg.method === "initialize") {\n` +
    `    const requested = msg.params && msg.params.protocolVersion;\n` +
    `    reply(msg.id, { protocolVersion: requested || "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "hive", version: "0.0.1" } });\n` +
    `    return;\n` +
    `  }\n` +
    `  if (msg.method === "tools/list") { reply(msg.id, { tools: TOOLS }); return; }\n` +
    `  if (msg.method === "tools/call") {\n` +
    `    const text = ENVELOPES[msg.params && msg.params.name];\n` +
    `    reply(msg.id, { content: [{ type: "text", text: typeof text === "string" ? text : "{}" }], isError: false });\n` +
    `    return;\n` +
    `  }\n` +
    `  reply(msg.id, {});\n` +
    `}\n` +
    `let buf = "";\n` +
    `process.stdin.setEncoding("utf8");\n` +
    `process.stdin.on("data", (chunk) => {\n` +
    `  buf += chunk;\n` +
    `  let cut;\n` +
    `  while ((cut = buf.indexOf("\\n")) >= 0) {\n` +
    `    const line = buf.slice(0, cut); buf = buf.slice(cut + 1);\n` +
    `    if (line.trim() === "") continue;\n` +
    `    let msg; try { msg = JSON.parse(line); } catch { continue; }\n` +
    `    handle(msg);\n` +
    `  }\n` +
    `});\n` +
    `process.stdin.on("end", () => process.exit(0));\n`
  )
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

const PROBE_TASK =
  "You have three MCP tools: hive_recall, hive_write, hive_capture. Make exactly these three " +
  "calls, in this order: (1) hive_recall with query \"what wrapping does the runtime deliver\"; " +
  "(2) hive_write with text \"probe lesson one\"; (3) hive_capture with text \"probe lesson two\". " +
  "Do not read or write any files. Then reply with one short sentence."

test("I4 · a real session's recall, write and capture all journal into the ledger", { skip: !LIVE }, () => {
  const base = mkdtempSync(join(tmpdir(), "hive-loop-journal-live-"))
  const repo = join(base, "repo")
  const stateDir = join(base, "state")
  mkdirSync(repo, { recursive: true })
  const server = join(base, "server.js")
  writeFileSync(server, serverSource(), "utf8")
  const mcpConfig = join(base, "mcp.json")
  writeFileSync(
    mcpConfig,
    JSON.stringify({
      mcpServers: { hive: { type: "stdio", command: process.execPath, args: [server] } },
    }),
    "utf8",
  )

  const r = spawnSync(
    "claude",
    [
      "-p",
      PROBE_TASK,
      "--setting-sources",
      "",
      "--strict-mcp-config",
      "--mcp-config",
      mcpConfig,
      "--plugin-dir",
      HARNESS_ROOT,
      "--allowedTools",
      "mcp__hive__hive_recall,mcp__hive__hive_write,mcp__hive__hive_capture",
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
    `no ledger was written — the inbound pipeline journaled nothing.\n` +
      `stdout: ${r.stdout}\nstderr: ${r.stderr}`,
  )
  const journaled = found.find(
    (l) =>
      Number(l["recalls"]) >= 1 &&
      Number(l["writes"]) >= 1 &&
      Number(l["captures"]) >= 1 &&
      Array.isArray(l["served"]) &&
      (l["served"] as unknown[]).includes(PROBE_ID),
  )
  assert.ok(
    journaled !== undefined,
    `no ledger journals the whole probe (recalls≥1, writes≥1, captures≥1, served ∋ ${PROBE_ID}).\n` +
      `ledgers: ${JSON.stringify(found)}\nstdout: ${r.stdout}\nstderr: ${r.stderr}`,
  )
})
