# TODOs — Demand-Driven Knowledge Gap Additions

Derived from: "Demand-Driven Context: A Methodology for Building Enterprise Knowledge Bases Through Agent Failure" (arXiv:2603.14057). Core thesis: agent retrieval failures are demand signals and should drive knowledge base prioritization.

---

## TODO 1 — Record recall misses (enabler for all below)

**File:** `hive/domain/recall.py`, `hive/adapters/store_sqlite.py`, `hive/domain/models.py`, `hive/domain/ports.py`

`RecallPipeline.recall()` currently calls `record_exposure` only on hits. When the entropy gate abstains or the result is `EMPTY_NO_DATA`, the query evaporates with no record.

Add a parallel `record_miss(query_text, agent_id, miss_type, ts)` path — symmetric to `record_exposure`. Miss types:
- `no_match` — sparse coverage, no candidates found
- `abstained` — entropy gate fired (high ambiguity)
- `secret_refused` — the query text itself triggered the scanner

Store in a new `recall_misses` table: `(id, query_text, query_vector BLOB, agent_id, miss_type, ts)`. Secret-scan the `query_text` before persisting (same scanner, refuse→drop, redact→masked). This table is the enabler for TODOs 2, 3, and 4.

RULE-2 mutations to verify: drop the `record_miss` call → miss count stays zero under forced-abstain test; wrong `miss_type` stored → type-filtered query returns wrong count.

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
