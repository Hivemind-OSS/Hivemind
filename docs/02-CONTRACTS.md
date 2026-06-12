# 02 — End-to-End Contracts

> Step 3: the consolidated data schema, every port interface, the MCP tool schemas, the request flows, the `hive_init` handshake, the module→contract ownership index, and the prose-boundary→enforcement table — in one place, concrete (real Python signatures, real DDL, real test names).
>
> This document is **authoritative over any contradicting module-spec prose**. Where a Cluster A/B/C resolution overrode a module text (the surfacer `order(...)` signature, the `NormalizedEntropyGate` softmax-over-β·sims extension, the single-DB ledger collapse, the BUILD-NEW head codec, the index-as-warm-cache crash-recovery guarantee, the import-path secret scan, the recall-group `epsilon_explore`), this file reflects the **resolution**, not the stale module line.
>
> **Locked pins honored verbatim:** hexagonal ports-and-adapters; SINGLE Docker service image; SQLite-WAL on one named volume; **exactly 8 MCP tools**; geometry bge-small(384)→PCA→d=256 dense cosine; exhaustive index AUTHORITATIVE (never silently ANN); normalized-entropy abstention (`H/ln(N_eff) > 0.5 ⇒ abstain`); the four invariants (never-hallucinate / approved-only / secret-safe / verifiable-credit-only); tests are a first-class contract (TDD + RULE-2 mutation per gate/state-machine/credit path).

---

## §0 — Trust-lifecycle delta (2026-06-10, AUTONOMY-PLAN v2 — supersedes the marked fragments below)

The mechanical memory-lifecycle build (quarantine → demand-promotion → decay → supersession)
amends this registry **additively**. Where the sections below still read "exactly 8 tools",
"5 tools", `hive_pending`/`hive_approve`, or the bare `status='approved'` recall predicate,
THIS section wins. The store starts **clean** (human decision): no migration/backfill exists,
and an old-format `episodes` table is refused at store construction.

### §0.1 DDL delta (all in the base `_SCHEMA`; column default = fail-safe)

```sql
-- episodes gains:
trust TEXT NOT NULL DEFAULT 'quarantined',   -- quarantined|provisional|established|deprecated
superseded_by INTEGER,                       -- latest applied successor (NULL = live)
last_active_ts INTEGER NOT NULL DEFAULT 0;   -- liveness clock: capture/write, promotion, exposure
CREATE INDEX idx_episodes_trust ON episodes(trust);
-- exposure gains: agent_id TEXT (who was served)
CREATE TABLE recall_misses(                  -- every non-answer, the demand signal
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query_text TEXT NOT NULL,                  -- ''/masked when the scanner fired
  query_vector BLOB,                         -- NULL on secret_refused
  agent_id TEXT NOT NULL,                    -- the asker (the anti-gaming key)
  miss_type TEXT NOT NULL CHECK(miss_type IN ('no_match','abstained','secret_refused')),
  ts INTEGER NOT NULL);
CREATE TABLE evidence_events(                -- SERVER-WRITTEN audit ONLY (no tool writes here)
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id INTEGER NOT NULL, kind TEXT NOT NULL,  -- kind ∈ {promote, ttl_expired, supersede}
  actor TEXT NOT NULL, ts INTEGER NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}');
```

`trust` is enforced in the `Episode` model (no DDL CHECK — Appendix-A layers widen the enum
without a table rebuild). The old `(status='approved') = (approved_by IS NOT NULL)` CHECKs do
not exist in the shipped schema; the Episode invariants v2 are: `approved_by ⇒ approved`,
`trust ∈ (established|provisional) ⇒ approved`, `superseded_by ⇒ deprecated`.

### §0.2 The serving predicate (replaces §1.2's bare status check)

```
servable ≡ status='approved' AND (
      trust='established'
   OR (trust='provisional' AND last_active_ts > now − provisional_ttl) )
```

ONE source: `hive.domain.lifecycle.is_servable`, re-evaluated by (1) `store.scan_servable`
(boot/index rebuild; `scan_approved()` is its no-clock FAIL-CLOSED alias = established-only),
(2) promotion/demotion index membership sync, (3) the RecallPipeline RESOLVE step (a lapsed
row is dropped BEFORE it can be exposed — exposure refreshes liveness and would resurrect
it), and (4) the mcp_server per-hit belt (redundancy). `status` now reads as *materialized*
(scanned + embedded + blob complete); `trust` carries trust — naming debt, accepted.

### §0.3 The lifecycle state machine

```
            hive_capture                    demand (DemandRule)            TTL (decayed)
  (born) ──────────────► quarantined ────────────────────► provisional ──────────────► deprecated
                              │  TTL: now−max(ts,last_active) > q_ttl        ▲   exposure refreshes
                              └──────────────────────────► deprecated        │   last_active_ts
  hive_write ────────────────────────────────────────────► established ──────┘
  (human-vouched, served instantly)        hive_write(replaces=X) │ supersede: the ONLY
                                                                  ▼ retirement of established
                                                              deprecated (+superseded_by stamped)
```

Promotion (`DemandRule.decide`, pure): ≥ `demand_m` window misses at cosine ≥ `demand_tau`,
≥1 matched identity ≠ the writer (structural anti-gaming), no servable competitor at cosine
≥ `competitor_tau` (inclusive veto). Non-finite inputs fail CLOSED. Triggers run synchronously
at the two demand-changing moments: `on_capture` (admission) and `on_miss` (recall, record-
then-trigger); the decay sweep runs at boot (before index rebuild) and after each capture.
`established` never age-decays; supersession (`store.supersede`, one owner, one tx, ONE audit
row, idempotent, self-supersede refused) is its only retirement.

### §0.4 Tool surface: EXACTLY 6 (supersedes "8 tools" / "5 tools" prose)

`hive_write` (+ optional `replaces`: validated-exists BEFORE staging, retirement after the
new row lands; envelope gains `superseded`) · `hive_capture` (NEW — required `text`; lands
`trust='quarantined'`, `approved_by` NULL; `{status:'disabled'}` when autonomy is off; NO
replaces) · `hive_recall` (hits gain `trust` + `ts`; exposure on CONFIDENT; every non-answer
recorded as a miss, secret-scanned first: REFUSE ⇒ empty text + NULL vector +
`secret_refused`; REDACT ⇒ masked text + re-encoded vector) · `hive_fetch` (+
`superseded_by: {episode_id, content_hash}` terminal-successor annotation) · `hive_health`
(+ `trust_counts`, `n_misses_7d`, `include_gaps` → top-10 deterministic cosine-clustered gap
report, `manifest_outdated`) · `hive_init` (manifest **v2**: capture-without-asking via
hive_capture; `correction` hook = hive_write replaces). `hive_evidence` does NOT exist.

### §0.5 ExposureLedger port (revived, sessionless)

```python
@runtime_checkable
class ExposureLedger(Protocol):
    def record_exposure(self, trace_id: str, items: Sequence[tuple[int, float]],
                        *, agent_id: str, ts: int) -> None: ...   # bumps last_active SAME tx
    def record_miss(self, query_text: str, query_vector: Optional[bytes],
                    agent_id: str, miss_type: str, *, ts: int) -> None: ...
```

Implemented by `SqliteEpisodeStore`; the RecallPipeline REQUIRES it (plus `clock_now`,
`scanner`, `provisional_ttl_s`) — fail-open per call, never silently absent. With
`autonomy.enabled=false` the read path writes ZERO rows (byte-stable; trust/ts labels stay,
additive-only) and capture refuses with `{status:'disabled'}`.

### §0.6 Config group

`autonomy`: `enabled` (tier C) · `demand_m=3` · `demand_window_days=14` · `demand_tau=0.75`
· `competitor_tau=0.85` · `quarantine_ttl_days=14` · `provisional_ttl_days=45` (all knobs
tier B; taus finite in (0,1], counts ≥ 1).

---

## §0b — Fleet-convergence delta (2026-06-11, CONVERGENCE-PLAN CV1–CV5 — additive over §0)

### §0b.1 The complete mechanical trust ladder (CV2 adds the second rung)

```
            hive_capture            DemandRule (landed)             SurvivalRule (CV2)
  agent ──► QUARANTINED ──demand──► PROVISIONAL ──survival-spread──► ESTABLISHED
                 │ TTL 14d               │ TTL 45d (exposure-refreshed)    │ never decays
                 ▼                       ▼                                 ▼ human supersession only
             DEPRECATED ◄────────────────┘                  hive_write(replaces=) ──► DEPRECATED
```

`SurvivalRule.decide(writer, exposures, now)` (pure, total): establish IFF ≥ `survival_e`
distinct identities − {writer} (the same anti-gaming key as demand) AND ≥
`survival_min_exposures` exposures AND first-to-last span ≥ `survival_days` (inclusive).
Evaluated ONLY inside `LifecycleService.sweep` (decay first; window = the provisional
liveness horizon); an establishment is `set_trust(ESTABLISHED)` + ONE `establish` audit row
(`evidence_events.kind` enum gains `establish`). Defaults `survival_e=2 / survival_days=14 /
survival_min_exposures=5`: establishment needs a 3-seat fleet minimum; 2-seat fleets keep
content provisional (served, labeled). Risk accepted (01-DECISIONS D-C2): same `established`
state, audit row records `rule=survival`, contested report watches it, supersession stays the
cheap correction.

**Lifecycle store surface (duck-typed, the `quarantined_candidates` precedent — deliberately
NOT a Protocol; behavior-tested on the real adapter):**

```python
def survival_candidates(self, *, since_ts: int, min_exposures: int) -> list[tuple[int, str]]:
    # (episode_id, proposed_by) of PROVISIONAL rows with >= min_exposures exposures
    # STRICTLY after since_ts — ONE aggregate (JOIN…GROUP BY…HAVING), never N+1.
def exposures_for(self, episode_id: int, *, since_ts: int) -> list[ExposureRow]: ...
    # ExposureRow(agent_id, ts), ts-ascending; NULL agent_id coerces to ''.
```

### §0b.2 Solo mode (CV1 §3.5 — operator-consented, NOT client-gameable)

`autonomy.solo_mode=true` (env, default OFF) swaps `DemandRule`'s diversity clause: distinct
identity → **elapsed-span demand** (`max(ts) − min(ts)` over matched misses ≥
`solo_min_span_days`·86400 — ELAPSED, never calendar-day buckets; a midnight-straddling burst
never promotes; failure reason `solo_span`). All other clauses unchanged. Survival-establish
is deliberately untouched ⇒ at solo scale `established` is reachable ONLY via `hive_write`
(HITL held structurally). `hive_health` gains `solo_hint` when ≥ `demand_m` window misses
all carry ≤ 1 identity while solo_mode is off (the stall self-describes).

### §0b.3 hive_recall serve path (CV3, default OFF) + report envelopes (CV3/CV4)

`recall.shadow=true` (tier C, default **OFF** ⇒ byte-identical, golden-tested): within one
confident resolved shortlist, of any pair with pairwise cosine ≥ `recall.shadow_tau` (0.95)
only the winner serves — trust rank (established > provisional), then newer `ts`, then lower
id. Runs at RESOLVE **before** exposure (a shadowed row's liveness is never refreshed by the
query that hid it); non-finite/absent vectors never shadow (fail-open = serve both); a filter
fault serves unfiltered, never EMPTY.

`hive_health(include_gaps=true)` additionally returns `contested` + a fixed `contested_note`:
miss clusters probed ONCE per cluster against the servable index; clusters within
`autonomy.contested_tau` (0.80) of a servable row group by that episode —
`{episode_id, trust, miss_count, miss_types, last_seen}`, the supersession-review queue.

`hive_health(include_trends=true)` (CV4) returns `{current, previous, deltas}` over two
half-open 14d windows `(lo, hi]`: confident/abstain/no_match counts + `confident_rate`,
`demand_entropy` (H/ln C over miss-cluster mass; 0 below 2 clusters; the convergence KPI:
confident_rate ↑ AND demand_entropy ↓ with `dead_capture_ratio` bounded), promotion/
establishment/supersession counts, `median_days_to_promotion` (None-safe),
`dead_capture_ratio`, `est_tokens_served` (Σ len(text)//4). Report-time SQL over existing
tables; no new table, no scheduler.

### §0b.4 Reach (CV1): codex profile + the vendorable client envelope

`hive_init.harness` enum gains `codex` (tier 1, AGENTS.md, MCP registration reference =
operator-owned `~/.codex/config.toml`); every recipe playbook emits the seat-identity line
(`--agent <repo-name>` stdio / one token per seat HTTP). `hive/client.py` (stdlib-only,
vendorable, fence-tested) speaks the existing JSON-RPC `tools/call` envelope over POST +
bearer; `recall()` returns `reference_context` VERBATIM (hit schema single-sourced in the
server); every transport/auth/rpc/tool failure raises `HiveError` — never a partial dict.
**One token per agent seat** is the documented operational contract: identity diversity is
the promotion fuel (demand + survival both key on it).

### §0b.5 Config delta

`autonomy`: + `survival_e=2` · `survival_days=14` · `survival_min_exposures=5` ·
`solo_mode=false` · `solo_min_span_days=1` · `contested_tau=0.80` (all tier B).
`recall`: + `shadow=false` (tier **C**) · `shadow_tau=0.95` (tier B, finite in (0,1]).
Dev-time (never imported by runtime): `hive/research/gate_eval.py` sweeps
(`H_frac_max`, `softmax_beta`) over replay-labeled misses (a miss is a false abstain iff a
row with `ts < miss.ts` sits at cosine ≥ `label_tau=0.80`); recommends ONLY on paired
bootstrap `lo > 0`; the change stays operator-applied.

---

## §1 — Consolidated SQLite schema (ONE WAL DB)

All persistent state lives in **one** SQLite-WAL file on the one named volume (`/data/shared.db`), under the one single-writer `BEGIN IMMEDIATE` lane (ported `_begin_immediate_retrying`, `persistence.py:90`). Resolution **A7** collapses the separate `telemetry.db` sink: `exposure`, `task_outcomes`, and `utility` are all M03-owned tables in this DB, so the whole producer tick (associate → settle → clawback → emit → drain → posterior-write) is **one transaction**.

The in-RAM `VectorIndex` is **not** in this DB — it is a derived warm cache rebuilt from `scan_approved()` on boot (Resolution **B3**); `status='approved'` in SQLite is the single durable truth of recallability.

### 1.1 PRAGMAs (asserted at boot, set by M03 — ported `persistence.py:171-175`)

```sql
PRAGMA journal_mode=WAL;        -- one writer + many readers, crash-durable
PRAGMA synchronous=NORMAL;      -- WAL-safe durability/latency balance
PRAGMA foreign_keys=ON;         -- exposure/task_outcomes/utility FK integrity
PRAGMA busy_timeout=5000;       -- SQLITE_BUSY backoff (with _begin_immediate_retrying jitter)
```

### 1.2 The single recall predicate (one definition of "recallable")

```python
# hive/store/sqlite_episode_store.py  — module-private, the ONE source of truth.
# Kills the verified 4-site tombstoned=0 scatter (persistence.py:518,543,626,654).
_RECALL_PREDICATE = "status='approved'"
```

Both the recall SELECT and the index-feed `scan_approved()` read `_RECALL_PREDICATE`; admission never inlines a `status='approved'` literal. A pending row is *unrepresentable* in the recall set by construction.

### 1.3 The annotated DDL block

```sql
-- ════════════════════════════════════════════════════════════════════════
-- EPISODES — append-only memories + the pending→approved admission state machine.
--   weight is IMMUTABLE post-capture; only `status` (once, on approval) and the
--   `utility` posterior layer ever change. value is the d=256 unit-norm float32 PCA
--   blob (NULL until approve — value-absence is the 2nd fail-closed defense, M05 §4).
-- ════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS episodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT    NOT NULL DEFAULT 'default',   -- constant label, NEVER a query filter (single-tenant)
    content_hash  BLOB    NOT NULL,                     -- sha256(STAGED text) — post-redaction if REDACT (A C5)
    text          TEXT    NOT NULL,                     -- the staged (post-redaction) verbatim; NEVER a raw secret
    value         BLOB,                                 -- float32[d] LE unit-norm; NULL while pending (M05 §4)
    weight        REAL    NOT NULL,                     -- salience, immutable post-capture
    source        TEXT,
    tags          TEXT,                                 -- JSON array string, optional
    ts            INTEGER NOT NULL,                     -- epoch seconds, capture time

    status        TEXT    NOT NULL DEFAULT 'pending',
    proposed_by   TEXT    NOT NULL,                     -- who proposed (agent id / 'import-admin' for §12 import)
    approved_by   TEXT,                                 -- NULL iff not approved
    approved_ts   INTEGER,                              -- NULL iff not approved
    version       INTEGER NOT NULL DEFAULT 0,           -- optimistic-CAS column (single-writer lost-update guard)
    w_version     INTEGER NOT NULL,                     -- geometry version that produced `value` (migration trigger)

    -- ── status is a closed enum (no 3-state cliff: reject DELETEs by default;
    --    keep_rejected retains the row as 'pending' ⇒ index-absent ⇒ never recallable) ──
    CHECK (status IN ('pending','approved')),

    -- ── approved-iff-approver: an approved row WITHOUT an approver, or a pending row
    --    carrying one, is unrepresentable at the storage layer (mirrors Episode.__post_init__) ──
    CHECK ( (status='approved') = (approved_by IS NOT NULL) ),
    CHECK ( (status='approved') = (approved_ts IS NOT NULL) ),

    -- ── a value may only exist on an approved row (value computed AT approve time, M05 §4) ──
    CHECK ( (value IS NULL) OR (status='approved') )
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_episodes_content_hash ON episodes(content_hash);   -- dedup is a hash lookup
CREATE INDEX        IF NOT EXISTS ix_episodes_status_ts    ON episodes(status, ts);     -- scan_approved + pending(since)

-- NOTE on hash-binds-text: content_hash is DERIVED in stage() (sha256 of the staged text),
-- never accepted from the caller. The Episode dataclass __post_init__ asserts
-- content_hash == sha256(text) as a backstop — the precondition is DESIGNED OUT, not documented.

-- ════════════════════════════════════════════════════════════════════════
-- BLOBS — content-addressed verbatim store (PORT as-is, blob_store.py:52). Idempotent put.
-- ════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS blobs (
    content_hash BLOB PRIMARY KEY,   -- sha256(content) (blob_store.py:19)
    content      BLOB NOT NULL
);
-- put_blob is: INSERT INTO blobs(content_hash, content) VALUES(?,?)
--              ON CONFLICT(content_hash) DO NOTHING;   -- idempotent, shared-blob dedup safe

-- ════════════════════════════════════════════════════════════════════════
-- META — kv store (PORT, persistence.py:1148). Holds W_version, the reembed in-flight
--   sentinel, the producer per-repo SHA cursor, the drain watermark (A7), and hive_init links.
-- ════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Reserved keys:
--   'W_version'                          -> current geometry version (int as text)
--   'reembed_head_<w_version>'           -> base64/codec bytes of the frozen PCA head (M01 owns the codec, A C2)
--   'reembed_inflight'                   -> set during a W_version migration; resume sentinel
--   '_last_drain_ts'                     -> no-double-credit watermark (A7; ported controller.py:318)
--   'producer_repo_cursor:<repo>'        -> last-seen SHA per watched repo
--   'last_producer_tick_ts'              -> health snapshot (loop-liveness)
--   'hive_init:link:<repo_path>'         -> LinkRecord JSON (M07; no new table)
--   'hive_init:block_version'            -> onboarding rules-block version

-- ════════════════════════════════════════════════════════════════════════
-- EXPOSURE — move-#6 capture: which approved episodes a confident recall injected, with
--   per-episode recall_margin (the credit-split weight). task_ref carried IN-FILE (A7):
--   recall writes the row with task_ref=NULL; the producer back-fills it at link time.
-- ════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS exposure (
    trace_id      TEXT    NOT NULL,                 -- uuid4 hex, the §11 join key (one per confident recall)
    episode_id    INTEGER NOT NULL REFERENCES episodes(id),
    recall_margin REAL    NOT NULL,                 -- softmax-mass gap of THIS hit to the next (∈[0,1]); credit-split weight
    agent_id      TEXT    NOT NULL,
    injected_ts   INTEGER NOT NULL,                 -- epoch seconds
    task_ref      TEXT,                             -- NULL until the producer associates a commit (A7)
    PRIMARY KEY (trace_id, episode_id)              -- one row per (trace, episode)
);
CREATE INDEX IF NOT EXISTS ix_exposure_task_ref    ON exposure(task_ref);     -- drain: rows for a settled task
CREATE INDEX IF NOT EXISTS ix_exposure_injected_ts ON exposure(injected_ts);  -- producer window association
-- The ported telemetry text-free guard is re-applied here as a write-time assertion (A C5/A7):
--   every written column is an int/float/hex/short-label — NEVER recalled text.

-- ════════════════════════════════════════════════════════════════════════
-- TASK_OUTCOMES — the producer's verifiable-outcome state machine (BUILD-NEW, A7 in-file).
--   PK (task_ref, trace_id): re-association UPSERTs, never duplicates. This IS the "sink"
--   the §11 prose calls a telemetry sink — it is this table; the "drain" is one tx-step.
-- ════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS task_outcomes (
    task_ref      TEXT    NOT NULL,                 -- the ORIGINAL commit SHA (or squash-resolved SHA)
    trace_id      TEXT    NOT NULL,                 -- the recall trace this commit was associated to
    family_scope  TEXT    NOT NULL,                 -- derived at link time: "<remote>|<lang>|<workflow>" (A2/§11)
    state         TEXT    NOT NULL,                 -- provisional | settled_pos | clawed_back
    reward        REAL    NOT NULL,                 -- +0.2 provisional/settled ; -1.0 clawback (pre-signed, A3)
    files_touched TEXT,                             -- JSON array of paths (clawback candidacy)
    introduced_lines TEXT,                          -- JSON {path:[[start,end],...]} — blame target for delayed clawback (M09 must-fix)
    merge_ts      INTEGER NOT NULL,
    settle_at     INTEGER NOT NULL,                 -- merge_ts + settle_days (the ripeness boundary)
    ts            INTEGER NOT NULL,                 -- last state-change ts (compared to _last_drain_ts)
    PRIMARY KEY (task_ref, trace_id),
    CHECK (state IN ('provisional','settled_pos','clawed_back'))
);
CREATE INDEX IF NOT EXISTS ix_task_outcomes_settle ON task_outcomes(state, settle_at);  -- O(due) sweep, not O(all)
CREATE INDEX IF NOT EXISTS ix_task_outcomes_files  ON task_outcomes(task_ref);          -- bugfix-SHA→original-row lookup

-- ════════════════════════════════════════════════════════════════════════
-- UTILITY — per-(episode_id, family_scope) Beta-Bernoulli posterior (BUILD-NEW, A3).
--   wins/losses are REAL (margin-split fractional). isolation flag set at approve() (A5).
--   C9 is the SOLE writer; the surfacer is read-only via utility_map. weight is NEVER touched.
-- ════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS utility (
    episode_id   INTEGER NOT NULL REFERENCES episodes(id),
    family_scope TEXT    NOT NULL,
    wins         REAL    NOT NULL DEFAULT 0.0,     -- Beta α-delta sum (>=0)
    losses       REAL    NOT NULL DEFAULT 0.0,     -- Beta β-delta sum (>=0)
    n_sources    INTEGER NOT NULL DEFAULT 0,       -- DISTINCT corroborating agents (not write count)
    version      INTEGER NOT NULL DEFAULT 0,       -- layer version; bump = roll the whole utility layer back (guardrail-4)
    isolation    INTEGER NOT NULL DEFAULT 0,       -- 1 ⇒ held-out, NEVER reweighted (guardrail-2, A5); set at approve()
    cas_version  INTEGER NOT NULL DEFAULT 0,       -- optimistic-CAS for apply_credit under concurrency
    PRIMARY KEY (episode_id, family_scope),
    CHECK (wins  >= 0.0),
    CHECK (losses >= 0.0),
    CHECK (isolation IN (0,1))
);
CREATE INDEX IF NOT EXISTS ix_utility_family ON utility(family_scope);   -- utility_map(family_scope) slice

-- ════════════════════════════════════════════════════════════════════════
-- UTILITY_SOURCES — sidecar for DISTINCT-agent n_sources (BUILD-NEW, M08 §4).
-- ════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS utility_sources (
    episode_id   INTEGER NOT NULL,
    family_scope TEXT    NOT NULL,
    source_agent TEXT    NOT NULL,
    PRIMARY KEY (episode_id, family_scope, source_agent),   -- two writes by same agent ⇒ n_sources stays 1
    FOREIGN KEY (episode_id, family_scope) REFERENCES utility(episode_id, family_scope)
);
```

### 1.4 CAS write idiom (the single-writer lost-update guard — ported `persistence.py:581`)

```sql
-- approve(): per id, in ONE tx, then index.add AFTER commit (best-effort warm-cache, B3):
UPDATE episodes
   SET status='approved', approved_by=?, approved_ts=?, value=?, version=version+1
 WHERE id=? AND version=? AND status='pending';
-- cur.rowcount==1 ⇒ won the CAS; ==0 ⇒ lost race / already approved (reported as skipped, never a lost update).
```

---

## §2 — Port registry

All ports are `runtime_checkable` Protocols (`hive/domain/ports.py`); all cross-boundary value types are frozen, self-asserting dataclasses. The pure domain (`hive/domain/**`) is CI-forbidden (AST import-linter) to import `sqlite3 | torch | subprocess | os | git`.

### 2.1 Frozen, self-asserting dataclasses (contracts that cannot lie)

```python
# hive/domain/models.py
from __future__ import annotations
import hashlib, random
from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional, Sequence
import numpy as np

Value = np.ndarray   # shape (d,), dtype float32, L2-normalized (‖v‖₂ == 1 ± 1e-5)

# ── Episode ───────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Episode:
    id: int
    content_hash: bytes
    text: str
    value: Optional[Value]                 # None while pending; unit-norm float32[d] once approved
    weight: float                          # immutable post-capture
    source: Optional[str]
    tags: tuple[str, ...]
    ts: int
    status: Literal["pending", "approved"]
    proposed_by: str
    approved_by: Optional[str]
    approved_ts: Optional[int]
    version: int
    w_version: int
    def __post_init__(self) -> None:
        # invariant 1: approved-iff-approver (state machine made a type, not prose)
        if (self.status == "approved") != (self.approved_by is not None):
            raise ValueError("approved-iff-approver violated")
        if (self.status == "approved") != (self.approved_ts is not None):
            raise ValueError("approved-iff-approved_ts violated")
        # invariant 2: hash-binds-text (designed out — backstop only; derived in stage())
        if self.content_hash != hashlib.sha256(self.text.encode("utf-8")).digest():
            raise ValueError("content_hash does not bind text")
        # invariant 3: unit-norm float32[d] when present (C3 ranker + C4 gate rest on this)
        if self.value is not None:
            if self.value.dtype != np.float32 or self.value.ndim != 1:
                raise ValueError("value must be 1-D float32")
            if abs(float(np.linalg.norm(self.value)) - 1.0) > 1e-5:
                raise ValueError("value must be L2-unit-norm")

@dataclass(frozen=True, slots=True)
class StagedEpisode:                        # the input to stage() — no id/value/approver yet
    content_hash: bytes
    text: str
    weight: float
    source: Optional[str]
    tags: tuple[str, ...]
    ts: int
    proposed_by: str
    status: Literal["pending"] = "pending"

# ── ScanVerdict (secret floor) — cannot lie ───────────────────────────────
ScanAction = Literal["clean", "redact", "refuse"]

@dataclass(frozen=True, slots=True)
class SecretFinding:
    rule: str                              # e.g. "aws_akia", "github_pat", "pem", "entropy"
    span: tuple[int, int]                  # [start, end) into the ORIGINAL text — NEVER the matched bytes

@dataclass(frozen=True, slots=True)
class ScanVerdict:
    action: ScanAction
    redacted_text: Optional[str]           # present iff action=="redact"
    findings: tuple[SecretFinding, ...]
    def __post_init__(self) -> None:
        if self.action == "clean"  and self.findings:
            raise ValueError("clean verdict cannot carry findings")
        if self.action == "redact" and self.redacted_text is None:
            raise ValueError("redact verdict requires redacted_text")
        if self.action == "refuse" and not self.findings:
            raise ValueError("refuse verdict requires at least one finding")

# ── Scored / RecallResult (the recall surface) ────────────────────────────
@dataclass(frozen=True, slots=True)
class Scored:
    episode_id: int
    sim: float                             # cosine ∈ [-1, 1]
    weight: float                          # immutable capture weight (the surfacer base multiplier — NOT sim/alpha)
    recall_margin: float                   # softmax-mass gap to next hit ∈ [0,1]; the credit-split weight

RecallState = Literal["CONFIDENT", "ABSTAIN", "EMPTY_NO_DATA"]

@dataclass(frozen=True, slots=True)
class RecallHit:
    episode_id: int
    text: str
    sim: float

@dataclass(frozen=True, slots=True)
class RecallResult:
    state: RecallState
    trace_id: str                          # fresh uuid4 hex, ALWAYS present (hit AND abstain) — §11 join key
    hits: tuple[RecallHit, ...]            # () unless CONFIDENT
    entropy_norm: float                    # H/ln(N_eff) ∈ [0,1]; 0.0 on EMPTY_NO_DATA
    top_margin: float
    @classmethod
    def empty(cls, trace_id: str) -> "RecallResult":
        return cls("EMPTY_NO_DATA", trace_id, (), 0.0, 0.0)
    @classmethod
    def abstain(cls, trace_id: str, h_norm: float, margin: float) -> "RecallResult":
        return cls("ABSTAIN", trace_id, (), h_norm, margin)
    def __post_init__(self) -> None:
        # abstain-no-resurrect, structural: a non-CONFIDENT result CANNOT carry hits
        if self.state != "CONFIDENT" and self.hits:
            raise ValueError("only CONFIDENT may carry hits (abstain-no-resurrect)")
        if not (0.0 <= self.entropy_norm <= 1.0):
            raise ValueError("entropy_norm must be in [0,1]")

# ── AgentContext / family (A2) ────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class AgentContext:
    repo_remote: str                       # normalized git remote, "" if none
    language: str                          # dominant language, "" if none
    workflow: str                          # "bugfix" | "dep-upgrade" | "general"

# ── BetaPosterior (utility read shape) ────────────────────────────────────
@dataclass(frozen=True, slots=True)
class BetaPosterior:
    episode_id: int
    family_scope: str
    wins: float                            # α-delta sum
    losses: float                          # β-delta sum
    n_sources: int
    version: int
    def mean(self) -> float:               # Beta mean = (a)/(a+b) with priors folded by the store
        a, b = self.wins, self.losses
        return a / (a + b) if (a + b) > 0 else 0.5

# ── GitFacts (the verifiable-credit source) ───────────────────────────────
CommitKind = Literal["merge", "revert", "bugfix", "plain"]

@dataclass(frozen=True, slots=True)
class CommitFact:
    sha: str
    repo_remote: str
    kind: CommitKind
    files_touched: tuple[str, ...]
    touched_blame: tuple[tuple[str, int, int], ...]   # (path, start, end) INTRODUCED-line provenance (impure side pre-attaches)
    trailer_trace_id: Optional[str]                   # Hive-Trace trailer if present (re-targets WHICH trace, never the sign)
    reverts_sha: Optional[str]
    language: str
    ts: int

@dataclass(frozen=True, slots=True)
class SourcePoll:
    commits: tuple[CommitFact, ...]
    repo_cursors: dict[str, str]                       # repo -> last-seen SHA (advances the watermark)
    errors: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class SettledOutcome:
    task_ref: str
    trace_id: str
    family_scope: str
    reward_sign: int                                   # ∈ {-1, +1} — REJECTED otherwise (verifiable-credit-only)
    reward_magnitude: float                            # ∈ (0, 1.0]
    ts: int
    def __post_init__(self) -> None:
        if self.reward_sign not in (-1, +1):
            raise ValueError("reward_sign must be ±1 (a ~0/CI-green event can never be a credit)")
        if not (0.0 < self.reward_magnitude <= 1.0):
            raise ValueError("reward_magnitude must be in (0,1]")

# ── ProducerTick (the typed audit record / structured-log contract) ───────
@dataclass(frozen=True, slots=True)
class ProducerTick:
    now: int
    poll_commits: int
    associated: int
    settled: int
    clawed_back: int
    emitted: int
    drained: int
    stamp_hits: int
    window_assoc: int
    errors: tuple[str, ...]                            # counted boundary failures; step() NEVER raises (I1)
```

### 2.2 Geometry / storage / search ports

```python
from typing import Iterable, Iterator, Optional, Protocol, Sequence, runtime_checkable

# ── SWAP AXIS 1 — EmbeddingProvider (M01) ─────────────────────────────────
@runtime_checkable
class EmbeddingProvider(Protocol):
    d: int               # projected value width == geometry.d == 256
    native_dim: int      # 384 pre-PCA (migration fence only)
    w_version: int       # stamped onto every produced value
    def encode(self, text: str) -> Value: ...                       # (d,) unit-norm; HOT PATH; no lazy fit, no fallback
    def encode_batch(self, texts: list[str]) -> np.ndarray: ...     # (n,d); INVARIANT encode(t)==encode_batch([t])[0]
    def encode_native_batch(self, texts: list[str]) -> np.ndarray: ...  # (n,384) MIGRATION ONLY

# Frozen PCA head codec (M01 owns it; A C2 — BUILD-NEW 24-byte HVH1 layout, w_version embedded).
@dataclass(frozen=True, slots=True)
class FrozenPcaHead:
    W: np.ndarray        # (d_out, d_in) float32
    d_in: int            # 384
    d_out: int           # 256
    w_version: int
    def apply(self, e: np.ndarray) -> Value: ...
    def to_bytes(self) -> bytes: ...           # struct.pack("<4sHHIII I", b"HVH1",1,1,w_version,d_out,d_in,0)+W(<f4 LE)
    @classmethod
    def from_bytes(cls, raw: bytes) -> "FrozenPcaHead": ...   # validates MAGIC/FMT/DTYPE/len; HeadCodecError on bad

# ── SWAP AXIS 2 — VectorIndex / MutableVectorIndex (M02) ──────────────────
SearchHit = tuple[int, float]   # (episode_id, cosine ∈ [-1,1])

@runtime_checkable
class VectorIndex(Protocol):                              # READ port handed to the ranker
    @property
    def is_authoritative(self) -> bool: ...               # exhaustive ⇒ True; ANN ⇒ False (never silently flips)
    @property
    def dim(self) -> int: ...
    def search(self, value_q: Value, k: int) -> list[SearchHit]: ...   # signed-cosine descending; []-on-empty no raise
    def __len__(self) -> int: ...

@runtime_checkable
class MutableVectorIndex(VectorIndex, Protocol):          # population port — ONLY the Store holds this
    def sync_approved(self, episode_id: int, value: Value) -> None: ...
    def drop(self, episode_id: int) -> None: ...
    def rebuild_from_store(self, rows: Iterable[tuple[int, Value]]) -> int: ...  # boot/post-migration warm-cache rebuild (B3)

# ── I/O — EpisodeStore (M03) — wide-but-conscious, 4 pre-segregated groups (A C1) ──
@runtime_checkable
class EpisodeStore(Protocol):
    # ── GROUP 1: EPISODES (state machine) ──
    def stage(self, ep: StagedEpisode) -> int: ...                          # status=pending, version=0; POST: NOT in index
    def approve(self, ids: Sequence[int], approver: str, now: int) -> list[int]: ...  # CAS-flip + value-embed + index.add (best-effort) + isolation stamp (A5)
    def reject(self, ids: Sequence[int], keep_rejected: bool = False) -> int: ...
    def get(self, episode_id: int) -> Optional[Episode]: ...
    def pending(self, since: Optional[int]) -> list[StagedEpisode]: ...
    def scan_approved(self, tenant_id: str = "default") -> Iterator[Episode]: ...     # WHERE _RECALL_PREDICATE — sole recall feed
    def by_content_hash(self, h: bytes) -> Optional[Episode]: ...
    def rebuild_index_from_store(self) -> int: ...                          # B3 divergence-recovery guarantee
    # ── GROUP 2: BLOB ──
    def put_blob(self, content: bytes) -> bytes: ...                        # idempotent ON CONFLICT DO NOTHING; -> sha256
    def get_blob(self, content_hash: bytes) -> Optional[bytes]: ...
    # ── GROUP 3: LEDGERS (move-#6, all in this DB per A7) ──
    def record_exposure(self, trace_id: str, agent_id: str,
                        rows: Sequence[tuple[int, float]], ts: int) -> None: ...   # (episode_id, recall_margin)
    def exposed_for(self, trace_id: str) -> list[tuple[int, float]]: ...
    def link_task(self, trace_id: str, fact: CommitFact, family_scope: str, settle_at: int) -> None: ...
    def due_settlements(self, now: int) -> list[str]: ...                   # task_refs WHERE state='provisional' AND settle_at<=now
    def settle(self, task_ref: str, reward: float) -> None: ...
    def clawback(self, task_ref: str, reward: float) -> None: ...
    def update_utility(self, episode_id: int, family_scope: str,
                       d_wins: float, d_losses: float, source_agent: str) -> None: ...   # bumps version; NEVER touches weight
    def posterior(self, episode_id: int, family_scope: str) -> Optional[BetaPosterior]: ...
    def utility_map(self, *, family_scope: str, confident_only: bool = True) -> dict[int, float]: ...
    def isolation_episode_ids(self) -> frozenset[int]: ...
    def zero_utility_layer(self) -> None: ...                               # guardrail-4 human rollback (bumps version)
    # ── GROUP 4: META + MIGRATION ──
    def meta_get(self, key: str) -> Optional[str]: ...
    def meta_set(self, key: str, value: str) -> None: ...

# ── Migrator (M03/ops) — the geometry round-trip + §12 import ─────────────
@runtime_checkable
class Migrator(Protocol):
    def reembed_from_text(self, embedder: EmbeddingProvider, new_head: FrozenPcaHead) -> int: ...  # re-projects value only; NO re-scan (A C5)
    def import_corpus(self, rows: Iterable["ArchivedRow"], *,
                      scanner: "SecretScanner", import_admin: str) -> "ImportReport": ...          # SCANS, stages pending (A C5)

# ── I/O — Clock (purity seam for settle-day windows) ──────────────────────
@runtime_checkable
class Clock(Protocol):
    def now(self) -> int: ...   # epoch seconds
```

### 2.3 Recall, gate, surfacer, exposure ports

```python
# ── RecallPipeline (M04) — the never-hallucinate enforcement point ────────
@runtime_checkable
class RecallPipeline(Protocol):
    def recall(self, query: str, *, agent_id: str, agent_ctx: AgentContext) -> RecallResult: ...
# Private to hive/domain/recall.py (the SOLE owner of the live query's family, A2):
#   def _resolve_query_family(ctx: AgentContext) -> str:
#       return f"{ctx.repo_remote or '*'}|{ctx.language or '*'}|{ctx.workflow or 'general'}"

# ── NormalizedEntropyGate (M04) — PORT+EXTEND: softmax(β·sim), β in ctor (B1) ──
class NormalizedEntropyGate:
    def __init__(self, h_frac_max: float, beta: float) -> None: ...   # both validated finite; beta > 0
    def evaluate(self, sims: list[float]) -> tuple[bool, float, float]:
        """(suppress, entropy_norm, top_margin). mass = softmax(beta·sim) max-shifted; both
        reference fallbacks ported verbatim (non-finite floor; total<=0 ⇒ uniform 1/n).
        suppress iff entropy_norm > h_frac_max. Empty ⇒ (False,0.0,0.0). Fail-closed ⇒ (True,1.0,0.0)."""
        ...

# ── UtilitySurfacer (M08 owns the file hive/domain/surfacer.py; A1+A2+A4) ──
class UtilitySurfacer:
    # O(n log n) stable sort, O(n) space; n bounded by recall_top_n.
    def __init__(self, *, enabled: bool, epsilon_explore: float,    # epsilon_explore ← cfg.recall.epsilon_explore (A4)
                 f_min: float, f_max: float, rng: random.Random) -> None: ...
    def order(self, scored: Sequence[Scored], utility_map: Mapping[int, float], *,
              family_scope: str) -> list[Scored]:
        """Stable-sort by weight * f(util) DESC; ties keep base order.
        f(u) = clamp(f_min + (f_max-f_min)*u, [f_min,f_max]); f(0.5)=1.0, f(1.0)=1.5, f(0.0)=0.5.
        eid absent from utility_map ⇒ f==1.0 (un-confident never moves rank).
        enabled is False ⇒ list(scored) byte-identical (Phase-1 inert).
        ε-explore: with prob epsilon_explore (rng.random()<eps) return list(scored) unchanged this call.
        BASE MULTIPLIER IS Scored.weight — never sim/alpha (A1)."""
        ...

# ── ExposureLedger (M04 write seam over the M03 exposure table) ───────────
@runtime_checkable
class ExposureLedger(Protocol):
    def record_exposure(self, trace_id: str, agent_id: str,
                        rows: Sequence[tuple[int, float]], ts: int) -> None: ...   # fire-and-forget; failure logs WARN

# ── SecretScanner (M05) ───────────────────────────────────────────────────
@runtime_checkable
class SecretScanner(Protocol):
    def scan(self, text: str) -> ScanVerdict: ...    # deterministic, side-effect-free, secret never in the verdict

# ── AdmissionLedger (M05) — the pending→approved gauntlet ─────────────────
@dataclass(frozen=True, slots=True)
class WriteResult:
    status: Literal["pending", "redacted", "refused"]
    pending_id: Optional[int]
    content_hash: Optional[bytes]
    scan: ScanVerdict
    deduped: bool = False

@runtime_checkable
class AdmissionLedger(Protocol):
    def write(self, text: str, *, weight: float, source: Optional[str],
              proposed_by: str) -> WriteResult: ...           # scan → (refuse 0-rows | redact-masked-pending | pending)
    def list_pending(self, since: int) -> list[StagedEpisode]: ...
    def approve(self, ids: Sequence[int], approver: str) -> tuple[list[int], list[int]]: ...  # (approved, skipped); indexes on approve
    def reject(self, ids: Sequence[int], keep_rejected: bool = False) -> tuple[list[int], list[int]]: ...
```

### 2.4 Outcome / credit ports (move-#6)

```python
# ── OutcomeSource (M09) — the SWAP-axis-3 port; ALL git/subprocess sealed here ──
@runtime_checkable
class OutcomeSource(Protocol):
    def poll(self, repo_cursors: dict[str, str]) -> SourcePoll: ...   # impure side pre-attaches touched_blame

# ── OutcomeJoiner (M09) — PURE §11 state machine; clock-injected, git-free ──
@dataclass(frozen=True, slots=True)
class JoinerEmit:
    settled: tuple[SettledOutcome, ...]
    associations: tuple[tuple[str, str], ...]   # (task_ref, trace_id) upserts

class OutcomeJoiner:
    def __init__(self, *, clock: Clock, cfg: "ProducerConfig") -> None: ...
    def step(self, poll: SourcePoll, exposures: Sequence[tuple[str, int, float]],
             open_rows: Sequence[tuple[str, str, str]], now: int) -> JoinerEmit:
        """associate → settle → clawback → emit, fixed order (I2). Clawback iff blame-line
        overlap (I3); reward_sign comes ONLY from the settled/clawed state (verifiable-credit-only)."""
        ...

# ── Attributor (M08) — PURE margin-split; no SQL/git/clock (A3) ───────────
@dataclass(frozen=True, slots=True)
class CreditDelta:
    episode_id: int
    family_scope: str
    d_wins: float                 # >=0
    d_losses: float               # >=0
    source_agent: str

class Attributor:
    def split(self, outcome: SettledOutcome, exposed: Sequence[tuple[int, float]],
              isolation: frozenset[int]) -> list[CreditDelta]:
        """share_i = reward_magnitude * margin_i / Σmargins (all-zero ⇒ uniform 1/n).
        +1 ⇒ d_wins=share_i ; -1 ⇒ d_losses=share_i. isolation ids EXCLUDED entirely (A5).
        POST: Σ(d_wins+d_losses) == reward_magnitude (rel-tol 1e-9) over non-isolation."""
        ...

# ── PredictionBiasMonitor (M08) — guardrail-3 made real (A6) ──────────────
class PredictionBiasMonitor:
    def __init__(self, store: "UtilityStore", *, clock: Clock) -> None: ...
    def divergence(self, family_scope: str, window_s: int) -> float:
        """mean(posterior_mean(eid,family) - realized) over settled outcomes in [now-window_s, now];
        realized = 1 if reward_sign>0 else 0. 0.0 on empty window. >0 ⇒ ranker over-predicts (stale)."""
        ...

# ── UtilityStore (M08) — posterior persistence port (swappable) ───────────
@runtime_checkable
class UtilityStore(Protocol):
    def apply_credit(self, deltas: Sequence[CreditDelta]) -> None: ...                 # transactional, CAS-bounded
    def posterior(self, episode_id: int, family_scope: str) -> Optional[BetaPosterior]: ...
    def utility_map(self, *, family_scope: str, confident_only: bool = True) -> dict[int, float]: ...
    def isolation_episode_ids(self) -> frozenset[int]: ...
    def zero_layer(self) -> None: ...                                                  # guardrail-4

# ── OutcomeProducer (M09) — the in-process single-writer tick (driving adapter) ──
@runtime_checkable
class OutcomeProducer(Protocol):
    def step(self, now: int) -> ProducerTick: ...   # NEVER raises (I1); ONE tx: settle→credit→drain (A7)

# ── InstallPlanner (M07) — hive_init handshake domain ─────────────────────
@dataclass(frozen=True, slots=True)
class RulesBlock:
    rendered_text: str
    trailer_key: str                       # ALWAYS producer.stamp_trailer (never literal — kills CONFIG_DRIFT)
    block_version: int
    block_hash: bytes                       # sha256(rendered_text)
    def __post_init__(self) -> None:        # ill-formed block (missing markers/version, bad hash) is unconstructable
        if self.block_hash != hashlib.sha256(self.rendered_text.encode("utf-8")).digest():
            raise ValueError("block_hash does not bind rendered_text")

@dataclass(frozen=True, slots=True)
class InstallPlan:
    rules_file: str
    harness: str
    rules_block: RulesBlock
    expected_confirm_hash: bytes            # == rules_block.block_hash
    watch_warning: Optional[str]            # WARN note if repo unwatched; NON-blocking (link still succeeds)

@runtime_checkable
class InstallPlanner(Protocol):
    def plan(self, repo_path: str, harness: str, rules_file: Optional[str]) -> InstallPlan: ...   # Phase 1: zero writes
    def confirm(self, repo_path: str, confirm_hash: bytes) -> dict: ...                            # Phase 2: hash-verified link
```

---

## §3 — MCP tool JSON schemas (EXACTLY 8 tools)

The net surface is **8 tools**: `hive_write, hive_recall, hive_fetch, hive_pending, hive_approve, hive_reject, hive_init, hive_health`. (Dropped: `consolidate, schemas, recall_cold, restore_cold, reconsolidate, audit, outcome`.) Schema validation runs **before** any domain port is touched (M06 §1.2 belt). Recalled text is framed under `reference_context`, never `instructions`.

```python
# hive/adapters/mcp/tool_defs.py  — the tools/list static table
TOOL_DEFINITIONS = [
  { "name": "hive_write",
    "inputSchema": {"type":"object","required":["text"],
      "properties":{"text":{"type":"string"},"source":{"type":"string"},
                    "tags":{"type":"array","items":{"type":"string"}},
                    "proposed_by":{"type":"string"}}}},
  { "name": "hive_recall",
    "inputSchema": {"type":"object","required":["query"],
      "properties":{"query":{"type":"string"},"k":{"type":"integer","minimum":1},
                    "repo_remote":{"type":"string"},"language":{"type":"string"},
                    "workflow":{"type":"string","enum":["bugfix","dep-upgrade","general"]}}}},
  { "name": "hive_fetch",
    "inputSchema": {"type":"object","required":["content_hash"],
      "properties":{"content_hash":{"type":"string"}}}},
  { "name": "hive_pending",
    "inputSchema": {"type":"object","required":[],
      "properties":{"since":{"type":"integer","minimum":0}}}},
  { "name": "hive_approve",
    "inputSchema": {"type":"object","required":["ids","approver"],
      "properties":{"ids":{"type":"array","items":{"type":"integer"}},
                    "approver":{"type":"string"}}}},
  { "name": "hive_reject",
    "inputSchema": {"type":"object","required":["ids"],
      "properties":{"ids":{"type":"array","items":{"type":"integer"}},
                    "keep_rejected":{"type":"boolean"}}}},
  { "name": "hive_init",
    "inputSchema": {"type":"object","required":["repo_path","harness"],
      "properties":{"repo_path":{"type":"string"},
                    "harness":{"type":"string","enum":["claude-code","cursor","windsurf","cline","opencode","generic"]},
                    "rules_file":{"type":"string"},
                    "confirm_hash":{"type":"string"}}}},
  { "name": "hive_health",
    "inputSchema": {"type":"object","required":[],
      "properties":{"repo_path":{"type":"string"}}}},
]
```

### 3.1 TypedDict result shapes

```python
from typing import Literal, Optional, TypedDict

class ScanReport(TypedDict):              # NEVER carries the raw secret bytes
    action: Literal["clean","redact","refuse"]
    rules: list[str]
    n_findings: int

class WriteResultJSON(TypedDict, total=False):
    status: Literal["pending","redacted","refused"]   # NEVER "approved" from write
    id: int                                            # present iff pending|redacted
    content_hash: str                                  # hex; sha256(post-redaction text)
    redacted_preview: str                              # present iff redacted
    reason: str                                        # present iff refused
    scan: ScanReport

class RecallHitJSON(TypedDict):
    episode_id: int
    text: str
    sim: float
    content_hash: str

class RecallEnvelope(TypedDict, total=False):
    reference_context: list[RecallHitJSON]   # [] on abstain/empty — neutral key, NOT "instructions"
    abstained: bool
    trace_id: str                            # ALWAYS present (hit AND abstain) — §11 join key
    state: Literal["CONFIDENT","ABSTAIN","EMPTY_NO_DATA"]
    entropy_norm: float
    note: str                                # neutral string on abstain/empty

class FetchResult(TypedDict):
    found: bool
    text: Optional[str]                      # clean miss: {found:False, text:None}, never raises

class PendingRow(TypedDict):
    id: int
    text_preview: str                        # truncated; full text via hive_fetch
    proposed_by: str
    ts: int
    scan_verdict: Literal["PASS","REDACT"]   # REFUSE rows were never staged

class PendingList(TypedDict):
    pending: list[PendingRow]
    count: int

class ApproveResult(TypedDict):
    approved: list[int]
    skipped: list[int]
    approver: str

class RejectResult(TypedDict):
    rejected: list[int]
    skipped: list[int]

class InstallPlanJSON(TypedDict, total=False):
    phase: Literal[1,2]
    rules_file: str
    harness: str
    rules_block: str                         # the marker-delimited text to write
    trailer_key: str                         # == producer.stamp_trailer
    block_version: int
    expected_confirm_hash: str               # hex
    watch_warning: Optional[str]
    linked: bool                             # phase-2 only
    error: Optional[str]                     # "stale_or_wrong_block" on mismatch

class HealthSnapshot(TypedDict, total=False):
    ok: bool
    tenant_id: str
    db_path: str
    db_size_bytes: int
    n_episodes: int
    n_pending: int
    embedder: str
    embedder_loaded: bool                    # gates the container HEALTHCHECK (healthy ≡ resident)
    embedder_projection: str                 # "pca"
    W_version: int
    d: int
    index_authoritative: bool                # §4.3 trap surfaced
    producer_watch_repos: int
    last_producer_tick_ts: Optional[int]     # loop-liveness
    uptime_s: int
    linked: bool                             # present iff repo_path given (M07)
    link: Optional[dict]
    trailer_key: str
    error: str                               # fail-closed subset {ok:False, error, db_path} ONLY on probe failure
```

### 3.2 JSON-RPC error semantics (PORT)

- Protocol errors → JSON-RPC `error` object: `-32700` parse, `-32601` unknown method, `-32602` unknown tool / bad params.
- Tool failures → **not** a JSON-RPC error: `result.isError=True` with `content[0].text`. The stdio loop never crashes on a tool exception; the stack is logged to **stderr**, never returned. stdout carries only JSON-RPC.

---

## §4 — Request flows

### 4.1 Capture + approve (`hive_write` → `hive_pending` → `hive_approve`)

```
AGENT                MCP (M06)          AdmissionLedger (M05)     SecretScanner   EpisodeStore (M03)   VectorIndex (M02)
  │ hive_write(text)   │                       │                      │                  │                   │
  │───────────────────▶│ validate schema       │                      │                  │                   │
  │                    │ (text required) ──────▶│ write(text,…)        │                  │                   │
  │                    │                        │── scan(text) ───────▶│ ScanVerdict      │                   │
  │                    │                        │◀─────────────────────│                  │                   │
  │                    │   REFUSE: 0 rows, 0 blobs ──────────────┐     │                  │                   │
  │                    │   REDACT: body=redacted_text; hash=sha256(body)                  │                   │
  │                    │   PASS:   body=text                     │     │                  │                   │
  │                    │                        │── put_blob(body) ───────────────────────▶│ (idempotent)      │
  │                    │                        │── stage(StagedEpisode status=pending) ──▶│ INSERT version=0  │
  │                    │                        │   POST: row NOT in index, value NULL     │   (no index.add)  │
  │◀── {status:pending,│◀── WriteResult ────────│                                          │                   │
  │     id, content_hash, scan} (or refused/redacted)                                      │                   │
  ...
  │ hive_pending(since)│── list_pending ────────────────────────────────────────────────▶│ scan ts>=since,   │
  │◀── {pending:[…]} ──│   (status='pending' only; REFUSE rows absent; preview truncated) │  status='pending' │
  ...
  │ hive_approve(ids,  │── approve(ids,approver) ───────────────────▶ per id, ONE tx:      │                   │
  │   approver)        │                                             CAS UPDATE status=    │                   │
  │                    │                                             'approved', approved_  │                   │
  │                    │                                             by/ts, value=encode(  │                   │
  │                    │                                             text), version+1,     │                   │
  │                    │                                             isolation stamp (A5)  │                   │
  │                    │                                             COMMIT ───────────────│                   │
  │                    │                                             AFTER commit (B3): ───────────────────────▶ sync_approved(id,value)
  │                    │                                             best-effort warm cache; crash here ⇒       │  (best-effort)
  │                    │                                             rebuild_index_from_store() on next boot     │
  │◀── {approved:[…],  │◀── (approved, skipped)                                                                  │
  │     skipped, approver}                                                                                       │
```

**Enforced:** REFUSE leaves zero rows + zero blobs (§6.1#5a). Pending is index-absent AND value-NULL (two independent fail-closed defenses). Approve is the **only** writer of the index; an approved row is recallable on the very next `hive_recall` (in-tx CAS flip; warm cache add post-commit; boot-rebuild recovers a crash between commit and add).

### 4.2 Recall — confident / abstain / EMPTY + trace emission + family derivation (A2)

```
AGENT          MCP (M06)        RecallPipeline (M04)     Embedder   VectorIndex   Gate      Surfacer    UtilityStore   ExposureLedger
  │ hive_recall  │                     │                    │           │          │          │            │               │
  │ (query, repo,│── validate ────────▶│ recall(q,agent_id, │           │          │          │            │               │
  │  language,   │                     │   agent_ctx)       │           │          │          │            │               │
  │  workflow)   │                     │ fam = _resolve_query_family(ctx)  ⇒ "<remote>|<lang>|<workflow>" (A2) │             │
  │              │                     │── encode(query) ──▶│ value_q   │          │          │            │               │
  │              │                     │── search(value_q,top_n) ──────▶│ scored   │          │            │               │
  │              │                     │   if len==0 ⇒ RecallResult.empty(trace) ──────────────────────────────────────────┐ EMPTY_NO_DATA
  │              │                     │── utility_map(family_scope=fam, confident_only=True) ─────────────▶│ {eid:f}     │ (NOT called)
  │              │                     │── order(scored, utility_map, family_scope=fam) ──────────▶│ reordered             │
  │              │                     │── evaluate([s.sim …]) ─────────────────────▶│ (suppress,│          │            │ │
  │              │                     │                                             │ h_norm,   │          │            │ │
  │              │                     │   suppress ⇒ RecallResult.abstain(trace,h_norm,margin) ──────────────────────────┤ ABSTAIN
  │              │                     │   pass     ⇒ build hits; record_exposure ───────────────────────────────────────▶│ INSERT exposure
  │              │                     │             (trace_id, agent_id, [(eid,recall_margin)…], ts)  fire-and-forget     │  task_ref=NULL
  │◀── envelope ─│◀── RecallResult ────│                                                                                    │
```

- **CONFIDENT:** `reference_context` non-empty, `abstained=False`, `trace_id` set; `record_exposure` called **exactly once** with per-episode `recall_margin`.
- **ABSTAIN** (`H/ln(N_eff) > 0.5`): `reference_context=[]`, `abstained=True`, `trace_id` set, exposure **not** written. The abstain branch returns before the exposure step — no path can repopulate hits (structural).
- **EMPTY_NO_DATA** (len-0 index): `entropy_norm=0.0`, distinct from ABSTAIN.
- Any internal failure (embedder/index/gate raise) ⇒ fail-closed `EMPTY_NO_DATA`, never a raise into the caller, never an un-vetted hit.
- Family isolation: a memory proven on `repoX|python|bugfix` returns no entries under a `repoY|go|general` query key, so the surfacer is identity for it (no cross-family boost — A2).

### 4.3 The full move-#6 learn loop (recall → commit → producer 5-hop → one-tx settle/credit → surfacer)

```
1. RECALL (4.2)          : confident recall writes exposure(trace_id, episode_id, recall_margin, task_ref=NULL).
2. COMMIT                : the agent does work, commits with a `Hive-Trace: <trace_id>` trailer (re-targets WHICH
                           trace; can NEVER set the reward sign/value — verifiable-credit-only).
3. PRODUCER TICK — OutcomeProducer.step(now), ONE SQLite tx on the hive WAL DB (A7), fixed hop order (M09 I2):
     (a) associate : OutcomeSource.poll() → CommitFacts (touched_blame pre-attached on the impure side).
                     Joiner associates each in-window commit (assoc_window_s, recall_margin- & ε-discounted) to its
                     trace, or uses the trailer override; UPDATE exposure SET task_ref=<SHA>; INSERT task_outcomes
                     (PK (task_ref,trace_id), state='provisional', reward=+0.2, settle_at=merge_ts+settle_days,
                      family_scope = "<remote>|<lang>|<workflow>", introduced_lines stored for delayed clawback).
     (b) settle    : due_settlements(now): task_outcomes WHERE state='provisional' AND settle_at<=now → 'settled_pos'.
     (c) clawback  : a revert OR a bugfix whose modified lines OVERLAP the original introduced lines (blame-line,
                     NOT same-file) → 'clawed_back', reward=-1.0. A same-tick provisional+revert nets to clawback.
     (d) emit+drain: for each newly settled_pos/clawed_back row with ts > meta['_last_drain_ts']:
                       SettledOutcome(reward_sign=±1) → Attributor.split(outcome, exposed_for(trace), isolation)
                         → CreditDelta[] (margin-split, conserved Σ==magnitude; isolation ids excluded — A5)
                       → UtilityStore.apply_credit(deltas): utility.wins/losses += share (NEVER episodes.weight),
                         utility.version+1, utility_sources upsert (DISTINCT-agent n_sources).
                     advance meta['_last_drain_ts'] = max(seen ts)   (ported watermark; no double-credit).
   COMMIT (single-writer CAS lane — the WHOLE tick is atomic; a posterior-write failure rolls back the settlement,
            so partial credit / torn write is impossible — A7).
4. NEXT RECALL — SURFACE : a later recall in the same family calls utility_map(family_scope, confident_only=True)
                           (only posteriors whose CI excludes the no-signal point appear) and
                           surfacer.order(scored, utility_map, family_scope=fam): rank by weight*f(util),
                           f∈[0.5,1.5] (DEMOTES on confident-negative), ε-explore skips utility on Bernoulli(eps).
                           Isolation slice + un-confident posteriors are identity (never moved).
5. GUARDRAIL-3 (A6)      : PredictionBiasMonitor.divergence(family, window_s); the tick logs WARN when
                           |divergence| > prediction_bias_threshold ("ranker stale, codebase moved").
```

---

## §5 — `hive_init` handshake (two-phase, content-hash-verified)

The first-run journey: clone → README → `./hive up` → MCP connect → `hive_init` phase 1 → write block → `hive_init` phase 2 confirm → `hive_health` link verify. The recorded link **cannot lie** about which block content landed.

```
 1. CLONE          : operator clones the repo; reads README.
 2. ./hive up      : thin liveness wrapper → `docker compose up -d`; polls `docker inspect
                     .State.Health.Status` until 'healthy' or 180s. HEALTHCHECK exits 0 IFF
                     health()['ok'] AND health()['embedder_loaded'] — so "healthy" ⇒ the CPU
                     bge-small model is RESIDENT (no recall routed at a cold server). Timeout ⇒
                     dump `docker compose logs` to stderr, exit non-zero.
 3. MCP CONNECT    : harness attaches to the container's stdin/stdout (stdio JSON-RPC). initialize
                     / tools/list returns the 8 TOOL_DEFINITIONS.
 4. hive_init P1   : hive_init(repo_path, harness)  [confirm_hash absent].
                     InstallPlanner.plan(): resolve the primary rules file (ordered candidates,
                     first existing wins, else create CLAUDE.md/AGENTS.md), detect hook support,
                     render a RulesBlock whose trailer_key = producer.stamp_trailer (SINGLE source —
                     never literal; kills §11 CONFIG_DRIFT). Phase 1 is PURE except read-only probes:
                     ZERO writes to meta. Returns InstallPlanJSON{phase:1, rules_block, trailer_key,
                     expected_confirm_hash = sha256(rendered_text), watch_warning?}.
 5. AGENT WRITES   : the agent writes the marker-delimited block (with embedded version) into the
                     rules file:  <!-- hive-init:start --> … <!-- hive-init:version=N --> …
                     <!-- hive-init:end -->. Idempotent: re-running replaces the marker region only.
 6. hive_init P2   : hive_init(repo_path, harness, confirm_hash=<hash of installed block>).
                     The server RE-RENDERS the expected block, recomputes block_hash, and requires
                     confirm_hash == block_hash:
                       match    → UPSERT LinkRecord into meta['hive_init:link:<repo_path>'];
                                  return {phase:2, linked:True, link:{…}}. EXACTLY one write.
                       mismatch → {phase:2, linked:False, error:'stale_or_wrong_block',
                                   expected:<hash>}; ZERO rows written.
                       idempotent re-confirm with same hash ⇒ no-op upsert.
 7. hive_health    : hive_health(repo_path) → HealthSnapshot with linked:True + link. The no-repo_path
                     path is byte-identical to the base snapshot (additive total=False keys).
                     `linked:true → ready` ends the sequence. hive_init NEVER mutates
                     producer.watch_repos (the deliberate cut); an unwatched repo yields a non-blocking
                     watch_warning and still LINKS.
```

---

## §6 — Module → contract ownership index

| Module | Owns (port / file / table) |
|---|---|
| **M01 embed** | `EmbeddingProvider` port; `hive/adapters/embedding/{local_st,head,factory}.py`; `FrozenPcaHead.to_bytes/from_bytes` codec (BUILD-NEW HVH1, A C2); the `reembed_head_<w>` meta blob (codec only) |
| **M02 index** | `VectorIndex`/`MutableVectorIndex` ports; `hive/adapters/index_exhaustive.py` (`ExhaustiveCosineIndex`, `is_authoritative=True`, no ANN branch); HNSW/external adapters behind the same port |
| **M03 store** | `EpisodeStore` (+ `Migrator`) port; `hive/store/sqlite_episode_store.py`; **all tables** (`episodes, blobs, meta, exposure, task_outcomes, utility, utility_sources`); `_RECALL_PREDICATE`; the single-writer tx; `_last_drain_ts` watermark (A7); `approve()` isolation stamp (A5); `rebuild_index_from_store` (B3) |
| **M04 recall** | `RecallPipeline` port; `hive/domain/recall.py`; `NormalizedEntropyGate` (PORT+EXTEND softmax-β, B1); `_resolve_query_family` (A2); `ExposureLedger` write seam |
| **M05 admit** | `SecretScanner` + `AdmissionLedger` ports; `hive/domain/secret_scan.py`, `hive/adapters/scanner_regex.py`; `ScanVerdict`/`SecretFinding` frozen contracts; the pending→approved policy (non-swappable) |
| **M06 mcp** | the 8-tool MCP surface; `hive/adapters/mcp/{server,tool_defs}.py`; `TOOL_DEFINITIONS`; approved-only belt + neutral `reference_context` framing; `HealthSnapshot` extension |
| **M07 onboard** | `InstallPlanner` port; `hive_init` handshake; `RulesBlock`/`InstallPlan`/`LinkRecord` domain; `teardown.sh`, `import.sh`, `./hive`; link kv keys (no new table) |
| **M08 loop** | `UtilityStore` port; `hive/store/sqlite_utility_store.py`; **`hive/domain/surfacer.py` (`UtilitySurfacer`, A1)**; `hive/domain/attribution.py` (`Attributor`, `PredictionBiasMonitor`, A3/A6); the Beta-Bernoulli posterior policy |
| **M09 produce** | `OutcomeSource` + `OutcomeProducer` ports; `OutcomeJoiner` (pure §11 state machine); `hive/adapters/producer_git.py` (`GitCliSource`); the producer tick driver; `task_outcomes`/`exposure.task_ref` write logic |
| **M10 eval** | `hive/research/{metrics_ir,keystone,eval_membrane}.py` (dev-time only; import-linter-fenced from core); `bootstrap_ci`, `abstention_auroc`, `run_keystone_eval`, `admit`/`export_baseline` |
| **M11 config** | `Config` + `Config.load`; the three provider registries (`EMBEDDING/INDEX/PRODUCER_PROVIDERS`); `RELOAD_TIER` + `reload`; `recall.epsilon_explore` validation (A4); `configure_json_logging`, `health`, `run_daily_backup` |
| **M12 container** | `Dockerfile`, `compose.yaml`, `hive/tools/{entrypoint,healthcheck,bake_model}.py`, `./hive`; the `/data` named WAL volume layout; the healthy≡embedder-resident HEALTHCHECK; non-root final user |

---

## §7 — Prose-only boundary → enforcement table

Each soft boundary, the **one test** that hardens it, and the **RULE-2 mutation** that proves the test has teeth.

| # | Boundary (invariant) | Owner | Enforcing test | RULE-2 mutation (fault → red → restore) |
|---|---|---|---|---|
| **B1** | **Approved-only recall** (pending never recallable; §6.1#5b) | M03 + M06 (belt-and-suspenders) | `test_pending_never_in_candidates` (M03); `test_recall_filters_to_approved_only` (M06) | M03 M2: `scan_approved` drops `_RECALL_PREDICATE` (returns all rows) → pending becomes recallable → RED. M06: delete the `status=='approved'` belt at candidate-assembly → `test_recall_filters_to_approved_only` RED (proves both layers have teeth) |
| **B2** | **Encode-chain single-source** (capture == recall; no lazy-fit/random split-brain) | M01 | `test_encode_eq_encode_batch_single` (atol 1e-6) | delete `out = out/n` in `ProjectionHead.apply` → unit-norm + `encode==encode_batch[0]` RED → restore |
| **B3a** | **Gate shape** (softmax-over-β·sims; β actually on the tested path — B1 resolution) | M04 | `test_gate_softmax_mass_uses_beta` (β=32 strictly more peaked than β=4; mass≈softmax(β·sim)) + `test_uniform_high_entropy_suppresses` | (i) flip `entropy_norm > h_frac_max` → `<` → `test_uniform_high_entropy_suppresses` RED. (ii) replace `beta*sim_i` with `sim_i` → `test_gate_softmax_mass_uses_beta` RED |
| **B3b** | **Index authoritativeness / boot-rebuild** (exhaustive never silently ANN; status=truth, index=warm cache rebuilt from `scan_approved` — B3 resolution) | M02 + M03 | `test_no_approx_threshold_attribute_on_exhaustive` + `test_recall_exact_above_legacy_threshold` (M02); `test_index_rebuilds_from_approved_only` + `test_rebuild_is_idempotent` + `test_rebuild_recovers_after_crash_between_commit_and_add` (M03) | M02 #2: prepend `if len(ids)>10_000: return self._ann.candidates(...)` → `test_recall_exact_above_legacy_threshold` RED. M03 predicate-bypass: feed `rebuild_index_from_store` `scan_all()` instead of `scan_approved()` → `test_index_rebuilds_from_approved_only` RED (pending becomes searchable) |
| **B4** | **Credit conservation + posterior-not-weight** (Σ(d_wins+d_losses)==magnitude; weight immutable — A3) | M08 | `test_credit_writes_posterior_never_weight` (margin-split conserved to magnitude; `episodes.weight` unchanged) + a hypothesis property test over random margins (incl. all-zero ⇒ uniform) | re-introduce `store.bump_weight(eid, +share)` in `apply_credit` → `test_credit_writes_posterior_never_weight` RED. Also (iv) `weight += alpha_u·utility` → `test_weight_never_written` RED |
| **B4b** | **Surfacer base multiplier is weight, not sim/alpha (A1)** | M08 | `test_weight_is_base_multiplier_not_alpha` (A weight=2.0 sim=0.9 outranks B weight=1.0 sim=0.95) | change base multiplier `s.weight*f` → `s.sim*f` → RED (B ranks first on sim) |
| **B4c** | **Verifiable-credit-only** (reward sign from git state only; trailer cannot inject reward) | M09 + M08 | `SettledOutcome` `test_reject_zero_sign` (reward_sign∉{−1,+1} raises); `test_stamp_trailer_overrides_window` (trailer re-targets trace, never sign) | allow `reward_sign` outside {−1,+1} in `__post_init__` → `test_reject_zero_sign` RED |
| **B4d** | **Isolation slice never reweighted (guardrail-2, A5)** | M03 + M08 | `test_isolation_membership_assigned_at_fraction` (≈5% deterministic); `test_isolation_ids_never_credited` | force `_is_isolation` → always `False` → `test_isolation_membership_assigned_at_fraction` RED; remove the isolation exclusion in `Attributor.split` → `test_isolation_ids_never_credited` RED |
| **B4e** | **Prediction-bias monitor real (guardrail-3, A6)** | M08 | `test_divergence_flags_stale_ranker` (wins=9/losses=1 vs 10 realized=0 ⇒ divergence≈0.9; empty window ⇒ 0.0) | change `predicted - realized` → `predicted - predicted` → RED (monitor blind) |
| **B5** | **Settlement ordering** (fixed hop order; blame-line clawback, not same-file; no double-credit watermark — M09 I2/I3/I4 + A7) | M09 + M03 | `test_same_file_no_blame_overlap_no_clawback` (GUARD a); `test_blame_overlap_fires_clawback`; `test_hop_order_settle_then_clawback_nets`; `test_settle_due_only_ripe_provisional` (M03); `test_producer_tick_is_one_transaction_one_db` (A7) | disable blame-overlap (clawback on any same-file) → GUARD a RED. M03 M3: `settle_at<=now` → `>=` → `test_settle_due_only_ripe_provisional` RED. Split step-(d) drain into its own connection/commit → `test_producer_tick_is_one_transaction_one_db` RED (settlement no longer rolls back with a posterior-write failure) |
| **B6** | **Secret-safe floor on BOTH write and import (A C5)** | M05 + M03(import) | `test_aws_akia_refused` (+ per-rule family); `test_stage_refuses_unscanned_secret`; `test_import_scans_secrets` (planted `AKIA`/`sk-` in archived row refused/redacted before persist) | delete the `aws_akia` regex → `test_aws_akia_refused` RED. Comment out `scanner.scan(...)` in `import_corpus` → `test_import_scans_secrets` RED (token persists into `episodes.text`/blob) |
| **B7** | **Value dtype / geometry / single-writer CAS** (unit-norm float32[d]; no lost-update) | M03 + M01 | `test_episode_rejects_float64_value` / `test_episode_rejects_2d_value` / `test_episode_rejects_non_unit_norm`; `test_head_bytes_roundtrip_preserves_w_version` (A C2); `test_cas_blocks_stale_approve` | M03 M4: CAS `WHERE id=? AND version=?` → `WHERE id=?` → `test_cas_blocks_stale_approve` RED (lost-update double admission). A C2: hard-code `W_VERSION` header to 0 → `test_head_bytes_roundtrip_preserves_w_version` RED |
| **B8** | **CONFIG_DRIFT floor + ε placement (A4)** | M11 | `test_gate_reads_same_frozen_recall_object` (`gate._recall is cfg.recall`); `test_recall_epsilon_validated_positive` (`recall.epsilon_explore=0.0` raises; `producer.assoc_epsilon=0.0` OK; `hasattr(cfg.producer,'epsilon_explore') is False`) | wire the gate with `cfg.recall.H_frac_max` (float copy) → `test_gate_reads_same_frozen_recall_object` RED. Move the `>0` check back to `producer.epsilon_explore` → `test_recall_epsilon_validated_positive` RED |
| **B9** | **Schema enforcement at the trust boundary** (malformed call never reaches a port) | M06 | `test_malformed_call_rejected_before_port_touched` (missing `text`/`approver` ⇒ `isError`, port `call_count==0`) | delete the pre-dispatch validation step → the test RED |
| **B10** | **Trailer-key single source / link cannot lie** (M07) | M07 | `test_trailer_key_is_single_sourced`; `test_phase2_stale_hash_refused` (mismatch ⇒ 0 rows) | hard-code the trailer literal → `test_trailer_key_is_single_sourced` RED. Flip `!=`→`==` in the phase-2 compare → `test_phase2_stale_hash_refused` RED |
| **B11** | **Healthy ≡ embedder resident** (container; no recall at a cold server) | M12 | `test_healthcheck_red_before_embedder_resident` | drop the `embedder_loaded` conjunct in `healthcheck.main()` → the test RED (reports healthy while model absent) |

> Every gate, ranker, state-machine, and credit path above carries a RULE-2 mutation: inject the fault, watch the named test go red, restore, watch it go green. Prefer contracts that cannot lie (frozen `__post_init__`, `CHECK` constraints, `is`-identity, conservation property tests) over prose — these are the spine of the first-class test mandate.
