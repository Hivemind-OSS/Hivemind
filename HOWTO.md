# HOWTO — operating Hivemind

The operator's path from clone to a fleet that remembers, for **solo devs and small
teams**. Everything here runs through the `hive` CLI (`pip install -e .` gives the
command; uninstalled, `python -m hive.tools.cli` is identical). Deep references:
`docs/CLIENTS.md` (every client shape), `docs/PLANS/AUTH-PLAN.md` §10 (auth model),
`docs/PLANS/CONVERGENCE-PLAN.md` §8 (outcome credit).

## 1. First-time setup (server host, once)

```bash
cp .env.example .env        # set HIVE_TENANT_ID=<team>  — everything fail-fasts without it
hive up                     # build + start; blocks until the daemon is actually healthy
```

- The daemon serves MCP over HTTP on **127.0.0.1:8765 only**. Public exposure is never
  implicit — see §3.
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

## 4. Outcome credit — close the loop (the part worth doing right)

Agents already stamp `Hive-Trace: <trace_id>` trailers on commits that drew on a
recalled memory. `hive credit` turns merged/reverted reality into win/loss credit on
those memories. **Nothing here is load-bearing**: never run it and the system behaves
identically; run it and readiness floors fill toward the keystone gate that can switch
recall ranking to measured utility.

### The seamless setup: one mirror, one cron, on the server host

Everything ingestible is visible from `main` alone, so nobody's working clone needs
scanning — teammates just commit:

```bash
# once per tracked repo (a dedicated scan mirror, NOT a workspace; read-only deploy key)
git clone --mirror git@github.com:team/repo.git ~/hive-scan/repo.git

# cron, daily or weekly
hive credit --fetch ~/hive-scan/repo.git
```

Re-scans are free (idempotent `(commit_sha, episode_id)` ingest), any clone may also
scan without coordination, and local-only repos work too — point `hive credit` at the
repo path on whatever machine has both the repo and server access.

### Make every credit catchable — the squash-merge settings detail

Merge commits and rebase-merges carry trailers onto `main` natively. **Squash-merge is
the one flow that destroys them**, and the fix is mechanical, set once per repo:

> **GitHub → Settings → General → Pull Requests → "Default commit message" for squash
> merging → select "Pull request title and commit details".**

That writes the constituent commit messages — trailers included — into the squash
commit on `main`, so credit survives **by construction**, no human discipline needed.
Two backstops for repos where you can't control settings:

1. **PR-description convention**: put the `Hive-Trace:` lines in the PR description
   too — the "title and description" squash default carries it onto main.
2. **The alarm**: every `hive credit` run prints `aged_unsettled` — trailer commits
   older than 14 days that never reached main. A climbing count means trailers are
   leaking (usually a squash-settings regression). Report-only, never a gate.

No double counting either way: only the main-side sha ever settles; feature-branch
originals stay unsettled forever and are never ingested.

### What the credit feeds

`task_outcomes` → readiness floors (200 settled / 30 distinct credited memories) →
the keystone 4-arm causal gate → only a proven WIN flips `utility_rerank`. Crude
labels by design — merge=win, revert=loss — the keystone decides whether that signal
beats recency, never a judge.

## 5. Day-2 operations

| Command | What it tells/does |
|---|---|
| `hive status` | server health, tunnel on/off + URL, seat count |
| `hive logs [svc]` | follow the daemon (or `ngrok`) logs |
| `hive tokens` | provisioned seat labels (never the tokens) |
| `hive revoke <seat>` | offboard a seat (next request → 401) |
| `hive credit --fetch <mirror>` | refresh + scan + ingest outcome credit |
| `hive down` / `hive nuke` | stop (keep data) / destroy (typed confirm) |
