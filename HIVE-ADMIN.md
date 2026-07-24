# HIVE-ADMIN — operating the Hivemind MCP server

The administrator's reference for standing up a Hivemind server, connecting a fleet,
registering the repos it works on, tuning the safety/recall knobs, and running it day-2.
Everything here goes through the `hive` CLI
(`pip install -e .` provides the command; uninstalled, `python3 -m hive.tools.cli` is identical).
For *what Hivemind is* and the agent-facing memory contract, see `llms-full.txt` (the complete
self-contained guide; `llms.txt` is the short link index); for the quickstart,
`README.md`. Agent-runnable runbook-skills for the procedures below live in `skills/`
(`skills/README.md`).

## 1. Prerequisites & first-time setup

On the server host: **Docker** + **Docker Compose v2**, and **Python 3.11+** (for the `hive` CLI,
which drives Compose). Then:

```bash
git clone https://github.com/Hivemind-OSS/Hivemind.git hivemind && cd hivemind
pip install -e .            # installs the `hive` command (venv on PEP-668 systems; or skip — python3 -m hive.tools.cli is identical)
cp .env.example .env        # persist the store across restarts (sets HIVE_STORE__DB_PATH)
hive up                    # build + start; blocks until the daemon is healthy
```

- **Zero config to boot** — the store DEFAULTS to `/data/shared.db` in the `hive-data` volume (persistent); only an explicit `HIVE_STORE__DB_PATH=:memory:` boots ephemeral. Copy
  `.env.example` to `.env` to **persist memory across restarts** (it sets
  `HIVE_STORE__DB_PATH=/data/shared.db`), and for any other deliberate override (§4) or the ngrok
  tunnel secrets (§3).
- **A persistent store lives in the `hive-data` Docker volume** once `HIVE_STORE__DB_PATH` points
  at it (`/data/shared.db`); **only an explicit `HIVE_STORE__DB_PATH=:memory:` boots it ephemeral (lost on restart)** — an
  ephemeral boot WARNs loudly and `hive_health` reports `store_ephemeral`. `hive down` preserves
  the volume; `hive reset` snapshots it out to the host and recreates it empty (recoverable — §5).
- **Schema upgrades are not in-place.** This build refuses old-format tables at boot (no silent
  migration). If `hive up` crash-loops after pulling a new build, the volume predates the current
  schema — run `hive reset` for a clean store, then re-register your repos (`hive repo add`, §4).
  Reset snapshots the prior store to the host first, so the upgrade is recoverable
  (`hive restore <snapshot>` rolls it back).
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

Agents are **thin, repo-agnostic MCP clients** — the registration line above is the entire
per-agent setup. The usage contract is delivered over MCP at connect (the `initialize`
instructions, which every client surfaces), fresh every time, so a session picks up a changed
contract on its next reconnect. Nothing is written into a rules file, no hooks or allowlists are
installed, and there is no per-project or per-device tooling to maintain; the operator installs
nothing on any workstation.

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
claude mcp add --transport http hive https://<your-domain>/mcp \
  --header "Authorization: Bearer <seat-token>"   # replace <seat-token> with the seat's token
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
boot loudly** (it never silently clamps); an env key naming an unknown group or field is ignored
with a WARN (so a leftover key from an older config is never a live switch). The defaults are a
conservative starting point — recalibrate the empirical floors (`tau_serve`, `conflict.tau`,
`demand_m`) against your real corpus and query distribution.

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
| `HIVE_AUTONOMY__VERIFIED_PROMOTION` | `true` | the verified-outcome rung out of quarantine: a memory carrying a SHA-bound verified win promotes at the next demand tick (competitor veto retained); `false` makes the rung unreachable |
| `HIVE_AUTONOMY__ANOMALY_TAU` | `0.95` | compact-cluster cosine floor for the flood-anomaly flag (detection-only) |
| `HIVE_AUTONOMY__ANOMALY_MIN_CLUSTER` | `5` | ≥ this many near-identical neighbors ⇒ flag the promotion (it never blocks it) |

**Conflict & suspect-consensus — review worklists (detection only)**

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

**Per-repo census sync — the repo registry + the automatic change-outcome feed**

Which repos the sync daemon feeds is **operational data, not boot config**: register them and the
daemon (always running) picks the change up on its next tick — no restart, and an empty registry
is an inert tick (no git, no clone).

```bash
hive repo add <url> [--name <slug>] [--branch <ref>] [--token-env <ENVNAME>]
hive repo remove <name>
hive repos
```

Rows land in the store's `repos` table. `--name` defaults to the URL basename (slug grammar
`[a-z0-9._-]+`); `--branch` is the canonical branch (default: the origin default branch);
`--token-env` is the **NAME** of an env var holding that repo's git token — the name is stored,
never the token, and unset it falls back to the fleet-default `HIVE_SYNC__TOKEN`, resolved from
the environment at tick time. At boot the entrypoint probes that every registered credential var
is present and fails loudly (`EX_CONFIG`, naming the vars) if one is missing. Removing a repo
stops the feed and prunes its mirror next tick; episode scope rows are kept, so a re-registered
repo picks its memories straight back up.

Per registered repo (each under its own fail-open guard) the daemon mirrors the remote at
`<mirror_dir>/<name>/` and runs three legs: it feeds every landing on the canonical branch into
the change-outcome evidence ledger (one unsigned receipt per new watermark..tip range, ingested
post-merge, then the verified-outcome `established` sweep runs), backfills missing anchor
fingerprints against the canonical tip, and materializes per-anchor staleness (drift) verdicts —
at the canonical tip plus any branch tips recall demanded. Every leg is fail-open: an unreachable
remote or a broken leg skips that repo's tick and the next tick retries; the other repos are
untouched. The loop's own knobs:

| Env var | Default | Controls |
|---|---|---|
| `HIVE_SYNC__INTERVAL_S` | `60` | poll cadence in seconds (floor 5) |
| `HIVE_SYNC__WEBHOOK_SECRET` | *(unset)* | arms `POST /census-webhook` on the tunnel door (constant-time HMAC-SHA256 vs `X-Hub-Signature-256`) — a push wakes the poll early for ALL registered repos; the interval stays the correctness floor |
| `HIVE_SYNC__MIRROR_DIR` | `/data/sync/mirror` | base dir for the per-repo mirrors (`<dir>/<name>` — rebuildable caches inside the `hive-data` volume) |

No compose change is needed: knobs ride `.env`, the registry rides the store, and the mirrors
live in the existing volume. Watch the feed via `hive_health(include_census_health=true)` — a
per-repo census/sync block (§6). Arm and test a repo with the runnable **`hive-connect-repo`**
skill (`skills/hive-connect-repo/SKILL.md`).

**Fixed by the image (not runtime knobs).** The embedder is **Qwen3-Embedding-0.6B**, baked in at
build and run fully offline (`HF_HUB_OFFLINE=1`) — it emits a native 1024-dim vector and there is no
dimension knob; changing the model means rebuilding the image. The in-container store path is
`/data/shared.db`; you manage the `hive-data` volume, not the path.

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
| `hive ui` | open the loopback operator dashboard in your browser — live status, seat mint/revoke, backup, non-blocking start/stop, tunnel activate/deactivate, restore from an in-volume backup (guarded, typed confirm), log tail (loopback-only, tokenless; no reset) |
| `hive status` | server health, tunnel state + URL, seat count |
| `hive logs [svc]` | follow the daemon (or `ngrok`) logs |
| `hive tokens` | list provisioned seat labels (never the tokens) |
| `hive token <seat>` | mint a per-seat token (printed once) |
| `hive revoke <seat>` | offboard a seat (next request → 401) |
| `hive repo add <url>` | register a repo for the server-side census sync (picked up next tick; `--name` / `--branch` / `--token-env` — §4) |
| `hive repo remove <name>` | deregister a repo (stops feeding; the mirror is pruned next tick, scope rows kept) |
| `hive repos` | list registered repos (names + urls; never a secret) |
| `hive backup` | snapshot the store now — manual (no scheduler); keeps the `backup_keep` most-recent |
| `hive ingest <receipt.json>` | feed an unsigned census receipt's change outcome into the append-only evidence ledger — the MANUAL escape hatch (the sync daemon feeds every registered repo itself, §4); idempotent (an already-ingested `(repo, base, head, phase)` range is skipped whole, reported `range_skipped`); refused receipts write zero rows; `--post-merge --verdict pass\|fail --signal randomized\|canary\|none` for rollout outcomes |
| `hive down` | stop the stack, preserve the `hive-data` volume |
| `hive reset` | snapshot the store out to the host, then destroy + recreate it empty (recoverable; typed confirm) |
| `hive restore <snap>` | replace the live store from a snapshot (the inverse of reset; typed confirm) |
| `hive upgrade [--ref release]` | move the server to a vetted release ref — snapshot → checkout → rebuild → health-gate → auto-rollback on failure (backup-gated; §8) |

## 6. KPIs to watch

The store earns its value only if it stays lean and current. Watch these read-only signals over MCP
on a fixed cadence (weekly suffices for a small team) — they are the only window into silent
fail-open rot:

| Call | Tells you | Act on |
|---|---|---|
| `hive_health(include_trends=true)` | `confident_rate` + `demand_entropy` (current vs prior 7d, with deltas) | `tau_serve`, `demand_m` |
| `hive_health(include_gaps=true)` | topics wanted but uncovered (clustered demand gaps; each names the repos that asked) | `hive_write` the answers |
| `hive_health(include_conflicts=true)` | near-duplicate / contradicting memories + agent advisories, bucketed by repo and anchor | `hive_supersede` |
| `hive_health(include_suspect_consensus=true)` | provisionals promoted on thin effective independence | re-examine / retire |
| `hive_health(include_stale_suspects=true)` | servable memories whose anchor sat in the blast radius of a breaking change | re-verify against the code, then `hive_supersede` / `hive_prune` |
| `hive_health(include_census_health=true)` | per-repo census/sync block for every registered repo — days since the last `change_outcome`, sync state (`last_tip`, `last_error`; a dark feed reads null) | check the registry row / remote reachability (§4) |

Highest-leverage operator moves: run agents as **distinct sessions** (that diversity is what
promotes good captures); **keep the store small** — let unused memory expire rather than lengthening
TTLs to hoard; and **seed and clean the served tier** — `hive_write` a good answer (it serves
immediately; verified outcomes on the canonical line establish it) and sweep the conflict and
stale-suspect worklists into retirement calls (the server retires only what a machine signal
qualifies) — that is what fights the dominant long-run decay.

## 7. Security posture

- Every memory is **scanned for secrets before persistence** by default (findings log rule names,
  never the matched bytes; the default action is refuse). The floor is on unless an operator opts
  out with `HIVE_SECRET_SCAN__ENABLED=false`, which is logged at boot and shown in `hive_health`.
- The repo registry stores **no secret bytes** — only the names of token env vars; per-repo git
  credentials resolve from the environment at tick time and live only in the mirror's git config.
- The server image is **hermetically offline** — a runtime model or dependency download is impossible.
- Only the **loopback door** is host-published; remote access is **always bearer-gated** by
  construction (the tunnel door binds token-required, and ngrok forwards only to it).
- Schema upgrades are a recoverable `hive reset`, never an in-place migration.

## 8. Staying current — server upgrades

There is exactly one install target: the **server**. Agents and workstations install nothing —
they are thin MCP clients (§2), and the anchor/census engines the sync daemon runs are
**first-party subpackages of the one `hive` distribution** (`hive/matrix`, `hive/combdrift`,
`hive/edge`), built into the server image straight from this repository: there is no wheelhouse
to refresh and no separate engine repository, and PyPI is not an install or publish channel for
our distribution. The engines' own third-party dependencies (tree-sitter grammars, networkx,
sqlglot) resolve through `uv lock` / `uv sync` like any other dependency. A contract change needs
no fleet action either: each session receives the served usage contract fresh at its next connect.

**Server upgrade / rollback.** Move the server to a vetted ref:

```bash
hive upgrade                    # → release; aborts on a dirty tree (no implicit stash)
hive upgrade --ref <old-tag>    # roll the server back, same backup-gated path
```

`hive upgrade` mirrors `reset`'s snapshot-first discipline: it snapshots the store to the host
**before** any checkout (the recoverable safety net), then checks out the ref → rebuilds → waits for
health → gates on the app status. On any post-checkout failure it **auto-reverts** — restoring the
code (`git checkout <prev>`) and the store (the just-taken snapshot), then rebuilding — and if that
revert itself fails it prints the exact manual `git checkout` + `hive restore <snap>` recovery.
Because the build **refuses an old-format store** rather than migrating it (§1), a release that
changes the schema comes up unhealthy and auto-rolls-back; cross a schema change with `hive reset`
(a clean store — then `hive repo add` each repo) or a restore from backup, not `hive upgrade`.
