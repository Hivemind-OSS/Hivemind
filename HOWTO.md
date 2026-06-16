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

## 4. Outcome credit — close the loop (one command)

Agents stamp `Hive-Credit: <trace_id> <episode_id> …` trailers on commits that a
recalled memory **materially shaped** (selective credit — nothing if none did).
`hive origin` turns merged/reverted GitHub reality into win/loss credit on exactly
those memories. **Nothing here is load-bearing**: never run it and the system behaves
identically; run it and readiness floors fill toward the keystone gate that can switch
recall ranking to measured utility.

### Setup — one command, from the hive checkout

```bash
hive origin team/repo            # or the github.com URL
```

That one command resolves a token (`--token-stdin` > `$GITHUB_TOKEN`/`$GH_TOKEN` >
`gh auth token` > none = public-repo mode), validates repo access, stores the origin
at `~/.config/hive/origins.json` (0600, token inline — the hourly cron has no env,
the one documented deviation from the env-var rule; `$GITHUB_TOKEN` at sync time
always overrides the stored token), installs ONE marker-tagged `@hourly` crontab
line, and runs a first **90-day backfill** sync. Private repos need a PAT: classic
`repo` scope, or fine-grained **Contents: Read + Pull requests: Read**.

Day-2 verbs: `hive origin sync` (manual sweep over every linked repo),
`hive origin ls` (linked repos — never tokens), `hive origin rm team/repo` (removing
the last origin also strips the cron line). The cron writes to
`~/.config/hive/origin-sync.log`; `--no-cron` at link time skips the install.

### How wins and losses settle (stateless, squash-safe by construction)

Every sync polls the GitHub API over a sliding 14-day window — no mirror, no cursor
state; the idempotent `(commit_sha, episode_id)` ingest makes hourly full-window
re-scans free:

- **Merged PRs** (primary): trailers are harvested from the PR's constituent commits
  (`/pulls/N/commits` — GitHub keeps `refs/pull/N/head` even after branch deletion),
  so **squash-merge is safe by construction** — no repo-settings dance, no trailer
  discipline. The win is keyed to the merge commit, timestamped `merged_at`.
- **Direct pushes** to the default branch credit their own sha; a rebase-merge twin
  (the same trace ids under rewritten shas) is dropped by trace-claim dedup and
  counted `deduped_rebase_copies` — never double credit.
- **Reverts**: `This reverts commit <sha>` flips that sha's wins to losses —
  **one-way and monotone**, so a later re-scan re-offering the win can never undo a
  revert (a revert-of-revert re-lands under a new sha and credits fresh).

Selective credit is enforced server-side: ingest credits `claimed ∩ actually-served-
on-that-trace`; ids never served on the named trace are counted (`unserved_claims`)
and written nowhere — credit can't be gamed or fat-fingered into the ledger.

### Operational notes

- **GitHub only.** Local-only or other-host repos have no credit loop in this build
  (the old mirror scanner is recoverable from git history if ever needed).
- **Outage longer than the window?** Wins that settled more than 14 days before the
  next sync are missed by the hourly line — backfill once with
  `hive origin sync --lookback 90` (any day count). The loop is non-load-bearing, so
  a gap costs only credit freshness.
- **Old trailers**: pre-v2 `Hive-Trace:` stamps earn nothing (their wording always
  promised "changes no reward") but every sync counts sightings as
  `legacy_trailers_seen` — a climbing count means a repo still runs the v1 rules
  block; re-run `hive_init` there to onboard the v2 convention.

### What the credit feeds

`task_outcomes` → recall envelopes (every hit carries `"credit": {wins, losses}`) →
readiness floors (200 settled / 30 distinct credited memories) → the keystone 4-arm
causal gate → only a proven WIN flips `utility_rerank`. Crude labels by design —
merge=win, revert=loss — the keystone decides whether that signal beats recency,
never a judge.

## 5. Day-2 operations

| Command | What it tells/does |
|---|---|
| `hive status` | server health, tunnel on/off + URL, seat count |
| `hive trends` | convergence KPIs: current vs previous 14d window + deltas (JSON) |
| `hive logs [svc]` | follow the daemon (or `ngrok`) logs |
| `hive tokens` | provisioned seat labels (never the tokens) |
| `hive revoke <seat>` | offboard a seat (next request → 401) |
| `hive origin sync [--lookback N]` | manual credit sweep (the hourly cron runs this) |
| `hive origin ls` / `rm <repo>` | linked credit origins (never tokens) / unlink |
| `hive down` / `hive nuke` | stop (keep data) / destroy (typed confirm) |

**Tuning the agent knobs:** `hive trends` is the observation surface for a config-tuning
agent. The safe loop — which knobs are tunable, their bounds, and the guarantee firewall —
is in [docs/TUNING-RUNBOOK.md](docs/TUNING-RUNBOOK.md).
