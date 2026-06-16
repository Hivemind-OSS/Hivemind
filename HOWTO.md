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

`hive connect` prints that line ready-made (with the public URL when the tunnel is up).
From there the system drives itself: the first `hive_*` call from an unlinked repo
returns the onboarding hint, the agent runs `hive_init`, writes the rules block, and
the verify gate confirms the loop end to end. No skill, no manual per-repo work.

**Solo (one dev, one identity)?** Set `HIVE_AUTONOMY__SOLO_MODE=true` on the server —
demand-promotion swaps its identity-diversity clause for an elapsed-span rule. Human
`hive_write(approved_by=…)` stays the only path to `established` trust.

## 3. Teammates on other machines

Loopback never leaves the host, so open exactly one door:

- **Tunnel (recommended)**: free ngrok account → set `NGROK_AUTHTOKEN` +
  `NGROK_DOMAIN` in `.env` → `hive up --tunnel` (fail-fasts if the secrets are
  missing; a plain `hive up` never exposes anything). Teammates use
  `https://<your-domain>/mcp` with their seat token — `hive connect` prints it.
- **SSH (zero extra accounts)**: `ssh -NL 8765:localhost:8765 you@host`, then the
  localhost line above works as-is.

Never publish `0.0.0.0:8765` — a bearer token over plain LAN HTTP is cleartext.
Offboard a seat any time: `hive revoke <seat>` → next request 401s.

## 4. Day-2 operations

| Command | What it tells/does |
|---|---|
| `hive status` | server health, tunnel on/off + URL, seat count |
| `hive logs [svc]` | follow the daemon (or `ngrok`) logs |
| `hive tokens` | provisioned seat labels (never the tokens) |
| `hive revoke <seat>` | offboard a seat (next request → 401) |
| `hive down` / `hive nuke` | stop (keep data) / destroy (typed confirm) |

**Convergence KPIs:** call `hive_health(include_trends=true)` over MCP — current vs previous
14d window + deltas, read-only off the warm store.
