# TODOs — Demand-Driven Knowledge Gap Additions

Derived from: "Demand-Driven Context: A Methodology for Building Enterprise Knowledge Bases Through Agent Failure" (arXiv:2603.14057). Core thesis: agent retrieval failures are demand signals and should drive knowledge base prioritization.

> **Status (2026-06-10, autonomy lifecycle landed):**
> **TODO 1 — LANDED-BY** the trust-lifecycle build (`recall_misses` table, the
> `ExposureLedger.record_miss` path with the three miss types, secret-scanned
> query text; `tests/mcp/test_tools_v2.py`).
> **TODO 2 — OBSOLETE**: the pending/approval queue it would weight no longer
> exists (client-gated v3 removed `hive_pending`/`hive_approve`).
> **TODO 3 — LANDED-BY** `hive_health(include_gaps=true)` (the fold-in option):
> deterministic cosine-clustered top-10 gap report in `hive/app/gaps.py`.
> **TODO 4 — RECAST as the demand-promotion rule**: instead of miss-triggered
> *proposals to a human queue*, misses now mechanically PROMOTE a matching
> quarantined capture (`DemandRule`: ≥ demand_m misses, ≥1 non-writer identity,
> no servable competitor). The feedback loop closed without load-bearing humans.

---

## TODO 1 — Record recall misses (enabler for all below)

**File:** `hive/domain/recall.py`, `hive/adapters/store_sqlite.py`, `hive/domain/models.py`, `hive/domain/ports.py`

`RecallPipeline.recall()` currently calls `record_exposure` only on hits. When the entropy gate abstains or the result is `EMPTY_NO_DATA`, the query evaporates with no record.

Add a parallel `record_miss(query_text, agent_id, miss_type, ts)` path — symmetric to `record_exposure`. Miss types:
- `no_match` — sparse coverage, no candidates found
- `abstained` — entropy gate fired (high ambiguity)
- `secret_refused` — the query text itself triggered the scanner

Store in a new `recall_misses` table: `(id, query_text, query_vector BLOB, agent_id, miss_type, ts)`. Secret-scan the `query_text` before persisting (same scanner, refuse→drop, redact→masked). This table is the enabler for TODOs 2, 3, and 4.

Mutations to verify: drop the `record_miss` call → miss count stays zero under forced-abstain test; wrong `miss_type` stored → type-filtered query returns wrong count.

---

## TODO 2 — Demand-weight the approval queue

**File:** `hive/app/mcp_server.py` (`_handle_recall` / pending surface), `hive/domain/models.py` (`PendingRow`)

Pending proposals currently surface to the approver with no context about retrieval demand for the proposed topic. A proposal addressing a topic queried unsuccessfully 40 times should rank above one queried once.

When surfacing pending proposals (session-start `<system-reminder>`):
1. For each pending proposal, compute `demand_score` = count of `recall_misses` rows whose `query_text` is semantically close to the proposal's `content_text` (cosine similarity via `ExhaustiveCosineIndex` against the miss vectors, threshold ~0.7), within a rolling 7-day window.
2. Sort pending proposals by `demand_score DESC` before surfacing.
3. Include the score in the proposal surface: `"queried N times with no result (last 7d)"`.

No schema change to `PendingRow` needed — `demand_score` is computed at surface time, not stored.

---

## TODO 3 — Add `hive_gaps` tool (knowledge gap report)

**File:** `hive/app/mcp_server.py`, `hive/app/tool_defs.py`, `hive/domain/ports.py`

`hive_health` reports what IS in the store. The paper's key insight is that the more actionable metric is what is systematically MISSING.

Add a `hive_gaps` MCP tool (9th tool — or fold into `hive_health` as an optional `include_gaps=true` param if the 8-tool constraint is hard). Returns:

```json
{
  "top_gaps": [
    {
      "representative_query": "...",
      "miss_count": 12,
      "miss_types": {"no_match": 10, "abstained": 2},
      "last_seen": "2026-06-05T...",
      "cluster_size": 4
    }
  ],
  "window_days": 7,
  "total_misses": 31
}
```

Implementation: cluster `recall_misses` rows by cosine similarity (reuse `ExhaustiveCosineIndex` on miss vectors), pick the highest-frequency clusters, surface the most recent `query_text` as the representative. Cap at top-10 clusters to keep the tool fast.

This turns the miss log into an actionable queue — the human can read it and decide what to `/hive-mark` or `/hive-log-bug` proactively.

---

## TODO 4 — Miss-triggered write proposals (close the feedback loop)

**File:** `hive/app/mcp_server.py` or a new `hive/app/gap_proposer.py`, `hive/domain/ports.py`

The current write-trigger path (Stop hook → deterministic-first detector → propose) is retrospective — triggered by output. The paper argues for a complementary path triggered by input failure (what the agent needed but couldn't find).

At session-start (alongside pending-proposal surface), for the top-N gap clusters from `recall_misses` (N=3, configurable):
1. Check if a pending or approved episode already covers the cluster (cosine threshold ~0.7 against the store). If yes, skip — the gap is already being filled.
2. If no coverage, emit a gap-triggered proposal: `proposed_by='gap-detector'`, `content_text` = a brief summary of the miss pattern ("Queried 12 times, no result: [representative query text]"), status `pending`.
3. These proposals go through the same approve/reject flow as Stop-hook proposals — human-gated, never auto-approved.

This does NOT replace the Stop hook path. It adds the demand side: what the agent was looking for but couldn't find, surfaced as a write prompt for the human.

Constraint: only propose for clusters with `miss_count >= min_demand` (default 3) to avoid noise from one-off queries.

---

## Dependency order

```
TODO 1  (record_miss table + RecallPipeline instrumentation)
  └─ TODO 2  (demand-weighted approval surface)
  └─ TODO 3  (hive_gaps tool)
  └─ TODO 4  (miss-triggered proposals)
```

TODOs 2, 3, and 4 all depend on the `recall_misses` table from TODO 1. Implement TODO 1 first.

---

## TODO 5 — Strip commit-stamp trailers when cutting outcome-credit — ✅ LANDED 2026-06-16

LANDED with the minimization producer cut (plan §D1, step 2): the rendered rules block
carries no `Hive-Credit` trailer text (`test_rules_block_has_no_credit_trailer`), the
`HOOK_MANIFEST` `commit` hook + `ProducerConfig`/`stamp_trailer` + the `trailer_key`
echoes (`_handle_init` / `hive_health` / `RulesBlock`) are deleted, and the hive_init
phase-1→2 handshake still confirms on the recomputed block hash. Original notes below.

Part of the credit/origin subsystem removal (`docs/PLANS/MINIMIZATION-PLAN.md` §D1). The credit loop has two halves: a **consumer** (the GitHub scanner that harvests trailers → wins/losses, removed with `originctl`) and a **producer** (agents are instructed to stamp `Hive-Credit: <trace_id> <episode_id>…` git trailers on commits a recalled memory shaped). Removing only the consumer leaves agents writing dead trailers nothing reads — the stamps that *link commits to memories* must go too.

**Files / what to strip (producer side):**
- `hive/app/onboard.py` — the "Credit your work" section + `<TRAILER_KEY>` interpolation in `_BLOCK_TEMPLATE` (~:229-260); the `HOOK_MANIFEST` `commit` trailer-stamp hook (~:124-127); the `InstallPlanner` / `render_rules_block` empty-trailer fail-fast (~:271-275, :296-300).
- `hive/app/config.py` — `ProducerConfig.stamp_trailer` (:127-135) + its `CONFIG_AUTHORITY` / `RELOAD_TIER` entries.
- `hive/app/mcp_server.py` — the `trailer_key` field echoed by `_handle_init` (phase-1) and `hive_health`; verify no other reader (`client.py`).

**Coupling:** if the onboarding cut (plan §B1) is taken, the rules-block + manifest pieces disappear with `onboard.py` — but `ProducerConfig` still must be removed here. If credit is CUT but onboarding KEPT, the rules block survives minus its credit section, so the `<TRAILER_KEY>` interpolation and the empty-trailer fail-fast must be removed *together* (an empty trailer otherwise raises by design).

**Mutations to verify:** a rendered rules block contains no `Hive-Credit`/trailer text after the strip; `hive_init` phase-1→phase-2 still round-trips on the recomputed block hash with the trailer removed; boot does not fail-fast on a missing `stamp_trailer`.

**Note:** only relevant if the credit subsystem is cut (a live product decision — see plan §5 T3). If credit is kept, this TODO is void.

---

## TODO 6 — Conflict-resolution logging + admin skill (the supersession-review workflow)

**File:** `.claude/skills/hive-resolve/` (new admin skill, documented in `ADMINSKILLS.md`), `hive/app/gaps.py`, `hive/app/mcp_server.py`, `hive/app/observability.py`

A contradiction between two memories is surfaced TODAY only as a *pull* report:
`hive_health(include_gaps=true)` → `contested_misses` (`gaps.py`), the "supersession-review
queue", resolved by one `hive_write(replaces=<episode_id>)`. There is no push signal and no
guided workflow — if no operator polls the report, conflicts accumulate invisibly. Build:

1. **Logging / notification (the push signal).** Emit a structured, throttled log line + a
   `hive status`-visible counter when the contested queue is non-empty. Today the only
   contested-related log is `mcp.contested_report_failed` (the failure path) — there is no
   "N memories pending review" signal. Host-side KPI reads deliberately exclude
   gaps/contested (they need the live servable index — `cli.py`), so the push path must
   originate daemon-side where the index is warm.
2. **Admin skill `/hive-resolve`** (alongside `/hive-tune` in `ADMINSKILLS.md`): pull the
   contested report, render each `{episode_id, trust, miss_count, miss_types, last_seen}`
   with the conflicting servable text side by side, and walk the operator through the
   `hive_write(replaces=…)` resolution. Confirm the queue shrank afterwards off the
   `admission.superseded` audit row (close the loop).

**Edge cases to cover — the coverage holes in the demand-derived report (`contested_misses` only
sees conflicts that PRODUCE misses/abstains):**
- **Quiet contradiction, dominant winner.** Two contradictory servable rows where one scores
  much higher: recall serves it CONFIDENT (the other as a lower hit), emits NO miss, so the
  demand-derived report never sees it. Needs a DIRECT pairwise scan over the servable index
  (rows within shadow/contested τ whose text — or outcome credit — diverges), not just
  clustered misses.
- **Quiet contradiction, co-served pair.** Both rows returned in one CONFIDENT result without
  splitting softmax mass enough to trip the entropy gate → no abstain → no miss → never
  queued. Same direct-scan fix.
- **No notification / pull-only.** The queue is invisible until `hive_health(include_gaps=true)`
  is called; the skill + log line are the missing push.
- **Trust asymmetry — never auto-resolve.** An `established` (human-vouched) vs `provisional`
  (demand-promoted) conflict must NOT be auto-superseded by recency; supersession is
  human-only by design. The skill PRESENTS; the human decides.
- **Self-resolving provisional.** A conflict may clear on its own when a provisional row hits
  its lazy TTL; don't prompt a human to supersede a row about to lapse.

**Mutations to verify:** seed two contradictory servable memories — (a) mass-splitting (loud:
already surfaced by `contested_misses`) and (b) one-dominant (quiet: currently invisible). The
new direct scan must surface BOTH; resolving one via `hive_write(replaces=)` removes the pair
from the next report and writes the `admission.superseded` audit row; the non-empty-queue log
line fires before resolution and not after.
