# Hivemind v-min — System Specification

**Status:** rebuild-ready spec (greenfield) · **Date:** 2026-06-04 · **Type:** system
spec + rebuild map. §1–§9 answer what the system *does*, what *components* it contains,
what *optimal hyperparameter values/windows* it runs at, and *how it must be tested*.
§10–§12 make it **drop-in for a fresh-folder rebuild**: the **reuse/delete map** against
the existing `cls_memory` tree (§10), the **one mechanism specified by neither this spec
nor the code — the trace↔outcome join** (§11), and the **build order + process model**
(§12). Target: author the full software-design doc in a new directory from §1–§12 + the
current tree as the port reference.

**Provenance:** every quantitative claim is a *prior measured result* from the
2026-06-03/04 investigation (real 12,324-episode corpus + 1,397 natural
query→gold pairs across AgentCortex/Reins/YoutubeOptimizer sharing one Hivemind),
recorded in memory `project_geometry_optimization`, `project_channel_flip_gate_
dominates`, `project_consolidation_clusters_are_noise`, `project_meta_structure_
geometry`, `project_recall_vs_concept_arc`. Nothing here was re-measured at spec
time; the open items in §5 are the ones still requiring a measurement before ship.

**MVP scope (2026-06-04 reframe).** "v-min" is the *commodity substrate* —
geometry-fixed episodic recall + abstention. The **MVP = v-min + exactly one
differentiated move: the utility/attribution loop (move #6).** That loop is the
single keystone the product thesis rests on; it is currently UNPROVEN and its one
scaling claim was REFUTED, so the MVP's entire job is to de-risk it on the
smallest honest slice, eval-gated, before any further concept-formation moves
(#1,#2,#4,#5,#7) are built. The closed loop is **already wired** in the tree
(shadow-mode); the MVP is the one missing **honest, verifiable signal producer**
that feeds it — smaller than "build the loop." **No consolidation/GMM is included**
— the loop credits *episodic* memories, not schemas. Additions tagged **[MVP+ #6]**
below are the move-#6 build; everything untagged is the substrate. **Build it in two
waves (§12):** Phase 1 ships the substrate + the outcome-*collection* plumbing with
utility *observed-not-applied*; Phase 2 (gated on the §6.6 keystone) flips utility into
recall. Same end-state, strictly smaller first-ship.

---

## 1. What the system does (capabilities)

Hivemind v-min is a **single-tenant episodic recall store for an agent fleet**. It
has three substrate capabilities + the move-#6 learn loop, under a never-hallucinate
guarantee:

| Capability | Contract |
|---|---|
| **Capture** | Accept a high-signal text insight from an agent (or a hook) and **stage it for user approval**; on approval it becomes recallable. Nothing enters the recallable store unreviewed. |
| **Recall** | Given a query, return the most relevant stored insights **or abstain** (return empty) when not confident. |
| **Fetch** | Resolve a stored item's content hash to its verbatim text. |
| **Learn** **[MVP+ #6]** | Credit each recalled insight by the *verifiable outcome* of the task it fed — a merge that survives, clawed back on revert/bug-on-files — and let that bias future recall, scoped to one task-family. |

**Product invariants:**
- **Never-hallucinate**: when the store is not confident, recall returns *nothing*
  rather than a weak guess. A refused query is never rescued by a fallback path
  ("abstain-no-resurrect").
- **Single-tenant-per-instance**: one deployment per org; the instance is the hard
  data boundary; `tenant_id` is a constant label, not a query filter.
- **Local & self-contained**: one OS process, one SQLite file, a local CPU
  embedder, no external network calls on the hot path (capture/recall/fetch).
- **Verifiable-credit-only [MVP+ #6]**: a memory's utility is updated *only* from
  a machine-checkable outcome (CI status, merge vs revert) — never from agent
  self-reported judgment (noisy, gameable). No verifiable signal ⇒ no credit.
- **Harness-agnostic (MCP-server-only) [MVP]**: the product is one MCP server (the
  `hive_*` tools) running identically under any MCP-speaking harness — Claude Code,
  Codex, Conductor, etc. **The server is the trust boundary**; harness hooks are
  optional per-platform convenience, never load-bearing. Audience: solo devs →
  startups → small companies (high-trust environments).
- **Human-approved writes [SEC]**: every write *stages*; the agent surfaces pending
  writes for approval in the harness's **native chat** ("save these N insights?"), and
  only approved memories become recallable. The server enforces **approved-only
  recall** — the one structural guarantee (depth-invariant identity machinery is out of
  scope; see §9).
- **Secret-safe substrate [SEC]**: the server's write handler runs a deterministic
  credential scan *before* staging — a raw secret is refused/redacted, never persisted.
  The one always-on floor; agent judgment sits on top of it, not instead of it.

**Explicit non-capabilities (scope boundary — see §8):** no consolidation /
schema-formation, no federation across instances, no differential privacy, no
hybrid BM25/graph channels, no cross-encoder reranking, no cold/archive tier.
These were measured to be unproven, no-ops at the operating point, or dead code,
and are deferred behind the eval gate. **The utility loop (#6) is the one
differentiated capability that IS built** — and it operates on episodic memories,
so it does *not* reintroduce consolidation/GMM.

---

## 2. Components in the system

Data flow:

```
   CAPTURE (admission-gated)                 RECALL (reads APPROVED only)
text ─▶ Embedder ─▶ PCA head ─▶ value(d)      query ─▶ (same encode chain) ─▶ value_q
                                  │                                            │
                                  ▼                                  score every APPROVED i:
                          STAGING (pending)                          sim_i = cos(value_i, value_q)
                                  │                                            │
                     user approval ─▶ admit                          ┌── Abstention gate ──┐
                                  │                                  │ H/ln(N_eff) > floor?│
                                  ▼                                  │  yes → ABSTAIN      │
                   Store (live/approved) + index ──────────────────▶│  no  → top-N hits   │
                                                                     └─────────────────────┘
```

| # | Component | Responsibility | I/O |
|---|---|---|---|
| C1 | **Embedder** | text → dense semantic vector. Local sentence-transformers model, frozen at deploy, pluggable behind a `TextEmbedder` interface. | `str → float[384]` |
| C2 | **Projection head (PCA)** | Dimensionality reduction that *raises effective rank* — the dominant recall lever. Fit unsupervised on the corpus; applied as a linear map at encode time. | `float[384] → float[d]` |
| C3 | **Recall ranker** | Score all live episodes against the query and order them. **Default: dense cosine-kNN on the `d`-dim value.** (A sparse-key dot-product variant is the §5 A/B alternative.) | `value_q → ranked (id, score)[]` |
| C4 | **Abstention gate** | Normalized-entropy confidence test on the recall distribution; decides return-vs-abstain. The never-hallucinate enforcement point. | `scores → {hits | EMPTY}` |
| C5 | **Store** | SQLite single-file: a **staging** area + the **approved** (recallable) episode set, an exact vector index over approved rows, and a content-hash blob store for verbatim fetch. | rows + vectors |
| C6 | **Write/admission path** | What gets *proposed*, and the one guarantee on it. Triggers = high-signal events (git commit, explicit mark/log-bug) + optional turn-distillation; coverage varies by harness (§9). **The server runs a deterministic secret scan, then stages the proposal; the agent surfaces pending writes for approval in native chat; only approved memories become recallable.** | events → secret-scan → staging → chat-approval |
| C7 | **Integration surface** | MCP tools (`hive_write` *(scan→stage)*, `hive_recall`, `hive_fetch`, + the admission trio `hive_pending` / `hive_approve` / `hive_reject` — §8.2) + a server-served **`hive-init`** prompt the agent runs to self-wire whatever hooks the local harness supports (the agent is the universal installer — §9) + **approval in native chat** (the agent asks the user to admit/reject pending writes; no separate UI). | MCP / agent-driven |
| C8 | **Eval membrane** | **Dev-time only, not a runtime component.** The acceptance/decision harness (§6) every change must pass. | corpus → metrics |
| C9 | **Utility/attribution loop [MVP+ #6]** | The single differentiated move. Joins exposure (which memories fed which task) × verifiable outcome, updates each memory's utility *posterior*, and lets utility bias recall — scoped to a task-family. **The loop is already wired (shadow); the MVP builds the one missing honest producer.** | outcome → utility → recall |
| C10 | **Verifiable-outcome producer [MVP+ #6]** | Server-internal git/CI watcher (background, NOT hot-path). Reads watched repos, maps commit→trace (recall-window default, `Hive-Trace` trailer override), computes the §4.7 asymmetric reward on merge/revert/bug-on-files (clawback confirmed by blame-overlap), writes it to the telemetry sink. **The one genuinely new component; everything it feeds already exists.** Full mechanism: §11. | git facts → sink |

**C9 — the loop as a bookmaker [MVP+ #6]** (every recall is a bet; CI/merge/revert
settles it; utility is the odds). *Verified against the tree:* the closed loop is
**already wired** — `ConsolidationController` is instantiated (`service.py:438`) and
`apply_outcomes_from_sink` drains the telemetry sink during consolidation, crediting
proven-useful recalls via a watermarked, bounded reconsolidate (`service.py:942`).
It is **starved** — the only thing feeding it is the manual, self-reported
`hive_outcome` (the noisy/gameable **L0** signal). **The MVP is the one missing piece
— an honest, verifiable producer** — not a new loop. Four parts, three already exist:

1. **Exposure ledger** *(partial — `telemetry.py`)* — `trace_id → [memory_ids]` plus
   each item's recall margin/entropy. The MVP adds a join key to the task/PR/commit.
2. **Verifiable outcome producer** *(ABSENT — the whole MVP)* — a thin hook over the
   existing commit/CI surfaces that writes a reward to the sink, keyed to the trace
   the task used. No new hot-path code. **Full mechanism: §11 (component C10).**
3. **Batch attributor** *(EXISTS — `service.py:942`, not dead)* — joins exposure ×
   outcome, updates utility, watermarked + CACE-bounded.
4. **Surfacer** *(EXISTS but positive-only — `service.py:184`, `α·(1+max(0,u))`)* —
   the MVP **un-cripples it** so a confident-negative utility actually demotes.

**Signal design — asymmetric clawback, NOT symmetric per-task Q.** Every change is
code-agent-written, so CI-green is *gameable* (weaken the test) and *high base-rate*
(low information). The discriminative signal is the **rare, ungameable negative**:
revert / bug-fix commit touching the same files. So: small provisional `+` on merge,
**settle only after N clean days**, **large `−` on revert/bug-on-files** (see §4.7).

**Attribution — co-occurrence-discounted.** When 8 memories were injected and one was
the cause, flat "+1 to all 8" is spurious reinforcement; split each task's reward
across injected memories **by recall margin** (the exposure ledger already logs it).
True counterfactual replay is L2/offline only.

**Utility representation — Beta-Bernoulli posterior, not running-Q.** A win/loss tally
with a prior is the same running mean but interpretable ("2 reverts → demote"),
auditable, and gives a confidence interval for free — **utility does not move ranking
until the posterior is confident.** Kept as a *separate, versioned field* (never
entangled into `weight`); the existing bounded weight-nudge is an acceptable v0.

**Family-scope** — the posterior is keyed on `(memory_id, family)`; family = one
denormalized string `git-remote × language × coarse-workflow` (O(1) join, not a
taxonomy). A memory proven on repo-X/dep-upgrade/python is not credited elsewhere.

**Cross-agent corroboration [A]** — high-trust standing prefers ≥
`promote_min_sources` distinct agents (already in config) as a "true regularity"
signal — one noisy source shouldn't mint a high-trust memory. In the high-trust MVP
this is a robustness nicety, not a poisoning defense; adversarial corroboration-gaming
is out of scope (§9).

This is a **self-modifying loop** (a ranker choosing its own future training data), so
the four guardrails in §4.7 are mandatory. The shadow-mode config *controller* (it
*proposes* bounded config steps) **stays gated** — the MVP only feeds and consumes the
outcome-crediting path; it does not flip the config-tuner live.

**Reserved seams (still inert — do not build, do not design out):**
- a second "mechanism vector" per episode — the future consolidation
  representation (variabilize-then-embed). Not written in the MVP.

(The previously-reserved `outcome` column and telemetry recall-outcome sink are
now **active** — they are the move-#6 credit path; see §3 + §4.6.)

---

## 3. Data model

Single table + blob store + three ledgers (`exposure`, `task_outcomes`, `utility`). The
dense value is the only episode representation (sparse key only if §5-D1 keeps it). **Utility is a separate, versioned
layer — never entangled into `weight`** (guardrail 4: a human can zero the utility
layer to roll back without losing memories).

```
episodes(
  id            INTEGER PK,
  tenant_id     TEXT,          -- constant label (single-tenant instance)
  text          TEXT,          -- verbatim insight (topical; the recall substrate)
  value         BLOB,          -- float[d] PCA-projected dense vector
  weight        REAL,          -- capture salience ONLY (immutable post-capture; loop never writes it)
  ts            INTEGER,       -- capture time (epoch s)
  source        TEXT,          -- hook | mark | log-bug | value-gate
  tags          TEXT,          -- freeform labels
  content_hash  TEXT,          -- sha256(text) for fetch + dedup
  status        TEXT,          -- [SEC] pending | approved; recall reads APPROVED only (server-enforced)
  proposed_by   TEXT,          -- writer agent identity (provenance for debugging / bulk cleanup)
  approved_by   TEXT NULL,     -- [SEC] human approver, relayed via native-chat approval; NULL until approved
  approved_ts   INTEGER NULL,  -- admission time; NULL until approved
  schema_id     INTEGER NULL   -- already exists (types.py:67); unused until concepts
  -- NOTE: no family_scope here. A memory recalled across repos has no single family;
  -- family is derived per-credit-event by the producer (§11), kept on task_outcomes/utility.
)

-- [MVP+ #6] exposure ledger: which memories fed which task, with credit weights.
exposure(
  trace_id      TEXT,          -- one recall event
  episode_id    INTEGER,       -- a memory injected into that task's context
  recall_margin REAL,          -- rank/score at recall → co-occurrence credit split
  task_ref      TEXT NULL,     -- PR / CI-run / commit join key (the MVP adds this)
  injected_ts   INTEGER
)

-- [MVP+ #6] producer's reward state machine, keyed by the stamped commit (§11).
task_outcomes(
  task_ref      TEXT,          -- commit SHA (the join key, from the Hive-Trace trailer)
  trace_id      TEXT,          -- the recall trace that commit stamped
  family_scope  TEXT,          -- derived HERE at link time: git-remote × language × workflow
  repo          TEXT,          -- watched repo the commit landed in
  files_touched TEXT,          -- for bug-on-files clawback matching
  state         TEXT,          -- provisional | settled_pos | clawed_back
  reward        REAL,          -- +provisional → settles, or − on clawback (§4.7 schedule)
  merge_ts      INTEGER,       -- commit/merge time
  settle_at     INTEGER,       -- merge_ts + settle_days; the sweep settles after this
  PRIMARY KEY (task_ref, trace_id)
)

-- [MVP+ #6] utility posterior, keyed on (memory, family); SEPARATE + versioned.
utility(
  episode_id    INTEGER,
  family_scope  TEXT,          -- credit does not cross families
  wins          REAL,          -- Beta-Bernoulli α: settled positives, score-split
  losses        REAL,          -- Beta-Bernoulli β: reverts / bug-on-files
  n_sources     INTEGER,       -- distinct agents corroborating (robustness signal)
  version       INTEGER,       -- bump to roll the whole utility layer back
  PRIMARY KEY (episode_id, family_scope)
)
```
No `schemas`, `federation`, `cold`, channel, `source_trust`, or `conflicts` tables —
those are post-MVP / fleet-tier (§9). Episodes are append-only; `weight` is immutable
post-capture; only `status` (on approval) and the `utility` posterior mutate.
**Recall queries `status='approved'` only** (server-enforced) — a pending row is never
embeddable into another agent's context. **`family_scope` is not an episode column** — a
memory recalled across repos has no single family; it is derived per-credit-event by the
producer (§11) and lives only on `task_outcomes` / `utility`.

---

## 4. Hyperparameters — optimal values / windows

Tier: **D** = re-embed/migration (a config edit alone corrupts a warm store);
**C** = restart; **B** = hot-swappable; **A** = next-run. Config field names are
`group.field` from `cls_memory/config.py`.

### 4.1 Representation geometry (tier D unless noted)

| Param | Optimal | Window | Evidence |
|---|---|---|---|
| `embedding.st_projection_head` | **`pca`** | pca only (random rejected) | PCA > random JL at every d (raises effective rank). |
| `geometry.d` (dense dim) | **256** | 256–384; 256 = sweet spot | recall@5 0.026→0.341 at 256; 384→0.351 (marginal, costs more downstream). |
| `geometry.beta` (gate sharpness) | **must re-tune** | see §5-D2 | β=32 was for the sparse-key dot range; dense cosine ∈[−1,1] needs its own β. (tier B) |
| `embedding.st_model_name` | **`BAAI/bge-small-en-v1.5`** | bge-small (384d) primary; `all-MiniLM-L6-v2` = CPU fallback | bge-small +0.068 recall@5 [CI +0.033,+0.102] through the PCA geometry, drop-in (same 384d). |
| `embedding.embedder` | **`st`** | st (force sentence-transformers path) | — |
| `geometry.W_version` | **bump on any tier-D change** | monotonic int | triggers re-embed migration. |

*Sparse-key path (only if §5-D1 keeps it):* `geometry.D` ≈ 2·d (512), `geometry.k`
≈ d (256), `geometry.beta` 32. The proven optimum drove k,D toward ~50% dense —
i.e. nearly equivalent to the dense value, which is why dense-only is the default.

*Knob reality (verify on port — current tree defaults, not the targets above):* today
`geometry.d`=**32** and `geometry.D`=**256**, so "set `d`=256" means promoting the dense
*value* to roughly today's `D` width — **not** a literal 32→256 bump on the same knob.
`geometry.beta` is **16** today (not 32); either way it is re-tuned for the dense-cosine
range at the geometry change (§5-D2). `embedding.st_projection_head`=`random`,
`st_model_name`=`all-MiniLM-L6-v2`, `embedder`=`auto` today — all three flip per §4.1.

### 4.2 Recall gate + return (tier B — hot-swappable)

| Param | Optimal | Window | Evidence |
|---|---|---|---|
| `recall.H_frac_max` | **0.5** | 0.3 (strict) – 0.7 (loose); product decision | the never-hallucinate floor. Lower = abstain more, higher precision; raise only with eval evidence. Validated range (0,1]. |
| `recall.recall_top_n` | **10** | ≥ eval k; size-only | number returned; never affects the abstain decision. |

### 4.3 Scale / index (tier C/B)

| Param | Optimal | Window | Evidence |
|---|---|---|---|
| `index.vector_index_backend` | **`exhaustive`** | exhaustive until scale forces hnsw | exact recall; the eval assumes exact. |
| `recall.approx_threshold` | **> N** (e.g. 200_000) | above live episode count | **proven trap**: above this the ANN path engages and exact-eval recall→0. |
| `recall.candidate_k` | 512 | inert in exact path | ANN pre-rank width for the future hnsw switch. |

**Structural fix (not just a value).** Today `approx_threshold`=**10_000**, so any store
over 10k silently engages the ANN path and exact-eval recall→0 — the 12,324-episode corpus
is *already* past it; this is a live default bug, not only an eval artifact. Setting it to
200_000 merely *relocates* the landmine. The durable fix: make `index.vector_index_backend
=exhaustive` **authoritative** — short-circuit the approx path whenever exhaustive is
selected, so growing N can never silently flip it; `approx_threshold` then applies only
once hnsw is explicitly opted into.

### 4.4 Force-OFF (defaults that are wrong for v-min)

| Param | Set to | Why |
|---|---|---|
| `channels.cross_encoder_rerank` | **`False`** | **defaults ON**; conditional benefit only + carries the `rerank_top_k=8` truncation bug. |
| `channels.hybrid_recall` / `graph_recall` / `native_ann` / `trust_rerank` / `cold_in_cascade` | `False` (already default) | no-op at this floor, +latency. (`utility_rerank` is the exception — ON for the MVP; see §4.7.) |
| (consolidation timer) | **do not schedule** | clusters measured as noise (silhouette 0.036); keeps the store append-only. **Caveat (Fix #1):** the move-#6 outcome-drain (`apply_outcomes_from_sink`) lives *inside* `consolidate()` (`service.py:942`) today — on the rebuild it MUST move onto the producer's own tick (§4.8 `poll_interval_s`), else dropping this timer silently kills the loop. |
| `privacy.dp_enabled` / `secagg.secure_aggregation` / `federation.*` | off (already default) | unproven moat; post-MVP. |
| `salience.use_surprise` | `False` (already default) | post-MVP. |

### 4.5 Write path (value-gate, only if enabled — tier A)

| Param | Optimal | Note |
|---|---|---|
| `value_gate.gate_model` | `claude-haiku-4-5` | cheap distillation. |
| `value_gate.max_insights` | 5 | cap per turn. |
| `value_gate.default_weight` | 1.8 | turn-insight weight. |
| `value_gate.min_turn_chars` | 80 | skip trivial turns. |

Absolute floor needs no value-gate: deterministic capture (commit hook +
`/hive-log-bug` + `/hive-mark`) is LLM-free and sufficient.

### 4.6 Ops / reserved (tier A/C)

| Param | Optimal | Why |
|---|---|---|
| `retention.backup_keep` | 30 | daily SQLite snapshot — the one ops must-have. |
| `observability.telemetry_enabled` | **`True`** **[MVP+ #6]** | the recall-outcome sink is now the attribution path (was a reserved seam). |
| `observability.log_level` | `INFO`, JSON logging on | structured failure-mode logging. |

### 4.7 Utility loop (move #6 — the MVP's differentiated build) [MVP+ #6]

**Reward schedule — asymmetric, clawback-based (the key correction over a symmetric
running-Q).** Code-agents can game CI-green and it is high-base-rate ⇒ low information;
key on the rare ungameable negative instead.

| Event | Reward | Gameable? | Role |
|---|---|---|---|
| CI pass / agent-reviewer approve | **~0** (ignore or tiny +) | yes (weaken test / agent reviewer) | high base-rate ⇒ near-zero info |
| Merge | **small provisional +** | partly | not settled yet |
| Survives N clean days (no revert / no bug-on-files) | **settles the provisional +** | no | positive counts only once reality confirms it |
| Revert / rollback | **large −** | no | the discriminative, ungameable signal |
| Bug-fix commit touching the same files | **large −** | no | delayed clawback |

Windows: `settle_days` **7** (3–14); provisional-merge `+` **0.2** (0.1–0.3); clawback
**−1.0** (fixed). The asymmetry *is* the design — few events, each high-information.

**Other knobs:**

| Param | Optimal | Window | Notes |
|---|---|---|---|
| `channels.utility_rerank` | **`True`** | on, behind family scope | flips the existing switch (default False). Tier C. |
| surfacer multiplier | **`weight × f(utility)`**, f allows demotion | f ∈ [0.5, 1.5] | **un-cripples** `service.py:184` (today `1+max(0,u)` can't demote); acts only when the posterior is confident. |
| credit split | **by recall margin** | — | co-occurrence discount; no flat +1-to-all. |
| confidence gate | **posterior CI excludes 0** | — | utility does not move ranking until confident. |
| `promote_min_sources` | **2** | ≥ 2 | cross-agent corroboration before high-trust (robustness vs one noisy source; not a poisoning defense in the MVP — §9). |
| family granularity | `git-remote × language × workflow` | one string | O(1) join; no taxonomy. |

**Guardrails (mandatory — this is a self-modifying loop):**
1. **ε-randomization** — a fraction (`ε` **0.1**, 0.05–0.2) of recalls ignore utility
   entirely; the only way to give novel memories exposure and detect loop decay
   ([[project_channel_flip_gate_dominates]] is the local proof that one unchecked
   ranking term starves the rest). Cap the utility term *and* keep ε > 0.
2. **Held-out isolation** — a slice the loop never reweights, as a drift control.
3. **Prediction-bias monitor** — recalled-utility must keep tracking realized
   outcomes; divergence ⇒ ranker stale (codebase moved underneath it).
4. **Versioned / separable utility** — surfacer reads `weight × f(utility)` at query
   time; bounded reversible steps (CACE); a human can zero the utility layer.

The bounded weight-nudge is the v0; **Beta-Bernoulli posterior is the target**. None
of these values has a proven optimum — the §6.6 keystone eval sets them.

### 4.8 Outcome producer / watcher (the new component C10) [MVP+ #6]

The reward *schedule* is §4.7; these are the *watcher* knobs. New config group `producer.*`.

| Param | Optimal | Note |
|---|---|---|
| `producer.watch_repos` | deployment list | absolute paths the server has git read access to; empty ⇒ producer idle (loop starved, not broken — logged at WARN). |
| `producer.poll_interval_s` | 300 | git scan cadence; the settlement sweep **and the outcome-drain** (`apply_outcomes_from_sink`, decoupled from consolidation — Fix #1/§4.4) ride this tick. Off the hot path. |
| `producer.assoc_window_s` | 1800 | **primary** commit→trace window (Decision A: window-primary); over-attribution discounted by `recall_margin` + ε. |
| `producer.stamp_trailer` | `Hive-Trace` | optional precision **override** — when the agent stamps it, replaces the window set with the exact traces at higher credit weight (§11). |
| `producer.bugfix_pattern` | `^(fix\|bug\|hotfix\|patch):` + `BUG-NN`/regression/crash/race | candidate clawback trigger; **confirmed only by blame-line overlap** (Decision B), not same-file. |
| `producer.require_stamp` | `False` (MVP) | when `True`, only stamped commits credit (drops window association). Tighten post-keystone only if stamp-hit-rate is high. |

---

## 5. Open decisions that MUST be resolved by test before ship

These are the only unknowns the spec deliberately leaves to a measurement. Each is
a single eval-membrane A/B; ship the winner.

| ID | Decision | Default to assume | Kill/keep criterion |
|---|---|---|---|
| **D1** | **Dense cosine-kNN vs sparse-key dot** for the recall ranker | dense (simpler, standard, expected ≥) | run both on the corpus at d=256; keep dense unless sparse wins by a CI-significant margin. If dense wins/ties → delete sparsifier, `D`, `k`. |
| **D2** | **β recalibration** for the chosen ranking space | re-tune from scratch | sweep β so the abstention gate AUROC is maximized on its own top-5 misses (target ≈0.77); β is meaningless carried over from the other ranking space. |
| **D3** | **`H_frac_max` floor** (product) | 0.5 | choose on the recall-vs-refuse curve per the product's hallucination tolerance; set in BOTH the dense gate and the cascade gate via `from_flat` (CONFIG_DRIFT trap if only one). |
| **D4** | **Embedder tier** (bge-small vs stronger/hosted) | bge-small | bge-small is proven; only escalate to bge-large/E5/hosted if a GPU/API budget exists AND the eval shows a CI-significant lift that justifies breaking CPU-local + the privacy boundary. |

---

## 6. How the system must be tested

### 6.1 Acceptance gate (all must pass to ship)

1. **Recall quality**: recall@5 **≥ 0.33** on held-out natural query→gold pairs.
2. **Honest abstention**: abstention-gate **AUROC ≈ 0.77** at flagging its *own*
   top-5 misses — measured with **no contrived masking** (see 6.3).
3. **Never-hallucinate**: a refused (EMPTY) query returns nothing and is provably
   not rescued by any later stage (abstain-no-resurrect).
4. **Migration round-trip**: a `geometry.W_version` bump re-embeds + re-keys the
   whole store and reproduces the recall numbers (no silent corruption).
5. **Substrate safety (§9)**: (a) the secret scan refuses a planted credential before
   it is staged; (b) a pending write is **never recallable** (server enforces
   approved-only recall); (c) recalled memories are presented as reference context.
   Adversarial poisoning/injection is out of scope for the MVP (§9).
6. **Producer join round-trip (§11) [MVP+ #6]**: a commit links its window/stamp traces →
   `task_outcomes`; a provisional `+` settles only after `settle_days`; a planted **revert**
   and a planted **bug-fix whose diff overlaps the original commit's blame lines** each fire
   the clawback `−`; the reward reaches the sink and moves the `(episode, family)` posterior.
   **Three guard tests are mandatory, not optional:** (a) a coincidental same-file edit that
   does NOT overlap blame lines **must NOT** clawback (the expensive false-positive direction);
   (b) a **squash-merge** still resolves the trace + blame target (join survives squash); (c)
   the outcome-drain fires on the **producer tick with the consolidation timer disabled**
   (proves the loop is not silently dead — Fix #1). Mutation-test (RULE 2): disable
   blame-overlap and confirm guard (a) goes red.

### 6.2 Test corpus + methodology

- **Corpus**: real episodes + natural query→gold pairs (commit subj→body, bug
  symptom→fix, doc title→body) from on-domain repos sharing one tenant. Not
  synthetic, not hand-curated to flatter the system.
- **Split**: train/test split for any hyperparameter selection; **PCA fit
  unsupervised on corpus only** (no label leak).
- **Stats**: bootstrap confidence intervals on every reported delta; a change
  ships only on a **CI-significant** improvement, not a point estimate.
- **Sizing**: evaluate at realistic N (≥10k) so the `approx_threshold` and
  decay/prune behaviors are exercised, not just toy N.

### 6.3 De-confounding rules (method lessons — non-negotiable)

Prior eval artifacts that faked results; the harness must guard against each:
- **Strip stamped tokens** before measuring purity (a stamped class word inflated
  a purity number 0.71→0.35 when removed — circular). Never let the label leak
  into the representation under test.
- **Reject contrived masked-abstention tests** — masking one gold while a
  near-duplicate remains forces false positives and fakes a "broken gate."
- **Watch known eval artifacts, not real failures**: (a) the `approx_threshold`
  ANN path silently engaging at N>10k → recall reads 0; (b) aggressive aging
  decaying/pruning the whole store; (c) re-consolidating an already-persisted
  store inflating schema counts. Force the exact path and a clean store for
  apples-to-apples runs.

### 6.4 Code-level verification (per global engineering standard)

- **Mutation testing (RULE 2)** for any code implementing a gate or ranker:
  introduce a deliberate fault (e.g. invert the entropy comparison, off-by-one in
  top-N), confirm the relevant test fails, restore, confirm green. A gate whose
  test still passes when broken is not tested.
- **Failure-mode logging**: every boundary (embedder load, SQLite I/O, missing
  model, ANN fallback, abstain decision) logs structured context; secrets/PII
  never written to the store or logs.

### 6.5 Performance targets (proven numbers as the bar)

| Metric | Target | Source |
|---|---|---|
| recall@5 | ~0.35 (≥0.33 gate) | geometry-optimized dense path |
| recall@10 | ~0.42 | same |
| abstention AUROC | ~0.77 | β-retuned gate, honest metric |
| embedder lift (bge vs MiniLM) | +0.07 recall@5 | A/B, stacks with geometry |

### 6.6 Keystone eval — does move #6 compound? [MVP+ #6]

This is the experiment the MVP exists to run. The utility loop's one scaling claim
was already **refuted (0–3)**, so the eval is adversarial by default. External
anchor: **MemoryAgentBench** (test-time learning + selective forgetting; "current
methods master none") — the property under test.

- **Signal information, not latency**: CI-green is high-base-rate + gameable ⇒
  near-zero information; the eval must show the lift comes from the **ungameable
  negative** (revert / bug-on-files clawback), not from rewarding the gameable
  channel. A symmetric "+1 per CI-green" design is the null hypothesis to beat.
- **Task family**: `fix-failing-CI` in one service (recommended over
  `dependency-upgrade` for higher *credit density* — more verifiable settles per
  distinct memory). Explicitly **not** incident-triage (delayed/noisy signal) or
  doc-writing (LLM-judge only) — the loop is honest only where the reward is
  machine-verifiable.
- **Control arms (mandatory)**: Q-weighted recall vs (a) **utility-off** (λ_Q=0),
  (b) **recency-weighted**, (c) **frequency-weighted**. A win must beat *recency
  and frequency*, not just nothing — else the effect is trivial recency, not learning.
- **Win condition**: Q-weighted recall improves family-scoped **task-success rate**
  over the recency baseline, **CI-significant**, with (i) **non-saturating** accrual
  as volume grows and (ii) **within-family transfer** (a memory credited on task A
  helps task B in the same family).
- **Kill criterion (pre-committed)**: *if Q-weighted recall does not beat the
  recency baseline on family-scoped task success, CI-significant, with
  non-saturating accrual and within-family transfer → the keystone is dead, and
  moves #1,#2,#4,#5,#7 are NOT built.*
- **Inconclusive ≠ negative**: if too few recalls settle (sparse credit), Q is
  undertrained — widen the family or extend the window; do not read sparsity as
  refutation.
- **Anti-gaming check**: the positive only settles after N clean days, so "delete the
  failing test → CI-green" earns nothing once the bug-on-files clawback lands; confirm
  the clawback fires on a reverted PR. The author must not be able to write the signal
  that rewards them.
- **Loop-health instruments** (continuous, not just at the gate): the ε-slice and
  held-out slice measure self-degradation; the prediction-bias monitor flags a stale
  ranker. A "win" that is not stable on the ε / held-out arms is not a win.

---

## 7. Performance expectation (headline)

The fixed geometry + bge-small embedder is expected to deliver **~13× the recall
of the current shipped baseline** (recall@5 0.026 → ~0.35) at honest abstention,
in a system roughly **70–75% smaller** than the current build (≈4–6k vs ~22k LOC),
with no consolidation/federation/channels.

**[MVP+ #6] keystone target (pass/fail, not a number to maximize):** utility-weighted
recall beats the recency baseline on family-scoped task success, CI-significant,
non-saturating, with within-family transfer — and the lift must trace to the
**ungameable clawback** signal, not to rewarding CI-green. This binary is the MVP's
real return; a clean *negative* is a successful MVP outcome — it kills six unbuilt
moves cheaply.

---

## 8. Non-goals / deferred (gated on the §6.6 keystone passing)

The utility loop (#6) graduated *into* the MVP (§2 C9). Everything below is built
**only if** #6 compounds; a failed keystone deletes most of it. Order = the proven
build order once the value signal is real:

1. **On-manifold key + exemplars** — relevant only when consolidation returns; moot
   in the episodic MVP (we return verbatim exemplars — there is no μ to decode).
2. **Invariant write representation (#2)** — the topic-neutral "mechanism vector"
   for consolidation. Proven only on a *proxy* (clustering purity), so it waits
   until #6 turns the proxy into verified end-value. *First post-keystone add-on.*
3. **Salience-weighted replay (#1)** — a reweight of the #6 loop; needs the utility
   signal to weight by.
4. **Schema-consistency routing (#4)** — needs a `schema_id` back-ref + stable
   concepts; premature before #6.
5. **Generative replay (#5) / role projection (#7)** — anti-forgetting +
   reconsolidation richness; only once concepts are stable and proven valuable.
6. **Relational graph (full) / federated consolidation-as-learning** — the moat;
   streaming-graph ops flagged as costly → research-bet only, after #6, on a budget.

Channels (BM25/graph) + cross-encoder are specified as a gated TODO in **§8.3** — the
first recall enhancement to evaluate *after* the MVP benchmarks pass.

### 8.1 Autonomy ladder (honest expectations) [MVP+ #6]

- **L0** — agent self-reported `hive_outcome`: no autonomy (noisy, gameable). *Today.*
- **L1** — verifiable producer + clawback, usage-gated, score-split, ε-explored:
  largely autonomous. **This is the MVP.**
- **L2** — graded/counterfactual attribution, offline replay, held-out retraining:
  self-optimizing + self-correcting. *Research bet (with the relational-graph tier).*

L1 ships and is genuinely autonomous, but "perpetual self-improvement" needs L2's
negative-causal signal + monitoring — set that expectation, don't over-promise L1.

### 8.2 Human-approval gate — native-chat [MVP+ #6 / SEC]

- **New memories** → **approve-to-apply**: every write *stages*; the agent surfaces
  pending writes for approval in the harness's **native chat** ("save these N insights?
  [list]") — no separate UI, works anywhere there is a chat (i.e. every harness). The
  server admits a write only when the agent relays a human approval.
  - **Server-enforced gate** — `status='approved'` is the recall boundary; the server,
    not a hook, enforces it, so the guarantee holds under any harness (or none).
  - **Honest seam (MVP)** — the server trusts the agent to *relay* the human's "yes"
    faithfully. Fine in a high-trust solo/small-co setting (no adversarial insider);
    this is exactly the seam a future fleet tier would harden (§9).
  - **Batching / fail-closed** — interactive sessions can approve inline; accumulated
    proposals can be admitted in one batch prompt. No human present ⇒ writes stay
    `pending`, never auto-admitted.
  - **Tool surface (C7)** — `hive_pending(since?)` lists staged rows (id, text-preview,
    `proposed_by`, secret-scan verdict); `hive_approve(ids[], approver)` flips
    `status→approved` and stamps `approved_by`/`approved_ts` + indexes the row;
    `hive_reject(ids[])` drops it. These are the *only* `pending→approved` path, and
    `hive_recall` filters `status='approved'` unconditionally — the server is the gate,
    not a hook. `hive_write` returns the pending `id` so the agent can surface it.
- **Reversible** (utility nudges, rerank on already-admitted memories) → **auto-apply**
  + a periodic ledger digest with one-click veto + rollback (utility is versioned).
  Grow the existing `cortex-report.timer` into this digest.
- The shadow-mode config *controller* stays gated regardless (eval-membrane replay
  before any config write goes live).

Admission is upstream of the loop — the utility loop only ever credits already-admitted
memories.

### 8.3 Hybrid recall (dense + BM25) — gated TODO, first post-benchmark recall step

**Status:** deferred TODO. **Gate:** runs *after* the §6 MVP benchmarks are green
(dense-only recall@5 ≥ 0.33 + honest abstention). **Independent of the §6.6 keystone**
— this is a recall-quality enhancement, not part of move #6. Do **not** fold it into
the MVP; build it as the first recall experiment once the dense-only baseline is locked.

**Why deferred — and why it must be re-tested, not assumed either way.** Measured on
LoCoMo: at the conservative floor, channels-ON was a **no-op (+51ms)**, **BM25 alone
moved 0/60**, and value appeared only with a cross-encoder + a relaxed floor
([[project_channel_flip_gate_dominates]]). But that A/B was **conversational data** —
the domain where lexical match helps *least*. The target domain is code/agentic memory
(exact identifiers, error strings, file paths, SHAs), where BM25 should help *most*.
The negative result is therefore **domain-mismatched**: re-test on the real code corpus
before shipping *or* dismissing it.

**Design (the machinery already exists in the tree, defaulted off).**
1. **BM25 channel** — FTS5 inverted index over each admitted memory's verbatim text
   (exact lexical, sublinear; `storage/bm25_index.py`). Index on admission (post-§8.2
   approval). Exact ranking, no exhaustive-vs-ANN trade-off (discrete term match).
2. **Fusion** — Reciprocal-Rank Fusion of the dense top-k and the BM25 top-k
   (`core/fusion`, `channels.rrf_k`). Dense brings semantics/paraphrase; BM25 brings
   exact tokens. Neither alone — fuse.
3. **Cross-encoder rerank** — re-score the fused candidate pool and promote the winner
   (`research/rerank.py`). BM25 widens the pool; the reranker promotes the right doc.
   *BM25 without the reranker is worthless — do not ship that arm.*
4. **Entropy gate AFTER fusion** — abstention computed over the *fused* distribution so
   the never-hallucinate property holds. Floor set in BOTH the dense gate and the
   cascade gate via `from_flat` (set one not both = CONFIG_DRIFT bug).
5. **Optional class-tag pre-filter** — narrow to a context-class partition first
   (exact, O(1)), then hybrid-rank within it. Strongest for code memory.

**Prerequisites (do first).**
- **Fix the `rerank_top_k=8` truncation bug** — the reranker must **reorder, never
  resize** the tail (today it silently truncates recall@k for k>8). Blocking.
- FTS5 index backfilled for all approved memories on enable.
- The §5-D3 **floor product decision** — channels pay off only if the floor is relaxed
  enough that the gate isn't already refusing the queries hybrid would help. Floor +
  channels are tuned **jointly**, not sequentially.

**Config to flip (`config.py`, all currently off/safe).**
| Knob | MVP | Hybrid step |
|---|---|---|
| `channels.hybrid_recall` | `False` | `True` |
| `channels.rrf_k` | (n/a) | `60`, then tune |
| `channels.cross_encoder_rerank` | `False` | `True` (after the truncation fix) |
| `channels.rerank_top_k` | (n/a) | `≥ recall_top_n` (reorder-not-resize) |
| `recall.H_frac_max` | `0.5` | the relaxed §5-D3 floor (set in BOTH gates) |

**Acceptance criteria to ADMIT hybrid (the benchmark gate).**
1. Re-run the A/B on the **real code-identifier corpus** (not LoCoMo), at realistic N.
2. Hybrid+rerank beats **dense-only** recall@k, **CI-significant**, on that corpus.
3. **Abstention-safe**: refuse-rate stays channel-independent; never-hallucinate intact
   (no resurrection of a refused query).
4. The **+51ms** cross-encoder latency is justified by the recall gain at the floor.
5. Demonstrate the **exact-identifier win** (queries by function name / error string /
   path) — the domain case the LoCoMo test missed.

**Kill condition.** If hybrid does not beat dense-only on the code corpus
CI-significantly, OR only helps at a floor the product won't accept, OR is not
abstention-safe → **do not ship; dense-only stands.** BM25-alone is never shipped.

---

## 9. Security / threat model — MVP [SEC]

**Setting.** The MVP runs in a **high-trust** environment (solo dev → startup → small
company) as an **MCP-server-only** product across harnesses (Claude Code, Codex,
Conductor, …). ~99% of writes derive from in-codebase / git / GitHub task work — benign
by construction. The posture is right-sized to *that* reality; the parts that assume an
adversary are named **out of scope** rather than half-built.

**The server is the trust boundary.** The only component identical in every install is
the MCP server, so every guarantee lives there; hooks are optional per-harness
convenience, never load-bearing.

| Layer | Lives in | Guarantee | Carries |
|---|---|---|---|
| **Substrate** | **MCP server** (universal) | **hard** | secret scan, content-hash dedup, provenance stamp, `pending→approved` state machine, **approved-only recall**, neutral framing of recalled results |
| **Triggers** | hooks (per-harness) | best-effort | *when* to auto-propose: git post-commit (universal) + whatever the harness supports; absent ⇒ the agent calls `hive_write` itself. Bootstrapped by the server-served **`hive-init`** prompt the agent runs when a project has no hive hooks — the agent is the universal installer. |
| **Judgment** | the agent (any model) | best-effort | what is worth remembering; whether recalled text is steering it; relaying the human's approval |

**The one always-on floor: secret scan.** The `hive_write` handler runs a deterministic
credential scan (`sk-`/`AKIA`/`ghp_`/`xox`/JWT/PEM/connection-strings + entropy) and
**refuses or redacts before staging** — the substrate never persists a raw secret, the
way a DB rejects a malformed row. This is the one floor kept from the heavier design:
the content the MVP proudly captures (codebase/git/GitHub) is exactly where credentials
*accidentally* live, and a shared small-company store would otherwise sprawl one dev's
key to the whole team + 30 days of backups. Agent judgment sits on top of the floor, not
instead of it.

**Recall framing.** Recalled memories are presented as *reference context*, distinct from
the user's instruction stream. The server does its part (neutral, structured framing);
honoring "reference, not command" ultimately depends on the consuming model, which the
agnostic product does not control — see residuals.

**Honest residuals (named, not solved).**
- **Consuming model is trusted for instructions.** In agnostic mode you cannot force a
  third-party model to treat recalled text as data-only; a crafted prose payload could
  still steer a weak model. Accepted for the MVP audience; the secret scan + neutral
  framing are the parts enforced regardless of model.
- **Approval relay is agent-mediated.** The server trusts the agent to relay the human's
  "yes" (§8.2). Fine without an adversarial insider.
- **Auto-capture coverage is uneven.** Rich on Claude Code, thinner elsewhere, none on a
  bare MCP client; the everywhere-baseline is agent-initiated `hive_write` + a git
  post-commit hook.

**Explicitly OUT OF SCOPE for the MVP** (real in an adversarial *fleet*, deferred until a
multi-team deployment forces them): **memory poisoning / prompt-injection contagion**,
**multi-writer & Sybil abuse**, **utility suppression / clawback-gaming**, **per-source
trust & write-conflict resolution**, **cross-tenant isolation beyond the single-instance
boundary**. The heavier mechanisms that would address these (authenticated agent
identity, `source_trust`, `conflicts`, depth-invariant human-only admission,
suppression-resistant outcome attribution) are intentionally *not* built; their full
sketch is preserved in `.AGENT/RAM/Archive/HIVEMIND_VMIN_fleet-security-CUT.md` for if a
fleet tier is ever scoped.

**Acceptance test (§6.1 #5).** (a) a planted credential is refused/redacted by the secret
scan; (b) a pending write is never recallable (approved-only recall holds); (c) recalled
memories surface as reference context. A deeper STRIDE / supply-chain pass is a job for
`/gstack-cso` before the rebuild.

---

## 10. Reuse / delete map (greenfield rebuild)

The rebuild is greenfield (fresh package), but ~65% of the substrate already exists as
working, tested code in the current `cls_memory` tree (22,236 LOC). This map is the bridge
between "reference the codebase" and an actual reference: per component, **port** (lift
as-is), **port+flip** (change a default), **port+simplify** (lift the core, strip the
machinery around it), **build-new**, or **drop**. Line numbers were verified 2026-06-04 but
are *indicative* — the tree is the reference; confirm on port. The ~22k→~5k reduction is
concrete here: the **drop** column (federation, consolidation, channels, cold, bi-temporal,
diagnostics) is most of today's mass.

**Substrate (§2 C1–C8):**

| Component | In tree | Where | Disposition |
|---|---|---|---|
| Embedder (C1) | EXISTS | `embedder.py` (`SentenceTransformerEmbedder`, `TextEmbedder`) | **PORT+FLIP** → `embedder=st`, model `bge-small` |
| PCA head (C2) | EXISTS | `embedder.py:50` `ProjectionHead.pca()`, `ops/projection_trainer.py` | **PORT+FLIP** → `st_projection_head=pca` |
| Dense ranker (C3) | EXISTS | `serving/sources/native_source.py` | **PORT+SIMPLIFY** (dense cosine-kNN; drop the GMM/Mahalanobis episodic path) |
| Sparse key (C3 alt) | EXISTS | `core/sparsifier.py` | **PORT iff §5-D1 keeps it**, else **DROP** with `geometry.D/k` |
| Abstention gate (C4) | EXISTS | `serving/gate_bundle.py:70` `NormalizedEntropyGate` (BUG-001 resurrect guard) | **PORT as-is** — cleanest port; re-tune β only |
| Store (C5) | EXISTS | `storage/` (persistence, row_codec, blob_store) | **PORT+SIMPLIFY**: keep blobs + content-hash + CAS/`version`; **drop** bi-temporal (`t_valid/t_invalid/t_created/t_expired`), supersession (`superseded_by/supersedes/subject_key`), `tombstoned`; **add** `status/proposed_by/approved_by/approved_ts` |
| Write path (C6) | PARTIAL | `serving/service.py` `write_text` (writes immediately) | **BUILD-NEW** staging + status machine over the existing write |
| Secret scan (C6) | ABSENT | — | **BUILD-NEW** (~0.2k: pattern set + entropy; refuse/redact pre-stage) |
| MCP surface (C7) | EXISTS | `serving/mcp_tools.py`, `serving/mcp_server.py` | **PORT+EXTEND**: keep write/recall/fetch; **add** pending/approve/reject; **drop** consolidate/schemas/recall_cold/restore_cold |
| Eval membrane (C8) | EXISTS | `research/eval_membrane.py`, `research/metrics_ir.py`, `serving/cli/commands/eval.py` | **PORT as-is** (dev-time) |
| Migration (tier-D) | EXISTS | `ops/migration.py` | **PORT+SIMPLIFY** (retrain + reembed-from-text; also the one-time corpus import — §12) |
| Backup | EXISTS | `cortex-backup.timer`, `retention.backup_keep` | **PORT** (ops floor) |

**Move #6 (§2 C9–C10):**

| Component | In tree | Where | Disposition |
|---|---|---|---|
| Loop machinery (C9) | EXISTS (shadow) | `controller.py:250` `apply_outcomes_from_sink` ← `service.py:942`; reconsolidate `core/episodic.py:422` | **PORT** — the "already wired" credit path; watermarked + CAS-bounded |
| Exposure ledger | PARTIAL | `ops/telemetry.py` (trace→memories + margin; **no** `task_ref`) | **PORT+EXTEND** (add `task_ref`) |
| Surfacer | EXISTS, crippled | `serving/service.py:184` `1+max(0,u)` | **PORT+FIX** — un-cripple to `weight × f(utility)`, f demotes |
| Utility posterior | ABSENT (soft via `SalienceConfig.utility_sigma`) | — | **BUILD-NEW** `utility` Beta-Bernoulli table |
| `task_outcomes` | ABSENT | — | **BUILD-NEW** (producer state machine — §3 / §11) |
| Producer (C10) | ABSENT | — | **BUILD-NEW** — git watcher (§11), the bridge |
| Config controller (shadow tuner) | EXISTS | `controller.py` (`shadow_mode=True`) | **PORT, stays gated** — never flipped in the MVP |

**Deliberately dropped (exists, excluded — the LOC savings):**

| Dropped | Where | Why |
|---|---|---|
| Consolidation / GMM / `schemas` | `federation/consolidator.py` | clusters measured as noise (silhouette 0.036) |
| Federation / DP / SecAgg | `federation/`, `privacy`, `secagg` | unproven moat (§4.4) |
| Channels (hybrid/BM25/graph/cross-encoder/trust/cold-in-cascade) | `channels.*`, `research/rerank.py`, `core/fusion` | no-op at the floor; BM25+rerank deferred to the gated §8.3 |
| Cold tier | `cold_episodes/schemas/blobs`, `recall_cold/restore_cold` | post-MVP |
| Bi-temporal + supersession | episodes cols above | not needed for episodic recall |
| Procedures / audit / decisions / graph_* | `procedures`, `audit`, `decisions`, `graph_*` | fleet / compliance tier, out of MVP |
| Diagnostics | `diagnostics/` (1,829 LOC) | minimize to health only |

**LOC reconciliation.** Ported core after stripping (gate, ranker, embedder, store, eval,
migration, loop) ≈ 3–4k; build-new (secret-scan, staging/approval, producer, posterior,
`task_ref` wiring) ≈ 1.5–2k; the drop column is the bulk of today's ~16k non-substrate
mass. Lands the §7 ~4–6k target.

---

## 11. The trace ↔ outcome join — full mechanism (the one new design)

This is the single mechanism specified by **neither** the prior spec **nor** the existing
code, and it is the heart of move #6. Everything *upstream* (recall→trace→exposure) and
*downstream* (sink→attribution→posterior→surfacer) exists; the **bridge** — stamping a
verifiable git outcome onto the recall trace that informed it — is the whole new build.

**Design principle: the agent writes provenance into git; the server reads git.** No trusted
MCP message carries the outcome. The join key lives in the commit (durable, auditable) and
the verifiable signals (merge / revert / bug-on-files) are themselves git facts. So the
producer (C10) is a server-internal git watcher — the trust boundary stays in the server
(§9) — and the agent's only new behavior is a one-line commit trailer.

**Three hops:**

1. **Recall → trace** *(EXISTS).* `hive_recall` returns a `trace_id`; `ops/telemetry.py`
   records `trace_id → [(episode_id, recall_margin)]`. The server keeps a per-(tenant,
   agent) list of trace_ids issued since that agent's last stamped commit.
2. **Trace → commit** *(NEW — window-primary association; Decision A).* The server already
   tracks each agent's recent recall window (hop 1). At commit time the watcher **associates
   the commit `<SHA>` with that agent's in-window traces by default** (`producer.assoc_window_s`),
   writing `exposure.task_ref = <SHA>` + `task_outcomes` rows, discounted by `recall_margin`
   and ε-explored so the inevitable over-attribution is absorbed. The agent MAY append a
   `Hive-Trace: <T1> <T2> …` commit trailer (taught by `hive-init`, one line like
   `Co-Authored-By`); when present it **overrides** the window with the exact set, at higher
   credit weight. Robust by default (no dependence on the agent remembering to stamp), precise
   when it does. **Stamp-hit-rate is logged** — it feeds the Phase-2 readiness gate (§12).
3. **Commit → verifiable outcome** *(NEW — the watcher, C10).* Per the §4.7 schedule:
   merge ⇒ small **provisional +** (`state=provisional`, `settle_at=merge_ts+settle_days`);
   CI status recorded but ~0 reward; the **settlement sweep** (same poll tick) settles the
   `+` once `settle_at` passes clean; a **revert** of the SHA ⇒ large **− clawback**,
   immediate; a later **bug-fix commit** (`producer.bugfix_pattern`) ⇒ large **− clawback**,
   delayed. **Clawback precision (load-bearing — the − is −1.0; Decision B):** the bug-on-files
   match is **blame-line overlap**, not same-file — the fix must touch lines the original
   commit *introduced* (`git blame`), so a coincidental edit to the same file does NOT punish
   a good memory. The watcher **follows branch→merge including squash** (resolving the
   merged/squashed SHA via git-log / PR) so the join key and the blame target survive a
   squash-merge that would otherwise drop the trailer and break attribution silently.

**Then (EXISTS):** each settled/clawed reward is written to the telemetry sink keyed by
`task_ref`; `apply_outcomes_from_sink` (`service.py:942`) drains it during the maintenance
tick, splits the reward across that trace's exposed memories **by `recall_margin`**
(co-occurrence discount), and updates the `(episode_id, family_scope)` Beta-Bernoulli
posterior. The surfacer reads `weight × f(utility)` once the posterior CI excludes 0.

**`family_scope` derivation (resolves the §2 hand-wave):** computed by the watcher **at
link time** from git facts — `git-remote` (the watched repo), `language` (dominant file
extension in the commit), `coarse-workflow` (classified from the commit: bug-fix-pattern ⇒
`fix-ci`/`bugfix`; manifest-only change ⇒ `dep-upgrade`; else `general`). One denormalized
string, O(1), no taxonomy — and it lives on the credit event, not the episode.

**Failure modes handled:** many-traces→one-commit (the trailer lists all); one-trace→
many-commits (each commit credits independently); async CI (the watcher polls, not blocks);
revert/bug days later (settlement window + clawback sweep); unstamped commit (window-default
association, margin-discounted + ε); author self-reward (the *signals* are revert/bug-on-files — git facts the author
cannot write to reward themselves; the only thing they relay is the stamp, §9 high-trust).

---

## 12. Build order & process model

**Phasing — build the collection before the self-modifying apply (the L1a/L1b split).**

- **Phase 1 (L1a — ship).** Substrate (§2 C1–C8, geometry-fixed) + the outcome-*collection*
  plumbing (C10 producer, `exposure.task_ref`, `task_outcomes`, the `utility` posterior) +
  secret-scan + staging/approval — but **utility observed, not applied**:
  `channels.utility_rerank=False`; the surfacer is un-crippled but inert until posteriors
  exist. This ships a usable, *measurable* product (recall@5 ≥0.33, honest abstention) **and**
  starts accruing the verifiable, clawback-settled outcome stream the keystone needs. The
  prediction-bias monitor (guardrail 3) and the versioned `utility` layer (guardrail 4) are
  built here — the monitor is the Phase-2 *readiness instrument*.
- **Phase 2 (L1b — gated on §6.6).** Flip `channels.utility_rerank=True`; add the live-loop
  guardrails ε-randomization (1) and held-out isolation (2); run the control-arm A/B (utility
  vs **recency** vs **frequency**). Keep or kill per the §6.6 pre-committed criterion.

*Why this order:* the apply-half is specified against values that do not exist until the
stream runs (§4.7's own concession); the substrate is the *instrument* that measures #6; and
a failed keystone deletes Phase 2 **before** it is built, not after. Same total functionality
in the success branch — strictly smaller first-ship.

**Phase-2 readiness gate (pre-registered — the consequence of Decision A's window-primary
join).** Do not run the §6.6 keystone underpowered. Phase 1 logs commit→trace
**stamp-hit-rate** and **credit density**; Phase 2 starts only once ≥ `N_settled` settled
outcomes span ≥ `M_memories` distinct memories within the chosen family (fix `N,M` on the
corpus *before* the run). Sparse credit ⇒ widen the family or extend the window — never read
it as a negative (§6.6's "inconclusive ≠ negative").

**Process / concurrency model (greenfield).**

- One OS process; one SQLite file in **WAL mode**.
- Hot path (recall/fetch) is read-only and concurrent.
- Writers are serialized via the existing optimistic CAS/`version` column (port from
  `core/episodic.py`): `hive_write` staging, approval admission, the producer's settlement
  sweep, backup. **No consolidation timer** (§4.4).
- The producer runs **in-process** (its poll / settlement / drain tick shares the single-writer
  discipline above) — a separate-process timer would add a 2nd SQLite writer and would need
  `BEGIN IMMEDIATE` + `busy_timeout`. Backup may stay a separate read-only snapshot.
- Embedder loaded once, frozen, CPU-local; no network on the hot path.

**Existing-memory import (one-time).** The fresh store starts empty. To seed the eval corpus
(≥10k episodes — §6.2), re-embed-from-text from the old store via the ported
`ops/migration.py` reembed path (it already re-embeds verbatim text through the new PCA head).
A one-shot import, not a hot-path migration; the old bi-temporal/supersession columns are
dropped on the way in.

---

*This file is the **source of truth for a greenfield Hivemind v-min rebuild**: author the
software-design doc in a fresh directory from §1–§12, with the current `cls_memory` tree as
the port reference per the §10 map. It currently lives in working-memory (`.AGENT/RAM`);
persist it into the new repo (e.g. `docs/SPEC.md`) when the rebuild starts.*
