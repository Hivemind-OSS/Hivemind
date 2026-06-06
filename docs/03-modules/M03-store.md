# M03 EpisodeStore + ledgers  (store)

**One-line:** The single SQLite-WAL durable owner of episodes, the content-hash blob store, the three move-#6 ledgers, optimistic-CAS single-writer admission that drives the authoritative vector index inside one transaction, and the W_version reembed-from-text migration + one-time corpus import — exposing approved-only recall reads behind a narrow `EpisodeStore` port.
**Port disposition:** PORT+SIMPLIFY of storage/ (persistence.py, row_codec.py, blob_store.py) per spec §10: keep blobs + content_hash + CAS/`version` single-writer; DROP bi-temporal (t_valid/t_invalid/t_created/t_expired), supersession (superseded_by/supersedes/subject_key), tombstoned, schemas/audit/decisions/procedures/graph_* tables; ADD status/proposed_by/approved_by/approved_ts + the three ledgers (exposure/task_outcomes/utility). Migration is PORT+SIMPLIFY of ops/migration.py reembed_from_text (drop reembed_native/retrain d→d/ANN paths; keep the native→d PCA refit + in-flight resume sentinel + stranded disposition) and is also the one-time old→new corpus import (§12). Blob store is PORT as-is (blob_store.py SqliteBlobStore). row_codec is PORT+SIMPLIFY (collapse EPISODE_COLS to the v-min column set; delete EPISODE_COLS_ARCHIVE). The wide-but-conscious EpisodeStore god-port is the accepted ISP trade (DECISIONS DIGEST) to keep the §12 single-writer transaction one object; its four method-groups (episodes / blob / ledgers / migration) are pre-segregated so a future ledger extraction tears cleanly.

---

# M03 — EpisodeStore + ledgers + blob + migration (C5 durable half)

> Source-of-truth: HIVEMIND_VMIN_SPEC.md §2 (C5), §3 (data model), §4.1/4.3/4.6,
> §6.1 (#4 migration, #5b approved-only), §10 (Store=PORT+SIMPLIFY), §11, §12.
> Port reference: `storage/persistence.py`, `storage/row_codec.py`,
> `storage/blob_store.py`, `storage/vector_index.py`, `ops/migration.py`.

## 1. Responsibility (one deep module)

M03 is **the durable half of C5**: the single SQLite-WAL file that *owns* the
episode table, the content-hash blob store, the three move-#6 ledgers
(`exposure` / `task_outcomes` / `utility`), the `meta` kv (W_version etc.), and
the W_version reembed-from-text migration + one-time corpus import. Behind a
**narrow `EpisodeStore` port** it hides four pieces of meaningful, error-prone
work that the rest of the system must never reason about:

1. **The single-writer transaction.** Every mutation (stage, approve, reject,
   ledger append/settle/clawback, migration) goes through one `BEGIN IMMEDIATE`
   writer with bounded full-jitter backoff (ported `_begin_immediate_retrying`,
   persistence.py:90) so SQLITE_BUSY never surfaces as a dropped write. This is
   the §12 single-writer discipline made one object — the conscious reason the
   port is wide (DECISIONS DIGEST: wide-port ISP smell accepted to keep the
   transaction atomic).
2. **The approved-only recall boundary** (the product keystone, §6.1 #5b). The
   Store is the SOLE mutator of the injected authoritative `VectorIndex`: it
   calls `index.add` *inside the same transaction that flips `status→approved`*,
   so the table and the searchable set can never diverge (the locked C5 synthesis
   decision: Store-owns-and-drives-the-index, no two-writer drift window). A
   single module-private constant `_RECALL_PREDICATE = "status='approved'"` is the
   ONE definition of "recallable" — killing the spec §10 predicate-scatter
   (`tombstoned=0` was duplicated across four query sites in the reference).
3. **Content-addressed verbatim storage + dedup.** `content_hash = sha256(text)`
   binds text to blob; fetch resolves hash→text; dedup is a hash lookup.
4. **Geometry migration as a tracked round-trip.** A `geometry.W_version` bump
   re-embeds every approved row *from its blob text* through a fresh native→d PCA
   head, rewrites `value`, rebuilds the index from the store, and is crash-safe
   via the ported in-flight sentinel — never a silent mixed-version store. The
   same path is the one-time old→new corpus import (§12).

What is **deliberately NOT here** (PORT+SIMPLIFY drop, §10): bi-temporal
(`t_valid/t_invalid/t_created/t_expired`), supersession
(`superseded_by/supersedes/subject_key`), `tombstoned`, and the
`schemas/audit/decisions/procedures/graph_*` tables. Episodes are append-only;
`weight` is immutable post-capture; only `status` (once, on approval) and the
`utility` posterior mutate.

## 2. Public surface + ENFORCED contract

The port surface is in `interface_block`. Contract details and the
**precondition that must be DESIGNED OUT, not documented**:

- **Frozen, self-asserting `Episode`** (`@dataclass(frozen=True)`). Three
  invariants are enforced in `__post_init__` so an illegal episode is
  *unconstructable* (contracts that cannot lie, agent-native §6):
  - `value.dtype == float32 and value.ndim == 1` — unit-norm `float[d]`; every
    downstream (C3 ranker, C4 gate) rests on this.
  - `(status=='approved') == (approved_by is not None)` — an approved row without
    an approver, or a pending row carrying one, cannot be built. This is the
    state-machine invariant made a type, not prose.
  - `content_hash == sha256(text)` — **designed out**: the reference allowed a
    caller-supplied `content_hash` that could desync from `text` (fetch then
    returns the wrong verbatim). Here the hash is *derived in `stage()`*, never
    accepted from the caller, and the assertion is a backstop. This removes the
    precondition "caller must pass a correct hash."
- **`stage(...) -> int`** — INSERT `status='pending'`, `version=0`,
  `put_blob(text)`, derive `content_hash`, return the pending id. **POST: the
  value is NOT in the index** (test_stage_never_indexes). Units: `weight` is
  salience (immutable hereafter); `now`/`ts` epoch seconds; `value` unit-norm
  float32[d].
- **`approve(ids, approver, now) -> list[int]`** — per id, in ONE tx: optimistic
  CAS `UPDATE … SET status='approved', approved_by=?, approved_ts=?, version=version+1
  WHERE id=? AND version=? AND status='pending'`, THEN `index.add(id, value)` in
  the *same* tx. Idempotent on already-approved (returns it omitted). Reuses the
  proven `update_cas` rowcount idiom (persistence.py:581) rather than a bespoke
  CAS (locked decision: lower-risk reuse). Error: a losing race raises
  `CASConflictError` (or is reported as a non-flip), never a lost update.
- **`reject(ids, keep_rejected=False) -> int`** — default DELETEs the pending row
  (and its blob if orphaned); `keep_rejected=True` retains the row but it stays
  `status='pending'` ⇒ **index-absent ⇒ never recallable** (allowlist semantics:
  any non-`approved` status is non-recallable by default — the fail-safe
  direction, locked decision). Avoids a 3-state terminal cliff.
- **`scan_approved(tenant_id) -> Iterator[Episode]`** — the SOLE feed of the
  authoritative index and of recall; `WHERE {_RECALL_PREDICATE}`. Two independent
  fail-closed defenses for never-hallucinate: the SELECT predicate AND
  index-absence of pending rows.
- **`rebuild_index_from_store() -> int`** — `index := f(scan_approved)`; the index
  is a *derived cache*, making "rebuild on boot / after migration" a first-class
  recovery (closes the crash-between-commit-and-add window named in the C5
  decision).
- **Ledgers** — `exposure`/`task_outcomes`/`utility` writes share the single-writer
  tx. `update_utility` bumps `utility.version` and **never touches `episodes.weight`**
  (spec §3: utility is a separate versioned layer; guardrail-4). `zero_utility_layer`
  is the human rollback.
- **`Migrator.reembed_from_text(...) -> int`** — returns the new W_version; PRE:
  embedder exposes `embed_native_batch` (else `ReembedError`); POST: every
  approved row at the new W_version with re-projected `value`, `content_hash`
  unchanged, index rebuilt. Crash-safe via `meta['reembed_inflight']` + persisted
  frozen head (ported).

## 3. Swap seam

M03 **is** the C5 durable port and **sits in front of** the vector-index port:

- **Port:** `EpisodeStore` Protocol (interface_block). **Default adapter:**
  `SqliteEpisodeStore` (SQLite/WAL on a named volume). **Second adapter** (e.g.
  Postgres/pgvector, Qdrant-backed) must implement the same Protocol — the
  approved-only predicate, the CAS single-writer, the in-tx index drive, and the
  blob/ledger/meta surface. Because callers receive an `EpisodeStore` and a
  `VectorIndex` only by injection and have **no index-mutation verb**, swapping
  external storage is an adapter, not a core refactor (SWAPPABILITY MANDATE;
  proven by the C5 synthesis: external-storage swap becomes an adapter, the C3
  ranker stays a separate port).
- **Index seam:** the Store holds a `VectorIndex` (add/remove/candidates/__len__,
  vector_index.py) and is its only writer; `is_authoritative=True` is asserted at
  construction so `exhaustive` can never silently flip to ANN (spec §4.3 trap).
  `scan_approved` is the single feed; `rebuild_index_from_store` makes the index a
  derived cache. A second index adapter (HNSW) needs no Store change.
- **Migration seam:** `Migrator` takes `embed_native_batch` + `fit_head` as
  injected callables, so it depends on the *embedding port* (M-embedder), not a
  concrete model — the geometry swap and the migration share one seam.

## 4. Data owned

DDL in `interface_block` (episodes, blobs, meta, exposure, task_outcomes,
utility). Owns the `_RECALL_PREDICATE` constant. **Config keys read:**
`geometry.d` (value width, CHECK on blob length), `geometry.W_version` (tier-D;
migration trigger), `retention.backup_keep` (ops floor), `observability.log_level`.
PRAGMAs: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`,
`busy_timeout=5000` (ported). Must NOT own: ranker/gate logic (C3/C4), embedder
(C1/C2), producer git logic (C10), config parsing (M-config).

## 5. Dependencies

- **Depends on:** the `VectorIndex` port (injected; Store is its sole mutator);
  the embedding port's `embed_native_batch`/`fit_head` callables (injected into
  `Migrator` only); `numpy`, `sqlite3`, `hashlib`, stdlib `logging`.
- **Must NOT know about** (named boundaries): the **C3 ranker / C4 gate** (Store
  yields `scan_approved`; scoring/abstention live above it — no recall math here);
  the **C10 producer's git logic** (Store only persists/queries the ledgers the
  producer fills — the §9 trust boundary and the §11 join policy live in C10/the
  pure joiner, never in the Store); the **MCP surface** (C7 calls the port; the
  Store has no tool schema); **config loading** (it receives resolved values).
  Crossing any of these is information leakage (red-flags.md).

## 6. Failure-mode logging (per engineering standard; secrets/text NEVER logged)

JSON structured logs; log identifiers, hashes, counts, versions — **never `text`,
blob content, or `value`** floats.

| Boundary | Level | Context logged |
|---|---|---|
| `BEGIN IMMEDIATE` busy backoff | WARN | attempt, sleep_ms, max_retries (ported) |
| busy-retries exhausted → `SqliteBusyExhausted` | ERROR | attempts, max_retries (typed hard fail, never silent drop) |
| CAS conflict in `approve`/CAS write | DEBUG→WARN | episode_id, expected_version (lost-update guard) |
| `stage` success checkpoint | INFO | pending_id, content_hash(hex), proposed_by, source |
| `approve` admit checkpoint | INFO | ids, approver, n_indexed |
| dedup hit on `stage` | DEBUG | content_hash(hex) (no insert) |
| `get_blob` miss on fetch | WARN | content_hash(hex) (verbatim fetch failed) |
| migration: stranded episode(s) | ERROR (abort) / WARN (quarantine) | stranded ids[:50], on_stranded |
| migration: W_version bump | INFO | old_W, new_W, migrated, head, explained_var (finding only) |
| migration: resume in-flight | WARN | new_W (reusing persisted head) |
| `rebuild_index_from_store` | INFO | n_indexed (recovery checkpoint) |
| ledger settle sweep | INFO | n_settled, n_clawed (move-#6 liveness — proves loop not dead) |
| missing required meta / bad status CHECK | ERROR | key/status (fail fast, §5 standard) |

## 7. Port disposition vs §10 map

| Piece | Disposition | Reference file |
|---|---|---|
| episodes table + CAS single-writer | **PORT+SIMPLIFY** (drop bi-temporal/supersession/tombstoned; add status/proposed_by/approved_by/approved_ts) | `storage/persistence.py` (SqliteEpisodeStore, update_cas:581, tx:385) |
| blob store (content-hash) | **PORT as-is** | `storage/blob_store.py` (SqliteBlobStore) |
| row codec | **PORT+SIMPLIFY** (collapse EPISODE_COLS to v-min set; delete EPISODE_COLS_ARCHIVE) | `storage/row_codec.py` |
| meta kv (W_version) | **PORT** | `storage/persistence.py` (SqliteMeta:1148) |
| reembed-from-text migration + corpus import | **PORT+SIMPLIFY** (keep native→d PCA refit + inflight resume + stranded; drop reembed_native/retrain-d→d/ANN) | `ops/migration.py:184` |
| index drive (Store owns add/remove) | **build-new wiring** over the ported `VectorIndex` | `storage/vector_index.py` |
| exposure / task_outcomes / utility ledgers | **BUILD-NEW** (exposure PORT+EXTEND task_ref per §10) | — (telemetry.py for exposure shape) |
| schemas/audit/decisions/procedures/graph_* | **DROP** | `storage/persistence.py` (excluded) |

## 8. TEST CONTRACT (test-first)

Full list in `test_contract`. Highlights mapping to gates/invariants:

- **§6.1 #5b approved-only recall** (keystone invariant): `test_pending_never_in_candidates`,
  `test_recall_returns_nothing_when_only_pending`, `test_recall_predicate_single_source`.
  Two independent fail-closed defenses tested: the SELECT predicate and index-absence.
- **§6.1 #4 migration round-trip**: `test_reembed_bumps_W_version_and_reprojects`,
  `test_reembed_reproduces_recall`, `test_reembed_index_rebuilt_from_store`,
  `test_reembed_stranded_abort`, `test_reembed_resume_uses_persisted_head`.
- **State-machine / CAS**: `test_approve_flips_and_indexes_atomically`,
  `test_approve_is_idempotent`, `test_cas_blocks_stale_approve`,
  `test_writer_serialized_under_wal`.
- **Move-#6 ledgers (§11)**: `test_set_task_ref_joins_window`,
  `test_settle_due_only_ripe_provisional`, `test_clawback_sets_negative_state`,
  `test_update_utility_bumps_version_and_accumulates`,
  `test_utility_never_writes_weight`, `test_zero_utility_layer_rolls_back`.
- **Contracts-that-cannot-lie**: `test_content_hash_binds_text`,
  Episode `__post_init__` approved-iff-approver, unit-norm dtype.

**MUTATION TESTS (RULE 2 — fault → named red → restore → green):**
- **M1** — in `approve()` delete the in-tx `index.add(id, value)` ⇒
  `test_approve_flips_and_indexes_atomically` goes RED (approved row absent from
  index, recall would miss it); restore ⇒ green. Proves in-tx index drive is
  load-bearing.
- **M2** — in `scan_approved` replace `_RECALL_PREDICATE` with no filter (return
  all rows) ⇒ `test_pending_never_in_candidates` goes RED (pending row recallable
  — a never-hallucinate breach); restore ⇒ green. Proves the single predicate is
  the recall boundary.
- **M3** — in `settle_due` flip `settle_at<=now` to `>=` ⇒
  `test_settle_due_only_ripe_provisional` goes RED (premature/never settle);
  restore ⇒ green. Proves the §11 settlement window.
- **M4** — in CAS write change `WHERE id=? AND version=?` to `WHERE id=?` ⇒
  `test_cas_blocks_stale_approve` goes RED (lost-update double admission);
  restore ⇒ green. Proves the single-writer guarantee.

**Coverage closure:** every §2 invariant (approved-only recall, immutable weight,
hash-binds-text, unit-norm value, approved-iff-approver), every §6 failure mode
(busy-backoff, CAS conflict, stranded blob, missing blob, bad status CHECK,
mixed-version refusal), and the two acceptance gates this module owns (§6.1 #4
migration round-trip, §6.1 #5b approved-only recall) each map to a named test that
goes red on the corresponding break — no functional path is untested.

---

## Design review (independent pass)

**Verdict:** A genuinely deep module with a strong, mostly enforced contract: the frozen self-asserting Episode (approved-iff-approver, hash-binds-text, unit-norm dtype as __post_init__ invariants), the single _RECALL_PREDICATE killing the spec §10 predicate-scatter, and the in-tx flip+index.add closing the two-writer drift window are all exactly the right APOSD moves (Define-errors-out-of-existence #11, deep module #4, a simple narrow surface #6). It is NOT build-ready: the spec consciously accepts a wide ISP-smell port to keep the transaction atomic, but that decision silently fuses THREE distinct write-knowledge domains (episode state machine, move-#6 ledger semantics, geometry migration) into one object whose contract for the ledger half is the weakest and least-tested part, and the Test Contract — strong on the approved-only keystone and migration round-trip — has real holes on the secret-safe invariant (entirely absent from M03's own tests), the dedup/blob-orphan paths, the unit-norm/dtype rejection direction, and the index/store divergence-recovery seam. Fix the must_fix items (chiefly: own or explicitly disown the secret-safe boundary, resolve the telemetry-sink-vs-same-file coupling contradiction, and close the named test gaps) before sign-off.

**Scores (1–10):**
- design_complexity: 6
- cognitive_load: 6
- information_leakage: 4
- extensibility_fit: 7
- agent_navigability: 7
- contract_enforcement: 7
- test_coverage: 5

**Red flags:**
- Special-General Mixture / wide-port ISP @ §1.1 single EpisodeStore — fuses episode state machine + move-#6 ledger policy + geometry migration into one object; atomic-tx justifies the state writes but not the ledger/family-scope policy. root: dependency -> change amplification across unrelated concerns, agent must load the whole contract to use stage/approve.
- Prose-Only Contract / status overload @ §2 reject(keep_rejected=True) — 'pending' means both awaiting-approval AND rejected-but-kept, distinguishable only by prose. root: obscurity -> hive_pending cannot tell the two apart; a status='rejected' terminal value would be honest and equally fail-safe.
- Information Leakage @ §4 ledger location vs port telemetry.py separate-sink — where outcome/exposure state lives is one decision reflected unreconciled in M03's DDL claim and §11's drain mechanism. root: dependency -> change amplification across M03 + producer + sink.
- Missing Feedback Signal @ §2 stage() hash-derivation — the headline 'designed out caller-supplied hash' win has no direct test of stage()'s derivation; only the dataclass binds-text test exists. root: obscurity -> the key precondition-removal path is untested.
- Vague / self-contradicting disposition @ §7 exposure row — 'BUILD-NEW (exposure PORT+EXTEND ...)' contradicts itself; reference col is '—' yet text cites telemetry.py whose shape (trace-keyed recalled-json list) differs from §3's per-(trace,episode) row. root: obscurity -> agent cannot tell whether to lift telemetry's shape or build fresh.

**Test gaps:**
- secret-safe boundary untested in M03
- Episode invariant rejection direction untested
- blob orphan-on-reject and shared-blob dedup untested
- index/store crash-recovery (rebuild) untested + non-idempotent risk
- is_authoritative ANN-flip refusal untested
- SqliteBusyExhausted typed-failure path untested
- mixed-version mid-rewrite refusal untested
- weight-immutability across approve/migration untested
- recall_margin credit-split untested
- utility.n_sources corroboration untested

**Must-fix:**
- SECRET-SAFE INVARIANT IS UNOWNED AND UNTESTED HERE. §6.1 #5b (a) — the secret scan refuses/redacts a planted credential BEFORE staging — is a named product invariant and an acceptance gate, but M03's Responsibility (§1) and Dependencies (§5) place the secret scan in C6/the write path, and the Test Contract contains ZERO secret-scan tests. stage() in §2 does put_blob(text)+derive content_hash with no mention of a scan precondition. Decide explicitly: either (a) stage() asserts the text was already scanned (a documented precondition — weak, prose-only, the exact 'Prose-Only Contract on Tricky Semantics' agent-native red flag), or (b) M03 owns a refusal at the storage boundary (a raw secret is never persisted to blob/episodes even if C6 is bypassed — the fail-safe direction). Per the product invariant 'a raw secret is never persisted,' the storage layer is the last line; add a test test_stage_refuses_unscanned_secret OR test_stage_requires_scanned_flag with a mutation (delete the guard -> secret persists -> test red). As written an agent wiring C6 wrong leaks a credential into 30 days of backups with no test catching it.
- RESOLVE THE LEDGER-LOCATION CONTRADICTION (information leakage / coupling). §4 says exposure/task_outcomes/utility live in the SAME SQLite-WAL file under M03's single-writer tx; but the port reference ops/telemetry.py puts the recall-outcome sink in a SEPARATE DB (~/cortex/telemetry.db) explicitly 'so telemetry volume never competes with the single-writer hot store,' and §11 says apply_outcomes_from_sink DRAINS that sink. The spec never reconciles which writes are in-file (M03) vs in the external sink, nor how the settle sweep / drain crosses that boundary atomically. This is an unresolved Information Leakage (red-flags.md: one decision — where outcome state lives — split across M03 and the producer/sink) that will force a coordinated multi-module edit. Name the boundary: which of {exposure.task_ref, task_outcomes state machine, utility posterior} M03 owns in-file, and where the reward arrives from. Until resolved, test_settle_due_only_ripe_provisional and the drain tests have no defined transaction boundary.
- DEFINE is_authoritative ENFORCEMENT AS A REAL CONTRACT, NOT PROSE. §3 says 'is_authoritative=True is asserted at construction so exhaustive can never silently flip to ANN.' The ported ExhaustiveVectorIndex (vector_index.py) has NO is_authoritative attribute — only D. So this is a build-new assertion with no named test. Add test_store_rejects_non_authoritative_index (constructing SqliteEpisodeStore with an index that can flip raises) and a mutation (remove the assert -> an ANN-capable index is accepted -> test red). The §4.3 approx_threshold trap is a named live bug; an unenforced assertion does not close it.
- SPECIFY THE INDEX/STORE DIVERGENCE-RECOVERY CONTRACT. rebuild_index_from_store() is named as the recovery for the crash-between-commit-and-add window, but the Test Contract has no test that (a) simulates a crash AFTER the status flip commits but BEFORE index.add lands (the exact window the in-tx drive is supposed to remove — but a process kill between COMMIT and a non-transactional in-memory index.add can still desync since the index is an in-RAM ExhaustiveVectorIndex, not a SQLite table inside the same tx). Clarify: is index.add inside the SAME SQLite tx (impossible for an in-memory numpy index — COMMIT durably persists the row, the RAM index mutation is not part of that durability) or is rebuild_index_from_store the actual guarantee? Add test_rebuild_recovers_after_crash_between_commit_and_add and test_rebuild_is_idempotent. This is the load-bearing never-hallucinate recovery and it is currently asserted, not proven.
- ADD THE MISSING FAILURE-DIRECTION TESTS FOR THE Episode __post_init__ INVARIANTS. The contract claims three unconstructable-illegal-episode invariants but the Test Contract only names the positive 'binds'/'approved-iff-approver'/'unit-norm dtype' checks — not the REJECTION direction. Add test_episode_rejects_float64_value, test_episode_rejects_2d_value, test_episode_rejects_approved_without_approver, test_episode_rejects_pending_with_approver, test_episode_rejects_hash_text_mismatch (each asserting the constructor RAISES). A contract that only tests the happy construction does not prove the invariant is enforced (agent-native §6: contracts that cannot lie need the lie tested).
