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

## 4. Day-2 operations

| Command | What it tells/does |
|---|---|
| `hive status` | server health, tunnel on/off + URL, seat count |
| `hive logs [svc]` | follow the daemon (or `ngrok`) logs |
| `hive tokens` | provisioned seat labels (never the tokens) |
| `hive revoke <seat>` | offboard a seat (next request → 401) |
| `hive backup` | snapshot the store now — manual (no scheduler); keeps the `backup_keep` most-recent you take |
| `hive down` / `hive nuke` | stop (keep data) / destroy (typed confirm) |

**Convergence KPIs:** call `hive_health(include_trends=true)` over MCP — current vs previous
7d window + deltas, read-only off the warm store. Host-side: `hive health` prints the same
trends off the warm store (no MCP seat needed).

### Associative recall (co-access edges) — optional, default OFF

Memories frequently *served together* can accrue a weighted edge, and a confident recall can
then surface those neighbors on a separate `associations` channel (related context, never
ranked answers). Two independent boot env knobs, both default OFF — set on the server host
(a restart applies config; there is no live reload):

| Env var | Default | What it enables |
|---|---|---|
| `HIVE_RECALL__CO_ACCESS` | `false` | **Write side** — accrue co-access edges after each confident recall (bounded: ≤28 upserts/recall, independent of `recall_top_n`). No read surface yet. |
| `HIVE_RECALL__ASSOCIATIONS` | `false` | **Read side** — surface co-accessed neighbors of the served hits under the `associations` recall key. |

**Staged rollout:** enable `HIVE_RECALL__CO_ACCESS=true` first and let edges accumulate while you
watch the per-recall write cost on your real store; only then enable
`HIVE_RECALL__ASSOCIATIONS=true` to start surfacing neighbors (with associations on but co-access
off, the edge table is simply empty, so the channel returns nothing — harmless). The
`associations` payload is omitted from the recall envelope whenever it is empty, so leaving both
off is byte-identical to a build without the feature.
