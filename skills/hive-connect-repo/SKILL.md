---
name: hive-connect-repo
description: "Arm and test the server-side census sync against a GitHub repo: walk through the prerequisites (repo URL, a read-only token for a private remote, a sync-capable server image), auto-detect the default branch, write the HIVE_SYNC__* config, restart to load it, and verify the change-outcome feed is live. Use when asked to connect / arm / wire the server to a repo, turn on the automatic census feed, set HIVE_SYNC__REPO_URL, or test whether the change-outcome feed is working."
---

# hive-connect-repo — arm & test the automatic census feed

Wire a **running** server to a GitHub repo so it feeds tracked-branch landings and PR outcomes
into the change-outcome ledger automatically. This is the runbook for `HIVE-ADMIN.md` §4's arming.
It does **not** connect agents/teammates (that is **hive-connect-team**) or tune an already-armed
feed (that is **hive-operate**); restart mechanics are **hive-bringup**.

The whole connection is **outbound `git`-over-HTTPS with the token in the remote URL** — no `gh`
CLI, no GitHub App, no SSH key, and no per-device git hooks (census is server-side). The server
reaches out to GitHub; nothing inbound is required for the feed.

**CLI resolve (once per shell):** `command -v hive >/dev/null 2>&1 || hive() { python3 -m hive.tools.cli "$@"; }`
— makes every `hive …` line below run on an uninstalled checkout (the CLI is stdlib-only;
Windows shells: `py -m hive.tools.cli <verb>`; prerequisites: **hive-bringup**).

## 1. Preflight — server up, and does the image have the daemon?

```bash
hive status          # or hive_health() over MCP — confirm the server is ok
```

If it is down, bring it up first (**hive-bringup**). The sync daemon shipped in **contract v.13**;
`hive_health()` returns `contract_version`. If it reads below v.13 the running image may predate
the daemon — arming `.env` on such an image is silently ignored. The **definitive** check is step 6
(no `sync` block ever appears); treat a sub-v.13 version as a warning to deploy the sync-capable
image first.

## 2. Gather inputs (walk through anything missing)

- **Repo URL** — the `https://…/ORG/REPO.git` form. An SSH URL (`git@github.com:…`) will **not**
  authenticate; no key is provisioned. HTTPS only.
- **Token — only for a private remote.** Mint a **least-privilege, read-only** GitHub token:
  *Settings → Developer settings → Personal access tokens → Fine-grained* → grant the repo
  **Repository permissions → Contents: Read-only**. That scope also exposes `refs/pull/*/head`,
  which the pre-merge candidate leg fetches. A classic PAT with `repo` scope or a machine-user /
  App-installation token work too — the daemon uses the git transport, not the GitHub API.
- **Do not** set a token (or webhook secret) without the repo URL — that is a half-installed sync
  and the server **fails boot loudly** (`EX_CONFIG`, naming the missing var).

## 3. Auto-detect the default branch (do this yourself)

Probe the remote's `HEAD` — this returns the default branch **and** proves the URL + token
authenticate before anything is armed or restarted:

```bash
# public repo: drop the credential prefix. Keep the token in an env var; do not paste the literal.
git ls-remote --symref "https://x-access-token:${TOKEN}@github.com/ORG/REPO.git" HEAD \
  | sed -n 's#^ref: refs/heads/\(.*\)\tHEAD#\1#p'
# → e.g.  main   (or master, or whatever the repo's default is)
```

If this fails, fix the URL/token now (a `403/401` is a token problem; a network error is egress).
Use whatever branch it reports — never assume `main` vs `master`. Set
`HIVE_CENSUS__CANONICAL_REF` to it: it names the tracked line and scopes the `last_verified`
freshness rider to receipts on that branch (better than the origin-default fallback).

## 4. Write the config to `.env`

`.env` is the single config source (compose loads it via `env_file`) and is gitignored. Add/update
these keys, preserving the rest:

```bash
HIVE_SYNC__REPO_URL=https://github.com/ORG/REPO.git
HIVE_SYNC__TOKEN=<token>                 # omit entirely for a public repo
HIVE_CENSUS__CANONICAL_REF=<detected branch>
```

Never commit the token, never `hive_capture` it, never echo the literal. It will live only in the
mirror's git remote config on the `hive-data` volume, redacted from every log line.

## 5. Restart to load it

Config applies **at boot only** — there is no live reload. Restart the server (**hive-bringup**).
**Confirm first:** connected sessions drop, though all memory persists in the `hive-data` volume.

## 6. Test the connection

```
hive_health(include_census_health=true)
```

The daemon writes a `sync` block once its first tick has run (give it one poll interval, default
**60 s**, then re-check). Read it:

| What `hive_health` shows | Meaning | Action |
|---|---|---|
| `sync` block with `tracked_ref` = your branch **and** `last_tip` set, no fresh `last_error` | **PASS** — connected, mirror cloned, feed live and baselined | Done. (`status: "sync stalled"` may show here — it only means "no landing since arming," not a fault.) |
| No `sync` block at all after ≥1 interval | the daemon isn't running — image predates it, or its start failed | confirm the image is sync-capable (contract v.13+); check boot logs for `sync_start_failed`; deploy the sync-capable image and re-run |
| `sync` block with `last_error`, no `last_tip` | the first clone/fetch faulted | `last_error` (redacted) names the leg: **auth** (bad/expired token, missing Contents:Read) or **unreachable** (bad URL / no egress to GitHub) — fix; the next tick retries |
| boot fails with `EX_CONFIG` naming a var | `TOKEN`/`WEBHOOK_SECRET` set without `REPO_URL` | set `REPO_URL`, or unset the other |

A `change_outcome` row then lands on the **next merge** to the tracked branch (first connect
baselines the current tip silently — no historical receipt). To confirm the full loop end to end,
watch `days_since_last_change_outcome` drop after the next landing, or open a throwaway PR with a
test to exercise the pre-merge candidate leg.

## 7. Optional — push-triggered early wake

The feed is a poll (`HIVE_SYNC__INTERVAL_S`, default 60 s). To wake it immediately on a push:

1. Set `HIVE_SYNC__WEBHOOK_SECRET` in `.env` and run the server with a public tunnel
   (`hive up --tunnel`; needs `NGROK_AUTHTOKEN`/`NGROK_DOMAIN`) — GitHub must reach the server
   inbound for this, unlike the poll.
2. In the repo's **Settings → Webhooks**, add `https://<your-tunnel>/census-webhook`, content-type
   `application/json`, secret = the same value, events = pushes (and PRs).

The handler verifies a constant-time HMAC-SHA256 of the body vs `X-Hub-Signature-256` on the tunnel
door only. It just advances the schedule — the poll interval stays the correctness floor, so this
is latency, never a requirement.

## Load-bearing invariants (do not relearn these the hard way)

- **Unarmed is byte-inert; arming needs a restart.** Unset `REPO_URL` ⇒ no thread, no clone,
  nothing changes. The daemon reads config only at boot.
- **The token lives only in the mirror's git remote config** on the `hive-data` volume — never
  logged, never in a receipt, never in a child process's env. Use a least-privilege read-only
  token; rotate or revoke it freely (the mirror is a rebuildable cache, not the durable truth —
  that is the `sync:last_tip` watermark).
- **HTTPS only.** An SSH remote passes through unauthenticated.
- **Detect-only (O7).** The feed writes evidence through the same door as `hive ingest`; it never
  mutates trust. Connecting a repo cannot, by itself, promote or retire a memory.
- **One repo per server.** `REPO_URL` is a single repo; feed others manually via `hive ingest`.
- **Verified promotion needs PR flow + runnable tests.** The rung's fuel
  (`outcome_verified_helped`) is produced only by the pre-merge PR candidate leg. A repo without
  PRs, or without a runnable test suite (+ `uv.lock` for env provisioning), still feeds coarse
  `change_outcome` rows on landings but yields no verified promotions.

Full reference: `HIVE-ADMIN.md` §4.
