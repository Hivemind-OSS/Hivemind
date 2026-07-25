# Hivemind

A stale-proof, stigmergic **episodic memory** for a fleet of coding agents. Hivemind
runs as a single self-hosted **MCP server** that every agent connects to; what one agent
learns, the others can recall. It is built for **solo devs and small teams** — single-tenant,
single-host, **one SQLite store, partitioned by repo at the memory level**: you register the
repos the fleet works on (`hive repo add`), memories bind to them (and to code anchors inside
them), and recall scopes by repo and branch.

Recall is deliberately conservative: a query is embedded, matched by dense cosine similarity,
and passed through an **absolute-relevance abstention gate** — when the top match does not
clear an absolute similarity floor, Hivemind returns nothing rather than guess. `hive_write`
serves immediately as `provisional` — prefer it for a lesson that would spare a future agent a
repeat mistake, a regression, or relearning what led to landed code; the trusted `established`
tier is reached **only** when a verified change outcome on the repo's canonical line confirms
the memory. `hive_capture` is for the ambiguous tail — it lands **quarantined** and becomes
servable only once independent fleet demand or a verified change outcome promotes it. Tag the
line wherever a memory is about real code (`repos=["name@branch"]` + `anchors=[{repo, anchor}]`)
— that declared line is what a branch-scoped recall and the retirement gate judge it against.
Retirement is **machine-gated**: the server
retires a memory only when it verifies a qualifying machine signal (anchor drift at the
canonical tip, hurt evidence, a mechanical contradiction) — never on an agent's say-so, and an
unqualified call is a benign no-op, not an error. Unused memories decay on a TTL. Nothing is
auto-trusted, and the store never silently migrates across schema generations.

## The MCP tools

A connected agent gets exactly eight tools:

| Tool | Purpose |
|---|---|
| `hive_recall(query, repos=…, anchor_prefix=…)` | Dense recall behind the abstention gate, scoped by repo (`repos=["name"]` or `["name@branch"]`; omit for global). Returns reference context (or abstains) with each hit's `trust`, `ts`, `polarity`, `kind`, `repos`, `anchors`, and a per-anchor `drift` verdict (a drifted hit is reference only). |
| `hive_write(text)` | Save a memory that **serves now** as `provisional`; healed afterward by the server's outcome/drift machinery (`established` = outcome-verified on the canonical line). `replaces=<id>` retires the corrected memory only when the server verifies a qualifying signal — else the write still lands and the retire is a no-op. |
| `hive_capture(text)` | Record an insight of unclear value. Lands quarantined; served only after fleet demand or a verified change outcome promotes it. |
| `hive_supersede(loser, winner)` | Retire one memory in favor of another — machine-gated: the server retires only on a verified qualifying signal; otherwise a benign no-op. |
| `hive_prune(episode_id)` | Retire an incorrect or misleading memory with no replacement (it stays in the audit ledger) — machine-gated, same rule. |
| `hive_flag(a, b, kind)` | Advisory only: record that two memories conflict or one supersedes the other. Retires nothing and never qualifies the retirement gate. |
| `hive_outcome(helped=[…], hurt=[…])` | Log which recalled memories helped or hurt the task; evidence only. Helped rows fuel promotion; hurt rows feed the machine retirement gate. |
| `hive_health(...)` | Liveness/identity snapshot; `include_trends=true` adds convergence KPIs, `include_gaps=true` the demand-gap report, `include_conflicts=true` the contested-memory worklist; further flags: `include_suspect_consensus`, `include_stale_suspects` (graph-propagated staleness), `include_census_health` (per-repo census/sync state + the sync daemon's own `fleet` block), `include_meta_versions`. |

## Requirements

- **Docker** + **Docker Compose v2** — the server, its store, and a baked offline embedder run
  in one container (the image is hermetically offline; no network at runtime beyond the sync
  daemon's git fetches for the repos you register with `hive repo add`).
- **Python 3.11+** on the host — to run the `hive` operator CLI (it drives `docker compose`).

## Quickstart (from a clone)

```bash
git clone https://github.com/Hivemind-OSS/Hivemind.git hivemind && cd hivemind
pip install -e .          # installs the `hive` command (venv on PEP-668 systems; uninstalled: python3 -m hive.tools.cli)
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

That registration line is the whole per-agent setup. Agents are **thin, repo-agnostic MCP
clients**: the server delivers its usage contract through the MCP `initialize` instructions
(every client surfaces them) — fresh at every connect, so a session picks up a changed contract
on reconnect. Nothing is written into any rules file, and there is nothing to install per
agent, per project, or per device.

## Register your repos

Partitioning is operational, not config: register each repo the fleet works on and the server
does the rest.

```bash
hive repo add https://github.com/you/your-repo.git     # slug defaults to the URL basename
hive repo add https://github.com/you/private.git --name priv --branch main --token-env PRIV_TOKEN
hive repos                                             # list the registry (never a secret)
hive repo remove priv                                  # stop syncing; memories keep their scope
```

A registered row lands in the store's `repos` table and the sync daemon picks it up on its next
tick — no restart. Per repo, the daemon mirrors the remote, feeds every landing on the canonical
branch into the change-outcome evidence ledger, mints missing anchor fingerprints, and
materializes the staleness (drift) verdicts that ride recall hits. `--token-env` is the **name**
of an env var holding that repo's git token — the registry never stores a secret byte; unset,
the fleet-default `HIVE_SYNC__TOKEN` is used. Removing a repo stops the feed and prunes its
mirror; its memories keep their scope, so re-registering picks them straight back up.

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

**Per-repo census feed.** Which repos the sync daemon feeds is operational data — the repo
registry (`hive repo add`, above), re-read every tick — not boot config. The `HIVE_SYNC__*`
group carries only the loop's own knobs (poll cadence, webhook nudge, mirror dir); the feed is
detect-only evidence plus the one mechanical trust movement it drives — the verified-outcome
`established` sweep. Arm and test a repo with the runnable
**[`hive-connect-repo`](skills/hive-connect-repo/SKILL.md)** skill; knob table + details:
**[HIVE-ADMIN.md §4](HIVE-ADMIN.md)**.

## Remote teammates

Loopback never leaves the host, so open exactly one door:

- **Tunnel** — a free ngrok account: set `NGROK_AUTHTOKEN` + `NGROK_DOMAIN` in `.env`, then
  `hive up --tunnel`. TLS terminates at the ngrok edge, so the seat token is encrypted in
  transit.
- **SSH** — `ssh -NL 8765:localhost:8765 you@host`, then the localhost registration line works
  as-is.

Never publish `0.0.0.0:8765` — that door is tokenless, so publishing it hands unauthenticated recall and write to the whole LAN.

## Day-2 operations

`hive ui` (loopback operator dashboard in the browser — live status, seat mint/revoke, backup,
non-blocking start/stop, tunnel activate/deactivate, restore from an in-volume backup behind a
typed confirm, log tail; no reset) / `hive status` / `logs` / `tokens` / `revoke <seat>` /
`repo add <url>` / `repo remove <name>` / `repos` (the synced-repo registry) /
`backup` (manual snapshot) / `ingest
<receipt.json>` (manually feed an unsigned census receipt's change outcome into the evidence
ledger — the escape hatch; the sync daemon feeds every registered repo itself) / `down`
(stop, keep data) / `reset` (snapshot the store out of the volume, then destroy + recreate it
empty — recoverable; typed confirm) / `restore` (replace the live store from a snapshot) / `upgrade
[--ref release]` (move the server to a vetted release ref — backup-gated, auto-rollback on failure).

A compatible release moves with `hive upgrade`; crossing a schema generation is a clean store —
`hive reset` (it saves the prior store to the host first), then `hive repo add` each repo; no
in-place migration ships. Moving onto the v3 schema, also one-time strip what an older server
had projects install client-side — the served rules block, the hive lifecycle hooks, and the
`hive_*` allowlist entries in each consuming repo's rules file / `.claude/settings.json` — the
v3 server serves everything at connect, so those are dead weight.

See **[HIVE-ADMIN.md](HIVE-ADMIN.md)** for the full admin & operator guide (setup, tunneling, tuning,
KPIs).

## Code anchors & staleness (server-side)

Anchor fingerprints and staleness verdicts are computed **in the server**: the anchor/census
engines are first-party subpackages of the `hive` distribution (`hive/matrix`, `hive/combdrift`,
`hive/edge`), built into the image from this repository, and the sync daemon runs
them per registered repo — minting fingerprints for stored anchors and materializing per-anchor
drift verdicts at the canonical tip (and at branch tips recall asked about). Every recall hit
carries the result as its `drift` field, and a hit the server already knows is stale arrives
with a remediation rider. There is nothing to install on a workstation or in an agent.

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
hive/                 the server: domain core, adapters (SQLite store, embedder), MCP app, CLI, sync daemon, and the first-party engine subpackages (matrix/combdrift/edge)
tests/                the test suite
compose.yaml          the single-service stack (+ opt-in ngrok tunnel profile)
Dockerfile            the hermetically-offline server image (embedder baked at build)
Makefile              the canonical mechanical gate — `make check` (format, lint, strict typecheck, tests)
docs/engines/         reference docs for the first-party anchor/census engines (matrix, comb-drift)
llms.txt              link index to the project docs (llmstxt.org convention)
llms-full.txt         the complete, self-contained operating guide for agents & integrators
HIVE-ADMIN.md         admin & operator guide
OPERATIONS.md         long-form operations reference & the tuning evidence behind the knobs
skills/               operator runbook-skills (bringup, connect-team, connect-repo, upgrade, backup/restore, operate)
CONTRIBUTING.md       how to contribute: the development-first branch flow and running the tests
LICENSE               this project's license (Apache-2.0)
THIRD_PARTY_NOTICES.md / LICENSES/   embedding-model attribution + license (the embedder)
```

## Contributing

Contributions are welcome. All work lands on the `development` branch; `master` is updated
only through a `development → master` pull request — a required check enforces that source,
and direct pushes to `master` are rejected. The canonical mechanical gate is **`make check`**
(ruff format check, ruff lint, `mypy hive/ --strict`, the default pytest suite — the heavy
`embed` tier is opt-in: run it with `-m embed` and the `embed` extra) — a change is done
when it passes. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full workflow.
