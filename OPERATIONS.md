# Operating Hivemind — what the lifecycle simulation says

This is the operator's posture for running Hivemind's shared memory well: which knobs
move the outcome, which KPIs to watch, and which calls are yours to make. It is derived
from a discrete-event Monte-Carlo simulation of **this trust lifecycle's structure** —
capture → quarantine → demand-promotion (`provisional`), the absolute serve floor, TTL
decay, and retirement of established falsehoods as the only removals. Every number below
is a steady-state result from that model. Hivemind already *implements* the structure the
model rewards — in v3, `hive_write` also serves immediately as `provisional`, `established`
is reached only by verified change outcomes on the canonical line, and retirement is
machine-gated rather than discretionary; this doc is about **operating** it.

## The governing principle: usefulness is a flow, not a stock

The store does not clean itself. The model's sharpest result is that the only forces
removing harm are **TTL decay** (of unused, un-established rows) and **retirement**
(of established falsehoods — in v3, agent-called and machine-gated). There is no
on-contact cleaning — a memory served as truth is not re-examined by being served. Two
consequences drive everything else:

- **Maintenance is the product, not overhead.** Left alone, a store degrades: good
  memories silently go stale faster than anything else erodes them, and nothing reverses
  that without deliberate upkeep — seeding answers and sweeping the worklists into
  retirement calls.
- **A bigger store is not a better one.** Past a point, dilution and rot outrun the fixed
  recall bandwidth. Hoarding lowers the value delivered per query.

## The headline the model proves

**The trust lifecycle — not write-time care — is the safety mechanism.** Holding coverage
fixed and sweeping the fraction of captures that are wrong from 0 → 50%:

| Store | false-serve @ 50% bad captures | success | coverage |
|---|---|---|---|
| Trust-everything (no gate, no dedup) | **0.83** | 0.16 | 0.99 |
| Absolute gate + trust-dominance dedup | **~0.01** | 0.98 | 0.99 |

Same coverage, two orders of magnitude apart on false-serve — purely from structure
(quarantine-by-default + serving the trusted twin over the unproven one). The corollary,
confirmed independently: the served harmful fraction is pinned by *throughput*, nearly
flat in how careful writes are. **You cannot filter your way to safety at write time;**
the gate and the promotion bar are what protect the fleet.

## Leverage map — principle → evidence → lever → action

Ranked by how much the model says each moves the outcome.

### 1. Quarantine by default; promote only on *independent* demand
Captures land `quarantined` and are never served until distinct fleet demand — or a SHA-bound verified outcome — promotes
them. The anti-gaming clause is load-bearing: with a single demand identity, **coverage
collapses to 0** in the model — a lone agent cannot vote up its own captures, so nothing
unvetted is served. The cost of safety here is paid in latency, not in poison.
- **Levers:** per-session identity is the promotion fuel (distinct agent sessions promote
  each other); `HIVE_AUTONOMY__DEMAND_M` (default 1 — the writer's own misses never count, so
  this is non-writer misses required), `HIVE_AUTONOMY__DEMAND_WINDOW_DAYS`
  (14); the demand-match cosine floors `HIVE_AUTONOMY__DEMAND_TAU` (0.75 — what counts as a
  recall-miss matching a quarantined candidate) and `HIVE_AUTONOMY__COMPETITOR_TAU` (0.85 —
  above which an already-servable answer covers the demand, so no promotion fires); the
  suspect-consensus worklist flags promotions with thin *effective* independence
  (`HIVE_SUSPECT_CONSENSUS__N_EFF_FRAC_MAX`, 0.5).
- **Operate:** make sure your agents really run as **distinct sessions** (that diversity is
  what promotes good captures). Audit
  `hive_health(include_suspect_consensus=true)` for the confidently-wrong-but-popular
  failure mode.

### 2. Resolve contested topics by trust-dominance, not by abstaining
When several near-duplicate versions of one answer compete, serving the highest-trust one
and suppressing the rest keeps the served answer clean **without losing coverage** — this
is the mechanism behind the 0.83 → 0.01 swing above. It is unconditional in recall
(trust-dominance drop + MMR collapse over an overscanned candidate pool).
- **Levers:** the near-duplicate cosine floor `HIVE_CONFLICT__TAU` (0.80) is the one
  empirical knob; it is shared by the serve-time selector and the conflict worklist.
- **Operate:** recalibrate `conflict.tau` on your real corpus (the benchmark measures
  genuine paraphrase/contradiction cosines vs distinct-fact cosines). Too low merges
  distinct facts; too high lets twins through. The asymmetric cost — a false merge can
  retire a real memory — argues for keeping margin.

### 3. Fight staleness on the established tier — it is the dominant decay
Good memories quietly going out of date erodes the store faster than injected poison does;
in the model the good→stale path is the single largest degradation. Established memories
**never decay mechanically** — no TTL cleans them; retirement is the only exit.
- **Levers:** every recall hit carries its `trust`, `ts`, and a per-anchor `drift` verdict
  (the sync daemon materializes drift at each repo's canonical tip, every line a live memory
  declares, and any branch tip recall demanded);
  `hive_health(include_stale_suspects=true)` surfaces servable memories whose anchor sat in
  the blast radius of a breaking change; `hive_supersede` / `hive_write(replaces=<id>)` /
  `hive_prune` retire — machine-gated, so the server retires only a target it can qualify
  with a verified signal (anchor drift, hurt evidence from another identity or a verified
  outcome, or a near-dup winner naming the successor — supersede only).
- **Operate:** treat an old `ts` on an established hit as a staleness suspect, not a
  guarantee. Sweep the stale-suspect and conflict worklists on a fixed cadence and resolve
  them into retirement calls — an unqualified call is a benign no-op, so calling is safe;
  the gate, not the caller, decides.

### 4. Keep it small; let unused memory expire
With staleness present, there is an **optimal (small) store size** — in the model a tight
store delivered ~50× the per-query value of a large one (6.78 vs 0.13). Beyond the optimum
the store rots faster than recall can cull, and bigger is actively worse.
- **Levers:** `HIVE_AUTONOMY__QUARANTINE_TTL_DAYS` (14), `HIVE_AUTONOMY__PROVISIONAL_TTL_DAYS`
  (45). Established is exempt by design.
- **Operate:** do **not** lengthen TTLs to hoard. The expiry of unused, un-established rows
  is a feature doing real work; resist the urge to keep everything "just in case."

### 5. Set the serve floor just below your true-match cosine
Abstention is a real cost, and the floor is the dial. Pushed above where genuine matches
actually land, the gate abstains on everything — in the model a floor of 0.9 (above the
~0.85 true-match band) **dropped coverage to 0.02**. Just below it, coverage is full and
the field stays clean.
- **Levers:** `HIVE_RECALL__TAU_SERVE` (0.70), `HIVE_RECALL__K_MIN` (1); `HIVE_RECALL__RECALL_TOP_N`
  (10, the max hits returned) and `HIVE_RECALL__OVERSCAN` (3, the candidate-pool multiple the
  unconditional decorrelated serve-set selection draws from) size the recall window. Calibrate with
  `scripts/eval_metrics.py` (recall@k / abstention AUROC) on the real query distribution.
- **Operate:** recalibrate `tau_serve` on **your** queries, not the default. Track the
  serve-confidence trend (below) — a falling `confident_rate` with a too-high floor is
  silent coverage starvation.

### 6. `demand_m` is your coverage ↔ safety dial, floored at 1 non-writer miss
Raising the promotion bar trades recall for cleanliness, roughly linearly: in the model
`demand_m=1` gave coverage 0.94 / false-serve 0.09, while `demand_m=5` gave coverage 0.34 /
false-serve 0.01. The `demand_m=1` point is exact under the current counting rule — it already
required exactly one identity other than the writer. The `demand_m=5` point predates subtracting
the writer's own misses from the count: under the corrected rule a bar of 5 now demands five
non-writer misses (from any number of non-writer identities), not one non-writer miss plus four of the writer's
own, so that reading is a lower bound on how strict `demand_m=5` now serves and wants a rerun
before it drives a decision.
- **Lever:** `HIVE_AUTONOMY__DEMAND_M` (1) — the anti-gaming floor itself; the writer's own
  misses never count toward it.
- **Operate:** choose anything above the floor from the *asymmetry of your costs*. If a wrong
  answer is expensive (production code, security), raise it; the floor of 1 is already the most
  permissive safe setting, so there is nowhere lower to go.

### 7. Seeding trusted answers is the biggest positive lever; retirement only helps the established tier
Seeding good answers via `hive_write` was the single strongest mover toward a clean,
high-coverage store in the model. But retirement is **scope-specific**: it had *no*
effect when the poison lived in the provisional tier (TTL handled it) — it only pays off on
**established** falsehoods, the tier nothing else cleans. There, pruning cut the
residual false-serve rate several-fold at no success cost.
- **Levers:** `hive_write` to seed (it serves immediately as `provisional`; a verified
  change outcome on the canonical line establishes it); `hive_supersede` /
  `hive_write(replaces=…)` / `hive_prune` to retire established errors (machine-gated).
- **Operate:** spend review attention **on the established tier specifically** — seed the
  store's answers and resolve its worklists. Chasing provisional churn is not worth it
  (TTL is already doing that work).

## What to watch — monitoring cadence

All read-only over MCP, off the warm store:

| Signal | Call | Read it for | Acts on |
|---|---|---|---|
| `confident_rate`, `demand_entropy` (+ 7d deltas) | `hive_health(include_trends=true)` | silent fail-open rot; coverage starvation; demand diversity | `tau_serve`, `demand_m` |
| demand-gap report | `hive_health(include_gaps=true)` | topics wanted but uncovered (each gap names the repos that asked) | `hive_write` those answers |
| contested-memory report | `hive_health(include_conflicts=true)` | servable rows with competing versions, bucketed by repo and anchor | `hive_supersede` the wrong one |
| suspect-consensus worklist | `hive_health(include_suspect_consensus=true)` | promotions on thin *effective* independence | re-examine / retire |
| stale-suspect worklist | `hive_health(include_stale_suspects=true)` | servable memories in the blast radius of a breaking/removed change | re-verify against the code, then retire |
| anomaly cluster flags | promote audit | compact near-identical clusters (flood signature) | investigate before trusting |

The trends window is the **only** view into silent fail-open rot — keep it instrumented and
read it on a fixed cadence (weekly is enough for a small team). Convert gaps into
`hive_write`s and contested rows into retirement calls in the same pass.

The evidence ledger has two inputs. The automatic one is **server-side and per-repo**:
register each repo the fleet works on (`hive repo add <url> [--name <slug>] [--branch <ref>]
[--token-env <ENVNAME>]`; `hive repos` lists, `hive repo remove <name>` stops feeding and forgets
that repo's feed state and every cache derived from it, keeping its memories' scope) and the sync
daemon — re-reading the registry every tick, so registration needs no restart — mirrors each
registered repo and feeds every landing on its canonical branch into the ledger: one unsigned
receipt per new watermark..tip range, ingested post-merge in-process, after which the
verified-outcome `established` sweep runs (the only trust movement the feed drives). The same
tick also materializes per-anchor drift verdicts by asking git — at the canonical tip, every line
a live memory declares, plus branch tips recall demanded — measuring each binding against the
commit it was BASELINED at (the repo watermark when it was written, recorded once and never
moved). The cost is two read-only plumbing reads per distinct baseline plus one `git log -L` for a
symbol anchor whose file actually moved: no worktree, no checkout, no engine subprocess. The
SHA-bound verified rows
it writes are also what the verified-promotion rung (`HIVE_AUTONOMY__VERIFIED_PROMOTION`,
default on) uses to promote a quarantined memory. Every leg is fail-open per repo; a push
webhook (`HIVE_SYNC__WEBHOOK_SECRET`, HMAC-gated on the tunnel door) only wakes the poll early —
one nudge wakes all registered repos; the poll interval stays the correctness floor. Watch the
feed via `hive_health(include_census_health=true)`, which answers in two slots: `repos` — per
registered repo, days since the last `change_outcome` plus that repo's sync state
under a `sync` sub-block (`tracked_ref`, `last_tip`, `last_sync_ts`, `last_error`,
`backfilled_total` = bindings the daemon had to baseline itself (write-time baselines are not counted), and
`status: "no change_outcome evidence yet"` only when the feed is configured yet dark) — and `fleet`, the daemon's own
`last_sync_ts` + `last_error`. Read `fleet` first: a fault in the tick shell (the registry read,
anything escaping a whole tick) is recorded before any repo is reached, so every repo block stays
frozen at its last healthy values and reads as passing while the daemon is down. A `fleet`
`last_error`, or a `fleet` `last_sync_ts` far behind now, means every repo block below it is a
stale snapshot. The manual escape hatch
remains `hive ingest <receipt.json>`: append a hand-built unsigned receipt's SHA-bound change
outcome — append-only, idempotent (an exact already-ingested `(repo, base, head, phase)` range
is skipped whole and reported `range_skipped`), trust-untouched; see
`skills/hive-operate/SKILL.md`.

For a browser view of the live picture, `hive ui` serves a loopback-only operator dashboard: the
same status/seats/logs read plus the controls (backup, seat mint/revoke, non-blocking loopback-only
start/stop, tunnel activate/deactivate, and restore from an in-volume backup behind a typed confirm).
The lifecycle/tunnel/restore actions validate synchronously then run detached under a single-flight
lock (a second concurrent op returns 409), so the browser never blocks on docker; the `/api/status`
poll shows the outcome. Restore is guarded — the snapshot name must be a bare basename that is a
member of the current in-volume listing, and a safety snapshot is taken before any overwrite; reset
has no surface. It reads the same `StatusSnapshot` the CLI `hive status` prints, so the two never
disagree.

## Decisions that are yours (operator taste)

The model gives directions, not a single setpoint — these depend on your costs:

- **`demand_m`** — coverage vs false-serve (lever 6). The central tradeoff.
- **`tau_serve` / `k_min`** — how conservative the gate is; recalibrate on your corpus.
- **`conflict.tau`** — the dedup near-duplicate floor; recalibrate on your corpus.
- **TTL lengths** — how fast un-established memory drains. Shorter favors freshness.
- **Worklist cadence** — how often conflicts, stale suspects, and suspect consensus get
  swept into resolution. The machine gate makes calling safe; the cadence is yours.
- **`HIVE_SECRET_SCAN__ENABLED`** — default on (the credential floor). Off bypasses the
  pre-persist scan, so raw secrets get stored unscanned — it loosens a safety gate, logged at
  boot and shown in `hive_health`. Leave on unless you have a specific, risk-accepted reason.

## Other operator knobs — the complete set

The levers above are the high-impact ones the model ranks; these are the remaining
operator-settable knobs, overridden the same way (`HIVE_<GROUP>__<FIELD>` in `.env`, applied
**only at boot** — there is no live reload, and an out-of-range value fails boot loudly). Lower
leverage on the outcome, but part of the full surface.

| Knob | Default | Controls |
|---|---|---|
| `HIVE_AUTONOMY__ENABLED` | `true` | master switch for the whole memory lifecycle; `false` ⇒ capture refused, no promotion/decay, no ledger writes (byte-inert with the pre-lifecycle build) |
| `HIVE_AUTONOMY__VERIFIED_PROMOTION` | `true` | the verified-outcome rung out of quarantine (SHA-bound verified win ⇒ promote at the next demand tick, competitor veto retained); `false` ⇒ the rung is unreachable |
| `HIVE_AUTONOMY__ANOMALY_TAU` | `0.95` | compact-cluster cosine floor for the promotion flood-anomaly flag (tighter than the near-dup floor; detection-only — it never blocks a promotion) |
| `HIVE_AUTONOMY__ANOMALY_MIN_CLUSTER` | `5` | ≥ this many near-identical quarantined neighbors ⇒ flag the promotion in the audit (the flood signature) |
| `HIVE_CONFLICT__ENABLED` | `true` | conflict detection (the recall `conflicts` carrier + the contested-memory worklist) and the `hive_flag` advisory verb; `false` ⇒ byte-inert |
| `HIVE_CONFLICT__TOP_N` | `10` | cap on the contested-memory worklist |
| `HIVE_SUSPECT_CONSENSUS__TOP_N` | `10` | cap on the suspect-consensus worklist |
| `HIVE_SYNC__INTERVAL_S` | `60` | census-sync daemon poll cadence in seconds (floor 5; an empty repo registry is an inert tick) |
| `HIVE_SYNC__WEBHOOK_SECRET` | *(unset)* | arms the `POST /census-webhook` nudge on the tunnel door — one nudge wakes the poll for all registered repos |
| `HIVE_SYNC__MIRROR_DIR` | `""` ⇒ `/data/sync/mirror` | where the sync daemon keeps its per-repo mirrors (`<dir>/<name>-<sha256(url)[:16]>` — rebuildable caches in the hive-data volume) |
| `HIVE_SYNC__WORKERS` | `1` | registered repos ticking concurrently (floor 1); raise toward the repo count when one serial pass outlasts the interval |
| `HIVE_HTTP_MAX_BODY_BYTES` | `1048576` | request-body cap in bytes on both HTTP doors (1 MiB) |
| `HIVE_RETENTION__BACKUP_KEEP` | `30` | most-recent `hive backup` snapshots kept |
| `HIVE_RETENTION__BACKUP_DIR` | `<db_dir>/backups` | where snapshots are written |
| `HIVE_OBS__LOG_LEVEL` | `20` | Python `logging` level (`20` = INFO) |

**Fixed by the image, not tuning knobs.** The embedder (`Qwen/Qwen3-Embedding-0.6B`, provider
`local_st`) is baked in at build and emits its native vector — there is no dimension knob, and
changing the model means rebuilding the image. The store path and tenant are set once via
`HIVE_STORE__DB_PATH` and `HIVE_TENANT_ID` (you manage the `hive-data` volume, not the path), not
recall/safety tuning.

## Release runbook — cutting a release over

A hivemind release has exactly one deploy target — the **server**. Agents are thin MCP clients
with nothing installed, so no step in any release directs the fleet at anything; each session
simply receives the served usage contract fresh at its next connect. Two concerns, each safe
to stop after:

1. **Land hivemind** — the server change. The census engines are first-party subpackages
   (`hive/matrix`, `hive/combdrift`), so an engine change is an ordinary in-repo edit
   carried in the same commit — there is no wheelhouse to rebuild and no separate engine
   repository to cut a release over; `uv lock` picks up any engine dependency shift alongside
   `pyproject.toml`. The gate is `make check` (format check, lint, strict typecheck, the default
   pytest suite — the heavy `embed` tier is opt-in).
2. **Cut the live server over** — `hive upgrade` to a vetted ref (backup-gated, auto-rollback
   on failure), or an in-place rebuild + restart from the landed tree, preserving the
   `hive-data` volume. **No compose change is required**: the sync knobs ride `.env`
   (`HIVE_SYNC__*`), the repo registry rides the store, and the mirrors live inside the
   existing volume (`/data/sync/`).

A release that crosses a **schema generation** is a clean-store cutover instead: `hive reset`
(snapshot-first, recoverable), then `hive repo add` each repo — never `hive upgrade`, whose
health gate refuses the old-format store and auto-rolls back (HIVE-ADMIN §8).

## Provenance & caveats

- The numbers are steady-state results of a mean-field / Monte-Carlo model of this
  lifecycle, not measurements of your live store — directionally validated, magnitude-
  indicative. Recalibrate the empirical floors (`tau_serve`, `conflict.tau`, `demand_m`)
  against the real corpus and query distribution.
- One-factor sweeps ignore interactions; assembled "optima" carry regime artifacts. The
  levers above are the **robust** ones — each confirmed by the mechanism and by 2-D
  confirmation sweeps, not by a single OFAT pass.
- The shipped recall path is the **absolute cosine floor**
  (`recall.tau_serve` + `k_min`) — shift-invariant entropy was deliberately removed
  ("flat-but-all-relevant" serves; "flat-but-weak" and "peaked-but-weak" abstain). This
  doc follows the code, which is also the gate the simulation found holds coverage on
  contested fields where an entropy gate over-abstains.
