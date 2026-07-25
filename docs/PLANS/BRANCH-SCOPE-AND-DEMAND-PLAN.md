# BRANCH-SCOPE-AND-DEMAND — plan

Closes BUG-063, BUG-064, BUG-065, BUG-066 and promotes the audit's end-to-end scenarios
into the permanent frozen contract suite.

Status: **awaiting human confirmation. No code until confirmed.**

---

## 0. Operator rulings already made (do not re-litigate)

| Ruling | Decision |
|---|---|
| BUG-066 | THEORY §9 #6 wins — the demand count subtracts self-identity (`n_other >= demand_m`) — **and** the default `demand_m` rebaselines `3 → 1`. (Amended mid-build, superseding this table's original `3 → 2`: once self-identity is subtracted, any count above 1 is a sensitivity dial, not an anti-gaming property. One identity outside the capturing session is demand.) |
| BUG-064 | Build the writer. `branch_scoped` becomes reachable on the served path. |
| Product | Tagging a memory with its **repo/branch + code anchor is required behavior wherever possible**. |
| Product | `hive_write` is the preferred verb — for a lesson that clearly saves a future agent from repeating a mistake, causing a regression, or relearning a hard-won insight that led to landed code. `hive_capture` is for the ambiguous or partly-ambiguous tail only. |

---

## 1. What the investigation changed about the fix shape

The BUG-064 entry says the daemon "never passes `--branches`." That is true but it is not
the first break. Three facts, each verified by code read:

1. `hive/app/anchors.py:71` — `normalize_anchors` constructs every `AnchorRef` with
   `fp_meta=""`: *"the SERVER mints fingerprints — an agent can never send one."*
2. `hive/app/sync.py:662` — the server's own `hive-edge mint` spawn never passes
   `--branch-scope`, so it can never produce a `git/branches` carrier.
3. `hive/app/mcp_server.py:669,762` — `repos=[name for name, _b in scope]`: the write path
   **discards the branch** an agent already declared in `repos=["alpha@feature"]`.

So nothing server-side records which line a memory belongs to. The fix is therefore **not**
"pass a flag" and **not** "widen `anchors` to accept agent-supplied `git/branches` meta"
(which would let a caller assert a tag that outranks `anchor_changed` and blocks the
retirement gate — a THEORY §9 #6 violation). The fix is: **stop discarding the branch the
agent already declares, and make every consumer judge at the line the memory names.**

Consequences that fall out of that choice:

- `fp_meta` stays server-minted-only. The "an agent can never send one" invariant remains
  literally true, so a forged `combdrift/fp` stays unconstructable.
- No new write-door grammar. `repos=["name@branch"]` is already advertised, already
  validated by `normalize_repos`, already parsed by `split_scope`. One grammar, both
  directions.
- `branch_scoped` is computed at **read** time, not materialization time (§3.C below) —
  which is the only shape that works, because the `anchor_drift` PK is
  `(repo, tip_sha, anchor)` and two episodes on the same anchor may declare different
  lines. A per-episode verdict cannot fit a per-tree cache key.
- The retirement-gate immunity hole closes properly: the gate judges a branch-scoped memory
  **at its own line's tip** (via BUG-063's new `ref_tips`), so an anchor that died on
  `feature` still qualifies. BUG-063's fix is what makes BUG-064's fix safe.

---

## 2. Intents

| # | Intent |
|---|---|
| **I1** | A recall scoped to `name@branch` serves the drift verdict the daemon actually computed at that branch's tip, instead of `unverifiable` forever. (BUG-063) |
| **I2** | A memory declares its line at write time, that line is durable, and every consumer — recall drift, the retirement gate, the materializer — judges the memory at the line it named. (BUG-064) |
| **I3** | Every drift verdict advertised in `tool_defs` is emittable by a production writer in `hive/`, enforced mechanically. (BUG-064, extending the BUG-059 seam) |
| **I4** | A retired memory stops consuming the sync daemon's capped per-tick verify and mint budgets, and its stale cache rows cannot be read by a later memory on the same anchor. (BUG-065) |
| **I5** | The demand-promotion gate and THEORY §9 #6 state the same anti-gaming rule: the writer's own misses contribute nothing to the demand count. (BUG-066) |
| **I6** | The served contract directs agents to tag repo/branch + anchor wherever the memory is about real code, and to prefer `hive_write` over `hive_capture` per the ruling above. |
| **I7** | The audit's end-to-end scenarios become permanent contract tests that drive real writers end to end — nothing seeded. |

---

## 3. Design

### 3.A — Two new tables (no migration; `CREATE TABLE IF NOT EXISTS` on a live v3 store)

The v3 store has **no in-place migration path** (`store_sqlite.py:150-154`); a new *column*
would force `hive reset` on the live dogfood volume. A new *table* costs nothing — the boot
guard only inspects `episodes` columns and `episode_anchors` presence, then runs
`executescript(_SCHEMA)`. Both facts are load-bearing for this plan.

```sql
-- the memory's DECLARED line: one ref per (episode, repo), matching normalize_repos'
-- "one scope entry per repo" rule. '' is never stored (canonical = no row).
CREATE TABLE IF NOT EXISTS episode_refs(
  episode_id INTEGER NOT NULL, repo TEXT NOT NULL, ref TEXT NOT NULL,
  PRIMARY KEY(episode_id, repo));

-- the materializer's per-ref watermark: the tip a non-canonical ref was last
-- materialized against. The branch twin of `sync:<repo>:last_tip`.
CREATE TABLE IF NOT EXISTS ref_tips(
  repo TEXT NOT NULL, ref TEXT NOT NULL, tip_sha TEXT NOT NULL, ts INTEGER NOT NULL,
  PRIMARY KEY(repo, ref));
```

**Why `ref_tips` is a table and not a `sync_keys` meta key.** A meta key
(`sync:<repo>:ref:<ref>:tip`) is more uniform for the reader but has no pruning verb —
one key per demanded ref, forever, which is the leak class of BUG-060 — and a 5-part key
strains the 3-part per-repo grammar `census_health` groups on. A table prunes with a real
verb and is greppable.

**Why the canonical tip stays in meta.** `sync:<repo>:last_tip` is *also* the ledger ingest
watermark and *also* a served health field. Folding it into `ref_tips` would conflate three
roles. The asymmetry is real, so it gets exactly one owner (§3.B).

### 3.B — One tip resolver, one owner

`hive/app/drift.py` gains the single answer to "which tip do I judge repo R at":

```python
def _tip_for(store: object, repo: str, ref: str) -> str | None:
    """The tip to judge ``repo`` at: ``ref_tips(repo, ref)`` for a non-canonical
    ref, else the canonical watermark ``sync:<repo>:last_tip``. None = unknown tip
    (the fail-safe: the caller reads unverifiable, never false-fresh)."""
```

`attach_drift` stops branching on canonical-vs-branch in its body; it calls `_tip_for`. The
`tips[repo] = None; continue` at `drift.py:205-209` — the whole of BUG-063 — is deleted.

`detail.ref` now rides whenever a repo was branch-routed, resolved or not (today it appears
only on the unresolved path). Strictly more honest; existing assertions still hold.

### 3.C — `branch_scoped` at read time

New pure function beside `wire_verdict`, keeping ONE owner of the wire vocabulary:

```python
STALE_TIER = frozenset({DRIFT_ANCHOR_MISSING, DRIFT_ANCHOR_CHANGED,
                        DRIFT_BLAST_RADIUS_CHANGED})

def branch_route_verdict(base: str, *, declared_ref: str, consumer_ref: str) -> str:
    """Route a materialized verdict through the memory's DECLARED line.

    A memory that names its line is not stale to a consumer on another line — it is
    scoped elsewhere. When both refs are known, differ, and ``base`` is stale-tier,
    the served verdict is the advisory ``branch_scoped``. Every other combination
    (no declared ref, same line, unknown consumer, ``fresh``/``unverifiable`` base)
    returns ``base`` verbatim. Pure, total, and NEVER upgrades to ``fresh`` — the
    most-advisory verdict this can emit is ``branch_scoped``. // O(1)."""
```

Applied per anchor in `_drift_for_hit`, before `aggregate_verdicts`.

**Deliberate divergence from `hive-edge verify`, pinned in the parity test.** The engine
suppresses the radius tier for an off-set consumer and returns `current`; the server returns
`branch_scoped` for an off-set `blast_radius_changed`. The server is strictly more
conservative (never upgrades to fresh) and strictly more informative ("about another line"
beats silence). Record it as a known, justified difference in
`tests/contract/test_engine_parity.py` rather than paying the engine's graph load for
off-set consumers.

### 3.D — The retirement gate judges the memory's own line

`mcp_server._gate_drift_verdicts` resolves the tip per anchor repo as
`ref_tips(repo, declared_ref)` when the episode declares a ref for that repo, else the
canonical watermark. `branch_route_verdict` is **not** applied here: consumer == declared,
so `branch_scoped` can never arise, and a dead anchor on the memory's own line still
qualifies for retirement. This is what keeps the declared ref from becoming caller-asserted
immunity.

### 3.E — The materializer covers declared lines, not just demanded ones

`_materialize_drift`'s tip list becomes, in this order:

1. the canonical tip (first — the served-most line never starves under the budget cap),
2. refs **declared** by live episodes of this repo (`store.declared_refs(repo)`),
3. refs **demanded** by recall (`ref_requests` inside the 7-day window),

deduped by resolved SHA. Each resolved `(ref, sha)` is written to `ref_tips` **before**
materializing, so a budget-starved tick reads "tip known, verdicts absent" ⇒ `unverifiable`
— never a `fresh` inherited from an older tip.

### 3.F — The work list drops retired memories, in ONE place

`sync.py:_repo_fps`'s raw SQL and `store.anchors_lacking_fp` are two near-identical
`episode_anchors ⋈ episodes` joins. BUG-065 is present in **both** legs; fixing one is how
it recurs. The daemon's raw read moves into the store as a sibling verb, and the
not-retired predicate is written once per verb, next to each other:

```python
def anchor_carriers(self, repo: str) -> list[tuple[str, str]]:
    """(anchor, fp_meta) for every NON-RETIRED approved anchor binding of ``repo``,
    in (episode_id, anchor) order — the drift materializer's work list. The carrier
    body is returned RAW (the store never parses it — meta-envelope law); the caller
    owns first-wins-per-anchor. // O(rows)."""
```

Both verbs gain `AND e.trust != 'deprecated'`. Quarantined and provisional rows stay in —
a quarantined memory can still be promoted and needs its fingerprints.

**The false-fresh path this opens, closed in the same change.** Once an anchor leaves the
work list, its cached rows at live tips survive; a *new* episode binding that same anchor
would read a verdict computed against the retired episode's fingerprint. `drift_prune` gains
`keep_anchors`:

```python
def drift_prune(self, repo: str, keep_tips: Sequence[str],
                keep_anchors: Sequence[str] | None = None) -> int:
```

`None` preserves today's behavior exactly; the daemon always passes the current work set.

### 3.G — The demand rule

`hive/domain/lifecycle.py:DemandRule.decide`:

```python
n_other = sum(1 for m in matched if m.agent_id != candidate_writer)
if n_other < self.demand_m:
    # the writer's own misses are not demand (THEORY §9 #6). Both diagnoses stay
    # live: no other identity at all vs. not enough of them.
    reason = "self_demand" if (matched and n_other == 0) else "insufficient_demand"
    return PromotionDecision(False, len(matched), n_other, comp, reason)
```

`PromotionDecision.n_misses` keeps reporting total matched (the audit record stays complete).
`config.py:105` `demand_m: int = 3` → `1`.

Net effect with defaults: promotion needs **1 non-writer miss** in the 14-day window. Today
the gate is `len(matched) >= 3` with a separate `n_other >= 1` floor, which lets the writer
supply 2 of the 3 for free — a quarantined memory is unservable, so the writer's own
follow-up recalls necessarily miss. Collapsing to `n_other >= 1` removes that subsidy and
states the rule the floor was always approximating: demand is what someone *else* wants.

Why 1 and not a higher count. The quarantine TTL and the demand bar interact, and only the
demand rule was ever tuned. A quarantined row is unservable, so `record_exposure` never
fires for it and `record_miss` writes a different table — **misses do not refresh a
quarantined memory's liveness clock** (`lifecycle.py:decayed`, `quarantined` branch). A
capture therefore has a hard 14 days from creation to clear the bar, and the misses it did
attract buy it nothing. At a bar of 3 most captures die unpromoted while being actively
sought. At 1, a single foreign hit inside the window moves the row onto the 45-day
exposure-refreshed clock, where continued use keeps it alive indefinitely. Quality work is
done downstream by outcome-gating and drift, not by the demand count.

### 3.H — Served contract

`hive/app/contract.py:WRITE_VS_CAPTURE` is the single source composed verbatim into
`SERVER_INSTRUCTIONS` and both tool descriptions. It carries the ruling:

- prefer `hive_write` — a lesson that saves a future agent from repeating a mistake, causing
  a regression, or relearning what led to landed code;
- `hive_capture` for the ambiguous / partly-ambiguous tail;
- tag the line: `repos=['name@branch']` + `anchors=[{repo, anchor}]` wherever the memory is
  about real code.

**Hard budget:** `METADATA_FIELD_LIMIT = 2048` per served metadata field, asserted by
existing tests. The string grows in three places at once. If `SERVER_INSTRUCTIONS` goes over,
trim there — never raise the limit. This is advisory guidance, **not** a new refusal path: a
memory with no anchors and no scope stays legal (general memories exist by design).

`tool_defs.py:146` keeps `branch_scoped` in the advertised enum — it now has a writer.

---

## 4. Contract tests — the frozen set

**Location:** `tests/contract/` (the repo's existing convention; `conftest.py` already
re-exports `Origin`, `git`, `make_rig`, `make_syncer`, `recall`, `register_repo`).

**`frozen_paths`: `tests/contract/**`**

Authored **first**, observed **red**, then frozen. The eight audit scenarios in
`/tmp/claude-1000/-home-null-Desktop-work-hivemind/4c631536-.../scratchpad/audit/` are the
source — copy them in rather than re-deriving. Four assertions were written to *prove* a bug
and must be **inverted** to assert correct behavior (that is what makes them red-first;
no `xfail` markers anywhere — a fix must turn a test green, never delete a marker).

| Source | Lands as | Inverted? |
|---|---|---|
| `test_s1_drift_e2e.py` | `test_drift_end_to_end.py` | `test_branch_route_degrades_and_records_ref_demand` splits: un-materialized route still degrades + records demand (unchanged); a second test asserts the verdict becomes readable after the tick — **red** |
| `test_s2_outcome_reinforcement_e2e.py` | `test_outcome_reinforcement_e2e.py` | no |
| `test_s3_demand_promotion_e2e.py` | `test_demand_promotion_e2e.py` | `test_theory_says_subtract_self_identity…` → `test_writer_misses_do_not_count_toward_the_demand_bar` — **red** |
| `test_s4_verified_promotion_e2e.py` | `test_established_rung_e2e.py` | no |
| `test_s5_retirement_gate_e2e.py` | `test_retirement_gate_e2e.py` | no; **add** `test_a_branch_scoped_memory_is_judged_at_its_own_line` — **red** |
| `test_s6_verdict_writer_coverage.py` | `test_verdict_writer_coverage.py` | `test_branch_scoped_has_no_production_writer…` → `test_every_advertised_drift_verdict_has_a_production_writer` — **red** |
| `test_s7_ttl_and_health_e2e.py` | `test_ttl_and_health_e2e.py` | no |
| `test_s8_branch_drift_writeonly.py` | `test_branch_scope_e2e.py` | both invert: `test_branch_tip_verdict_reaches_the_agent_that_demanded_it` and `test_a_pruned_anchor_leaves_the_verify_work_list` — **red** |

Every promoted module keeps the audit's defining invariant in its docstring: **nothing is
seeded — every fact is manufactured by production code** (real git origin, real
`hive-edge mint`/`verify` subprocesses, real MCP handler, real sync tick).

### Traceability

| Intent | Contract (given / when / then) | Scenarios covered | Contract test(s) |
|---|---|---|---|
| I1 | **Given** a memory anchored in `alpha` and a branch `feature` whose tip the daemon has materialized, **when** an agent recalls with `repos=["alpha@feature"]`, **then** the hit's `drift.type` is the verdict stored for that branch tip and `detail.per_anchor[].tip_sha` is that tip | materialized branch tip; un-materialized branch tip (⇒ `unverifiable`, tip known); deleted branch (⇒ `unverifiable`); canonical route unchanged; budget-starved tick; multi-anchor across two repos with different routes | `test_branch_scope_e2e.py::test_branch_tip_verdict_reaches_the_agent_that_demanded_it`, `::test_unmaterialized_branch_tip_is_unverifiable_not_fresh`, `test_drift_end_to_end.py::test_branch_route_records_demand_then_serves_the_verdict` |
| I2 | **Given** `hive_write(repos=["alpha@feature"], anchors=[…])`, **when** the anchor goes stale on `main`, **then** a `main` consumer reads `branch_scoped`, a `feature` consumer reads `anchor_changed`, and the retirement gate qualifies only on `feature`'s own tip | declared==consumer; declared≠consumer + stale; declared≠consumer + fresh (⇒ fresh); no declared ref (⇒ base verbatim); radius-changed off-set (⇒ `branch_scoped`, the pinned divergence); dedup re-write keeps the first declaration; scope-only (no anchors ⇒ `n/a`) | `test_branch_scope_e2e.py::test_declared_line_downgrades_off_line_staleness`, `::test_same_line_consumer_still_sees_the_stale_verdict`, `test_retirement_gate_e2e.py::test_a_branch_scoped_memory_is_judged_at_its_own_line` |
| I3 | **Given** the advertised drift vocabulary in `tool_defs`, **when** the suite runs, **then** every member is emitted by a real writer driven end to end (or by `wire_verdict` from real engine output) | all 7 members incl. `n/a` and `unverifiable` | `test_verdict_writer_coverage.py::test_every_advertised_drift_verdict_has_a_production_writer` |
| I4 | **Given** an anchored memory that is then `hive_prune`d, **when** the next tick runs at a new tip, **then** its anchor is absent from the work list, no verify spawn is made for it, and its cached rows are pruned | prune; supersede; TTL decay; anchor shared with a live memory (⇒ stays); all-retired repo (⇒ empty work set); the mint-backfill leg's twin | `test_branch_scope_e2e.py::test_a_pruned_anchor_leaves_the_verify_work_list`, `::test_a_shared_anchor_survives_one_memorys_retirement`, `::test_retired_anchors_leave_the_mint_backfill_sweep` |
| I5 | **Given** the default `demand_m=1` and a quarantined memory, **when** the window holds only the writer's own misses, **then** it does not promote; at 1 non-writer miss it promotes and then serves. The ladder itself is exercised at a pinned `demand_m=2` so the *shape* (`n_other >= demand_m`) is proven independently of the shipped default | 0 other + N self (⇒ `self_demand`); 1 other; pinned m=2 with 1 other (⇒ `insufficient_demand`); out-of-partition other (⇒ no); non-finite | `test_demand_promotion_e2e.py::test_writer_misses_do_not_count_toward_the_demand_bar`, `::test_two_other_identities_promote_and_the_row_then_serves`, `test_demand_repo_scoped.py` (existing, updated) |
| I6 | **Given** a fresh MCP `initialize`, **when** the instructions and tool descriptions are read, **then** each is ≤ 2048 bytes and states the write-vs-capture preference and the repo/branch+anchor tagging directive | `initialize.instructions`; `hive_write`; `hive_capture`; every other tool description | `test_served_contract.py` (existing, extended) |
| I7 | **Given** the promoted suite, **when** it runs against the fixed system, **then** every scenario passes with nothing seeded | all eight modules | the whole of `tests/contract/*_e2e.py` |

### External-interaction inventory

| Boundary | Failure behavior | Observable outcome | Failure-path test |
|---|---|---|---|
| `git` subprocess (clone/fetch/rev-parse/worktree) | fail-open per repo per leg; fault → `sync:<name>:last_error`, tick survives | other repos unaffected; serve path untouched | existing `tests/sync/test_contract_mirror.py::test_unreachable_fail_open` |
| `hive-edge verify` / `mint` subprocess | nonzero / unparseable ⇒ `unverifiable` + skip | never false-stale, never false-fresh | existing `tests/contract/test_drift_materializer.py`; extended for the new tip list |
| `ref_tips` read (recall path) | fail-open — any fault degrades that hit to `unverifiable` | read never breaks | **new** `test_branch_scope_e2e.py::test_a_faulting_ref_tip_read_degrades_the_hit_not_the_read` |
| `ref_tips` / `episode_refs` write | inside the existing per-leg / per-tx guards | a failed write means "tip unknown" ⇒ `unverifiable` | covered by the budget-starved and fault cases above |
| `episode_refs` read (retirement gate) | gate is **fail-closed**: any feed fault ⇒ ineligible ⇒ noop | a broken feed can refuse a retirement, never allow one | existing `tests/mcp/test_retirement_gate_boundary.py`, extended |
| sqlite store | unchanged | — | existing |
| env vars | **no new required env var.** `HIVE_SYNC__TOKEN` / row-named token vars keep `_resolve_token`'s fail-fast (`KeyError` names the var, surfaced per-repo). `HIVE_SYNC__DRIFT_PER_TICK` / `BACKFILL_PER_TICK` / `WORKERS` unchanged. `HIVE_AUTONOMY__DEMAND_M` default moves 3→1 | — | `tests/config/test_config.py` (the real home for a `demand_m` default assertion — `tests/sync/test_contract_config.py` covers `SyncConfig` and holds no `demand_m` reference) |

---

## 5. Implementation order

Each step is safe because the one before it is inert or independent.

**Step 1 — author the frozen contract suite (red).**
Copy the eight scenarios into `tests/contract/`, invert the four bug-proving assertions,
add the new I2/I4 scenarios. Run and **capture the red output as evidence** — a suite that
was never red proves nothing when it goes green. No `hive/` file is touched in this step.

**Step 2 — store layer (inert additions).** `hive/adapters/store_sqlite.py`
- `_SCHEMA` += `episode_refs`, `ref_tips`.
- `stage(..., repos: Sequence[tuple[str, str]] = ())` — accept `(name, branch)` pairs;
  write an `episode_refs` row per non-empty branch. Dedup-by-content-hash keeps returning
  early untouched (identity is the text hash alone — document that a re-write does not
  restate the line).
- `episode_refs(episode_id) -> dict[str, str]`, `declared_refs(repo) -> list[str]`.
- `ref_tip(repo, ref) -> str | None`, `ref_tips_put(rows)`, `ref_tips_prune(repo, keep_refs)`.
- `anchor_carriers(repo)` (new, replaces the daemon's raw SQL) — both it and
  `anchors_lacking_fp` gain `AND e.trust != 'deprecated'`.
- `drift_prune(repo, keep_tips, keep_anchors=None)`.
- The `Episode` carrier projection (`store_sqlite.py:463-492`) gains the declared refs.
  **`Episode.repos` stays name-only** — `scope_matches` is name-keyed, and putting
  `alpha@feature` in that set would silently stop the memory matching an `alpha` recall.

**Step 3 — BUG-065 (independent; frees budget for everything after).** `hive/app/sync.py`
`_repo_fps` calls `store.anchor_carriers`, keeps the JSON parse and first-wins locally;
`_materialize_drift` passes `keep_anchors` to `drift_prune`; `_backfill` inherits the
predicate through `anchors_lacking_fp`. Turns green: I4's tests.

**Step 4 — BUG-063.** `hive/app/drift.py` `_tip_for` + delete the `tips[repo] = None`
guard; `detail.ref` on every branch route. `hive/app/sync.py` `_materialize_drift` writes
`ref_tips` for every resolved ref before materializing. Turns green: I1's tests.

**Step 5 — BUG-064.** `mcp_server.py:669,762` stop discarding the branch and thread pairs to
`admission.capture`/`write`; `hive/domain/admission.py` widens `repos` to the pair shape and
threads to `stage`; `hive/app/drift.py` gains `branch_route_verdict` + applies it per anchor;
`mcp_server._gate_drift_verdicts` resolves the declared-ref tip; `sync._materialize_drift`
adds `declared_refs` to the tip list; the served hit's `anchors` entries carry the declared
`ref`. Turns green: I2, I3.

**Step 6 — BUG-066 (independent).** `lifecycle.py` `DemandRule.decide` + docstring;
`config.py` `demand_m = 1`. Turns green: I5.

**Step 7 — contract + docs.** `contract.py:WRITE_VS_CAPTURE`; `tool_defs.py` write/capture
descriptions and the `hive_recall` drift line; measure every served field against
`METADATA_FIELD_LIMIT`. Turns green: I6.

**Step 8 — reconcile the instruction layer.**
- `CONTEXT/BUGS.md` — BUG-063/064/065/066 → `SOLVED` with Date solved + Solution.
- `CONTEXT/INTERACTIONS.md` — branch-scoped recall now serves real verdicts; the declared
  line is a new stored fact; the retirement gate's tip source changed; the demand bar moved.
  New/changed entries with `file:symbol` anchors and DETECT/MUTATE tags, in this change.
- `CONTEXT/THEORY.md` — §9 #6 needs no edit (the code moved to it). Record the
  `demand_m` default change wherever defaults are stated.
- `CHANGELOG.md`; `llms.txt` / `llms-full.txt` / `README.md` / `HIVE-ADMIN.md` /
  `OPERATIONS.md` wherever the drift vocabulary or the write guidance appears; then
  `/audit-docs --changed` over the touched set.
- `graphify update .`

**Step 9 — gates.** `make check` (format → lint → mypy --strict → full suite).
Then `/verify`: `/update-dogfood-server` and drive the real flow against the live server —
this change has a runtime surface (a live v3 store gaining two tables at boot, a real
branch-scoped recall, a real prune). Confirm the dogfood store gains `episode_refs` /
`ref_tips` **without** a reset.

### Existing tests that must move with the code (not frozen — these are pre-existing)

`tests/app/test_drift.py` (new `branch_route_verdict` table), `tests/contract/test_hit_drift.py`
(the `branch_scoped` case now drives the real declared-ref path), `tests/contract/test_drift_materializer.py:35`
(`WIRE_VERDICTS` set + the new tip list), `tests/contract/test_engine_parity.py` (the pinned
divergence), `tests/store/test_drift_cache_store.py` (`drift_prune` signature),
`tests/sync/test_contract_drift.py` + `test_contract_backfill.py` (work-list predicate),
`tests/domain/test_demand_rule.py` + `tests/contract/test_demand_repo_scoped.py` +
`tests/config/*` (demand rule + default), `tests/mcp/test_retirement_gate_boundary.py`
(declared-line tip), `tests/contract/test_served_contract.py` + `tests/app/test_contract.py`
(the text), `tests/app/test_sync_keys.py` (unchanged — no new `sync:` key is introduced,
deliberately).

---

## 6. Risks, named

| Risk | Mitigation |
|---|---|
| `SERVER_INSTRUCTIONS` exceeds 2048 once the tagging directive composes in | existing tests assert the fit and go red; trim `SERVER_INSTRUCTIONS`, never raise the limit |
| `demand_m` 3→1 moves any benchmark measured on the current floor | ruled by the operator; record the before/after bar in the CHANGELOG so a later benchmark reads against the right baseline |
| **`demand_m` 3→1 costs the thin-vote detector its resolution at the promotion boundary.** `detect_suspect_consensus` flags a promotion whose votes were correlated (`n_eff / k < n_eff_frac_max`, default 0.5, strict `<`). Promotion now fires at the first non-writer miss, so a row promoted at exactly the bar has `k = 1` and `n_eff / k = 1.0` — mathematically unflaggable. The detector still fires on rows that accumulate several correlated misses between sweeps, but it no longer covers the modal promotion | accepted, not silently. The detector is advisory-only by construction (`consensus.py`: *"Detection ONLY (THEORY §10 O7): it asserts nothing about truth, retires nothing"*) and gates nothing, so the loss is of an operator hint, not of a safety property. `n_eff_frac_max` is a separate config group and is deliberately **not** retuned here — retuning it would change detector sensitivity far beyond the promotion path, and the operator ruled only on the demand bar. Recorded in `CONTEXT/BUGS.md` under BUG-066 and in the CHANGELOG so a later tuning pass finds it |
| A pre-existing test that hardcodes the old bar reads as a regression | three do (`test_demand_promotion_e2e.py::test_fleet_demand_promotes_and_the_row_then_serves`, `test_outcome_reinforcement_e2e.py::test_self_reported_help_flips_the_martingale_warning_an_operator_reads`, `tests/mcp/test_solo_hint.py::test_health_solo_hint_fires_precisely`). They assert the superseded default, so they are updated to the new bar rather than counted as regressions. The first two are inside `frozen_paths`, so **only the contract-suite chunk may touch them** — the freeze holds |
| The tip list grows with declared + demanded refs | canonical is materialized **first**, so the served-most line never starves; `drift_per_tick` still bounds spawns; `ref_tips_prune` + `drift_prune(keep_tips)` bound storage |
| A dedup'd re-write cannot restate a memory's line | documented on `stage`; identity is the text hash alone, unchanged by this plan |
| Two new tables on the live dogfood store | `CREATE TABLE IF NOT EXISTS`; the boot guard inspects only `episodes`/`episode_anchors`. Verified explicitly in Step 9 — **no `hive reset`** |

## 7. Definition of done

`make check` green, the frozen contract suite green (having been observed red in Step 1),
`/verify` driven against the live dogfood server with no store reset, and Step 8's
instruction layer reconciled in the same change.

## 8. Build mode

Steps 2–5 are one dependent chain through `store → sync → drift → mcp_server` with a large
blast radius across ~20 test modules; Steps 1, 6, 7, 8 are separable. This is above a single
agent's comfortable context. Run `/build-dispatcher` on this file at build time; expect
`/daisy-chain-build` with `frozen_paths: tests/contract/**`.
