---
name: hive-operate
description: "Operate and tune a running Hivemind server: read the convergence KPIs over MCP, interpret them, turn the recall / safety knobs, manage the synced-repo registry (hive repo add/remove, hive repos), watch the per-repo automatic census feed, and feed manual census receipts into the evidence ledger (hive ingest). Use when asked to check hive health or KPIs, run the weekly maintenance sweep, tune recall or promotion, fix coverage starvation or silent rot, decide demand_m / tau_serve / conflict.tau, register or deregister a synced repo, check the census sync, or ingest a census receipt. Knobs apply at boot — restart to take effect; repo registration does not."
---

# hive-operate — watch the KPIs & turn the knobs

Keep a running server lean and current. The store does not clean itself — **maintenance is the
product.** Full leverage map: `OPERATIONS.md`; knob tables: `HIVE-ADMIN.md` §4 & §6.

**CLI resolve (once per shell):** `command -v hive >/dev/null 2>&1 || hive() { python3 -m hive.tools.cli "$@"; }`
— makes every `hive …` line below run on an uninstalled checkout (the CLI is stdlib-only;
Windows shells: `py -m hive.tools.cli <verb>`; prerequisites: **hive-bringup**).

For a browser view of the live picture, `hive ui` serves a loopback-only operator dashboard (live
status, seat mint/revoke, backup, non-blocking start/stop, tunnel activate/deactivate, restore from
an in-volume backup behind a typed confirm, log tail; no reset) — `--no-open` for a headless host.
The KPIs below stay MCP-only; the dashboard is the docker-side status/lifecycle surface.

## The KPIs — read-only over MCP, off the warm store

Call these from any connected agent (there is no host-side verb):

| Call | Tells you | Act on |
|---|---|---|
| `hive_health(include_trends=true)` | `confident_rate` + `demand_entropy`, current vs prior 7d + deltas | `tau_serve`, `demand_m` |
| `hive_health(include_gaps=true)` | topics wanted but uncovered (misses carry their repo scope) | `hive_write` the answers |
| `hive_health(include_conflicts=true)` | near-duplicate / contradicting memories + agent advisories, bucketed by repo and anchor; established rows with rivals rank above provisional | `hive_supersede` the wrong one |
| `hive_health(include_suspect_consensus=true)` | promotions on thin *effective* independence | re-examine; retire via `hive_supersede` / `hive_prune` |
| `hive_health(include_stale_suspects=true)` | servable memories whose anchor sat in the blast radius of a breaking/removed change | re-verify each against the code; retire the truly stale |
| `hive_health(include_census_health=true)` | `repos` — per registered repo: days since the last `change_outcome` + the `sync` block (`tracked_ref`/`last_tip`/`last_sync_ts`/`last_error`/`backfilled_total`; `status: "no change_outcome evidence yet"` when configured yet dark — a measured fact about EVIDENCE, never a daemon verdict). `fleet` — the daemon's own `last_sync_ts`/`last_error` | read `fleet` FIRST: a `last_error` there (or a frozen `last_sync_ts`) means the daemon is down and every repo block is a stale snapshot that still reads as passing → `hive logs`. Otherwise check the registry (`hive repos`), the token env var, remote reachability |

The **trends window is the only view into silent fail-open rot** — read it on a fixed cadence
(weekly suffices for a small team).

## The weekly sweep

In one pass: read trends → gaps → conflicts → suspect-consensus → stale-suspects; convert each
demand-gap into a `hive_write`, each contested / stale established row into a `hive_supersede`
(or `hive_prune` for the flat-wrong). Retirement is **machine-gated**: the server retires only
when it verifies a qualifying machine signal for the target (anchor drift at the canonical tip,
hurt evidence from another identity or a verified outcome, or — for `hive_supersede` /
`hive_write(replaces=)` — a near-dup winner naming the successor) — an
unqualified call is a benign no-op, never an error. The sweep is operator-paced and there is no
substitute — staleness on the **established** tier is the dominant long-run decay and the
worklists only surface it; resolving it is this pass.

## The knobs (edit `.env`, then `hive up` to restart — no live reload)

| Knob | Default | Move it when |
|---|---|---|
| `HIVE_RECALL__TAU_SERVE` | 0.70 | coverage is starved (gate over-abstains → lower) or weak matches serve (→ raise). Recalibrate on your corpus |
| `HIVE_AUTONOMY__DEMAND_M` | 1 | non-writer misses required — the anti-gaming floor; raise above it if a wrong answer is expensive (there is no lower, safer setting than the floor) |
| `HIVE_CONFLICT__TAU` | 0.80 | distinct facts get merged (→ raise) or near-duplicate twins slip through (→ lower) |
| `HIVE_AUTONOMY__QUARANTINE_TTL_DAYS` / `…__PROVISIONAL_TTL_DAYS` | 14 / 45 | **do not lengthen to hoard** — expiry of unused memory is doing real work |
| `HIVE_SYNC__INTERVAL_S` | 60 | the sync poll cadence (floor 5 s) — the webhook only wakes it early, never replaces it |
| `HIVE_SYNC__WEBHOOK_SECRET` | unset | arm the push-triggered early wake (`POST /census-webhook`, HMAC-gated, tunnel door only) |
| `HIVE_SYNC__WORKERS` | 1 | many registered repos make one serial pass outlast the interval — raise toward the repo count |

There is **no per-tick drift or backfill cap** to tune: the git ladder spawns no engine, costs two
plumbing reads per distinct baseline commit, and reconverges a repo's whole anchor set in one tick
however many anchors it has. A leftover `HIVE_SYNC__DRIFT_PER_TICK` / `…__BACKFILL_PER_TICK` in an
`.env` logs `config.env_unknown_field` at boot and is ignored.

**Which repos the sync feeds is not a knob** — it is the durable repo registry, changed live with
`hive repo add/remove` (next section), no restart.

The three sync capacity knobs bound **throughput only** and can never change a verdict: a tick
that runs out of budget leaves the remainder for the next one, and an un-materialized anchor
reads `unverifiable` — never a false `fresh`. Under-provisioning costs coverage *latency*, so
the symptom to watch for is recall hits reading `unverifiable` on a repo you know is synced.

## The synced-repo registry (`hive repo`)

```bash
hive repo add <url> [--name <slug>] [--branch <ref>] [--token-env <ENVNAME>]
hive repo remove <name>
hive repos            # list: name  url  branch  token-env  ('-' = default)
```

- Registry rows are **operational data in the store**, re-read by the sync daemon every tick —
  registering or deregistering needs **no restart**. `remove` stops the feed and prunes the
  mirror next tick; the repo's episode scope rows are kept (a re-registered repo picks its
  memories straight back up).
- `--token-env` is the **NAME** of the env var holding that repo's git token — never a secret
  value; no secret byte is ever stored or printed. Unset ⇒ the fleet default `HIVE_SYNC__TOKEN`
  at tick time; that too absent ⇒ anonymous (public repos). A registered name absent from the
  server's environment fails the next boot fast (`EX_CONFIG`, naming the var).
- The full register-and-verify runbook (inputs, preflight probe, the per-repo health table) is
  **hive-connect-repo**.

## Feeding change outcomes (`hive ingest`)

`hive ingest <receipt.json>` feeds an unsigned census receipt's change outcome into the append-only
evidence ledger (the receipt is piped to the in-container censusctl over stdin). Censusctl validates
the DSSE-shaped receipt (shape + predicate digest; a refused receipt writes **zero** rows, exit 65),
derives the verdict server-side (pre-merge: decided execution lines only — a receipt with nothing
decided refuses), joins the receipt's touched `path::Symbol` subjects against episode anchors, and
appends one `change_outcome` row per matched episode in one transaction — plus, pre-merge under a
full version stamp, the mechanical `outcome_verified_*` / `verify_*` rider rows and the advisory
`stale_suspect` rows for the receipt's optional propagation block. Idempotent — re-ingesting the
same receipt reports `already_recorded` and adds nothing — and trust-untouched (evidence only;
recall serves the same bytes). Post-merge rollout outcomes ride flags:

```bash
hive ingest receipt.json                                        # pre-merge: verdict derived
hive ingest receipt.json --post-merge --verdict fail --signal canary   # rollout outcome
```

`--signal randomized|canary` is what makes a post-merge outcome machine-checked (anything else
records unverified judgment). The one-line JSON report on stdout carries
`inserted/already_recorded/matched/range_skipped/skipped_lines` and the
`verified_helped/verified_hurt/stale_suspects` rider counters
(`range_skipped: true` = the exact `(repo, base, head, phase)` range was already in the ledger —
the whole receipt was skipped before any row work).
Receipts are unsigned by policy — there is no signature to check; integrity rides the subject
digest, which the ingest door re-checks against the predicate.

### The automatic feed (server-side sync)

With repos registered (`hive repo add`), the server closes this loop itself — no operator receipt
handling, no per-repo or per-device wiring in the path. Each poll tick (`HIVE_SYNC__INTERVAL_S`,
default 60 s) it re-reads the registry and, per repo against a local mirror:

- **feeds the ledger** — ONE unsigned receipt per new `watermark..tip` range on the tracked
  branch (the row's `--branch`, else the origin default), verdict derived server-side, ingested
  through the same door as `hive ingest`; the server's mechanical promotion sweep runs after each
  ingest (outcome-verified on the canonical line ⇒ established);
- **baselines anchors** — a stored binding with no baseline commit yet gets one at the tip the
  server can observe (the ordinary baseline is recorded at write time from the repo's watermark,
  and a recorded one is never moved);
- **materializes drift** — per-anchor fresh/stale verdicts at the canonical tip (and
  recall-demanded branch tips), asked of git against each binding's own baseline: what stamps a
  recall hit `fresh` vs drifted, and what commit SHAs ride a stale one as evidence.

A push webhook (`HIVE_SYNC__WEBHOOK_SECRET`, HMAC-gated on the tunnel door) only wakes the poll
early — one nudge wakes the loop for ALL registered repos; the interval stays the correctness
floor. Every leg is fail-open **per repo** (a faulting repo never touches the other repos or the
serve path), and the store's durable range ledger skips any exact `(repo, base, head, phase)`
range already ingested (`range_skipped` in the report), so the manual and automatic feeds can
never double-count a range. Watch it with `hive_health(include_census_health=true)` (the KPI
table above); register/deregister with the registry verbs above.

## Posture (why)

Usefulness is a **flow, not a stock**: a bigger store is a *worse* one — past an optimum, dilution
and staleness outrun the fixed recall bandwidth. Keep it small, let unused memory expire, run agents
as **distinct sessions** (that diversity is what promotes good captures), and spend scarce operator
review on the established tier. See `OPERATIONS.md` for the evidence behind each lever.
