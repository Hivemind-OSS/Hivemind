# Hivemind

A stigmergic **episodic memory** for a fleet of coding agents working one codebase. Hivemind
runs as a single self-hosted **MCP server** that every agent connects to; what one agent
learns, the others can recall. It is built for **solo devs and small teams** — single-tenant,
single-host, one SQLite store.

Recall is deliberately conservative: a query is embedded, matched by dense cosine similarity,
and passed through an **absolute-relevance abstention gate** — when the top match does not
clear an absolute similarity floor, Hivemind returns nothing rather than guess. Memories enter **quarantined**
via `hive_capture` and become servable only once independent fleet demand or a verified change outcome promotes them
(`provisional`); the trusted `established` tier is reached **only** by an explicit
human-approved `hive_write`. Unused memories decay on a TTL. Nothing is auto-trusted, and the
store never silently migrates across schema generations.

## The MCP tools

A connected agent gets exactly eight tools:

| Tool | Purpose |
|---|---|
| `hive_recall(query)` | Dense recall behind the abstention gate. Returns reference context (or abstains) with each hit's `trust`, `ts`, `polarity`, `kind`, and `anchor`. |
| `hive_capture(text)` | Record a durable insight. Lands quarantined; served only after fleet demand or a verified change outcome promotes it. |
| `hive_write(text, approved_by=…)` | Human-vouched memory served immediately as `established`. `replaces=<id>` supersedes an existing one. |
| `hive_supersede(loser, winner, approved_by=…)` | Human-vouched: retire one memory in favor of another. Nothing new is written. |
| `hive_prune(episode_id, approved_by=…)` | Human-vouched: retire an incorrect or misleading memory with no replacement (it stays in the audit ledger). |
| `hive_flag(a, b, kind)` | Advisory only: record that two memories conflict or one supersedes the other, for a human to resolve. Retires nothing. |
| `hive_outcome(helped=[…], hurt=[…])` | Log which recalled memories helped or hurt the task; records evidence only — changes no trust. |
| `hive_health(...)` | Liveness/identity snapshot; `include_trends=true` adds convergence KPIs, `include_gaps=true` the demand-gap report, `include_conflicts=true` the contested-memory worklist. |

## Requirements

- **Docker** + **Docker Compose v2** — the server, its store, and a baked offline embedder run
  in one container (the image is hermetically offline; no network at runtime beyond the opt-in census sync's git fetches (`HIVE_SYNC__REPO_URL`)).
- **Python 3.11+** on the host — to run the `hive` operator CLI (it drives `docker compose`).

## Quickstart (from a clone)

```bash
git clone https://github.com/Hivemind-OSS/Hivemind.git hivemind && cd hivemind
pip install -e .          # installs the `hive` command (uninstalled: python -m hive.tools.cli)
cp .env.example .env      # persist the store across restarts (sets HIVE_STORE__DB_PATH)
hive up                   # build + start; blocks until the daemon is healthy
```

`hive up` is zero-config to boot, but **the store defaults to `/data/shared.db` in the `hive-data` volume — persistent;
only an explicit `HIVE_STORE__DB_PATH=:memory:` boots ephemeral, losing all memory on restart.** Copy `.env.example` to `.env` (above) to persist into the
`hive-data` volume via `HIVE_STORE__DB_PATH=/data/shared.db`; an ephemeral boot WARNs loudly and
`hive_health` reports `store_ephemeral`. **Agents should bring the server up with the runnable
[`hive-bringup`](skills/hive-bringup/SKILL.md) skill** rather than the raw commands above — it
carries the bounded health-wait, the boot failure modes, and the schema-refusal recovery;
**[`hive-connect-team`](skills/hive-connect-team/SKILL.md)** then registers agents & teammates.

The daemon serves MCP over **two
doors**: a tokenless **loopback** door on **127.0.0.1:8765** for local agents, and a
token-required **tunnel** door for remote teammates (compose-internal, never host-published).
Identity is per-agent-session — every connection gets its own identity automatically (the
server-minted `Mcp-Session-Id`, or an explicit `X-Hive-Agent-Id` for readable provenance), so a
fleet of K agents behaves the same whether 1 or N engineers run it. The token only
authenticates the tunnel door — it is never the identity.

Connect a **local** agent (no token needed — the loopback door is tokenless):

```bash
hive connect              # prints the ready-made tokenless `claude mcp add …` line
claude mcp add --transport http hive http://localhost:8765/mcp
```

For a **remote** teammate, mint a seat token (the tunnel door authenticates with it):

```bash
hive token alice-laptop   # prints the token ONCE — hand it over via a secret manager
claude mcp add --transport http hive https://<your-domain>/mcp \
  --header "Authorization: Bearer <seat-token>"   # replace <seat-token> with the seat's token
```

From there onboarding is automatic and server-side: at connect the server delivers its usage
contract through the MCP `initialize` instructions (every client surfaces them) — recall-first,
capture-by-default, and the per-agent-session identity model. **Nothing is
automatically written into a rules file at connect** — installing the optional persistence block
into your project's rules file is an agent-driven step via `hive_health(include_onboarding=true)`. On **Claude Code only**, `hive_health(include_onboarding=true)` additionally
serves optional lifecycle-hook nudges you can merge into `.claude/settings.json`.

## Agents
Read **[`llms-full.txt`](llms-full.txt)** for the complete, self-contained explanation of how
agents use the memory; **[`llms.txt`](llms.txt)** is the short link index to every project doc.

## Configuration

All config has safe code defaults in `hive/app/config.py` and is applied **only at boot** (a
restart — there is no live reload). To override a knob, copy `.env.example` to `.env`, set the
`HIVE_<GROUP>__<FIELD>` key, and run `hive up`. Out-of-range values fail boot loudly rather than
silently clamping.

**Two doors** (auth is a property of the listening socket, not a config knob):

- **Loopback door** (`127.0.0.1:8765`, host-published) — **tokenless**, for local agents on the
  host. Identity is the per-session `X-Hive-Agent-Id` (or the server-minted `Mcp-Session-Id`),
  else the `local` bucket.
- **Tunnel door** (compose-internal `8766`, ngrok-forwarded) — **token-required**; the only
  remote-reachable door. The bearer token authenticates; it is never the identity.

There is no `HIVE_AUTH__MODE` switch — delete any leftover one from `.env` (it is ignored).

**Automatic census feed (optional).** Set `HIVE_SYNC__REPO_URL` (plus `HIVE_SYNC__TOKEN` for a
private remote) and the server itself mirrors the repo and feeds every landing on the tracked
branch into the change-outcome evidence ledger — detect-only, fail-open, byte-inert when unset;
nothing is wired per repo or per device. Arm and test it with the runnable
**[`hive-connect-repo`](skills/hive-connect-repo/SKILL.md)** skill; knob table + details:
**[HIVE-ADMIN.md §4](HIVE-ADMIN.md)**.

## Remote teammates

Loopback never leaves the host, so open exactly one door:

- **Tunnel** — a free ngrok account: set `NGROK_AUTHTOKEN` + `NGROK_DOMAIN` in `.env`, then
  `hive up --tunnel`. TLS terminates at the ngrok edge, so the seat token is encrypted in
  transit.
- **SSH** — `ssh -NL 8765:localhost:8765 you@host`, then the localhost registration line works
  as-is.

Never publish `0.0.0.0:8765` — a bearer token over plain LAN HTTP is cleartext.

## Day-2 operations

`hive ui` (loopback operator dashboard in the browser — live status, seat mint/revoke, backup,
non-blocking start/stop, tunnel activate/deactivate, restore from an in-volume backup behind a
typed confirm, log tail; no reset) / `hive status` / `logs` / `tokens` / `revoke <seat>` /
`backup` (manual snapshot) / `ingest
<receipt.json>` (manually feed an unsigned census receipt's change outcome into the evidence
ledger — the escape hatch; with `HIVE_SYNC__REPO_URL` set the server feeds itself) / `down`
(stop, keep data) / `reset` (snapshot the store out of the volume, then destroy + recreate it
empty — recoverable; typed confirm) / `restore` (replace the live store from a snapshot) / `upgrade
[--ref release]` (move the server to a vetted release ref — backup-gated, auto-rollback on failure).
A compatible release moves with `hive upgrade`; crossing a schema generation is a single `hive reset`
— it saves the prior store to the host, then recreates empty; no in-place migration ships.

See **[HIVE-ADMIN.md](HIVE-ADMIN.md)** for the full admin & operator guide (setup, tunneling, tuning,
KPIs).

## Edge tooling

Anchor mint/verify (recall freshness, including the dependency-neighborhood `radius` advisory) and
a persistent per-repo code graph (`hive-edge graph`) ride a companion, per-workstation CLI,
**[`hive-edge`](https://github.com/Hivemind-OSS/Hive-edge)** — the server image bakes its own copy for the server-side census leg; each workstation installs its own. It is not required for the core recall/capture/write loop (absent, mint and
verify simply no-op; nothing else is affected), but the trust-lifecycle verify step depends on it.
Census change evidence does **not**: it is computed and fed server-side (`HIVE_SYNC__REPO_URL`,
above), so there is nothing to wire per repo or per device.

You don't need to install it yourself: a connected agent checks for it during onboarding and
installs or updates it automatically. To install it manually instead — uv required (the CLI's
workspace engines resolve from git subdirectories via uv sources, which pip/pipx cannot read):

```bash
uv tool install git+https://github.com/Hivemind-OSS/Hive-edge@release
uv tool update-shell   # once, so uv's tool directory is on your PATH
```

Per-device edge state (worktree-delta baselines, the per-checkout code-graph cache) lives under
`~/.hive-edge/` (`HIVE_EDGE_HOME` overrides); it is safe to delete and regenerates. See
**[HIVE-ADMIN.md §8](HIVE-ADMIN.md)** for the full install/update/rollback flow.

## Embedding model & attribution

Recall is powered by [**Qwen3-Embedding-0.6B**](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
(© Alibaba Cloud / the Qwen Team), used unmodified and baked into the image at build time. Its
native 1024-dim output is used directly, L2-normalized (no weight modification). The
model is licensed under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0); the
full text and attribution travel with this repository in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and
[`LICENSES/`](LICENSES/Qwen3-Embedding-0.6B-Apache-2.0.txt).

## Repository layout

```
hive/                 the server: domain core, adapters (SQLite store, embedder), MCP app, CLI
tests/                the test suite
compose.yaml          the single-service stack (+ opt-in ngrok tunnel profile)
Dockerfile            the hermetically-offline server image (embedder baked at build)
llms.txt              link index to the project docs (llmstxt.org convention)
llms-full.txt         the complete, self-contained operating guide for agents & integrators
HIVE-ADMIN.md         admin & operator guide
OPERATIONS.md         long-form operations reference & the tuning evidence behind the knobs
skills/               operator runbook-skills (bringup, connect-team, connect-repo, backup/restore, operate)
LICENSE               this project's license (Apache-2.0)
THIRD_PARTY_NOTICES.md / LICENSES/   embedding-model attribution + license (the embedder)
```
