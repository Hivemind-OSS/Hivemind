---
name: hive-connect-repo
description: "Register a repo with the server-side census sync: `hive repo add <url>` writes the durable repo registry (the sync daemon picks it up on its next tick — no restart), private remotes authenticate via a token env var NAME (never a stored secret), and `hive_health(include_census_health=true)` verifies the per-repo change-outcome feed is live. Use when asked to connect / register / wire a repo to the hive, turn on the automatic census feed, add or remove a synced repo, list registered repos, or test whether the change-outcome feed is working."
---

# hive-connect-repo — register a repo & test the census feed

Register git repos with a **running** server so the sync daemon feeds tracked-branch landings
into the change-outcome ledger — and judges anchor drift — automatically, per repo. One store,
partitioned by repo: register each repo the fleet works in. This skill does **not** connect
agents/teammates (that is **hive-connect-team**) or tune the sync loop's knobs (that is
**hive-operate**).

The whole connection is **outbound `git`-over-HTTPS with the token resolved from an env var** —
no `gh` CLI, no GitHub App, no SSH key, and **nothing installed in the consuming repo** (census
and drift are server-side; agents are thin, repo-agnostic MCP clients). The server reaches out
to the remote; nothing inbound is required for the feed.

**CLI resolve (once per shell):** `command -v hive >/dev/null 2>&1 || hive() { python3 -m hive.tools.cli "$@"; }`
— makes every `hive …` line below run on an uninstalled checkout (the CLI is stdlib-only;
Windows shells: `py -m hive.tools.cli <verb>`; prerequisites: **hive-bringup**).

## 1. Preflight — server up

```bash
hive status          # or hive_health() over MCP — confirm the server is ok
```

If it is down, bring it up first (**hive-bringup**).

## 2. Gather inputs (walk through anything missing)

- **Repo URL** — the `https://…/ORG/REPO.git` form. An SSH URL (`git@github.com:…`) will **not**
  authenticate; no key is provisioned. HTTPS only. A **public** repo needs no token (the fetch
  runs anonymous).
- **Token — only for a private remote.** Mint a **least-privilege, read-only** GitHub token:
  *Settings → Developer settings → Personal access tokens → Fine-grained* → grant the repo
  **Repository permissions → Contents: Read-only**. A classic PAT with `repo` scope or a
  machine-user / App-installation token work too — the daemon uses the git transport, not the
  GitHub API. Put the **value** in an env var in the server's `.env`; the registry gets only the
  var's **NAME** (`--token-env`). Unset, the daemon falls to the fleet-default `HIVE_SYNC__TOKEN`
  at tick time; that too absent ⇒ anonymous.
- **Branch — optional.** Unset, the daemon tracks the remote's **origin default branch**. Pass
  `--branch <ref>` only to pin a different tracked line.

Optional preflight — prove the URL + token authenticate before registering anything:

```bash
# public repo: drop the credential prefix. Keep the token in an env var; never paste the literal.
git ls-remote --symref "https://x-access-token:${TOKEN}@github.com/ORG/REPO.git" HEAD
# a 401/403 is a token problem; a network error is egress — fix before step 3
```

## 3. Register it

```bash
hive repo add https://github.com/ORG/REPO.git                          # name defaults to REPO
hive repo add <url> --name alpha --branch main --token-env ALPHA_TOKEN # explicit everything
hive repos                                # list: name  url  branch  token-env  ('-' = default)
hive repo remove <name>                   # deregister (stops the feed)
```

- **Registration is operational data, not boot config**: the row lands in the store's durable
  repo registry and the sync daemon re-reads the registry every tick
  (`HIVE_SYNC__INTERVAL_S`, default 60 s) — **no restart**, in either direction.
- `--name` is the registry slug (`[a-z0-9._-]+`; default: the URL basename). It is the name
  agents scope memories by (`repos=["alpha"]`, `anchors=[{repo, anchor}]`). An invalid
  derivation is refused with a message pointing at `--name`; nothing is written.
- `--token-env` takes the env var **NAME only — never a secret value**; the row stores no secret
  byte. A registered row naming a var absent from the server's environment fails the **next
  boot** fast (`EX_CONFIG`, naming the var), and that repo's tick fails open until it is set.
- `remove` stops the feed and prunes the server-side mirror next tick; the repo's episode
  **scope rows are kept**, so a re-registered repo picks its memories straight back up.

What the daemon then does, per registered repo, each tick:

- **feeds the ledger** — ONE unsigned receipt per new `watermark..tip` range on the tracked
  branch, verdict derived server-side, ingested through the same door as `hive ingest`; the
  server's mechanical promotion sweep runs after each ingest;
- **backfills fingerprints** — anchor fingerprints absent on stored memories are minted
  server-side against the mirror;
- **materializes drift** — per-anchor fresh/stale verdicts at the canonical tip (and
  recall-demanded branch tips): what stamps a recall hit `fresh` vs drifted.

## 4. Test the connection

```
hive_health(include_census_health=true)
```

`census_health` carries **one block per registered repo**, keyed by registry name. Give the
daemon one poll interval (default **60 s**), then read your repo's block:

| What the repo's block shows | Meaning | Action |
|---|---|---|
| `sync` sub-block with `tracked_ref` + `last_tip` set, no fresh `last_error` | **PASS** — mirror cloned, feed live and baselined | Done. (`status: "sync stalled"` may show — it only means "no `change_outcome` row for this repo yet," not a fault.) |
| no block for the repo at all | the repo is not registered (or the name differs) | `hive repos` — check the slug; re-run `hive repo add` |
| block present, no `sync` sub-block after ≥1 interval | the daemon has not completed a tick for it | check `hive logs` for that repo's sync errors; wait one more interval |
| `sync` with `last_error`, no `last_tip` | the first clone/fetch faulted | `last_error` (redacted) names the leg: **auth** (bad/expired token, missing Contents: Read) or **unreachable** (bad URL / no egress) — fix; the next tick retries |
| boot fails `EX_CONFIG` naming an env var | a registered `--token-env` var is unset on the server | set the var in `.env`, or re-register without it |

A `change_outcome` row then lands on the **next landing** on the tracked branch (first sync
baselines the current tip silently — no historical receipt). To confirm the loop end to end,
watch the repo's `days_since_last_change_outcome` drop after the next landing.

## 5. One-time cleanup — leftovers of the old per-repo contract

Connecting a repo installs **nothing in the repo**: the usage contract reaches every agent over
MCP at connect, served fresh each session. If the consuming repo still carries artifacts of the
old contract, delete them once:

- the marker-fenced **HIVEMIND-RULES** block in the repo's rules file (`CLAUDE.md` /
  `AGENTS.md`),
- any hivemind lifecycle hooks and any `hive_*` auto-approve allowlist entries in the repo's
  `.claude/settings.json`.

Nothing replaces them — the served contract is the whole client-side surface.

## 6. Optional — push-triggered early wake

The feed is a poll (`HIVE_SYNC__INTERVAL_S`, default 60 s). To wake it immediately on a push:

1. Set `HIVE_SYNC__WEBHOOK_SECRET` in `.env` — this one IS boot config (restart to load,
   **hive-bringup**), unlike registration — and run the server with a public tunnel
   (`hive up --tunnel`; needs `NGROK_AUTHTOKEN`/`NGROK_DOMAIN`): GitHub must reach the server
   inbound for this, unlike the poll.
2. In the repo's **Settings → Webhooks**, add `https://<your-tunnel>/census-webhook`,
   content-type `application/json`, secret = the same value, events = pushes.

The handler verifies a constant-time HMAC-SHA256 of the body vs `X-Hub-Signature-256` on the
tunnel door only. One nudge wakes the loop for ALL registered repos, and it only advances the
schedule — the poll interval stays the correctness floor, so this is latency, never a
requirement.

## Load-bearing invariants (do not relearn these the hard way)

- **An empty registry is inert; registration needs no restart.** No registered repo ⇒ no git, no
  clone, nothing runs. Only the loop's own knobs (`HIVE_SYNC__INTERVAL_S`,
  `HIVE_SYNC__WEBHOOK_SECRET`) are boot config.
- **Secrets ride env-var indirection: names in rows, values in env.** The token value lives only
  in the server's environment and the mirror's git remote config on the `hive-data` volume —
  never in a registry row, never logged (redacted), never in a receipt. Rotate or revoke it
  freely: the mirror is a rebuildable cache, the durable truth is the per-repo watermark
  (`sync:<name>:last_tip`).
- **HTTPS only.** An SSH remote passes through unauthenticated.
- **Evidence-first; trust moves only mechanically.** The feed writes `change_outcome` evidence
  through the same door as `hive ingest`; the only trust movement is the server's own
  post-ingest promotion sweep (outcome-verified on the canonical line ⇒ established).
  Registering a repo can never inject an instruction into recall.
- **Many repos, one server.** One store partitioned by repo: register each repo, and every
  memory, miss, and receipt carries its repo scope.
