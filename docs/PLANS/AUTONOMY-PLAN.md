# Hivemind — Mechanical Memory Lifecycle (quarantine → demand → decay)

**Status:** APPROVED v2 — building (C1 in progress)
**Amendment (2026-06-10, human-directed):** the §3.3 in-place migration/backfill and AC9
are CUT — this build starts from a **clean, empty store** (no prior-version memories are
carried over; context loading is a later concern). The store **refuses** an old-format
episodes table at construction instead of migrating it. The v2 columns live directly in
the base `_SCHEMA`; no `_ensure_column`/`schema_version` machinery exists.
**Date:** 2026-06-10 · **v2 (this rewrite):** the approved de-engineering of the v1 full
trust-lifecycle draft. v1's evidence economy made humans (`trustctl` review labor) and
cooperating agents (voluntary `hive_evidence` calls) **load-bearing** — fuel that would not
reliably arrive. v2 keeps the structural safety (quarantine, serving gate, decay, supersession,
anti-echo) and rebuilds every promotion signal from **traffic the server already observes**
(writes, recalls, identities, time). The cut layers live in **Appendix A** with add-back paths.
v1's supersession design survived its `/software-design-review` (Mode B) unchanged and is
retained verbatim in §4.4.
**Companions:** `ADMIN-CLI-PLAN.md` (unchanged), `HYBRID-RECALL-PLAN.md` (reconcile §4.2
miss-recording shape with this plan before either lands — this plan's version wins),
`TODOS.md` (TODO 1/3 absorbed; TODO 2 obsolete — no pending queue; TODO 4 recast as the
demand-promotion rule).
**Builds on:** AUTH-PLAN (landed — token identity is the anti-gaming key), REMOTE-ACCESS-PLAN
(landed — 413/429 belts bound the new capture surface).

---

## 0. Goal & acceptance criteria

**Goal.** Let fleet agents **capture without asking** and let the store **serve, retire, and
correct itself mechanically**: autonomous captures land structurally unservable; *measured
demand from other agents* — not verification labor — promotes them into labeled serving; use
keeps memories alive, disuse decays them; humans correct by superseding, never by reviewing
queues. **Zero load-bearing humans, zero load-bearing cooperative agent calls, zero server-side
scheduler/LLM/repo-FS.**

| AC | Criterion | Owner |
|---|---|---|
| AC1 | A `hive_capture` lands `trust='quarantined'`: embedded but **structurally unservable** — absent from the index, excluded by the servable predicate, dropped by the recall belt. No tier, flag, or path serves it unpromoted. | §3, §4.1, C4 |
| AC2 | **Promotion is mechanical demand**: a quarantined memory matching ≥ `demand_m` recall misses within `demand_window_days`, from **≥1 identity other than its writer**, with **no servable competitor**, auto-promotes to `provisional` and is served **with its trust label**. No other promotion path exists in this build. | §4.2, C3/C5 |
| AC3 | **Anti-gaming is structural**: demand consisting solely of the writer's own misses never promotes (the writer cannot manufacture both supply and demand). | §4.2, C3 |
| AC4 | **Decay is the default**: quarantined rows unpromoted after `quarantine_ttl_days` and provisional rows unexposed for `provisional_ttl_days` read as `deprecated` — enforced at the recall belt even while the warm index is stale, materialized by the boot/post-capture sweep. `established` never age-decays. | §4.3, C2/C3 |
| AC5 | **Supersession is the only retirement of `established`** and is trust-asymmetric by construction: `hive_write(replaces=X)` (human-vouched) retires X immediately — deprecated + `superseded_by` stamped + de-indexed + audit row, one owner (`store.supersede`), one tx; `hive_capture` has **no** `replaces` and no retirement power of any kind. `hive_fetch` of a superseded row annotates the **terminal** successor `{episode_id, content_hash}`. | §4.4, C2/C4/C5 |
| AC6 | Recall hits carry `trust` and `ts` — consumers can discount provisional content and order coexisting versions. | §5.2, C5 |
| AC7 | Every non-answer is recorded (`abstained` / `no_match` / `secret_refused`, secret-scanned before persistence) and `hive_health(include_gaps=true)` returns clustered demand + `trust_counts` (quarantine pile-up visible, never silent). | §4.2, §5.3, C5 |
| AC8 | **Zero**: new dependencies; server-side LLM; scheduler; repo filesystem; load-bearing human step; load-bearing cooperative agent call. With `autonomy.enabled=false` the system is byte-stable with today (capture refused, no promotion/decay/labels-only-additive). | §1, §6, C5 |
| ~~AC9~~ | ~~The live DB migrates in place~~ **CUT by amendment** — clean-store start; old-format episodes tables are refused at construction. | §3.3, C1 |

---

## 1. Principles (what holds, what this build refuses)

**Held invariants (unchanged from today):** never-hallucinate (entropy gate + abstain-no-resurrect,
untouched); the secret floor (extended to miss query-text); recall framed as `reference_context`;
identity = authenticated token label (INV-2); single-writer WAL; hexagonal purity (the new
lifecycle policy is pure domain code behind ports).

**The v2 design rule — every load-bearing signal is server-observable:**
| Signal | Derived from |
|---|---|
| demand | `recall_misses` (already needed for gaps) |
| independence | token identity on misses vs. the capture's writer |
| usage / liveness | exposure records the server writes when it serves |
| correction | `replaces` riding the natural human write flow |
| death | time |

Humans and cooperative agent calls may *accelerate* a future layer (Appendix A); nothing in this
build waits for them.

**Refused in this build (deliberately):** content-correctness verification. Promotion means
*"demanded, unique, and not self-demanded"* — not *"true."* The guards against wrongness are the
trust label on every served hit, the entropy gate, cheap human supersession, and decay. Anchors /
server-side verification are the named next layer (Appendix A), adopted knowingly later — not
smuggled in here.

---

## 2. Architecture (delta view)

```
 agent session (hooks from manifest v2)
   │ task-start : hive_recall(query) ──► hits + {trust, ts} labels   (or abstain → miss recorded)
   │ turn-end   : hive_capture(text)            — no ask, lands quarantined
   │ correction : (user confirms) hive_write(text, approved_by, replaces=old_id)
   ▼
 ┌────────────────────────── hive server (trust boundary unchanged) ──────────────────────────┐
 │ mcp_server: 6 tools ── validation belt ── identity = token label                            │
 │ write  : scan → stage → embed → complete(trust='established') [─ supersede(X) if replaces]  │
 │ capture: scan → stage → embed → complete(trust='quarantined') → on_capture trigger → sweep  │
 │ recall : encode → search(servable index) → gate → hits+labels ── exposure(agent_id) ──┐     │
 │              └─ abstain/empty → record_miss ── on_miss trigger (demand-promotion) ────┤     │
 │ LifecycleService (PURE, domain): DemandRule.decide · decay · sweep   ◄─ ports only ───┘     │
 │ store (one WAL): episodes(+trust,superseded_by,last_active_ts) · recall_misses              │
 │                  exposure(+agent_id) · evidence_events(server-written audit ONLY) · meta v2 │
 └─────────────────────────────────────────────────────────────────────────────────────────────┘
   ▲ operator: nothing required. (Optional later: ADMIN-CLI promote/demote forwards — Appendix A)
```

**Serving boundary** (single source, `is_servable` in domain + one SQL predicate):

```
servable ≡ status='approved' AND (
      trust='established'
   OR (trust='provisional' AND last_active_ts > now − provisional_ttl) )
```

Three independent fail-closed layers, as today: the scan predicate (index rebuild), index
membership (synced on promote/demote/sweep), and the per-hit recall belt — the belt re-evaluates
`is_servable(ep, now)` so a TTL-lapsed provisional row is dropped even while the warm index is
stale. Quarantined and deprecated rows are unreachable the way pending is today.

**No scheduler:** promotion triggers run synchronously at the two moments demand can change —
a capture arriving (check it against existing misses) and a miss arriving (check it against
quarantined rows, O(Q·d) linear scan, Q bounded by TTL decay). The decay sweep runs at boot and
piggybacks after each capture. Between sweeps, the lazy predicate/belt is authoritative.

---

## 3. Data model (all additive; no table rebuild)

### 3.1 Episode columns (idempotent `_ensure_column` helper, PRAGMA table_info-guarded)

```sql
ALTER TABLE episodes ADD COLUMN trust TEXT NOT NULL DEFAULT 'quarantined';  -- fail-safe default
ALTER TABLE episodes ADD COLUMN superseded_by INTEGER;     -- §4.4: latest applied successor (NULL = live)
ALTER TABLE episodes ADD COLUMN last_active_ts INTEGER NOT NULL DEFAULT 0;
  -- liveness clock: set at capture/write, refreshed at promotion and at every exposure.
ALTER TABLE exposure ADD COLUMN agent_id TEXT;             -- who was served (liveness + future layers)
CREATE INDEX IF NOT EXISTS idx_episodes_trust ON episodes(trust);
```

- `trust ∈ {quarantined, provisional, established, deprecated}` — enforced in the `Episode`
  model (`__post_init__`), not a DDL CHECK (rebuild-free).
- **Model invariant changes** (`models.py::Episode.__post_init__`): the biconditional
  `approved ⇔ approved_by` relaxes to implications — `approved_by is not None ⇒
  status='approved'`; `trust ∈ ('established','provisional') ⇒ status='approved'`;
  `superseded_by is not None ⇒ trust='deprecated'` (a live row cannot point at a successor);
  `trust ∈ TRUST_STATES`.
- **`RecallHit` gains `trust: str` and `ts: int`** — the pipeline has the full `Episode` in hand
  at resolve time and today drops both at this boundary; with immutable rows + dedup, "update"
  always coexists as a second row, so the consumer MUST be able to label and order versions.
- **Naming debt, accepted (unchanged from v1):** `status='approved'` now reads as *materialized*
  (scanned + embedded + blob complete); `trust` carries trust. Renaming the enum is a
  table-rebuild migration — deferred, recorded in 02-CONTRACTS.

### 3.2 New tables

```sql
CREATE TABLE IF NOT EXISTS recall_misses(                 -- TODO 1, absorbed
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query_text TEXT NOT NULL,                               -- ''/masked when the scanner fired
  query_vector BLOB,                                      -- NULL on secret_refused
  agent_id TEXT NOT NULL,                                 -- the asker (the anti-gaming key)
  miss_type TEXT NOT NULL CHECK(miss_type IN ('no_match','abstained','secret_refused')),
  ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_misses_ts ON recall_misses(ts);

CREATE TABLE IF NOT EXISTS evidence_events(               -- SERVER-WRITTEN AUDIT LOG ONLY.
  id INTEGER PRIMARY KEY AUTOINCREMENT,                   -- No tool writes here; no client kind exists.
  episode_id INTEGER NOT NULL, kind TEXT NOT NULL,        -- kind ∈ {promote, ttl_expired, supersede}
  actor TEXT NOT NULL, ts INTEGER NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}');                    -- e.g. {"rule":"demand","n_misses":4,...}
CREATE INDEX IF NOT EXISTS idx_evidence_episode ON evidence_events(episode_id, kind);
```

(`evidence_events` keeps the v1 name deliberately: Appendix-A layers extend it with client-fed
kinds by `ALTER`/enum-widening, not by a new table.)

### 3.3 Migration & backfill — CUT (clean-start amendment, 2026-06-10)

No migration/backfill exists. The v2 columns/tables live directly in the base `_SCHEMA`;
a store whose `episodes` table predates the lifecycle columns is **refused at
construction** (clear RuntimeError, never a mid-recall "no such column" crash). The column
default stays `'quarantined'` so any forgotten future write-site **fails safe** (lands
unserved, never over-served). `container._REQUIRED_TABLES` gains `recall_misses`,
`evidence_events` (kept from the original plan).

---

## 4. The pure domain core (`hive/domain/lifecycle.py`, NEW — purity-fenced like its siblings)

### 4.1 States + the one servability predicate

```python
QUARANTINED, PROVISIONAL, ESTABLISHED, DEPRECATED = ("quarantined", "provisional",
                                                     "established", "deprecated")
TRUST_STATES = (QUARANTINED, PROVISIONAL, ESTABLISHED, DEPRECATED)

def is_servable(*, status: str, trust: str, last_active_ts: int,
                now: int, provisional_ttl_s: int) -> bool:
    """THE single servability rule (§2). Used by the store's scan predicate (rebuild), the
    promotion/demotion index sync, and the mcp_server recall belt. Pure, total. // O(1)."""
```

### 4.2 Demand-promotion (the only promotion path)

```python
@dataclass(frozen=True, slots=True)
class MissRow:        # carrier the store returns for the demand window
    vector: "np.ndarray"; agent_id: str; ts: int

@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promote: bool; n_misses: int; n_other_identities: int
    competitor_sim: float; reason: str          # machine-readable; goes into the audit payload

class DemandRule:
    def __init__(self, *, demand_m: int, demand_tau: float, competitor_tau: float) -> None: ...
    def decide(self, *, candidate_vec: "np.ndarray", candidate_writer: str,
               misses: Sequence[MissRow], competitor_top_sim: float) -> PromotionDecision:
        """promote IFF:
             matched   = [m for m in misses if cos(m.vector, candidate_vec) >= demand_tau]
             len(matched) >= demand_m
         AND any(m.agent_id != candidate_writer for m in matched)      # AC3 anti-gaming
         AND competitor_top_sim < competitor_tau                       # a servable row this
                                                                       # close already answers it
        Pure, total, never raises; non-finite vectors ⇒ promote=False (fail-closed). // O(|misses|·d)."""
```

- `misses` = the window slice (`ts > now − demand_window_days`, vector non-NULL) — the store
  serves it; the rule stays pure.
- `competitor_top_sim` = top-1 cosine of the candidate against the **servable** index (one
  existing `index.search(candidate_vec, 1)` call) — if an established/provisional row is already
  that close, demand is answerable and promotion is vetoed (near-dup pile-up prevention).
- **Trigger sites** (both synchronous, both inside the single-writer lane):
  `on_capture(new_eid)` — evaluate the new candidate against the existing window;
  `on_miss(new_miss)` — linear-scan live quarantined rows (`O(Q·d)`, Q bounded by decay) and
  evaluate each whose cosine to the new miss ≥ `demand_tau`.
- Applying a promotion = `store.set_trust(eid, PROVISIONAL)` (index add) + `last_active_ts=now`
  + one `promote` audit row carrying the `PromotionDecision` fields.

### 4.3 Decay (lazy + sweep; no scheduler)

```python
def decayed(*, trust: str, last_active_ts: int, created_ts: int, now: int,
            quarantine_ttl_s: int, provisional_ttl_s: int) -> bool:
    """quarantined: dead if now − max(created_ts, last_active_ts) > quarantine_ttl.
    provisional: dead if now − last_active_ts > provisional_ttl (exposure refreshes it;
    promotion stamps it, so a fresh promotion is never instantly dead).
    established/deprecated: never. Pure. // O(1)."""
```

- **Lazy:** `is_servable` already excludes lapsed provisional rows; lapsed quarantined rows are
  excluded from `on_miss` candidate scans (don't promote the dead).
- **Sweep** (`LifecycleService.sweep(now)`): materializes `decayed` rows to `deprecated`
  (+`ttl_expired` audit row each, index remove for lapsed provisional). Runs at **boot**
  (container step, before index build) and **after each capture** (low-frequency piggyback).
  Idempotent; bounded by the live quarantined+provisional count.
- `established` never age-decays — supersession (§4.4) is its only retirement.

### 4.4 Supersession (unchanged from the design-reviewed v1 amendment)

Episodes stay immutable; "edit" = supersede. **One owner**: every retirement-with-replacement
goes through `store.supersede()` (retire + stamp + audit row + index remove, one tx).

- **Human path only in this build:** `hive_write(replaces=X)` — after the new row lands
  `established`, `supersede(X, new_id)` runs. Justified by the v3 trust model (the same
  `approved_by` vouch already *creates* instantly-served memories; a wrong retirement is the
  cheaper, recoverable error). Crash between the two txs is benign (both versions served =
  pre-plan status quo; retry idempotent).
- **`hive_capture` has no `replaces`** — with no claim machinery in this build there is nothing
  for an autonomous "replaces" to safely do, and a quarantined capture must never gain
  retirement power (AC5). The claim-only capture path returns with the evidence layer
  (Appendix A).
- **Guards (enforced, not prose):** unknown target fails the WHOLE call before staging (no
  stored-but-not-retired partial); `supersede` refuses `target == replacement` (reachable: a
  `replaces=X` write whose text dedups back to X); idempotent re-run (column already stamped ⇒
  no duplicate audit row); re-supersede = last-write-wins on the column, history in the ledger.
- **Chains:** walked server-side at read time (`terminal_successor`, depth-capped +
  visited-set-bounded; cycles unconstructable over MCP — the replacement is always the
  just-created row). `hive_fetch` annotates the **terminal** successor as
  `{episode_id, content_hash}` (hash because fetch is hash-keyed — a bare id would be
  unfollowable). Superseded rows are deprecated ⇒ already unservable; recall needs no change.

---

## 5. Surfaces

### 5.1 Tools (6 — `tool_defs.py`)

| Tool | Change |
|---|---|
| `hive_write` | + optional `replaces` (integer — §4.4 immediate human-vouched supersession; unknown target fails the whole call). Otherwise unchanged: human-approved → `established`, served instantly. |
| `hive_capture` | **NEW.** `required=["text"]`; optional `tags`, `source`. Scan → stage (dedup) → embed → complete `trust='quarantined'`, `approved_by=NULL` → `on_capture` promotion check → decay sweep. Returns `{status:'quarantined', id, content_hash, scan}`. With `autonomy.enabled=false` → `{status:'disabled'}`, nothing written. |
| `hive_recall` | Hits gain `trust` + `ts` (AC6). Server-side: exposure recorded on CONFIDENT (trace, eids, margins, **agent_id**; bumps `last_active_ts`); miss recorded on ABSTAIN (`abstained`) / EMPTY (`no_match`), then the `on_miss` promotion trigger runs. No new arguments. |
| `hive_fetch` | Envelope gains `superseded_by: {episode_id, content_hash}` (terminal successor) when the fetched row is superseded. |
| `hive_health` | + `trust_counts` (per-state), `n_misses_7d`; + optional `include_gaps` → top-10 cosine-clustered miss report `{representative_query, miss_count, miss_types, last_seen}` (TODO 3 folded; deterministic, app-side `hive/app/gaps.py`, capped window). |
| `hive_init` | Surface unchanged; **manifest v2** content (§5.4). |

`hive_evidence` does **not** exist in this build. `recall_misses` query text is secret-scanned
before persistence: REFUSE ⇒ row stored with `query_text=''`, `query_vector=NULL`,
`miss_type='secret_refused'` (counts in telemetry, can never drive promotion); REDACT ⇒ masked
text + a vector **re-encoded from the redacted text** (never the raw query's vector).

### 5.2 Read path (`hive/domain/recall.py`)

- Revive the ledger port (minus the v1 session machinery):
  ```python
  @runtime_checkable
  class ExposureLedger(Protocol):
      def record_exposure(self, trace_id: str, items: Sequence[tuple[int, float]],
                          *, agent_id: str, ts: int) -> None: ...
      def record_miss(self, query_text: str, query_vector: Optional[bytes],
                      agent_id: str, miss_type: str, *, ts: int) -> None: ...
  ```
- `RecallPipeline.__init__` gains `ledger: ExposureLedger`, `clock_now: Callable[[], int]`.
  Exposure written on the CONFIDENT path (post-resolve, with margins — pinned values unchanged);
  `record_miss` on ABSTAIN/EMPTY. Ledger calls are fail-open for the read (a ledger fault never
  breaks recall — logged) but never silently absent (RULE-2 pins both).
- `RecallHit(episode_id, text, sim, trust, ts)`; the mcp_server belt re-checks
  `is_servable(ep, now)` per hit (replacing the bare `status=='approved'` check).

### 5.3 Admission + store (`hive/domain/admission.py`, `hive/adapters/store_sqlite.py`)

```python
# admission — write() unchanged in behavior; gains replaces: Optional[int] = None (§4.4:
# validated-exists BEFORE staging; supersede runs after the new row lands established).
def capture(self, text: str, *, proposed_by: str, weight: float = 1.0,
            source: Optional[str] = None, tags: str = "",
            request_id: str = "-") -> WriteResult     # status='quarantined' in the result

# store — new/changed methods (single-writer lane unchanged):
def complete(self, episode_id, value, *, expected_version, trust, approver=None,
             approved_ts=0, last_active_ts=0) -> bool   # generalizes approve(); index add IFF servable
def approve(...)                                        # kept: thin wrapper → complete(trust='established')
def set_trust(self, episode_id: int, new_trust: str, *, now: int) -> bool
    # transactional; index add on →servable / remove on servable→not (best-effort, [B3] rebuild recovers)
def supersede(self, target_id: int, replacement_id: int, *, actor: str, ts: int) -> bool
def terminal_successor(self, episode_id: int, *, max_depth: int = 10) -> Optional[tuple[int, str]]
def record_exposure / record_miss                       # the ExposureLedger port; exposure bumps
                                                        # episodes.last_active_ts in the same tx
def misses_window(self, since_ts: int) -> list[MissRow]
def quarantined_candidates(self, *, now: int, quarantine_ttl_s: int) -> list[tuple[int, "np.ndarray", str, int, int]]
    # live (non-decayed) quarantined rows: (id, value, proposed_by, ts, last_active_ts)
def insert_audit(self, episode_id: int, kind: str, actor: str, ts: int, payload: str) -> int
def sweep_decayed(self, *, now: int, q_ttl_s: int, p_ttl_s: int) -> dict   # materialize §4.3
def scan_servable(self, *, now: int, provisional_ttl_s: int) -> list[tuple[int, "np.ndarray"]]
    # the single predicate source; scan_approved() delegates here (compat alias)
def trust_counts(self) -> dict[str, int]
```

`LifecycleService` (domain, composes the above through ports + `DemandRule` + injected clock)
exposes `on_capture(eid)`, `on_miss(vector, agent_id)`, `sweep(now)`; the container wires it
into admission (capture trigger + sweep) and the recall handler (miss trigger).

### 5.4 Hook manifest v2 (`hive/app/onboard.py`)

`ONBOARDING_MANIFEST_VERSION = 2`. Data-only change:

| event | action | directive (abridged) |
|---|---|---|
| task-start | recall | `hive_recall(topic)`; treat hits as reference; prefer higher-`trust`, newer-`ts` versions. |
| turn-end | capture | store durable insights via `hive_capture(text)` — **no need to ask**; bugs+fixes, dead-ends, decisions, gotchas. |
| correction | write | when the user confirms existing team memory is wrong/outdated: `hive_write(corrected_text, approved_by=<user>, replaces=<episode_id>)`. |
| commit | capture | unchanged (bug-fix capture, now via `hive_capture`). |

`hive_health` reports `manifest_outdated` when a link record carries `manifest_version < 2`
(drives re-init). Tier mechanics (claude-code Stop-hook reminder etc.) unchanged.

### 5.5 Config (`hive/app/config.py`)

```python
@dataclass(frozen=True)
class AutonomyConfig:
    enabled: bool = True            # False ⇒ capture refused; promotion/decay inert; byte-stable today
    demand_m: int = 3               # misses required in window
    demand_window_days: int = 14
    demand_tau: float = 0.75        # miss ↔ candidate cosine floor
    competitor_tau: float = 0.85    # candidate ↔ servable cosine ⇒ demand already answered
    quarantine_ttl_days: int = 14
    provisional_ttl_days: int = 45
    # __post_init__: 0 < taus <= 1; demand_m/window/ttls >= 1; demand_tau < competitor_tau NOT
    # required (independent axes) but both validated finite.
```

`_GROUP_TYPES += {"autonomy": AutonomyConfig}`; `RELOAD_TIER`: all autonomy fields tier `"B"`
(hot) except `enabled` tier `"C"` (flips tool behavior + trigger wiring → restart). Container
threads the group into `LifecycleService`, `scan_servable`, and the capture handler.

---

## 6. Dependencies & exclusions

**New dependencies: none** (stdlib + existing numpy). **The server still never**: calls an LLM,
reads a repo, runs a scheduler, or waits on a human. The only new compute on the hot path is
one `index.search(vec, 1)` per capture and an `O(Q·d)` scan per miss — both bounded by decay.

---

## 7. Implementation chunks (TDD; each green + RULE-2'd before the next)

> Mutation hygiene (standing): restore via Edit with unique anchors, never sed; run mutation
> suites under `timeout`, foreground; clear `__pycache__` after same-size restores.

**C1 — Schema + carriers** *(foundation)*
Files: `hive/adapters/store_sqlite.py` (DDL + `_ensure_column` + backfill),
`hive/domain/models.py` (Episode invariants v2; `RecallHit` +`trust`+`ts`),
`hive/domain/lifecycle.py` (constants + `is_servable` + `decayed` only),
`hive/app/container.py` (`_REQUIRED_TABLES`).
| Test (`tests/store/test_schema_v2.py`, `tests/domain/test_lifecycle.py`) | Assertion |
|---|---|
| `test_new_columns_and_tables_present` | all §3 columns/tables on a fresh store. |
| `test_ensure_column_idempotent` | double-construction adds nothing twice. |
| `test_backfill_marks_human_approved_established` | v1 rows (approved+approved_by) → `established` with `last_active_ts` stamped; a pending row stays `quarantined`. |
| `test_backfill_runs_once` ★ | after backfill, demote a row, reconstruct the store ⇒ NOT flipped back (schema_version gate). (AC9) |
| `test_episode_invariants_v2` | bad trust raises; `approved_by` with pending raises; established-with-pending raises; `superseded_by` on a live row raises; auto row (`approved_by=None, approved, quarantined`) constructs. |
| `test_recall_hit_carries_trust_and_ts` | carrier fields present (wiring in C5). |
| `test_is_servable_truth_table` | the §2 predicate exhaustively: each state × fresh/lapsed × status. |
| `test_decayed_boundaries` | quarantine/provisional TTL edges; established never; promotion-stamp freshness (a just-promoted row is alive). |
RULE-2: skip the schema_version gate → `test_backfill_runs_once` red. Flip the trust column
default to `'established'` → pending-row arm red. Invert a `decayed` comparison → boundary red.

**C2 — Store lifecycle methods** *(after C1)*
Files: `hive/adapters/store_sqlite.py`, `hive/domain/ports.py` (ExposureLedger).
| Test (`tests/store/test_lifecycle_store.py`) | Assertion |
|---|---|
| `test_complete_quarantined_is_unindexed` ★ | `complete(trust='quarantined')` ⇒ absent from index AND `scan_servable`. (AC1) |
| `test_set_trust_promote_adds_demote_removes` ★ | promote ⇒ searchable; deprecate ⇒ vector gone from search. |
| `test_scan_servable_predicate_single_source` | established always; provisional iff fresh; quarantined/deprecated never; `scan_approved` alias delegates. |
| `test_rebuild_indexes_exactly_servable_set` | boot rebuild over mixed states/freshness. |
| `test_exposure_bumps_last_active_same_tx` | served provisional row's liveness refreshed atomically. |
| `test_record_miss_types_and_secret_handling` | abstained/no_match rows with vectors; refused ⇒ empty text + NULL vector; redacted ⇒ masked text + redacted-text vector (bytes differ from the raw-query vector). |
| `test_supersede_atomic_idempotent_self_refused` ★ | one tx: deprecated + stamped + de-indexed + ONE audit row; re-run no dup; `supersede(X,X)` refused. (AC5) |
| `test_terminal_successor_walks_bounded` | A→B→C ⇒ (C, C.hash); forced-cycle fixture (raw SQL) terminates. |
| `test_sweep_decays_and_deindexes` | lapsed quarantined → deprecated + `ttl_expired` rows; lapsed provisional also de-indexed; idempotent. |
| `test_quarantined_candidates_excludes_dead` | TTL-lapsed rows absent from the promotion scan. (feeds AC4) |
RULE-2: `set_trust` skips index remove on demote → ★ red. `scan_servable` ignores trust →
predicate red (the never-hallucinate analog). Sweep skips the de-index → sweep test red.

**C3 — Pure `DemandRule` + `LifecycleService`** *(after C1; store-independent via fakes — parallel-safe with C2)*
Files: `hive/domain/lifecycle.py` (complete), `tests/domain/test_demand_rule.py`.
| Test | Assertion |
|---|---|
| `test_promotes_at_m_distinct_demand` | m matched misses incl. ≥1 non-writer identity, no competitor ⇒ promote. (AC2) |
| `test_writer_only_demand_never_promotes` ★ | m misses ALL from the writer ⇒ no promote, reason='self_demand'. (AC3) |
| `test_competitor_vetoes` ★ | `competitor_top_sim ≥ competitor_tau` ⇒ no promote (near-dup pile-up prevention). |
| `test_tau_and_window_boundaries` | cosine just-below `demand_tau` excluded; m−1 insufficient. |
| `test_nonfinite_fails_closed` | NaN in candidate/miss vector ⇒ promote=False (no raise). |
| `test_on_miss_scans_only_live_quarantine` | decayed candidates never evaluated. |
| `test_decision_payload_is_auditable` | PromotionDecision fields round-trip into the audit payload JSON. |
RULE-2: drop the distinct-identity clause → ★ red (the anti-gaming mutation, the headline).
Invert the competitor comparison → ★ red. `demand_tau` applied as `<=` on the wrong side →
boundary red.

**C4 — Write paths** *(after C2+C3)*
Files: `hive/domain/admission.py`, `tests/domain/test_admission.py` (extend).
| Test | Assertion |
|---|---|
| `test_capture_lands_quarantined_unserved` ★ | result `quarantined`; `approved_by IS NULL`; invisible to `scan_servable`. (AC1) |
| `test_capture_secret_floor_unmoved` | planted credential ⇒ `SecretRefused`, 0 rows. |
| `test_capture_triggers_promotion_check_and_sweep` | a capture matching staged demand promotes (via injected fakes); sweep invoked once per capture. |
| `test_capture_disabled_refuses_cleanly` | `enabled=False` ⇒ `{status:'disabled'}`, store untouched. (AC8) |
| `test_write_unchanged_lands_established` | byte-compat with today + `trust='established'`. |
| `test_write_replaces_supersedes_atomically` ★ | per §4.4 guards (unknown-target fails whole call; self-supersede refused; idempotent). (AC5) |
RULE-2: make capture land `trust='established'` → ★ red (the quarantine-breach mutation).
Give capture a retirement side effect (wire it to `supersede`) → AC5 test red. Skip the
`enabled` guard → disabled test red.

**C5 — Read path + tools + wiring** *(integration; after C2/C3/C4)*
Files: `hive/domain/recall.py`, `hive/app/{tool_defs,mcp_server,gaps,onboard,config,container}.py`,
`tests/mcp/test_tools_v2.py`, `tests/acceptance/test_lifecycle_acceptance.py`.
| Test | Assertion |
|---|---|
| `test_tool_list_is_exactly_6` | the pinned name set (no `hive_evidence`). |
| `test_exposure_recorded_on_hit_not_on_abstain` | the old exposure pin restored + agent_id present. |
| `test_miss_recorded_on_abstain_and_empty` | TODO-1 contract + the on_miss trigger fires after. |
| `test_recall_hits_carry_trust_and_ts` | label wiring end-to-end. (AC6) |
| `test_recall_belt_drops_lapsed_provisional` ★ | a TTL-lapsed provisional row still in the warm index is belt-dropped (AC4's authoritative layer). |
| `test_fetch_annotates_terminal_successor` | chain fixture ⇒ `{episode_id, content_hash}` of C. (AC5) |
| `test_health_trust_counts_misses_gaps` | counts + `include_gaps` clustering shape. (AC7) |
| `test_manifest_v2_wording_and_outdated_hint` | capture-without-asking directive; v1 link ⇒ hint. |
| `test_autonomy_config_validation_and_tiers` | bounds; RELOAD_TIER rows (`enabled`=C, rest B). |
| `test_acceptance_demand_promotion_e2e` ★ | full container: 3 misses from 2 identities → `hive_capture` of the answer → next recall serves it labeled `provisional`; writer-only-demand variant does NOT serve. (AC2+AC3 end-to-end) |
| `test_acceptance_disabled_byte_stable` ★ | `enabled=False`: capture refused; recall output byte-identical to today's build on the same store; whole pre-existing suite green. (AC8) |
| `test_boot_sweep_runs_before_index_build` | container boot order: migrate → sweep → index. |
RULE-2: drop `record_miss` → miss test red. Belt keeps the bare `status` check (drop
`is_servable`) → ★ lapsed-provisional red. Promotion ignores identity diversity → e2e ★ red.
Drop the fetch annotation → red. Drop `ts`/`trust` from the hit envelope → label test red.

**C6 — Docs** *(no code)*
`02-CONTRACTS.md` (DDL delta, 6-tool schemas, ExposureLedger port, the lifecycle state machine
+ §2 predicate), `06-DESIGN-DOC.md` (decision-log row: v1 evidence economy → v2 mechanical),
`TODOS.md` (1/3 LANDED-BY; 2 OBSOLETE; 4 RECAST), `HOOK-RELOCATION-PLAN.md` (superseded-by
banner for the capture directive), README, this file → LANDED. Then `graphify update .`.

**Order safety.** C1 is additive with fail-safe defaults (suite green). C2 and C3 are mutually
independent behind C1 (parallelizable). C4 composes them behind admission's existing surface.
C5 is the only chunk that changes observable tool behavior, and its two ★ acceptance tests
(demand e2e + disabled-byte-stable) are written first as the standing regression sentinels.

## 8. Files touched (complete)

- **New:** `hive/domain/lifecycle.py` · `hive/app/gaps.py` · tests as listed.
- **Modified:** `hive/domain/{models,ports,admission,recall}.py` ·
  `hive/adapters/store_sqlite.py` · `hive/app/{tool_defs,mcp_server,onboard,config,container}.py`.
- **Untouched (deliberately):** surfacer/attribution/utility stores (dormant), secret_scan,
  embedding/index adapters, http_server/auth/rate_limit, entrypoint internals beyond the boot
  sweep call, Dockerfile/compose.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Demand-promoted content is unverified (may be wrong) | adopted knowingly (§1): trust label on every hit + `ts` ordering + cheap human supersession + decay; verification is Appendix-A's first layer. |
| A buggy agent floods captures | quarantine is unserved + TTL'd; 413/429 belts (landed) bound the rate; `trust_counts` keeps pile-up visible. |
| Self-demand gaming | structural: distinct-identity clause (AC3) keyed on token identity the client cannot assert. |
| Near-dup promotion pile-up | competitor veto (`competitor_tau`) against the servable index. |
| Warm index staleness vs lazy decay | belt re-check (`is_servable` per hit) is authoritative; sweep + [B3] rebuild recover the cache. |
| Forgotten write-site grants trust | column default `quarantined` (fail-safe); only `write()` and `set_trust` reach servable states. |

---

## Appendix A — Deferred layers (the v1 cut, with add-back paths)

| Layer (v1 §) | What it adds | Add-back path |
|---|---|---|
| `hive_evidence` + client-reported kinds (anchor/corroboration/usage/contradiction) | verified correctness signals | widen `evidence_events` kinds + the tool; promotion rules consume new tally fields |
| Derivations + independence accounting | echo-proof corroboration | `derivations` table + exposure-set snapshot at write; only needed once client corroboration can promote |
| `kind` + `anchors` + verify-on-read | mechanical truth-checking of code facts | columns + the **repo-mount decision** (read-only volumes; reverses a v3 lock — decide then, moot now) |
| Tiers + shadow keystone + `trustctl` | human-audited autonomy ramp | the v1 §4.2 policy table + §8/§9 verbatim; trustctl as optional ops |
| Survival-establish (provisional → established by exposure across identities) | a second mechanical rung | one more `DemandRule`-style pure rule over exposure data C5 already records |
| Librarian / distillation (tier 3) | consolidation of incident clusters | client agent + `kind='distillation'` + a deterministic consolidation-candidate report (cluster stored episodes, not just misses) |
| Capture-side `replaces` claims + parity rule | autonomous correction | claim payload convention + reverse-claim index, keystone-gated |
| Session threading (`session_id`) | finer independence than token+time | optional args, refines what token identity already provides |

---

**Gate:** awaiting approval to start at **C1**. One chunk at a time; each lands with its diff,
green suite, and RULE-2 report before the next begins.
