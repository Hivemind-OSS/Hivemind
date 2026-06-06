# Hivemind — Spike: MCP-over-HTTP live-client verification

**Status:** EXECUTED — **PASS (2026-06-06)**. A real Claude Code client (v2.1.167) round-tripped
the §4 transport; both §1 criteria met. Result + evidence recorded in `AUTH-PLAN.md` §13;
harness deleted. No §4 fallback branch was needed.
**Date:** 2026-06-06
**Resolves:** the single open gate in `AUTH-PLAN.md` §13 — does a **real Claude Code client**
round-trip against the minimal `POST → application/json` (no SSE) server defined in
`AUTH-PLAN.md` §4?
**Durable output:** a recorded PASS/FAIL in `AUTH-PLAN.md` §13 and, on fail, *which* bounded
fallback. All harness artifacts are deleted on completion.

---

## 0. The question (and why a spike, not a guess)

The MCP Streamable-HTTP spec **explicitly allows** a server to answer a POST request with a
single `application/json` JSON-RPC response (no SSE) and makes `Mcp-Session-Id` optional —
confirmed in `AUTH-PLAN.md` §13. But **spec-compliance ≠ client-leniency**: the only thing
that proves Claude Code's HTTP client accepts this is observing it do so. This spike observes
it, at minimum cost, before any production code is written.

## 1. Pass/fail criterion (fixed before running)

**PASS** iff, against the §2 harness:
1. With the **valid** token, a real Claude Code completes the full lifecycle —
   `initialize` → `notifications/initialized` (**202**) → `tools/list` → `tools/call spike_echo`
   — and surfaces the tool's result; **and**
2. With a **wrong** token, the client surfaces a clean auth failure (server logged **401**)
   with **no hang**.

Anything else is **FAIL** → §4 decision tree. Nothing about the real hive stack is exercised;
this tests the **transport + auth shapes only**.

## 2. The minimal harness (the entire spike)

One throwaway stdlib file, **outside `hive/`** (e.g. `/tmp/hive-spike/spike_mcp_http.py`,
~60 lines), implementing **exactly** the `AUTH-PLAN.md` §4 contract with **canned** responses
(no embedder, no store, no token DB):

- **POST, valid `Authorization: Bearer <TESTTOKEN>`, body has `id` (request):** →
  `200 application/json` with a canned JSON-RPC result:
  - `initialize` → `{protocolVersion, capabilities:{tools:{listChanged:false}}, serverInfo:{name:"hivespike",version:"0"}}`
  - `tools/list` → one tool `spike_echo` (`{text:string}` required)
  - `tools/call` `spike_echo` → `{content:[{type:"text",text:"echo: "+text}], isError:false}`
  - `ping` → `{}`
- **POST, body has no `id` (notification, e.g. `notifications/initialized`):** → **202**, no body.
- **GET / DELETE:** → **405**.
- **Any request with an `Origin` header:** → **403**.
- **Missing/!= TESTTOKEN bearer:** → **401** `{"error":"unauthorized"}`.
- Logs `(method, http_status)` to stderr/file so the handshake sequence is observable.
- Hardcoded `TESTTOKEN`; `ThreadingHTTPServer` bound `127.0.0.1:8765`.

This is the §4 contract verbatim — so a PASS validates the real design, and a FAIL points at
the exact clause to fix.

## 3. Run procedure (~5 commands)

1. Start the harness in the background; tee its log.
2. `claude mcp add --transport http hivespike http://127.0.0.1:8765/mcp --header "Authorization: Bearer <TESTTOKEN>"`
3. **Probe (headless preferred):** `claude -p "Call the hivespike spike_echo tool with text=ping and report exactly what it returned."`
   — *fallback if nested CLI is not viable here:* run the same ask in a normal Claude Code session.
4. **Assert PASS-1:** the server log shows `initialize` → `notifications/initialized`(202) →
   `tools/list` → `tools/call`, and the client reported `echo: ping`.
5. **Negative (PASS-2):** repeat step 3 after re-adding with a wrong token (or rotating
   `TESTTOKEN`); confirm the server logged **401** and the client reported a clean auth error
   (no hang/timeout loop).
6. **Cleanup:** `claude mcp remove hivespike`; delete `/tmp/hive-spike`; stop the server.

## 4. Outcomes → decision (fallbacks are evidence-chosen, never pre-built)

- **PASS** → the §4 transport is client-valid. Flip `AUTH-PLAN.md` → **READY**; proceed to
  chunk 1 (the `mcp_server`/`tool_defs` identity seam).
- **FAIL — client rejects `application/json` / demands `text/event-stream`** → minimal fix:
  emit the single JSON-RPC response as **one SSE event** (still stdlib `http.server`, ~10
  lines) and re-run. Only if *that* also fails → escalate: adopt the `mcp` SDK's
  Streamable-HTTP server (**breaks zero-dependency — your call**).
- **FAIL — client requires `Mcp-Session-Id`** → issue a UUID on `initialize`, accept it on
  later requests (~8 lines); re-run.
- **FAIL — auth (header not sent / 401 causes a retry-hang)** → adjust per the observed
  behavior (header scope, or return a JSON-RPC error body alongside the status); re-run.

Each branch is bounded and selected by what the spike *shows*, so we never speculatively build
SSE or sessions.

## 5. Scope, effort, ownership

- **Scope:** Claude Code only (the team's primary harness and the config flow already
  verified). Other IDEs are MCP-spec clients served by the same endpoint; spot-check post-PASS.
- **Effort:** ~60-line throwaway + ~5 commands; minutes.
- **Owner:** I attempt it headlessly via `claude -p`; if a nested `claude` invocation isn't
  viable in this environment, you run §3's commands in a Claude Code session and paste the
  server log. Either way the result is recorded into `AUTH-PLAN.md` §13.

---

**This spike writes no code under `hive/` and changes no production file.** Its only purpose is
to turn the last assumption in `AUTH-PLAN.md` into an observed fact.
