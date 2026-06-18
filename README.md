# Hivemind

A shared **episodic memory** for a fleet of coding agents working one codebase. Hivemind
runs as a single self-hosted **MCP server** that every agent connects to; what one agent
learns, the others can recall. It is built for **solo devs and small teams** — single-tenant,
single-host, one SQLite store.

Recall is deliberately conservative: a query is embedded, matched by dense cosine similarity,
and passed through a **normalized-entropy abstention gate** — when the top matches are not
clearly separated, Hivemind returns nothing rather than guess. Memories enter **quarantined**
via `hive_capture` and become servable only once independent fleet demand promotes them
(`provisional`); the trusted `established` tier is reached **only** by an explicit
human-approved `hive_write`. Unused memories decay on a TTL. Nothing is auto-trusted, and the
store never silently migrates across schema generations.

## The MCP tools

A connected agent gets exactly four tools:

| Tool | Purpose |
|---|---|
| `hive_recall(query)` | Dense recall behind the abstention gate. Returns reference context (or abstains) with each hit's `trust` + `ts`. |
| `hive_capture(text)` | Record a durable insight. Lands quarantined; served only after fleet demand promotes it. |
| `hive_write(text, approved_by=…)` | Human-vouched memory served immediately as `established`. `replaces=<id>` supersedes an existing one. |
| `hive_health(...)` | Liveness/identity snapshot; `include_trends=true` adds convergence KPIs, `include_gaps=true` the demand-gap and contested-memory reports. |

## Requirements

- **Docker** + **Docker Compose v2** — the server, its store, and a baked offline embedder run
  in one container (the image is hermetically offline; no network at runtime).
- **Python 3.11+** on the host — to run the `hive` operator CLI (it drives `docker compose`).

## Quickstart (from a clone)

```bash
git clone <repo-url> hivemind && cd hivemind
pip install -e .          # installs the `hive` command (uninstalled: python -m hive.tools.cli)
hive up                   # build + start; blocks until the daemon is healthy
```

`hive up` is zero-config — it boots on safe code defaults. The daemon serves MCP over HTTP on
**127.0.0.1:8765 only**; it is never exposed to the network implicitly.

Give each agent its own seat (one token per agent — never shared):

```bash
hive token alice-laptop   # prints the token ONCE — hand it over via a secret manager
hive connect              # prints the ready-made `claude mcp add …` registration line
```

The teammate registers the server with that seat token:

```bash
export HIVE_TOKEN=hive_…
claude mcp add --transport http hive http://localhost:8765/mcp \
  --header "Authorization: Bearer ${HIVE_TOKEN}"
```

From there onboarding is self-serve: the `hive_health` tool description carries a rules block a
connected agent writes into its own primary rules file (`CLAUDE.md` / `AGENTS.md` / …).

## Configuration

All config has safe code defaults in `hive/app/config.py` and is applied **only at boot** (a
restart — there is no live reload). To override a knob, copy `.env.example` to `.env`, set the
`HIVE_<GROUP>__<FIELD>` key, and run `hive up`. Out-of-range values fail boot loudly rather than
silently clamping.

**Auth posture** (`HIVE_AUTH__MODE`):

- `token` (default) — every request carries a per-seat `Authorization: Bearer` token.
- `open` — tokenless, for a **trusted loopback-only** fleet on one host; identity is
  self-asserted via an `X-Hive-Agent-Id: <seat>` header (no anonymous access). Refused for any
  tunneled deployment.

## Remote teammates

Loopback never leaves the host, so open exactly one door:

- **Tunnel** — a free ngrok account: set `NGROK_AUTHTOKEN` + `NGROK_DOMAIN` in `.env`, then
  `hive up --tunnel`. TLS terminates at the ngrok edge, so the seat token is encrypted in
  transit.
- **SSH** — `ssh -NL 8765:localhost:8765 you@host`, then the localhost registration line works
  as-is.

Never publish `0.0.0.0:8765` — a bearer token over plain LAN HTTP is cleartext.

## Day-2 operations

`hive status` / `logs` / `tokens` / `revoke <seat>` / `backup` (manual snapshot) / `down`
(stop, keep data) / `nuke` (destroy the data volume, typed confirm). Upgrading across schema
generations is `hive nuke` then `hive up` — no in-place migration ships.

See **[HOWTO.md](HOWTO.md)** for the full operator guide (setup, tunneling, KPIs, tuning) and
**[CONTEXT/THEORY.md](CONTEXT/THEORY.md)** for the design theory — the master laws, the
hexagonal architecture, and the rules any change must follow.

## Repository layout

```
hive/            the server: domain core, adapters (SQLite store, embedder), MCP app, CLI
tests/           the test suite
compose.yaml     the single-service stack (+ opt-in ngrok tunnel profile)
Dockerfile       the hermetically-offline server image (embedder baked at build)
HOWTO.md         operator guide      CONTEXT/THEORY.md   design theory
ADMINSKILLS.md   admin-agent lifecycle skills            CONTEXT/BUGS.md   bug registry
```
