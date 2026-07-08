# HIVE-ADMIN — operating the Hivemind MCP server

The administrator's reference for standing up a Hivemind server, connecting a fleet, tuning the
safety/recall knobs, and running it day-2. Everything here goes through the `hive` CLI
(`pip install -e .` provides the command; uninstalled, `python -m hive.tools.cli` is identical).
For *what Hivemind is* and the agent-facing memory contract, see `llms-full.txt` (the complete
self-contained guide; `llms.txt` is the short link index); for the quickstart,
`README.md`. Agent-runnable runbook-skills for the procedures below live in `skills/`
(`skills/README.md`).

## 1. Prerequisites & first-time setup

On the server host: **Docker** + **Docker Compose v2**, and **Python 3.11+** (for the `hive` CLI,
which drives Compose). Then:

```bash
git clone https://github.com/Hivemind-OSS/Hivemind.git hivemind && cd hivemind
pip install -e .            # installs the `hive` command
cp .env.example .env        # persist the store across restarts (sets HIVE_STORE__DB_PATH)
hive up                    # build + start; blocks until the daemon is healthy
```

- **Zero config to boot**, but the store DEFAULTS to in-memory (`:memory:`) — ephemeral. Copy
  `.env.example` to `.env` to **persist memory across restarts** (it sets
  `HIVE_STORE__DB_PATH=/data/shared.db`), and for any other deliberate override (§4) or the ngrok
  tunnel secrets (§3).
- **A persistent store lives in the `hive-data` Docker volume** once `HIVE_STORE__DB_PATH` points
  at it (`/data/shared.db`); **without it the store is in-memory and lost on restart** — an
  ephemeral boot WARNs loudly and `hive_health` reports `store_ephemeral`. `hive down` preserves
  the volume; `hive reset` snapshots it out to the host and recreates it empty (recoverable — §5).
- **Schema upgrades are not in-place.** This build refuses old-format tables at boot (no silent
  migration). If `hive up` crash-loops after pulling a new build, the volume predates the current
  schema — run `hive reset` for a clean store. Reset snapshots the prior store to the host first,
  so the upgrade is recoverable (`hive restore <snapshot>` rolls it back).
- The first `hive up` builds the image and warms the embedder; if the health-wait times out on a
  slow host, raise it with `HIVE_HEALTH_TIMEOUT=<seconds>` (default `180`).

## 2. Connect local agents (the trusted host)

The daemon serves MCP over HTTP on a **tokenless loopback door** at `127.0.0.1:8765`. A local
agent registers with no token:

```bash
hive connect                                              # prints the ready-made line
claude mcp add --transport http hive http://localhost:8765/mcp
```

Identity is **per-agent-session** — every connection gets its own identity automatically (the
server-minted `Mcp-Session-Id` a conforming client echoes, or an explicit `X-Hive-Agent-Id` header
for readable provenance). That per-session diversity is what promotes captures, so a **solo dev's**
independent agents earn each other's memories with no flag and no per-agent token.

Onboarding's **floor is served**: at connect the server delivers the full usage contract over MCP
(the `initialize` instructions), so each agent learns the recall-first / capture-by-default
discipline with nothing required to be written into its rules file. Optionally, an agent may persist
that contract as a version-stamped rules block in its own project file — the server beacons a
`contract_version` on every result so a stale block re-onboards, and a missing block degrades to the
served floor. That is the agent's own act; the operator installs nothing.

## 3. Connect a remote team

Loopback never leaves the host, so open exactly **one** door for teammates.

**Tunnel (recommended).** A free ngrok account, then in `.env`:

```bash
NGROK_AUTHTOKEN=...               # from the ngrok dashboard
NGROK_DOMAIN=your.ngrok.app       # your account's static domain (stable URL)
```

```bash
hive up --tunnel                  # starts the ngrok sidecar; forwards https://<domain>/mcp
hive token alice-laptop           # mint a per-seat token (printed ONCE — hand over via a secret manager)
```

Each teammate registers the public URL with their seat token (`hive connect` prints this line when
`NGROK_DOMAIN` is set):

```bash
export HIVE_TOKEN=hive_…
claude mcp add --transport http hive https://<your-domain>/mcp \
  --header "Authorization: Bearer ${HIVE_TOKEN}"
```

TLS terminates at the ngrok edge, so the token is encrypted in transit. `hive up --tunnel`
fail-fasts if either ngrok secret is missing — a plain `hive up` never exposes anything.

**SSH alternative (no extra accounts):** `ssh -NL 8765:localhost:8765 you@host`, then the loopback
line from §2 works as-is.

**The two doors — auth is a property of the socket, not a config knob.** There is no
`HIVE_AUTH__MODE` switch; which door a request reaches decides auth:

| Door | Address | Auth | For |
|---|---|---|---|
| Loopback | `127.0.0.1:8765` (host-published) | **tokenless** — a missing identity floors to `local`; never 401/400 | local agents on the trusted host |
| Tunnel | compose-internal `8766`, ngrok-forwarded | **token-required** — the only remote-reachable door | remote teammates |

The bearer token **authenticates** the tunnel door; it is never the identity. **Never publish
`0.0.0.0:8765`** — a bearer token over plain LAN HTTP is cleartext. A leftover `HIVE_AUTH__MODE` in
an old `.env` is ignored (a WARN, not a crash) — remove it. Offboard a seat any time with
`hive revoke <seat>` (its next request 401s).

## 4. Tuneable parameters

All config is applied **only at boot** — restart to change it; there is no live reload. Override by
copying `.env.example` to `.env` and setting `HIVE_<GROUP>__<FIELD>`. An out-of-range value **fails
boot loudly** (it never silently clamps). The defaults are a conservative starting point —
recalibrate the empirical floors (`tau_serve`, `conflict.tau`, `demand_m`) against your real corpus
and query distribution.

**Recall — the never-hallucinate gate**

| Env var | Default | Controls |
|---|---|---|
| `HIVE_RECALL__TAU_SERVE` | `0.70` | absolute-cosine serve floor; the gate abstains unless ≥ `k_min` candidates clear it. `1.0` ⇒ only exact matches serve |
| `HIVE_RECALL__K_MIN` | `1` | how many candidates must clear the floor to serve |
| `HIVE_RECALL__RECALL_TOP_N` | `10` | max hits returned |
| `HIVE_RECALL__OVERSCAN` | `3` | candidate overscan feeding the decorrelated serve-set selection |

**Autonomy — the memory lifecycle (quarantine → demand-promotion → decay)**

| Env var | Default | Controls |
|---|---|---|
| `HIVE_AUTONOMY__ENABLED` | `true` | master switch; `false` makes the whole lifecycle inert (no capture, promotion, or decay) |
| `HIVE_AUTONOMY__DEMAND_M` | `3` | independent recall-misses needed to promote a quarantined memory — your coverage↔safety dial |
| `HIVE_AUTONOMY__DEMAND_WINDOW_DAYS` | `14` | window over which demand is counted |
| `HIVE_AUTONOMY__DEMAND_TAU` | `0.75` | miss↔candidate cosine floor (what counts as "this demand matches this memory") |
| `HIVE_AUTONOMY__COMPETITOR_TAU` | `0.85` | candidate↔servable cosine above which the demand is "already answered" (no promotion) |
| `HIVE_AUTONOMY__QUARANTINE_TTL_DAYS` | `14` | how long an unused quarantined memory lives |
| `HIVE_AUTONOMY__PROVISIONAL_TTL_DAYS` | `45` | how long an unused provisional memory lives |
| `HIVE_AUTONOMY__ANOMALY_TAU` | `0.95` | compact-cluster cosine floor for the flood-anomaly flag (detection-only) |
| `HIVE_AUTONOMY__ANOMALY_MIN_CLUSTER` | `5` | ≥ this many near-identical neighbors ⇒ flag the promotion (it never blocks it) |

**Conflict & suspect-consensus — human worklists (detection only)**

| Env var | Default | Controls |
|---|---|---|
| `HIVE_CONFLICT__ENABLED` | `true` | conflict detection + the `hive_flag` advisory verb; `false` → byte-inert |
| `HIVE_CONFLICT__TAU` | `0.80` | near-duplicate cosine floor (serve-time dedup + the conflict worklist) |
| `HIVE_CONFLICT__TOP_N` | `10` | conflict worklist cap |
| `HIVE_SUSPECT_CONSENSUS__N_EFF_FRAC_MAX` | `0.5` | flag a promotion whose effective independence `n_eff/k` falls below this (thin-independence) |
| `HIVE_SUSPECT_CONSENSUS__TOP_N` | `10` | suspect-consensus worklist cap |

**Operations & safety**

| Env var | Default | Controls |
|---|---|---|
| `HIVE_RETENTION__BACKUP_KEEP` | `30` | most-recent snapshots `hive backup` keeps |
| `HIVE_RETENTION__BACKUP_DIR` | `<db_dir>/backups` | where snapshots are written |
| `HIVE_OBS__LOG_LEVEL` | `20` | logging level (Python `logging`; `20` = INFO) |
| `HIVE_SECRET_SCAN__ENABLED` | `true` | the credential secret floor; `false` **loosens a safety gate** (bypasses the scan — raw secrets get stored) — see below |
| `HIVE_AGI__MODE` | `false` | **loosens a safety gate** — see below. Leave OFF unless you mean it |

**Fixed by the image (not runtime knobs).** The embedder is **Qwen3-Embedding-0.6B**, baked in at
build and run fully offline (`HF_HUB_OFFLINE=1`) — it emits a native 1024-dim vector and there is no
dimension knob; changing the model means rebuilding the image. The in-container store path is
`/data/shared.db`; you manage the `hive-data` volume, not the path.

**`HIVE_AGI__MODE` — the agent self-authorization opt-in.** Default OFF: a human
`hive_write(approved_by=…)` is the only path to `established` trust and the only authority to retire
(`hive_supersede`) or prune (`hive_prune`) a memory. Set it `true` only to deliberately delegate
that per-write vouch to the fleet — an agent may then self-authorize with `approved_by="AGI_OVERRIDE"`,
stamped byte-distinguishably in the audit (`provenance=agent_reasoned`). It loosens a safety gate, so
it stays off unless you mean it.

**`HIVE_SECRET_SCAN__ENABLED` — the credential secret floor.** Default ON: every memory is scanned
for credentials *before* it is persisted, and a detected secret is refused (nothing is stored). Set
it `false` only to deliberately **bypass** that scan — raw text, secrets included, is then written
unscanned into the fleet-shared store. It loosens a safety gate, so the default is the safe posture
(floor on) and disabling is opt-in; a disabled floor is **not silent** — it logs a loud WARN at boot
and surfaces as `secret_scan_disabled` in `hive_health`. Leave it ON unless you have a specific
reason (e.g. a trusted corpus that legitimately contains secret-shaped tokens) and accept the risk.

## 5. Day-2 operations

Everything runs through the `hive` CLI (it drives Docker Compose for you):

| Command | Does |
|---|---|
| `hive ui` | open the loopback operator dashboard in your browser — live status, seat mint/revoke, backup, safe start/stop, log tail (loopback-only, tokenless; no reset/restore) |
| `hive status` | server health, tunnel state + URL, seat count |
| `hive logs [svc]` | follow the daemon (or `ngrok`) logs |
| `hive tokens` | list provisioned seat labels (never the tokens) |
| `hive token <seat>` | mint a per-seat token (printed once) |
| `hive revoke <seat>` | offboard a seat (next request → 401) |
| `hive backup` | snapshot the store now — manual (no scheduler); keeps the `backup_keep` most-recent |
| `hive ingest <receipt.json>` | feed a signed census receipt's change outcome into the append-only evidence ledger (idempotent; refused receipts write zero rows; `--post-merge --verdict pass\|fail --signal randomized\|canary\|none` for rollout outcomes) |
| `hive down` | stop the stack, preserve the `hive-data` volume |
| `hive reset` | snapshot the store out to the host, then destroy + recreate it empty (recoverable; typed confirm) |
| `hive restore <snap>` | replace the live store from a snapshot (the inverse of reset; typed confirm) |

## 6. KPIs to watch

The store earns its value only if it stays lean and current. Watch these read-only signals over MCP
on a fixed cadence (weekly suffices for a small team) — they are the only window into silent
fail-open rot:

| Call | Tells you | Act on |
|---|---|---|
| `hive_health(include_trends=true)` | `confident_rate` + `demand_entropy` (current vs prior 7d, with deltas) | `tau_serve`, `demand_m` |
| `hive_health(include_gaps=true)` | topics wanted but uncovered (clustered demand gaps) | `hive_write` the answers |
| `hive_health(include_conflicts=true)` | near-duplicate / contradicting memories + agent advisories | `hive_supersede` |
| `hive_health(include_suspect_consensus=true)` | provisionals promoted on thin effective independence | human audit / retire |

Highest-leverage operator moves: run agents as **distinct sessions** (that diversity is what
promotes good captures); **keep the store small** — let unused memory expire rather than lengthening
TTLs to hoard; and spend human review on the **established tier** (`hive_write` a good answer,
`hive_supersede` a stale one) — that is the only thing that fights the dominant long-run decay.

## 7. Security posture

- Every memory is **scanned for secrets before persistence** by default (findings log rule names,
  never the matched bytes; the default action is refuse). The floor is on unless an operator opts
  out with `HIVE_SECRET_SCAN__ENABLED=false`, which is logged at boot and shown in `hive_health`.
- The server image is **hermetically offline** — a runtime model or dependency download is impossible.
- Only the **loopback door** is host-published; remote access is **always bearer-gated** by
  construction (the tunnel door binds token-required, and ngrok forwards only to it).
- Schema upgrades are a recoverable `hive reset`, never an in-place migration.
