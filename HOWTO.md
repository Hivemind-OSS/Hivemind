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
- **Agent self-authorization (`HIVE_AGI__MODE`, default OFF)** — leave it off and a human
  `hive_write(approved_by=…)` is the only path to `established` trust and the only authority to
  retire (`hive_supersede`) or prune (`hive_prune`) a memory. Set it `true` only to deliberately
  delegate that per-write vouch to the fleet: an agent may then self-authorize with
  `approved_by="AGI_OVERRIDE"`, stamped byte-distinguishably in the audit (`provenance=agent_reasoned`).
  It loosens a safety gate, so it stays off unless you mean it.

- The daemon serves MCP over HTTP on **two doors**: a tokenless **loopback** door on
  **127.0.0.1:8765** (local agents) and a token-required **tunnel** door (compose-internal
  `8766`, ngrok-forwarded — the only remote-reachable one). Public exposure is never implicit —
  see section 3.
- Data lives in the `hive-data` volume: `hive down` preserves it, `hive reset` snapshots it
  out to the host and then recreates it empty (recoverable; typed confirmation).
- **Upgrading across schema generations**: this build refuses old-format tables at boot
  (no silent migration). If `hive up` crash-loops after a rebuild, the volume predates
  the current schema — run `hive reset` for a clean store (it snapshots the prior store to
  the host first, so the upgrade is recoverable).

## 2. Connect an agent

Identity is **per-agent-session**: every connection gets its own identity automatically — the
server-minted `Mcp-Session-Id` a conforming client echoes, or an explicit `X-Hive-Agent-Id`
header for readable provenance. That per-session diversity is the promotion fuel, so distinct
agents promote each other's captures with **no per-agent token** and no shared-token footgun.

**Local agents** use the tokenless loopback door — no token at all:

```bash
claude mcp add --transport http hive http://localhost:8765/mcp
```

**Remote teammates** authenticate at the tunnel door with a per-seat token:

```bash
hive token alice-laptop     # prints the token ONCE — hand over via a secret manager
export HIVE_TOKEN=hive_…
claude mcp add --transport http hive https://<your-domain>/mcp \
  --header "Authorization: Bearer ${HIVE_TOKEN}"
```

`hive connect` prints the right line ready-made — the tokenless `http://localhost:8765`
loopback line by default, or the token-gated public `https://…` line when `NGROK_DOMAIN` is set
on the host. The token only authenticates the tunnel door; it is never the identity. From there
onboarding is **served-only**: the usage contract reaches every agent over MCP — the `initialize`
instructions carry the full contract, with a secondary reference in the `hive_health` description.
Nothing is written into a client's rules file; the served copy is the only copy, so it can't
drift. No skill, no handshake call, no manual per-repo work.

**Solo (one dev)?** Nothing to set — solo is automatic. A solo dev's independent agents each
carry a distinct per-session identity (the server-minted `Mcp-Session-Id`, or an explicit
`X-Hive-Agent-Id`), so their shared demand promotes under the one identity-diversity rule with
no flag. Human `hive_write(approved_by=…)` stays the only path to `established` trust — unless
you deliberately opt into `HIVE_AGI__MODE` (section 1), which lets an agent self-authorize.

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

### The two doors (auth is a property of the socket, not a config knob)

There is no `HIVE_AUTH__MODE` switch. Auth is decided by **which door** a request reaches:

- the **loopback door** (`127.0.0.1:8765`, host-published) is **tokenless** — for local agents
  on the trusted host. A missing identity floors to `local`; it never 401s and never 400s.
- the **tunnel door** (compose-internal `8766`, ngrok-forwarded) is **token-required** — the
  only remote-reachable door, so a public caller is always bearer-gated by construction. The
  loopback door is never host-published beyond `127.0.0.1`, so it is never reachable remotely.

On both doors the per-request identity is the same: `X-Hive-Agent-Id` → the server-minted
`Mcp-Session-Id` a conforming client echoes → `local`. The token authenticates the tunnel door;
it is never the identity. A leftover `HIVE_AUTH__MODE` in an old `.env` is ignored (a WARN, not a
crash) — remove it.

## 4. Day-2 operations

| Command | What it tells/does |
|---|---|
| `hive status` | server health, tunnel on/off + URL, seat count |
| `hive logs [svc]` | follow the daemon (or `ngrok`) logs |
| `hive tokens` | provisioned seat labels (never the tokens) |
| `hive revoke <seat>` | offboard a seat (next request → 401) |
| `hive backup` | snapshot the store now — manual (no scheduler); keeps the `backup_keep` most-recent you take |
| `hive down` / `hive reset` | stop (keep data) / snapshot-out then recreate empty (recoverable; typed confirm) |
| `hive restore <snap>` | replace the live store from a snapshot (inverse of reset; typed confirm) |

**Demand-health KPIs:** call `hive_health(include_trends=true)` over MCP — current vs previous
7d window + deltas (`confident_rate` + `demand_entropy`), read-only off the warm store. This
trend is the only window into silent fail-open rot, so it stays instrumented.
