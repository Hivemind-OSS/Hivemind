# Hivemind — Warm HTTP Daemon + Per-Device Token Auth

**Status:** READY — chunks 1–6 (§7) are landed on branch `auth-http-daemon`, TDD-first and
RULE-2 mutation-verified, full ex-embedder suite green. The design was **verified against the
codebase + the MCP Streamable-HTTP spec** (see §13). The **last gate — the live-client
transport spike** (`AUTH-SPIKE-MCP-HTTP-PLAN.md`) — **PASSED on 2026-06-06**: a real Claude
Code client round-tripped the §4 transport (`initialize`→`initialized` 202→`tools/list`→
`tools/call`, result surfaced) and cleanly rejected a bad token (401, no hang), confirming
client-leniency by observation rather than assumption.
**Date:** 2026-06-06
**Scope:** Add an always-on **warm HTTP daemon** (PID 1 serves HTTP instead of stdio) and
**per-device bearer-token authentication**, so a small team's fleets of agents can
read+write the shared store from their own machines. **Zero new dependencies** (stdlib
`http.server`).
**Reviewed via:** `/software-design-review` (Mode B, design-it-twice). The load-bearing
change vs. the first stripped draft: **per-request identity is threaded through
`handle()`** rather than the transport mutating `proposed_by` — this removes a transport↔
protocol information leak. See §9.
**Decisions taken into this draft:** (a) capabilities/read-only tiers **deferred** (add-back
path in §12); (b) the client-supplied `proposed_by` write-arg is **removed** (spoofing
defined out of existence).

---

## 0. Goal & acceptance criteria

**Goal.** One warm process authenticates every connecting device by a per-device token,
makes that device's label the verified `proposed_by`, and lets an admin mint/revoke tokens
individually — over HTTP, with no new dependencies, fitting the existing hexagonal
architecture.

| AC | Criterion | Owner |
|---|---|---|
| AC1 | A device with a valid token can call all 5 `hive_*` tools over HTTP; its writes are attributed to the token's label and its recalls log under it. | §4 `run_http` + §5 identity seam |
| AC2 | A request with a missing / unknown / revoked token returns **HTTP 401** and **never reaches `handle()`** (no recall/write path touched). | §4 `run_http` (INV-1) |
| AC3 | A client **cannot** assert `proposed_by` — the field is gone from the write schema; `proposed_by` is always the authenticated label. | §5 `tool_defs` + `_handle_write` (INV-2) |
| AC4 | An admin mints and revokes per-device tokens via a CLI; revoke takes effect on the device's next request. | §4 `authctl` |
| AC5 | **Zero new dependencies**; the existing stdio path and the full current test suite stay green unchanged (identity defaults to the process identity). | §5 (`identity=None` default) |
| AC6 | One warm process serves the whole team — boot-once-warm reused verbatim; shared SQLite connection accessed safely under threads. | §5 entrypoint + global lock |

---

## 1. Principles (locked)

1. **The daemon is the natural generalization of `ServerIdentity`** — stdio is "one
   process, one identity"; the HTTP daemon is "one process, **per-request** identity." The
   identity→attribution mapping stays in the **one** module that already owns it
   (`HiveMCPServer._handle_write`); the transport only resolves *who is calling*.
2. **Authentication, not authorization.** This plan does identity + revocation. Capability
   tiers (read-only/CI) are a *separate* abstraction, deferred (§12) behind the same
   `verify` seam.
3. **Define spoofing out of existence.** `proposed_by` is removed from the write input
   schema; a client has no field through which to assert an identity (APOSD "define errors
   out of existence").
4. **The token is the app identity; the network door is the operator's.** The daemon is
   plain HTTP bound to host-loopback. TLS/reachability is provided by an SSH tunnel or a
   reverse proxy (§11). The bearer token gives identity + per-device revocation regardless.
5. **Tokens are hashed at rest.** Only `sha256(token)` is stored; the 256-bit plaintext is
   shown once at mint. A DB leak yields no usable token.
6. **Fit the conventions.** Auth store is an adapter on the shared WAL conn (sibling of
   `SqliteUtilityStore`); the admin CLI lives in `hive/tools/` (sibling of `healthcheck`,
   `bake_model`); every new module carries the codebase-standard header docstring.

---

## 2. Architecture & where it sits (hexagonal)

```
 client (Claude Code, …)  ──HTTP POST JSON-RPC + Bearer──▶  hive/app/http_server.run_http
                                                              │  verify(token) ⇒ label  (else 401)
                                                              │  identity = ServerIdentity(tenant, label)
                                                              ▼
                                            hive/app/mcp_server.HiveMCPServer.handle(req, identity=…)
                                                              │  (unchanged dispatch + belts)
                                                              ▼
                                       admission / recall / store  (pure domain + adapters, UNCHANGED)

 hive/adapters/auth_store_sqlite.SqliteTokenStore   ── shares the one WAL conn ──▶  access_tokens table
 hive/tools/authctl.main()  (admin CLI: create / revoke)
 hive/tools/entrypoint.main()  ── serve seam ──▶  run_http(…)   (PID 1 = the warm daemon)
```

- **No domain port** for the token store: `ports.py` is "the ports the *pure domain*
  depends on," and the pure domain (recall/admission) never touches tokens. The token
  store is an app-layer adapter used only by the transport + CLI. The transport depends on
  a **`verify` callable**, not the concrete SQLite class — a narrow seam with no SQLite
  leak into the transport.
- **Marker policy untouched.** `entrypoint` keeps its boot order and readiness markers
  (`boot:serve_pid` / `boot:serve_starttime` / `boot:embedder_loaded`); only the final
  `serve` step changes from stdio to HTTP. The container `HEALTHCHECK`
  (`python -m hive.tools.healthcheck`) reads markers from the DB and is transport-agnostic.

---

## 3. Data model — `access_tokens` (new table, created by its adapter)

```sql
CREATE TABLE IF NOT EXISTS access_tokens(
  label      TEXT PRIMARY KEY,            -- device identity → proposed_by (e.g. "alice-laptop")
  token_hash TEXT NOT NULL UNIQUE);       -- sha256(plaintext) hex; the UNIQUE constraint indexes the lookup
```

`verify` is `SELECT label WHERE token_hash=?`; `revoke` is `DELETE WHERE label=?`
(a revoked token simply stops verifying — no soft-delete state to reason about).

---

## 4. New files (exact signatures)

### `hive/adapters/auth_store_sqlite.py`
Adapter + the two pure token helpers (cohesive: the whole token lifecycle in one place;
mirrors `utility_store_sqlite.py` — owns its table via `executescript(_SCHEMA)` on the
shared conn).
```python
TOKEN_PREFIX = "hive_"

def new_token() -> str:            # TOKEN_PREFIX + secrets.token_hex(32)   (256-bit)
def token_hash(plaintext: str) -> str:     # hashlib.sha256(plaintext.encode()).hexdigest()

class SqliteTokenStore:
    def __init__(self, conn: sqlite3.Connection) -> None: ...        # executescript(_SCHEMA)
    def create(self, label: str) -> str: ...
        # generate plaintext, INSERT(label, token_hash); returns plaintext ONCE; raises on duplicate label
    def verify(self, plaintext: str) -> Optional[str]: ...           # SELECT label WHERE token_hash=?  (None ⇒ reject)
    def revoke(self, label: str) -> bool: ...                        # DELETE WHERE label=?; True if a row was removed
```

### `hive/app/http_server.py`
The transport + auth glue (~70 lines). Mirrors `run_stdio`'s parse/shape guards.
```python
def run_http(server: HiveMCPServer, *, host: str, port: int,
             verify: Callable[[str], Optional[str]], lock: threading.Lock) -> None: ...
```
Endpoint contract (single path, e.g. `/mcp`; obligations confirmed against the MCP
Streamable-HTTP spec, §13):
- **GET / DELETE → HTTP 405** — the spec requires the endpoint to accept GET; a server that
  offers no SSE stream replies `405 Method Not Allowed` (we do server→client messaging on
  none). DELETE (session teardown) → 405 likewise (no sessions).
- **POST handler:**
  1. **Origin guard (spec MUST):** if an `Origin` header is present → **HTTP 403**. No
     browser is a legitimate client; CLI MCP clients send none. This is the required
     DNS-rebinding protection. *(INV-4)*
  2. `tok = bearer(headers)`; `label = verify(tok) if tok else None`; **if `None` → HTTP 401**
     (body `{"error":"unauthorized"}`); `server.handle` is never called. *(INV-1)*
  3. Parse the JSON-RPC body with the same guards as `run_stdio` (`-32700` parse error,
     `-32600` non-object, non-dict `params` → `{}`).
  4. **Notification / response (no `id`) → HTTP 202 Accepted, no body** (spec MUST). This is
     how the lifecycle `initialized` notification is acked; `handle` is not called for it.
  5. **Request (has `id`):** `ident = ServerIdentity(tenant_id=server.identity.tenant_id,
     agent_id=label)`; `with lock: resp = server.handle(req, identity=ident)`;
     → **HTTP 200 `application/json`**, `resp.to_json()`.
- **`MCP-Protocol-Version` header:** accepted and ignored (lenient — we do not branch on
  version, so we never 400 a supported client).
- **Robustness (INV-3):** a malformed request or any handler exception yields an error
  response (JSON-RPC error in a 200, or HTTP 500 for a transport-level fault) and **never
  crashes the daemon** — the HTTP analog of the stdio "loop never crashes on a bad line."

> **The one global lock:** the shared `sqlite3.Connection` and the embedder are not
> thread-safe, so all handler execution (incl. the `verify` DB read) is serialized; WAL
> read-concurrency is intentionally traded for simplicity (escape valve §12).
> **Channel separation:** auth/transport failures are HTTP status codes (401/403/405/202);
> protocol/handler errors are JSON-RPC errors inside a 200. The two never mix.

### `hive/tools/authctl.py`
Admin CLI (injection seams like the other `main()`s; sibling of `healthcheck`/`bake_model`).
```python
def main(argv=None, *, env=None, connect_fn=None, out=None) -> int: ...
#   create LABEL   -> mints + prints the token to stdout ONCE (metadata to stderr)
#   revoke LABEL   -> deletes the token
#   db_path resolved from --db / $HIVE_STORE__DB_PATH (fail-fast if absent, mirroring entrypoint)
```

### `docs/PLANS/AUTH-PLAN.md`
This document (the instruction layer for the change).

---

## 5. Modified files (additive; all read during planning)

### `hive/app/mcp_server.py` — the per-request identity seam (the §9 winner)
- `handle(self, req: MCPRequest, *, identity: Optional[ServerIdentity] = None) -> MCPResponse`
  → `ident = identity or self.identity`; pass `ident` to `_tools_call`.
- Handlers receive `ident` (uniform `Callable[[dict, ServerIdentity], dict]`):
  - `_handle_write(args, identity)` → `proposed_by = identity.agent_id` (no longer reads `args["proposed_by"]`).
  - `_handle_recall(args, identity)` → `recall(query, agent_id=identity.agent_id, …)` (per-caller, was the process default).
  - the other three accept `identity` and currently ignore it (request context, available for future use).
- **Backward compatible:** `identity=None` ⇒ `self.identity`, so the stdio entrypoint and every existing test behave identically.

### `hive/app/tool_defs.py`
Remove the `proposed_by` property from the `hive_write` `inputSchema`. *(INV-2 — no field to spoof.)*

### `hive/adapters/sqlite_db.py`
`connect(path: str = ":memory:", *, check_same_thread: bool = True)` — pass `check_same_thread`
through to `sqlite3.connect`. The daemon passes `False` so the threaded server can share the
one connection (used only under the §4 lock). Harmless to all existing single-threaded callers.

### `hive/app/container.py`
- Construct `token_store = SqliteTokenStore(conn)` (independent of the store-order constraint).
- Add `token_store` to `Container.__init__` and as an attribute.

### `hive/tools/entrypoint.py`
- Resolve the port in `main()`: `port = int(env.get("HIVE_HTTP_PORT") or 8765)` (no change to
  `_resolve_env`'s arity — keeps its tests untouched).
- After `boot` is built, default the serve step to HTTP:
  `serve = serve or _make_http_serve(boot, port)`, where
  `_make_http_serve(boot, port, *, run_http=run_http)` returns
  `lambda s: run_http(s, host="0.0.0.0", port=port, verify=boot.token_store.verify, lock=threading.Lock())`.
  The injectable `run_http` param exists **only** so the default-serve path is unit-testable
  (every existing entrypoint test injects `serve`, so this path is otherwise uncovered — see §6).
- Boot order + readiness markers **unchanged**; tests still inject `serve`.
- Add `token_store: Any` to the `Boot` Protocol **and** a `token_store` stub (with `.verify`)
  to the entrypoint test fake `_RecordingBoot`, and extend the `hasattr`-conformance list in
  `test_build_container_is_boot_conformant` to include `"token_store"` (per the
  protocol-conformance discipline — a widened Protocol with only the fake updated is the known
  trap; prod `Boot` is `Container`, which gets it in §5 container).

### `compose.yaml`
- `ports: ["127.0.0.1:8765:8765"]` — **host-loopback only**. (The daemon binds `0.0.0.0`
  *inside* the container; the container network namespace is the isolation boundary and the
  host mapping is loopback-only — comment this in the file so it doesn't read as "exposed.")
- `stdin_open: false` (no longer needed once PID 1 serves HTTP).

`pyproject.toml` — **unchanged** (stdlib only).

---

## 6. Tests (written first — the enforced contract)

| Test file | Contract |
|---|---|
| `tests/store/test_auth_store.py` | create→verify returns the label; the stored row contains **only the hash** (plaintext absent); revoke→verify returns `None`; unknown token→`None`; duplicate label rejected. Conn via prod `connect(":memory:")`. |
| `tests/mcp/test_http_server.py` | real loopback `ThreadingHTTPServer` on `127.0.0.1:0` (the harness already runs live-socket tests — §13): valid token + JSON-RPC request→**200 `application/json`** with the result; missing/garbage token→**401, `server.handle` never called** (spy); the `identity.agent_id` reaching `handle` equals the token label; **notification (no `id`)→202 no body**; **GET→405**; **request carrying an `Origin` header→403**; a handler that raises→error response, server still serving (INV-3). |
| `tests/mcp/test_identity_threading.py` | `handle(req, identity=X)` makes `hive_write` persist `proposed_by == X.agent_id`; `handle(req)` (no identity) falls back to `self.identity` — proving the existing suite stays valid. |
| `tests/container/test_authctl.py` | `create` prints a token whose hash lands in the table; `revoke` makes a previously valid token verify `None`. (Tools are tested under `tests/container/`, per convention.) |
| `tests/container/test_entrypoint.py` (extend) | new: `_make_http_serve(boot, port, run_http=fake)` calls `run_http` with `port` and `verify=boot.token_store.verify` — the default-serve path the existing injected-`serve` tests never exercise. |

A `token_store` wiring assertion (`build_container(...).token_store.create/verify` works
end-to-end) folds into the container tests.

---

## 7. Implementation order (each green before the next; dependencies point backward)

1. **`mcp_server` + `tool_defs` identity seam** — backward-compatible; the **entire existing
   suite must stay green** after this chunk (the proof that `identity=None` preserves behavior).
2. **`auth_store_sqlite.py`** (+ test).
3. **`http_server.py`** (+ test).
4. **Wiring**: `sqlite_db`, `container`, `entrypoint` (+ Boot fake update; container wiring assert).
5. **`authctl.py`** (+ test).
6. **`compose.yaml`** + this doc.

---

## 8. Mutation testing protocol (RULE-2; per chunk, before "done")

For each chunk: introduce one deliberate fault, confirm the matching test goes **red**,
restore, confirm **green**, and report which fault / which test. Run mutations in the
**foreground under `timeout`**; **clear `__pycache__`** after any same-byte-size restore.

- `verify`: drop the `WHERE token_hash=?` predicate (return first row) → the 401 / unknown-token tests must fail.
- `_handle_write`: use `self.identity` instead of the passed `identity` → the identity-threading + http attribution tests must fail.
- `run_http`: skip the `verify`-is-None → 401 guard → the missing-token test must fail.

---

## 9. Design decisions (design-it-twice) — chosen vs. rejected

**D1 — Where per-request identity is applied (the load-bearing call).**
- *Rejected (first draft):* the HTTP transport overwrites `arguments["proposed_by"]` and
  branches on `name == "hive_write"`. Smell: **information leakage / special-general
  mixture** — the transport encodes protocol semantics; the "identity → proposed_by" truth
  is **scattered** across two modules; a second write-tool or an arg rename silently breaks
  attribution; `_handle_recall` keeps logging the wrong (process-default) caller.
- **Chosen:** thread `identity` through `handle()` to the handlers; remove `proposed_by`
  from the schema. The transport stays ignorant of tool internals; attribution lives in the
  one module that owns it; recall becomes per-caller-correct; spoofing is impossible by
  construction. Cost: ~6 lines in `mcp_server`/`tool_defs`, all backward-compatible.

**D2 — Token-store layering.** Chosen: an app-layer adapter, **no domain port**, transport
depends on a `verify` *callable*. Rationale: `ports.py` is the pure-domain's ports; auth is
not a domain dependency. Rejected: a `runtime_checkable` `TokenStore` port + fake +
conformance test — unjustified ceremony for a non-domain seam.

**D3 — Concurrency.** Chosen: `ThreadingHTTPServer` + one global lock + `check_same_thread=
False`. Rejected: single-threaded `HTTPServer` (one hung client stalls the whole fleet —
an availability unknown-unknown). Rejected: ASGI/uvicorn (new dependency). The global lock
serializes at the embed rate, which is adequate at team scale; the read-concurrency escape
valve is §12.

**D4 — Revoke = row `DELETE`** (not soft-delete). Simplest; a deleted token just stops
verifying. Audit trail is a §12 add-back.

**D5 — CLI location** = `hive/tools/authctl.py` (operational tools live there:
`healthcheck`, `bake_model`, `entrypoint`). Rejected: `hive/app/` (misfit).

---

## 10. User flow

**Admin — once per teammate-device** (run where the DB lives):
```
docker compose exec hive-server python -m hive.tools.authctl create alice-laptop
→ hive_9f3c…            # shown ONCE; hand over via a secret manager
```

**Teammate — once per device** (token stays in an env var, never written to a file —
Claude Code expands `${HIVE_TOKEN}` in the header and refuses to start if it is unset):
```
export HIVE_TOKEN=hive_9f3c…
claude mcp add --transport http hive http://localhost:8765/mcp \
  --header "Authorization: Bearer ${HIVE_TOKEN}"
```
Every agent on that device now authenticates as `alice-laptop`; its writes are attributed
to it and its recalls log under it — no per-agent setup. Other IDEs: paste the equivalent
`{"type":"http","url":…,"headers":{"Authorization":"Bearer ${HIVE_TOKEN}"}}` snippet.

**Offboard:**
```
docker compose exec hive-server python -m hive.tools.authctl revoke alice-laptop
→ that device's next request returns 401
```

**Remote teammate — any OS, any network** (`REMOTE-ACCESS-PLAN.md`, **landed**): the
operator starts the opt-in tunnel (needs `NGROK_AUTHTOKEN` + `NGROK_DOMAIN`, see
`.env.example`; a plain `up` never exposes anything):
```
docker compose --profile tunnel up -d
```
The teammate runs the same one-liner against the stable HTTPS URL instead of localhost:
```
export HIVE_TOKEN=hive_9f3c…
claude mcp add --transport http hive https://your-brain.ngrok.app/mcp \
  --header "Authorization: Bearer ${HIVE_TOKEN}"
```
TLS terminates at the ngrok edge (the token is never cleartext on an untrusted hop), and
the endpoint self-defends: per-token throttle → 429 + `Retry-After` (default 120 req /
60 s; `HIVE_HTTP_RATE_LIMIT=0` disables; `HIVE_HTTP_RATE_WINDOW_S` sets the window) and a
request body cap → 413 (default 1 MiB; `HIVE_HTTP_MAX_BODY_BYTES`). Same tokens, same
revoke flow — the tunnel is only the network door.

---

## 11. Security model & the one requirement

- **INV-1:** an absent/unknown/revoked token never reaches `handle()` (HTTP 401 at the transport).
- **INV-2:** `proposed_by` is always the authenticated label (no client field to assert it).
- **INV-3 (robustness):** a malformed request or a handler exception returns an error and
  never crashes the daemon (HTTP analog of the stdio "loop never crashes").
- **INV-4 (DNS-rebinding, spec MUST):** a request carrying an `Origin` header is rejected
  (403) — a browser-originated request is never a legitimate client; CLI MCP clients send no
  `Origin`. (Tokens already block exfil; this is the spec-mandated belt.)
- **At rest:** only `sha256(token)` stored; 256-bit plaintext shown once.
- **Bind vs. the spec SHOULD-localhost:** the daemon binds `0.0.0.0` *inside the container*
  (required for the Docker `ports:` map to reach it), but the host mapping is
  `127.0.0.1:8765` only, so it is never exposed beyond host-loopback — the spec's intent.
  INV-4 + the token are the rebinding guards.
- **Transport encryption is required for remote use** (the token is a cleartext credential).
  **Landed path: the profile-gated ngrok sidecar** (`REMOTE-ACCESS-PLAN.md`) — TLS at the
  ngrok edge, and the app self-defends with a per-token 429 throttle + 413 body cap, both
  in-repo tested. Zero-dependency alternative: an **SSH tunnel**
  (`ssh -NL 8765:localhost:8765 user@host`); or a **Caddy/HTTPS** front. The tunnel/proxy
  is the network door, the token is the identity.

---

## 12. Deferred / out of scope (with add-back paths)

| Deferred | Add-back cost |
|---|---|
| **Capabilities (read-only/CI tokens)** — *authorization* is a separate abstraction | 1 column (`capabilities`); `verify` returns a small `TokenRecord`; one gate in `handle`/`run_http`. The `verify` seam absorbs it without changing `run_http`'s call site if the record is introduced now — chosen NOT to, per minimal directive. |
| **Token audit** (`created_ts`, `last_used_ts`, soft-revoke) | columns + a `touch` on verify + revoke→update instead of delete. |
| **Token rotation/expiry** | `expires_ts` column + one clause in `verify`. |
| **Read-concurrency past the embed-serialized ceiling** | per-thread read connections, or swap the store adapter to Postgres (the named multi-writer seam). |
| **HTTP liveness in the container healthcheck** | add a port probe to `hive.tools.healthcheck` (markers remain the embedder-resident gate). |
| **Tailscale/SSO identity** | replace the `verify` callable with a header-identity resolver; the rest is unchanged. |

---

## 13. Verification status (DRAFT → READY gate)

Closed during planning (RULE-1: assumptions → confirmed facts), with evidence:

| # | Check | Result |
|---|---|---|
| Backward-compat of the identity seam | grep every `.handle(`, `_handle_*`, `_tool_handlers`, `proposed_by` site | **Confirmed safe.** All `.handle(` callers (run_stdio + every test) pass a positional `req` only → keyword-only `identity=None` is invisible to them. No test calls a `_handle_*` method directly (only the dispatch dict in `mcp_server` does), so the `(args, identity)` signature change is internal. Every `proposed_by` in tests is a `admission.write`/`store.stage` (domain/store) call — **no test asserts a client-supplied `proposed_by` via a `hive_write` tool call**, so removing it from the schema breaks nothing. |
| Files the plan edits but were unread | read `tests/container/test_entrypoint.py`, `tests/container/test_build_container.py` | **Confirmed.** The Boot fake is `_RecordingBoot`; **all** entrypoint tests inject `serve` (so the HTTP default path is otherwise uncovered → §6 adds a test). Boot conformance is a `hasattr` list, **not** `isinstance(c, Boot)` → widening the Protocol is safe; extend the list with `"token_store"`. |
| `connect()` ripple | grep all callers | **Safe.** Every caller of the prod `connect()` passes a single positional path → adding keyword-only `check_same_thread=True` is backward-compatible. |
| Test harness tolerates a real loopback HTTP server | grep tests for sockets | **Yes.** `tests/container/test_container_live.py` already drives real Docker/sockets; an in-process `127.0.0.1:0` server is well within tolerance. |
| Embedder under the global lock from worker threads | reasoned | **OK** — the lock serializes to one caller at a time; the model is loaded once at boot and only invoked under the lock (not benchmarked; throughput ceiling is the embed rate, per §12). |
| **MCP Streamable-HTTP server obligations** | read the official spec | **Approach confirmed + 3 obligations surfaced.** POST→single `application/json` response is explicitly allowed (no SSE); `Mcp-Session-Id` is `MAY` (optional). Added to §4/§11: **GET→405**, **notification→202**, **`Origin` MUST-validate**. |
| **Live-client transport (the READY gate)** | real Claude Code `claude -p` vs. a §4-exact throwaway stdlib harness | **PASS (2026-06-06).** Full lifecycle observed (`initialize`→200, `initialized`→**202**, `tools/list`→200, `tools/call`→200), result `echo: ping` surfaced; wrong token → `401`, clean client error, **no hang**. The client `GET`s for the optional SSE stream, accepts **405**, and proceeds on plain `application/json` — no SSE, no `Mcp-Session-Id` needed. See the gate-closed note below. |

**Gate CLOSED — live-client spike PASSED (2026-06-06).** A real Claude Code client (v2.1.167,
`claude -p` + `--mcp-config --strict-mcp-config`, model `haiku`) round-tripped a throwaway
stdlib harness (`/tmp/hive-spike/spike_mcp_http.py`) implementing the §4 contract verbatim with
canned responses. Evidence:
- **Pre-flight (deterministic):** an 11/11 self-test of the §4 shapes
  (200 `application/json` / 202 / 401 / 403 / 405); a RULE-2 mutation (auth guard → no-op)
  flipped *only* the two 401 checks red, proving the negative path has teeth.
- **PASS-1 (valid token):** server handshake log =
  `initialize→200` · `notifications/initialized→202` · `GET→405` · `tools/list→200` ·
  `tools/call→200`; the client surfaced the tool result `echo: ping` verbatim. The `GET→405`
  line proves the client *probes* for the optional server→client SSE stream and **proceeds on
  plain `application/json` when refused** — neither SSE nor `Mcp-Session-Id` is required.
- **PASS-2 (wrong token):** a single `POST (auth) → 401` (no retry loop); the client reported
  a clean "tool not available" failure and exited 0 in ~6 s — **no hang/timeout loop**.

Spec-compliance is now confirmed by observed client-leniency. **None** of §4's fallback
branches (SSE-wrap, `Mcp-Session-Id`, auth-retry) is needed. The harness was deleted on
completion (the spike changed no production file). **This doc is READY.**

---

**Implementation note (landed):** chunks 1–6 (§7) are complete on branch `auth-http-daemon`,
each test-first + RULE-2 mutation-verified + committed. Three reconciliations vs. this draft,
surfaced and approved before coding: (a) `compose.yaml` drops `stdin_open`/`tty` entirely (an
HTTP service does not attach to stdin) — `test_compose.py` updated to assert the host-loopback
port map instead; (b) `HIVE_HTTP_PORT` is resolved fail-fast (malformed/out-of-range →
EX_CONFIG) so the boot path never raises; (c) the §4 endpoint contract is factored into
`http_server._build_handler` so it is unit-testable on a real loopback `127.0.0.1:0` server
(`run_http` stays the thin blocking bind+serve wrapper). `ServerIdentity` is imported from
`hive.app.mcp_server` (its actual home).

**Next action:** the §13 spike is **GREEN** (2026-06-06) and this doc is **READY**. Chunks 1–6
are merged on `master` (the "branch `auth-http-daemon`" notes above are historical), so the
warm HTTP + per-device-auth daemon is ready to deploy. Remaining follow-up: spot-check one
non-Claude-Code MCP client (Cursor / Codex / etc.) against the live daemon per the §5 scope
note — they are MCP-spec clients served by the same endpoint, but client-leniency was only
*observed* for Claude Code.
