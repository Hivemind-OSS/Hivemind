---
name: hive-operate
description: "Operate and tune a running Hivemind server: read the convergence KPIs over MCP, interpret them, turn the recall / safety knobs, and feed census change-outcome receipts into the evidence ledger (hive ingest). Use when asked to check hive health or KPIs, run the weekly maintenance sweep, tune recall or promotion, fix coverage starvation or silent rot, decide demand_m / tau_serve / conflict.tau, or ingest a census receipt. Knobs apply at boot — restart to take effect."
---

# hive-operate — watch the KPIs & turn the knobs

Keep a running server lean and current. The store does not clean itself — **maintenance is the
product.** Full leverage map: `OPERATIONS.md`; knob tables: `HIVE-ADMIN.md` §4 & §6.

For a browser view of the live picture, `hive ui` serves a loopback-only operator dashboard (live
status, seat mint/revoke, backup, non-blocking start/stop, tunnel activate/deactivate, restore from
an in-volume backup behind a typed confirm, log tail; no reset) — `--no-open` for a headless host.
The KPIs below stay MCP-only; the dashboard is the docker-side status/lifecycle surface.

## The KPIs — read-only over MCP, off the warm store

Call these from any connected agent (there is no host-side verb):

| Call | Tells you | Act on |
|---|---|---|
| `hive_health(include_trends=true)` | `confident_rate` + `demand_entropy`, current vs prior 7d + deltas | `tau_serve`, `demand_m` |
| `hive_health(include_gaps=true)` | topics wanted but uncovered; established rows with rivals | `hive_write` the answers; `hive_supersede` the wrong one |
| `hive_health(include_conflicts=true)` | near-duplicate / contradicting memories + agent advisories | `hive_supersede` |
| `hive_health(include_suspect_consensus=true)` | promotions on thin *effective* independence | human audit / retire |

The **trends window is the only view into silent fail-open rot** — read it on a fixed cadence
(weekly suffices for a small team).

## The weekly sweep

In one pass: read trends → gaps → conflicts → suspect-consensus; convert each demand-gap into a
`hive_write`, each contested / stale established row into a `hive_supersede`. This is human-paced and
there is no substitute — staleness on the **established** tier is the dominant long-run decay and
nothing cleans it automatically.

## The knobs (edit `.env`, then `hive up` to restart — no live reload)

| Knob | Default | Move it when |
|---|---|---|
| `HIVE_RECALL__TAU_SERVE` | 0.70 | coverage is starved (gate over-abstains → lower) or weak matches serve (→ raise). Recalibrate on your corpus |
| `HIVE_AUTONOMY__DEMAND_M` | 3 | your coverage↔safety dial — raise if a wrong answer is expensive, lower if a miss costs more |
| `HIVE_CONFLICT__TAU` | 0.80 | distinct facts get merged (→ raise) or near-duplicate twins slip through (→ lower) |
| `HIVE_AUTONOMY__QUARANTINE_TTL_DAYS` / `…__PROVISIONAL_TTL_DAYS` | 14 / 45 | **do not lengthen to hoard** — expiry of unused memory is doing real work |
| `HIVE_AGI__MODE` | false | only to deliberately let the fleet self-authorize the human-gated trust actions |

## Feeding change outcomes (`hive ingest`)

`hive ingest <receipt.json>` feeds an unsigned census receipt's change outcome into the append-only
evidence ledger. The in-container censusctl validates the DSSE-shaped receipt (shape + predicate digest;
a refused receipt writes **zero** rows, exit 65), derives the verdict server-side (pre-merge:
decided execution lines only — a receipt with nothing decided refuses), joins the receipt's
touched `path::Symbol` subjects against episode anchors, and appends one `change_outcome` row per
matched episode in one transaction. Idempotent — re-ingesting the same receipt reports
`already_recorded` and adds nothing — and trust-untouched (evidence only; recall serves the same
bytes). Post-merge rollout outcomes ride flags:

```bash
hive ingest receipt.json                                        # pre-merge: verdict derived
hive ingest receipt.json --post-merge --verdict fail --signal canary   # rollout outcome
```

`--signal randomized|canary` is what makes a post-merge outcome machine-checked (anything else
records unverified judgment). The one-line JSON report on stdout carries
`inserted/already_recorded/matched/skipped_lines` and the verified/verify rider counters.
Receipts are unsigned by policy — there is no signature to check; integrity rides the subject
digest, which the ingest door re-checks against the predicate.

### Automated wiring (`.githooks/post-merge` + `.githooks/post-commit`)

`hive-edge census init` (above) writes a hook pair that closes this loop on every landing without
an operator in the path: `post-merge` builds an unsigned receipt for `ORIG_HEAD..HEAD` after a
clean in-process merge, and `post-commit` builds one for `HEAD^..HEAD` after a direct commit or a
conflict-resolved merge completed via `git commit` (skipping only the parentless initial commit) —
git's two hook paths are disjoint, so every landing is receipted exactly once. Each hook first
refreshes the persistent per-repo code graph (`hive-edge graph update` — this runs even where the
`hive` CLI is absent), then builds the receipt (`hive-edge census build --propagate --hive-url …`)
and feeds it via `hive ingest <receipt> --post-merge --verdict pass --signal none` (`--post-merge`
means LANDED — a direct commit is landed; `none` is honest for a bare local landing — no rollout
telemetry checked it, so it records as unverified judgment; CI can re-ingest with a stronger
signal). Enable once with `git config core.hooksPath .githooks`. No key or setup is required —
receipts are unsigned by policy; the ingest-door trust boundary is transport/auth (loopback
locally, per-seat tokens over a tunnel), not a receipt signature. The hooks are a fail-open
side-channel: everything runs detached in the background, output lands in
`~/.hive-edge/last-postmerge.log` / `last-postcommit.log`, and any missing piece (`hive-edge` on
PATH, a merge/commit base, the hive CLI) skips silently — a merge or commit is never delayed or
failed by evidence plumbing. The hooks' bytes are constant across devices (binaries and config
resolve at run time), so a clone without the `hive` CLI still refreshes the graph but stays
census-inert — the operator clone's pulls receipt the shared repo's merges (per-device details:
`HIVE-ADMIN.md` §8). When both binaries resolve at wiring time, `hive-edge census init` self-tests
by building a zero-diff receipt in the hooks' own `GIT_DIR`/`GIT_WORK_TREE` environment and prints
`self-test PASSED`/`FAILED`, so a broken build environment is caught immediately rather than only
surfacing in the hook logs.

## Posture (why)

Usefulness is a **flow, not a stock**: a bigger store is a *worse* one — past an optimum, dilution
and staleness outrun the fixed recall bandwidth. Keep it small, let unused memory expire, run agents
as **distinct sessions** (that diversity is what promotes good captures), and spend scarce human
review on the established tier. See `OPERATIONS.md` for the evidence behind each lever.
