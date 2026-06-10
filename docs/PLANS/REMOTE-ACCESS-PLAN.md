# Hivemind — Secure Remote Team Access via an ngrok Tunnel

**Status:** DRAFT — awaiting human approval. **Supersedes the LAN-bind approach** of the earlier
`LAN-ACCESS-AND-ADMIN-CLI-PLAN.md` (now split in two): host-LAN binding cannot *guarantee*
cross-OS / cross-network reach (WiFi AP client-isolation, Mac/Windows host firewalls, DHCP IP churn
all defeat it, none inside the app's control). This plan instead **bakes an outbound ngrok tunnel
into the container stack** — the only class of mechanism that achieves "any teammate, any host OS,
any network." Design-reviewed via `/software-design-review` (Mode B, design-it-twice); the five
load-bearing decisions are in §7.
**Date:** 2026-06-10
**Companion plan:** **`ADMIN-CLI-PLAN.md`** — the operator-facing `hive` CLI that *drives* this stack
(`hive up --tunnel`, `hive connect`, `hive status`). This file owns the **tunnel + server hardening**;
the CLI is specified there. The two ship together but land as independent green steps.
**Builds on:** `AUTH-PLAN.md` (per-device bearer tokens, **landed**) — **no auth-core change**.
**Reference studied:** `garrytan/gbrain` — same problem at the same maturity stage hivemind is at now
(built-in HTTP + per-device bearer). Its answer was a **dumb outbound tunnel (ngrok, recommended) in
front of a self-defending server** — not LAN binding, not Tailscale. We adopt that shape and copy the
one piece of hardening it flags **load-bearing under a tunnel: a per-token rate limit** (+ a body cap).
We do **not** copy its heavier later tiers (OAuth 2.1, scopes, audit table) — those are post-MVP (§12).

**Scope (two cohesive, separable parts):**
- **Part A — Secure tunnel (the network change):** an `ngrok` sidecar in `compose.yaml`,
  **profile-gated** (off by default), reaching the daemon over the compose network. Zero change to the
  hive image (it stays hermetically offline). Gives every teammate a stable public **HTTPS** URL.
- **Part S — Minimum-viable hardening (the "secure"):** because a tunnel makes the endpoint
  internet-reachable, the server must defend itself. Add a **per-token rate limit** + a **request body
  cap** to the existing transport. Both transport-layer, stdlib, backward-compatible (default-off).

**Decisions taken into this draft (see §7):** ngrok runs as a **sidecar**, never baked into the hive
image (D1); the **server self-defends**, the tunnel is dumb and swappable (D2); a **single post-auth,
per-token** rate bucket — not a pre-auth IP bucket (D3); rate-limit + body-cap knobs resolved
**fail-fast in `entrypoint`** like `HIVE_HTTP_PORT` (D4); the tunnel **replaces** LAN exposure, the
host publish **stays loopback-only** (D5).

---

## 0. Goal & acceptance criteria

**Goal.** A teammate on *any* OS and *any* network points their agent at the team's stable ngrok HTTPS
URL with a token the admin minted on the host, and it authenticates — the internet-facing endpoint
defending itself with per-token throttling and a body cap. *(The operator drives all of this through
the `hive` CLI — see `ADMIN-CLI-PLAN.md`.)*

| AC | Criterion | Owner |
|---|---|---|
| AC1 | With the tunnel up, a client on a **different machine on a different network** authenticates over HTTPS: valid token → 200 + tool result; bad/absent/revoked token → 401. | §4 ngrok sidecar + landed auth |
| AC2 | **Default is loopback-only.** A plain `docker compose up` (no `tunnel` profile) starts **no** ngrok and publishes `127.0.0.1:8765` only. Exposure requires the explicit `--profile tunnel`. | §4 profile |
| AC3 | The hive **image is unchanged and still fully offline** (`HF_HUB_OFFLINE=1`, no egress tool baked in). ngrok lives in its **own** image; the brain image's "makes no network calls" invariant holds. | §4 sidecar (D1) |
| AC4 | A flood of requests on **one valid token** is throttled with **HTTP 429 + `Retry-After`**; a **different** token is unaffected (independent buckets). Limiting is configurable and disablable (`=0`). | §5 rate limit |
| AC5 | A request body over the cap is rejected with **HTTP 413** **before** the body is read into memory and **before** `handle()`; the daemon never crashes (INV-3 preserved). | §5 body cap |
| AC6 | **Zero new dependency in the hive runtime image** (Part S is stdlib); the **landed auth core + full existing suite stay green unchanged** — the new belts default to **off** (no limiter, generous cap). | §5, §8 |

---

## 1. Principles (locked)

1. **You cannot out-package a firewall you don't control.** Reach across arbitrary OSes/networks is a
   *network-layer* problem; no packaging change touches it. The only fix is an **outbound,
   NAT-traversing tunnel** — which is what ngrok is. (This is why the draft abandons LAN binding.)
2. **Dumb tunnel, hard server (the gbrain lesson).** Security lives in the *application*, enforced and
   tested **in-repo**, identical whether the front door is ngrok, Tailscale, or Cloudflare — so
   swapping tunnels never silently drops a protection. The tunnel only moves bytes.
3. **The offline image stays offline.** The brain image is hermetically offline by design. The tunnel
   is a process that *phones home*; isolating it in its own container keeps the brain image's "I make
   no network calls" property intact and auditable (APOSD information hiding at the image boundary).
4. **Default-safe, opt-in exposure.** Loopback is the default; the public tunnel is a non-default
   compose **profile**. Accidental internet exposure is structurally impossible without it.
5. **Minimum viable, honestly scoped.** Ship the *smallest* hardening that makes a token-gated public
   endpoint defensible (per-token throttle + body cap), and **name** what is deferred (scopes, audit,
   pre-auth bucket) rather than pretending it's covered.
6. **Fit the conventions.** Transport belts live beside the transport (`hive/app/`), depend on injected
   callables (mirroring the landed `verify`/`lock` seam), carry the standard header docstring, and are
   backward-compatible (default-off) so the existing suite is the regression proof.

---

## 2. Architecture & where it sits

```
 TEAMMATE (any OS, any network)                    OPERATOR (host: repo + Docker)
   │ agent → HTTPS                                   │ hive up --tunnel   ◀── ADMIN-CLI-PLAN.md
   ▼                                                 │ hive token alice ; hive connect
  https://<your>.ngrok.app/mcp                       ▼
   │  (TLS at ngrok edge; bearer            ┌──────────────────────────────────────────┐
   │   token never cleartext)               │ hive/tools/cli.py  (companion plan)        │
   ▼                                        └───────────────┬────────────────────────────┘
 ┌───────────────────────────┐  outbound, NAT-traversing    │ docker compose --profile tunnel up -d
 │ ngrok edge (ngrok cloud)  │◀──── encrypted tunnel ─────┐  ▼
 └───────────────────────────┘                            │  ┌─────────────────────────────────────┐
                                                          │  │ COMPOSE PROJECT  (one network)        │
                                                          └──│  ┌──────────────┐   ┌──────────────┐  │
                                                             │  │ ngrok (image)│──▶│ hive-server   │  │
                                                             │  │ http         │   │ (UNCHANGED    │  │
                                                             │  │ hive-server: │   │  image)       │  │
                                                             │  │   8765       │   │ run_http      │  │
                                                             │  └──────────────┘   │  ├ Origin→403  │  │
                                                             │   no host ports     │  ├ verify→401  │  │
                                                             │   (egress only)     │  ├ body→413 ◀──┼─ §5
                                                             │                     │  ├ rate→429 ◀──┼─ §5
                                                             │  host publish:      │  └ handle(ident)│ │
                                                             │  127.0.0.1:8765 ────┼─▶ (local agents)│ │
                                                             │  (loopback only)    └──────────────────┘ │
                                                             └──────────────────────────────────────────┘
```

- **ngrok reaches the daemon over the compose network** (`hive-server:8765`), *not* the host publish.
  The daemon already binds `0.0.0.0:8765` **inside** its namespace (entrypoint hardcodes
  `host="0.0.0.0"`), so a sibling container reaches it with **no host-side exposure change at all**.
  The host publish stays `127.0.0.1:8765` for same-host/local agents (D5).
- **Origin guard still applies.** ngrok forwards client headers; a CLI MCP client sends no `Origin`, so
  INV-4 (browser → 403) is unaffected. ngrok injects no `Origin`.
- **The per-token rate limit sidesteps the X-Forwarded-For trust hazard entirely** — keying on the
  *authenticated label* (not the source IP) means we never decide whether to trust ngrok's forwarded IP
  (the exact footgun gbrain's SECURITY.md devotes a section to). A clean consequence of D3.
- **CORS not needed** for MVP: MCP CLI clients are not browsers (and browsers are 403'd by INV-4). A
  browser-client allowlist is a deferred add (§12).

---

## 3. Part A — the ngrok tunnel sidecar (exact compose changes)

### `compose.yaml` — add one service (the only functional change)
```yaml
  # ---- OPT-IN PUBLIC TUNNEL (profile: tunnel) ----------------------------------------
  # Outbound, NAT-traversing HTTPS tunnel to the daemon — reachable from any OS/network.
  # Profile-gated: a plain `docker compose up` does NOT start this (AC2). Reaches the
  # daemon over the COMPOSE NETWORK (hive-server:8765), so NO host port is published here
  # and the daemon needs no LAN bind. ngrok lives in its own image — the hive image stays
  # offline (AC3). TLS terminates at the ngrok edge, so the bearer token is never cleartext.
  ngrok:
    image: ngrok/ngrok:3
    profiles: ["tunnel"]
    # `:-` (not `:?`) so a non-tunnel `up` still parses; `hive up --tunnel` fail-fasts on
    # these before invoking the tunnel profile (secrets-via-env at the front door — see
    # ADMIN-CLI-PLAN.md §4).
    environment:
      NGROK_AUTHTOKEN: "${NGROK_AUTHTOKEN:-}"
    # `--url` pins the account's (free) static domain so the teammate URL is STABLE across
    # restarts; ngrok v3 flag. Forwards to the daemon's in-container port over the compose net.
    command: ["http", "hive-server:8765", "--url", "${NGROK_DOMAIN:-}"]
    depends_on:
      hive-server: { condition: service_healthy }   # only tunnel a daemon that's actually warm
    restart: unless-stopped
```
- **`hive-server` is unchanged** — still `ports: ["127.0.0.1:8765:8765"]` (loopback) and `0.0.0.0`
  inside. No `HIVE_HTTP_BIND`, no `0.0.0.0` host publish, none of the prior draft's LAN machinery.
- The free **static domain** (`NGROK_DOMAIN`) gives a stable URL at no cost; without it ngrok issues a
  random per-restart URL (teammate configs churn) — so `hive up --tunnel` requires it (companion plan).

### `.env.example` (new)
```dotenv
HIVE_TENANT_ID=acme                 # required (compose fails fast if unset)
# --- public tunnel (only needed for `hive up --tunnel`) ---
# NGROK_AUTHTOKEN=2abc...            # from https://dashboard.ngrok.com (kept out of git)
# NGROK_DOMAIN=your-brain.ngrok.app  # your account's free static domain (stable URL)
```

### `tests/container/test_compose.py` (contract reframe)
- **Drop** the `HIVE_HTTP_BIND` tests (LAN approach gone); **keep** `assert ":8765:8765" in _text()`
  loopback + `restart: unless-stopped` + `run --rm` absent.
- **Add** `test_tunnel_is_profile_gated` (the `ngrok` service declares `profiles:["tunnel"]` and
  publishes **no host ports**), `test_tunnel_depends_on_healthy_daemon`,
  `test_tunnel_uses_compose_network_upstream` (`hive-server:8765` in the command, not a host address),
  and `test_hive_image_has_no_tunnel_baked_in` (ngrok image ≠ hive image; Dockerfile installs no ngrok
  — guards D1/AC3).

---

## 4. Part S — minimum-viable hardening (exact signatures)

> Both belts are **transport-layer** (not domain): they live in `hive/app/`, depend on injected values,
> and never touch `ports.py` or the pure domain — the same layering the landed token store used. Both
> default to **off/generous**, so AC6 holds (existing suite green, byte-for-byte).

### `hive/app/rate_limit.py` (new — the one genuinely new module)
A continuous-refill token bucket per key, in a **bounded** map (so attacker-varied keys can't grow
memory). Pure, deterministic (clock injected), unit-testable in isolation. ~50 stdlib lines.
```python
@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_s: int            # 0 when allowed; ceil(seconds to next token) when not

class TokenBucketLimiter:
    def __init__(self, *, limit: int, window_s: float, max_keys: int = 10_000,
                 now: Callable[[], float] = time.monotonic) -> None: ...
        # limit<=0 ⇒ DISABLED sentinel (check() always allows) — the AC4 "=0 disables" path.
    def check(self, key: str) -> RateLimitResult: ...
        # refill = floor(elapsed * limit / window_s); consume 1 if available; else deny + retry_after.
        # Bounded: on insert past max_keys, evict the least-recently-touched key (DoS-resistant).
```
Called **under the existing global lock** (the handler already holds it for `verify`/`handle`), so the
limiter needs no internal locking — one fewer moving part, no new concurrency surface.

### `hive/app/http_server.py` (additive; backward-compatible)
- `_build_handler(server, verify, lock, *, limiter=None, max_body_bytes=_DEFAULT_MAX_BODY)` —
  **keyword-only, defaulted**, so every existing `_build_handler(server, verify, lock)` call site/test
  is unchanged (AC6). `_DEFAULT_MAX_BODY = 1 << 20` (1 MiB, matching gbrain).
- **Body cap (→ 413), before reading the body into memory** — in `_read_body`/`do_POST`:
  `n = int(Content-Length or 0)`; `if n > max_body_bytes: self._json(413, {"error":"payload_too_large"}); return`.
  Reject **before** `verify`, parse, or `rfile.read(n)` — the cheapest flood (huge bodies) dies first.
- **Per-token rate limit (→ 429 + `Retry-After`), post-auth** — right after `label = verify(...)`
  resolves non-None and before `handle`:
  `if limiter: r = limiter.check(label); if not r.allowed: return self._json(429, {"error":"rate_limited"}, extra={"Retry-After": str(r.retry_after_s)})`.
  Post-auth + per-label is the **load-bearing** limiter under a shared-egress tunnel (D3); never fires
  for an absent/invalid token (that path already 401s earlier).
- `run_http(server, *, host, port, verify, lock, limiter=None, max_body_bytes=_DEFAULT_MAX_BODY)` —
  threads the two new keyword-only, defaulted knobs to `_build_handler`. Channel separation preserved:
  413/429 are **HTTP status** (transport outcomes), consistent with the module's 401/403/405/202
  doctrine; JSON-RPC errors stay inside 200s.

### `hive/tools/entrypoint.py` (additive; mirrors `_resolve_port`)
- `_resolve_rate_limit(env) -> tuple[int, float]` — `HIVE_HTTP_RATE_LIMIT` (req; default **120**,
  generous for a busy single agent; `0` disables) and `HIVE_HTTP_RATE_WINDOW_S` (default **60**).
  Malformed/negative → log + `None` → `main` maps to `EX_CONFIG` (fail-fast, never raise out of boot —
  same discipline as `_resolve_port`).
- `_resolve_max_body(env) -> Optional[int]` — `HIVE_HTTP_MAX_BODY_BYTES` (default 1 MiB; `EX_CONFIG`
  on malformed).
- `_make_http_serve(boot, port, *, run_http=None)` constructs the `TokenBucketLimiter` from the
  resolved values and passes `limiter=` + `max_body_bytes=` into `run_http`. Marker policy + boot order
  **unchanged**.

---

## 5. Tests (written first — the enforced contracts; all pytest)

| Test file | Contract |
|---|---|
| `tests/mcp/test_rate_limit.py` (new) | refill math (N allowed, N+1 denied with `retry_after>0`, refill after `window_s` re-allows); `limit<=0` ⇒ always allowed; **bounded eviction** (insert > `max_keys` drops the LRU key, map stays bounded); deterministic via injected clock. |
| `tests/mcp/test_http_server.py` (extend) | with a `limiter` injected: `limit+1` POSTs on one token → **429 + `Retry-After`**, `handle` **not** called on the 429; a **second** token still 200 (independent buckets); no limiter ⇒ never 429 (AC6). Body `Content-Length > cap` → **413**, `handle` never called, body never read; daemon still serving after (INV-3). |
| `tests/container/test_entrypoint.py` (extend) | `_resolve_rate_limit` / `_resolve_max_body`: defaults; override; `0` disables; malformed/negative → `EX_CONFIG` (no raise). `_make_http_serve` passes a `TokenBucketLimiter` + `max_body_bytes` into the injected `run_http`. |
| `tests/container/test_compose.py` (reframe) | §3: `ngrok` service `profiles:["tunnel"]`, no host port, upstream `hive-server:8765`, `depends_on` healthy; hive-server still loopback-only; hive image bakes in no ngrok (D1/AC3). Drop the `HIVE_HTTP_BIND` tests. |

**Auth/recall/admission/healthcheck suites are untouched and must stay green** — the proof Part A adds
no server code and Part S is default-off.

---

## 6. Dependencies — **none new in the hive runtime image.**
- Hive image: **unchanged** (Part S is stdlib `http.server` + `time`/`dataclasses`; AC3/AC6).
- ngrok: a **pulled image** (`ngrok/ngrok:3`), not a Python/Go dependency of the repo. Operator needs a
  free ngrok account (authtoken + one free static domain).

## 7. Design decisions (design-it-twice) — chosen vs. rejected

**D1 — ngrok packaging: sidecar container vs. baked into the hive image.**
- *Rejected — bake ngrok in* (install the binary, run it alongside the daemon via a supervisor):
  forces a process supervisor, which **breaks the PID-1 model** the boot/health system depends on —
  `_proc_starttime` (field 22) and the readiness markers assume "server is PID 1, a restart reuses PID
  1"; a second in-container process voids that. It also **bloats the image and pollutes its hermetic
  offline property** (a phone-home binary in the brain image).
- **Chosen — ngrok as a profile-gated sidecar.** Docker-idiomatic one-concern-per-container; hive image
  stays byte-identical and offline (AC3); ngrok has its own lifecycle/restart/logs; reaches the daemon
  over the compose network with **zero** host-exposure change. The literal "package the overlay into
  the packaging" move — without touching the boot state machine.

**D2 — Where security lives: ngrok edge auth vs. app self-defense.**
- *Rejected — lean on ngrok edge auth* (OAuth/SSO, IP allowlist, `--basic-auth`): near-zero hivemind
  code, but **couples the trust boundary to a third-party paid feature + out-of-repo config** that is
  untestable here and **evaporates the instant the tunnel is swapped**. Splits the security decision
  across ngrok config and the app (information leakage); contradicts Principle 2.
- **Chosen — the app self-defends; the tunnel is dumb.** The landed bearer token is the wall; Part S
  adds the throttle + cap **in-repo, enforced, unit-tested** on a real loopback server. Portable across
  any tunnel; security owned by one layer. ~65 lines buys a contract that can't silently lie.

**D3 — Rate-limit shape: one post-auth token bucket vs. gbrain's two (pre-auth IP + token).**
- *Rejected — also add a pre-auth IP bucket:* under **any** tunnel, every request shares the tunnel's
  egress IP, so a per-IP bucket collapses to one shared bucket (gbrain's own SECURITY.md flags this) —
  it would only work by **trusting `X-Forwarded-For`**, reintroducing the IP-spoofing trust hazard.
  Dead weight + a footgun for our deployment.
- **Chosen — one post-auth bucket keyed by the authenticated label.** The limiter gbrain calls
  *load-bearing under a tunnel*; throttles a compromised/runaway device without touching others; keying
  on the verified label means we **never parse or trust a forwarded IP**. Residual (an unauthed flood
  still costs one `verify()` DB read each): bounded by the body cap (kills oversized floods pre-auth),
  the global-lock throughput ceiling, and ngrok-edge abuse protection — acceptable for MVP; pre-auth
  bucket is the named add-back (§12).

**D4 — Hardening config home: env in `entrypoint` vs. a new `Config` group.**
- *Rejected — a `cfg.http.*` group:* the "right" long-term home, but pulls in an M-config registry
  entry + validation + a `test_compose_env_keys_resolve` row — ceremony beyond MVP.
- **Chosen — `_resolve_rate_limit`/`_resolve_max_body` in `entrypoint`,** mirroring `_resolve_port`
  (fail-fast → `EX_CONFIG`, never echo a value, never raise out of boot). Promote to a Config group iff
  the knob set grows (§12).

**D5 — The tunnel replaces LAN exposure (host publish stays loopback).**
Because ngrok reaches the daemon over the compose network, **no** host-side LAN bind is needed —
deleting the prior draft's entire `HIVE_HTTP_BIND` / `0.0.0.0`-publish / "never-hardcode-all-interfaces"
apparatus. Loopback publish remains for local agents. *Net deletion of complexity*, and it removes the
unfixable LAN caveats (client isolation, host firewalls). Strictly simpler **and** strictly more capable.

## 8. Implementation order (each green before the next)
1. **Part S — `rate_limit.py` + transport belts** (default-off) — the **entire existing suite stays
   green** after this chunk (proof the belts are invisible until wired). `+ entrypoint` resolution/wiring.
2. **Part A — compose `ngrok` sidecar + `.env.example` + `test_compose` reframe.**
3. **Docs** — `AUTH-PLAN §10` runbook + README → the tunnel. *(The `hive` CLI that drives `--tunnel`
   lands in `ADMIN-CLI-PLAN.md`; until then the path is `docker compose --profile tunnel up`.)*

Safe because: Part S is default-off; Part A is opt-in (profile); **no landed auth/recall code is
modified** — only added beside.

## 9. Mutation testing protocol (RULE-2; per chunk, foreground under `timeout`, clear `__pycache__`)
- **rate limit:** `check()` always-allow → 429 test red; flip `>`→`>=` in refill → off-by-one red.
- **body cap:** flip `n > max` → `n < max` → 413 test red.
- **transport wiring:** drop the post-auth `limiter.check` guard → 429 test red; drop the body-cap
  return → 413 test red.
- **compose:** remove `profiles:["tunnel"]` → `test_tunnel_is_profile_gated` red (default `up` would
  start ngrok).

## 10. Security model
- **INV-1/2/3/4 (landed) preserved:** unauth → 401 before `handle`; `proposed_by` is the verified
  label; daemon never crashes; browser `Origin` → 403.
- **New belts:** per-token **429** (runaway/compromised device throttled in isolation); **413** body
  cap (oversized flood killed pre-auth, pre-allocation).
- **Token never cleartext on an untrusted hop:** ngrok TLS-terminates at its edge and the tunnel is
  encrypted; inside the compose network it is host-private. (Fixes AUTH-PLAN §11's cleartext caveat for
  the remote path.)
- **Honest residual (stated, not hidden):** every token is full read+write (no scopes yet), and an
  unauthenticated flood still costs one `verify()` lookup each. Both are **deferred, named** add-backs
  (§12) — acceptable for a trusted small team behind a token-gated HTTPS tunnel with ngrok-edge abuse
  protection; **not** yet a hostile-traffic public service.

## 11. Files touched / removed
- **Add:** `hive/app/rate_limit.py`; `.env.example`; `tests/mcp/test_rate_limit.py`.
- **Modify (additive/backward-compatible):** `compose.yaml` (+ngrok service); `hive/app/http_server.py`
  (+413/+429, defaulted kwargs); `hive/tools/entrypoint.py` (+resolve/wire limits);
  `tests/container/test_compose.py` (§3 reframe); `tests/mcp/test_http_server.py` +
  `tests/container/test_entrypoint.py` (+tests).
- **Docs:** `AUTH-PLAN.md §10`, README. *(`hive.sh` removal + the `hive` CLI are in `ADMIN-CLI-PLAN.md`.)*

## 12. Deferred / out of scope (add-back paths)
| Deferred | Add-back |
|---|---|
| **Capability scopes** (read-only / per-source tokens) | `verify` returns a small record incl. scopes; one gate in `handle`; mirrors gbrain v0.26+. AUTH-PLAN §12 already names this. |
| **Pre-auth throttle** (unauth-flood → `verify` cost) | second bucket keyed on a *trusted* forwarded IP, or ngrok-edge rate rules. |
| **Request audit table** (gbrain `mcp_request_log`) | a `meta`/new table write per request; hivemind already has structured stderr logs. |
| **Browser-client CORS allowlist** | env allowlist echoed into `Access-Control-Allow-Origin` (today browsers 403 by INV-4). |
| **Random-URL ngrok** (no static domain) | resolve the live URL from the ngrok agent API at `:4040/api/tunnels`. |
| **Tailscale / Cloudflare front** | swap the sidecar image; **Part S is unchanged** (the dumb-tunnel dividend). |
| **OAuth 2.1 / SSO at the edge** | ngrok edge OAuth as a defense-in-depth belt on a paid plan. |

---

**This plan abandons LAN binding** (it cannot meet the cross-OS/network goal) in favor of an **outbound
ngrok tunnel baked into the compose stack as a profile-gated sidecar** — the simplest mechanism that
*actually* achieves the target — plus the **smallest app-layer hardening** that makes a token-gated
public endpoint defensible (per-token 429 + 413 cap), both **default-off and in-repo tested** so the
landed auth core and full suite stay green. The hive image stays hermetically offline; security is
owned by the app and is portable across any tunnel. **The operator CLI that drives this lives in the
companion `ADMIN-CLI-PLAN.md`.**
