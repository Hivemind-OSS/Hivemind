# Operating Hivemind — what the lifecycle simulation says

This is the operator's posture for running Hivemind's shared memory well: which knobs
move the outcome, which KPIs to watch, and which calls are yours to make. It is derived
from a discrete-event Monte-Carlo simulation of **this exact trust lifecycle** —
`hive_capture` → demand-promotion (`provisional`) → human `hive_write` (`established`),
behind the absolute serve floor, with TTL decay and human supersession as the only
removals. Every number below is a steady-state result from that model. Hivemind already
*implements* the structure the model rewards; this doc is about **operating** it.

## The governing principle: usefulness is a flow, not a stock

The store does not clean itself. The model's sharpest result is that the only forces
removing harm are **TTL decay** (of unused, un-established rows) and **human
supersession** (of established falsehoods). There is no on-contact cleaning — a memory
served as truth is not re-examined by being served. Two consequences drive everything
else:

- **Maintenance is the product, not overhead.** Left alone, a store degrades: good
  memories silently go stale faster than anything else erodes them, and nothing reverses
  that without an operator in the loop.
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
  each other); `HIVE_AUTONOMY__DEMAND_M` (default 3), `HIVE_AUTONOMY__DEMAND_WINDOW_DAYS`
  (14); the demand-match cosine floors `HIVE_AUTONOMY__DEMAND_TAU` (0.75 — what counts as a
  recall-miss matching a quarantined candidate) and `HIVE_AUTONOMY__COMPETITOR_TAU` (0.85 —
  above which an already-servable answer covers the demand, so no promotion fires); the
  suspect-consensus worklist flags promotions with thin *effective* independence
  (`HIVE_SUSPECT_CONSENSUS__N_EFF_FRAC_MAX`, 0.5).
- **Operate:** make sure your agents really run as **distinct sessions** (that diversity is
  what promotes good captures). Keep `HIVE_AGI__MODE` **off** unless you mean it — on, an
  agent can self-authorize the human-gated trust actions. Audit
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
**never decay mechanically** — they are the one tier with no automatic cleaning.
- **Levers:** every recall hit carries its `trust` and `ts`; the conflict worklist surfaces
  contradictions among established rows; `hive_supersede` / `hive_write(replaces=<id>)` /
  `hive_prune` retire them.
- **Operate:** treat an old `ts` on an established hit as a staleness suspect, not a
  guarantee. Periodically review the oldest established memories and the contested-memory
  report; a fact that was true when written can rot. This is human-paced work and there is
  no substitute for it.

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

### 6. `demand_m` is your coverage ↔ safety dial
Raising the promotion bar trades recall for cleanliness, roughly linearly: in the model
`demand_m=1` gave coverage 0.94 / false-serve 0.09, while `demand_m=5` gave coverage 0.34 /
false-serve 0.01.
- **Lever:** `HIVE_AUTONOMY__DEMAND_M` (3).
- **Operate:** choose it from the *asymmetry of your costs*. If a wrong answer is expensive
  (production code, security), raise it; if a missing answer wastes more time than a
  questionable one, lower it.

### 7. Human establishment is the biggest positive lever; supersession only helps the established tier
Seeding trusted answers via `hive_write` was the single strongest mover toward a clean,
high-coverage store in the model. But supersession is **scope-specific**: it had *no*
effect when the poison lived in the provisional tier (TTL handled it) — it only pays off on
**established** falsehoods, the tier nothing else cleans. There, human pruning cut the
residual false-serve rate several-fold at no success cost.
- **Levers:** `hive_write(approved_by=…)` to establish; `hive_supersede` /
  `hive_write(replaces=…)` / `hive_prune` to retire established errors.
- **Operate:** spend human review **on the established tier specifically**. Establishing a
  good answer is high-leverage; chasing provisional churn with manual review is not.

## What to watch — monitoring cadence

All read-only over MCP, off the warm store:

| Signal | Call | Read it for | Acts on |
|---|---|---|---|
| `confident_rate`, `demand_entropy` (+ 7d deltas) | `hive_health(include_trends=true)` | silent fail-open rot; coverage starvation; demand diversity | `tau_serve`, `demand_m` |
| demand-gap report | `hive_health(include_gaps=true)` | topics wanted but uncovered | `hive_write` those answers |
| contested-memory report | `hive_health(include_conflicts=true)` | established rows with competing versions | `hive_supersede` the wrong one |
| suspect-consensus worklist | `hive_health(include_suspect_consensus=true)` | promotions on thin *effective* independence | human audit / retire |
| anomaly cluster flags | promote audit | compact near-identical clusters (flood signature) | investigate before trusting |

The trends window is the **only** view into silent fail-open rot — keep it instrumented and
read it on a fixed cadence (weekly is enough for a small team). Convert gaps into
`hive_write`s and contested rows into supersessions in the same pass.

The evidence ledger also takes one operator-fed input: `hive ingest <receipt.json>` appends an
unsigned census receipt's SHA-bound change outcome as `change_outcome` evidence rows on the
episodes whose anchors the change touched — append-only, idempotent, trust-untouched
(detect/surface only for the plain change_outcome row -- a pre-merge receipt with a full version stamp can also emit an outcome_verified_helped row, which the verified-promotion rung, HIVE_AUTONOMY__VERIFIED_PROMOTION, default on, may use to promote a quarantined memory). The repo's
`.githooks/post-merge` hook automates the feed (build + ingest on every merge, fail-open,
detached); see `skills/hive-operate/SKILL.md` for the wiring.

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
- **Supersession cadence and owner** — how much human review the established tier gets.
- **`HIVE_AGI__MODE`** — default off. On, the fleet self-authorizes trust actions; it
  loosens the one human gate, so opt in deliberately, not by default.
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
| `HIVE_AUTONOMY__ANOMALY_TAU` | `0.95` | compact-cluster cosine floor for the promotion flood-anomaly flag (tighter than the near-dup floor; detection-only — it never blocks a promotion) |
| `HIVE_AUTONOMY__ANOMALY_MIN_CLUSTER` | `5` | ≥ this many near-identical quarantined neighbors ⇒ flag the promotion in the audit (the flood signature) |
| `HIVE_CONFLICT__ENABLED` | `true` | conflict detection (the recall `conflicts` carrier + the contested-memory worklist) and the `hive_flag` advisory verb; `false` ⇒ byte-inert |
| `HIVE_CONFLICT__TOP_N` | `10` | cap on the contested-memory worklist |
| `HIVE_SUSPECT_CONSENSUS__TOP_N` | `10` | cap on the suspect-consensus worklist |
| `HIVE_RETENTION__BACKUP_KEEP` | `30` | most-recent `hive backup` snapshots kept |
| `HIVE_RETENTION__BACKUP_DIR` | `<db_dir>/backups` | where snapshots are written |
| `HIVE_OBS__LOG_LEVEL` | `20` | Python `logging` level (`20` = INFO) |

**Fixed by the image, not tuning knobs.** The embedder (`Qwen/Qwen3-Embedding-0.6B`, provider
`local_st`) is baked in at build and emits its native vector — there is no dimension knob, and
changing the model means rebuilding the image. The store path and tenant are set once via
`HIVE_STORE__DB_PATH` and `HIVE_TENANT_ID` (you manage the `hive-data` volume, not the path), not
recall/safety tuning.

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
