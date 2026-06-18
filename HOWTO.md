# HOWTO — operating Hivemind

The operator's path from clone to a fleet that remembers, for **solo devs and small
teams**. Everything here runs through the `hive` CLI (`pip install -e .` gives the
command; uninstalled, `python -m hive.tools.cli` is identical).

## 1. First-time setup (server host, once)

```bash
hive up                     # build + start (zero-config); blocks until the daemon is healthy
```

- Zero config required: `hive up` boots on safe code defaults. Only `cp .env.example .env`
  and edit it if you need an operator override (a non-default DB path, log level, a guarantee
  knob, or the `--tunnel` ngrok credentials).

- The daemon serves MCP over HTTP on **127.0.0.1:8765 only**. Public exposure is never
  implicit — see section 3.
- Data lives in the `hive-data` volume: `hive down` preserves it, `hive nuke` destroys
  it (typed confirmation).
- **Upgrading across schema generations**: this build refuses old-format tables at boot
  (no silent migration). If `hive up` crash-loops after a rebuild, the volume predates
  the current schema — `hive nuke`, then `hive up` for a clean store.

## 2. Connect a seat (once per agent seat)

**One token per seat — never share across agents.** Identity diversity is the promotion
fuel: a fleet on one token structurally cannot promote its own captures.

```bash
hive token alice-laptop     # prints the token ONCE — hand over via a secret manager
```

The teammate (or you, locally):

```bash
export HIVE_TOKEN=hive_…
claude mcp add --transport http hive http://localhost:8765/mcp \
  --header "Authorization: Bearer ${HIVE_TOKEN}"
```

`hive connect` prints that line ready-made — the public `https://…` URL when
`NGROK_DOMAIN` is set on the host, the `http://localhost:8765` loopback URL otherwise.
From there onboarding is self-serve: the `hive_health` tool description carries the
rules block, so a connected agent writes it into its primary rules file (CLAUDE.md /
AGENTS.md / …) and skips re-touch when the marker is already present. No skill, no
handshake call, no manual per-repo work.

**Solo (one dev, one identity)?** Set `HIVE_AUTONOMY__SOLO_MODE=true` on the server —
demand-promotion swaps its identity-diversity clause for an elapsed-span rule. Human
`hive_write(approved_by=…)` stays the only path to `established` trust.

## 3. Teammates on other machines

Loopback never leaves the host, so open exactly one door:

- **Tunnel (recommended)**: free ngrok account → set `NGROK_AUTHTOKEN` +
  `NGROK_DOMAIN` in `.env` → `hive up --tunnel`. This starts the ngrok sidecar
  (compose `tunnel` profile) beside the daemon and forwards `https://<your-domain>/mcp`
  to its loopback port; TLS terminates at the ngrok edge, so the seat token is
  encrypted in transit. It fail-fasts if either secret is missing — a plain `hive up`
  never exposes anything. Teammates register the `https://<your-domain>/mcp` line
  `hive connect` prints, with their seat token. Works from any network, no SSH.
- **SSH (zero extra accounts)**: `ssh -NL 8765:localhost:8765 you@host`, then the
  localhost line above works as-is.

Never publish `0.0.0.0:8765` — a bearer token over plain LAN HTTP is cleartext.
Offboard a seat any time: `hive revoke <seat>` → next request 401s.

### Tokenless mode for a trusted, loopback-only fleet (`HIVE_AUTH__MODE=open`)

By default every request is authenticated by a per-seat Bearer token (`token` mode). A
**trusted internal fleet on one host** can instead run **tokenless** by setting
`HIVE_AUTH__MODE=open` on the server (a restart applies it; there is no live reload):

- Token verification is skipped. Each request still states a **distinct identity** via an
  `X-Hive-Agent-Id: <seat>` HTTP header — that header becomes the `agent_id` for attribution
  and demand anti-gaming. There is no anonymous access: a request **missing or with a blank**
  `X-Hive-Agent-Id` is rejected with **400** (open means *unverified*, never *anonymous*).
- Register a seat with the header instead of a token:

  ```bash
  claude mcp add --transport http hive http://localhost:8765/mcp \
    --header "X-Hive-Agent-Id: alice-laptop"
  ```

- **Loopback-only, never tunneled.** Open mode trusts the network, so it is safe **only** on
  `127.0.0.1` (or a private host every member trusts). `hive up --tunnel` **refuses to start**
  while `HIVE_AUTH__MODE=open` (exits `EX_CONFIG`, spawns nothing) — a public, unauthenticated
  endpoint would let anyone poison the shared memory. For any tunneled or cross-machine
  deployment, keep `token` mode (the default) and hand out per-seat tokens as in section 2.
- Identity is still **trust, not crypto**: a member could forge another seat's
  `X-Hive-Agent-Id`. That is acceptable for an honest fleet (the same assumption a local-first
  store already makes); operators who don't trust their fleet keep `token` mode. For a *single*
  identity, prefer `HIVE_AUTONOMY__SOLO_MODE=true` (section 2) over open mode.

## 4. Day-2 operations

| Command | What it tells/does |
|---|---|
| `hive status` | server health, tunnel on/off + URL, seat count |
| `hive logs [svc]` | follow the daemon (or `ngrok`) logs |
| `hive tokens` | provisioned seat labels (never the tokens) |
| `hive revoke <seat>` | offboard a seat (next request → 401) |
| `hive backup` | snapshot the store now — manual (no scheduler); keeps the `backup_keep` most-recent you take |
| `hive down` / `hive nuke` | stop (keep data) / destroy (typed confirm) |

**Demand-health KPIs:** call `hive_health(include_trends=true)` over MCP — current vs previous
7d window + deltas (`confident_rate` + `demand_entropy`), read-only off the warm store. This
trend is the only window into silent fail-open rot, so it stays instrumented.
