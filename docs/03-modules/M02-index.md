# M02 VectorIndex  (index)

**One-line:** An in-memory cosine-kNN search index over APPROVED episode values that the Store owns and drives: search(value_q, k) returns signed-cosine-ranked (episode_id, cosine) with no mutation verb in the caller's reach, exhaustive is authoritative-by-control-flow (the approx_threshold trap is structurally impossible, not merely defaulted away), and the whole index is a deterministic derived cache of scan_approved().
**Port disposition:** PORT+SIMPLIFY of storage/vector_index.py (spec §10 dense-ranker/Store rows). Lift ExhaustiveVectorIndex's stacked-matrix + swap-remove core; (1) FLIP the ranking from absolute |q·x| (vector_index.py:74, the latent exact-negation bug) to signed positive cosine q·x (port NativeVectorIndex's rank at vector_index.py:149-158) and return the cosine SCORE alongside the id, not a bare id list; (2) DELETE the approx_threshold flip site (the search() surface has no N-gated ANN branch at all — exhaustive is the only code path the default adapter can execute, killing the episodic.py:277 trap structurally rather than relocating the threshold per spec §4.3); (3) RAISE the boundary so the Store is the sole mutator — replace the public add/remove surface with a Store-driven sync_approved()/rebuild_from_store() fed only by scan_approved(), per the locked C5 synthesis decision (no two-writer drift window, no caller-desync leak). KEEP HNSWVectorIndex/LSHVectorIndex as config-selected adapters behind the same VectorIndex port but is_authoritative=False (DROP NativeVectorIndex as a separate class — its cosine ranking folds into the default exhaustive adapter). DROP the SparseVec |q·x| sign convention and all "keys may be signed" machinery (v-min values are unit-norm cosine vectors, never signed sparse keys).

---

# M02 — VectorIndex (C5 search half)

## 1. Responsibility (one deep module)

M02 owns the **searchable geometry over the approved episode set**: given a query
value `value_q` (the same d-dim PCA-projected unit vector the embedder produced at
capture, per spec `:91`) it returns the top-k approved episodes ranked by cosine
similarity, or an empty list. It is the search half of C5 Store (spec §2): the Store
owns the durable `episodes` table; M02 owns the **in-memory derived cache** that makes
"score every APPROVED i by cos(value_i, value_q)" (spec `:93`) fast and exact, plus the
deterministic reconstruction of that cache from the durable table.

The *meaningful work hidden behind the narrow surface* is threefold and all non-obvious:
1. **Exact cosine-kNN with the correct sign.** A stacked `(N,d)` matvec + top-k
   partition that ranks by **signed** cosine — so an anti-correlated value is pushed to
   the tail and *never* surfaces ahead of a correlated one. The reference
   `ExhaustiveVectorIndex` ranks by `|q·x|` (vector_index.py:74) because it indexed
   *signed sparse keys*; for v-min's positive-cosine values that absolute-value ranking
   is a latent correctness bug (a vector's exact negation scores top-1). M02 ports the
   *cosine-correct* ranking from `NativeVectorIndex` (vector_index.py:149-158) into the
   one default adapter.
2. **Authoritative-exhaustive by control flow.** The reference flips to ANN whenever
   `len(ids) > approx_threshold` (episodic.py:277, default 10_000) — a proven trap: the
   12,324-episode corpus is *already past it*, so exact-eval recall reads 0 (spec §4.3).
   M02 does not "set the threshold higher" (which only relocates the landmine); the
   exhaustive adapter **has no threshold branch and no ANN field at all** — exhaustive is
   the only path its `search()` can execute. `is_authoritative` is a typed property the
   hot path asserts. Growing N can never silently change the answer.
3. **Derivation, not duplication.** The index is a *cache* of `episodes WHERE
   status='approved'`. Callers cannot mutate it; the Store drives population
   (`sync_approved`/`drop`) inside the same write lock/tx that flips `status` (per the
   locked C5 decision: Store-owns-and-drives-the-index, no two-writer drift window), and
   `rebuild_from_store(scan_approved())` reconstructs it byte-deterministically on boot
   and after a `W_version` migration.

Narrow surface (`search`, `__len__`, `is_authoritative`, `dim` for readers;
`sync_approved`/`drop`/`rebuild_from_store` for the Store), rich enforced contract.

## 2. Public surface + ENFORCED contract

```python
Value = npt.NDArray[np.float32]   # unit-norm float[d]; d == geometry.d (256)
SearchHit = tuple[int, float]     # (episode_id, cosine ∈ [-1, 1])

@runtime_checkable
class VectorIndex(Protocol):              # what the C3 ranker is handed (READ-ONLY)
    @property
    def is_authoritative(self) -> bool: ...
    @property
    def dim(self) -> int: ...
    def search(self, value_q: Value, k: int) -> list[SearchHit]: ...
    def __len__(self) -> int: ...

@runtime_checkable
class MutableVectorIndex(VectorIndex, Protocol):   # what the Store holds (population)
    def sync_approved(self, episode_id: int, value: Value) -> None: ...
    def drop(self, episode_id: int) -> None: ...
    def rebuild_from_store(self, rows: Iterable[tuple[int, Value]]) -> int: ...
```

**Invariants (enforced, not prose):**
- **I1 — cosine ordering, signed.** `search` returns hits sorted by
  `cos(value_i, value_q)` **descending** (cosine == dot since values are unit-norm).
  Anti-correlated (negative) scores sort last. *Enforced by* `test_search_ranks_by_descending_cosine`, `test_anticorrelated_value_never_surfaces_first`.
- **I2 — scores returned, in [-1,1].** Each hit carries the actual cosine, not a bare
  id. *Enforced by* `test_search_returns_cosine_scores_not_bare_ids`. (The C4 entropy
  gate consumes these scores; a bare-id index would force the ranker to re-fetch and
  re-dot — information leakage. Returning the score is the deep-module move.)
- **I3 — exact top-k truncation.** `len(result) == min(k, N)`; `k<=0 ⇒ []`; empty index
  `⇒ []` without raising (abstain-clean — spec never-hallucinate). *Enforced by*
  `test_search_topk_truncates_len_min_k_n`, `test_k_nonpositive_returns_empty`,
  `test_empty_index_search_returns_empty_no_raise`.
- **I4 — authoritative-exhaustive.** The default adapter's `is_authoritative is True`
  and its `search` contains **no N-gated branch**. *Enforced by*
  `test_exhaustive_is_authoritative_true`, `test_no_approx_threshold_attribute_on_exhaustive`,
  `test_recall_exact_above_legacy_threshold`, `test_growing_n_never_flips_path`.
- **I5 — dim agreement.** `dim == geometry.d`; a `value`/`value_q` whose shape ≠ `(dim,)`
  raises `ValueError` at the boundary (a silent dim mismatch would corrupt the matvec).
  *Enforced by* `test_dim_mismatch_query_raises_valueerror`.
- **I6 — approved-only.** The index never holds a non-approved row; population flows
  exclusively from `scan_approved()`. *Enforced by* `test_only_approved_values_searchable`
  (a pending value handed to search must be absent — belt-and-suspenders with the
  Store's status filter; this module's contribution is *having no public add* a caller
  could use to desync).
- **I7 — derived determinism.** `rebuild_from_store(rows)` produces an index whose
  `search` output equals incremental `sync_approved` of the same rows. *Enforced by*
  `test_rebuild_from_store_is_deterministic`, `test_rebuild_matches_incremental_sync`.

**Designed-out preconditions (not documented — removed):**
- *"Don't call search above N entries / set approx_threshold > N first."* Designed out
  by deleting the ANN branch from the authoritative adapter — there is no threshold to
  misconfigure (spec §4.3 structural fix).
- *"Callers must keep the index in sync with the table."* Designed out by giving readers
  **no mutation verb** — only the Store (holding `MutableVectorIndex`) can populate, and
  it does so atomically with the status flip (locked C5 decision).
- *"Pass the score back yourself."* Designed out by returning `SearchHit` (id + cosine)
  so no caller re-derives the score.

**Error semantics:** `ValueError` for shape/dim violations (programmer error, fail
loud). Empty/`k<=0` → `[]` (a true non-result, never an exception). No other raises on
the hot path — an empty or absent index abstains cleanly.

## 3. Swap seam

**Port:** `VectorIndex` (read) / `MutableVectorIndex` (population), both
`runtime_checkable`. The C3 ranker (M03) depends only on the read port.

**Default adapter:** `ExhaustiveCosineIndex(dim)` — `is_authoritative=True`, stacked
`(N,d)` float32 matrix, signed matvec + `argpartition` top-k, swap-remove, one
`threading.Lock`. **No `approx_threshold`, no `candidate_k`, no ANN field.**

**Second adapter (proof of zero-core-change swap):** `HnswIndex` / `ExternalIndex`
(pgvector/Qdrant) implements the *same* `MutableVectorIndex` Protocol with
`is_authoritative=False`. To swap, a second adapter must implement exactly:
`is_authoritative`, `dim`, `search(value_q,k)->list[SearchHit]`, `__len__`,
`sync_approved`, `drop`, `rebuild_from_store`. **No core change is needed** because:
(a) the Store calls only the Protocol methods — external storage becomes an adapter, not
a Store refactor (the pure-A failure mode the locked decision rejected); (b) selection is
one config key `index.vector_index_backend` routed through `build_index(...)` which
**fails fast** on an unknown backend (never silently falls back to exhaustive); (c)
`is_authoritative` lets the eval/Store *assert* exactness on the hot path, so an ANN swap
is an explicit opt-out of exactness, never a silent one. *Enforced by*
`test_hnsw_adapter_satisfies_protocol`, `test_factory_unknown_backend_fails_fast`.

## 4. Data owned

**No tables, no blobs.** M02 is a pure in-memory derived cache. It owns only its
internal matrix/id-map and lock. The durable source of truth is the `episodes` table
(M01 Store) filtered to `status='approved'`; the `value BLOB` is float32
little-endian (`np.frombuffer`, per row_codec.py:31/37) decoded by the Store and handed
to `sync_approved`/`rebuild_from_store` as a `Value` array.

**Config keys read** (`group.field`): `index.vector_index_backend` (default
`"exhaustive"` → authoritative), `geometry.d` (== `dim`; every value must match).
`recall.candidate_k` and `recall.approx_threshold` are read by the **ANN adapters only**
and are *inert/ignored* in the exhaustive adapter (it has no code that consults them).

## 5. Dependencies

- **Depends on:** numpy; the `Value`/`SearchHit` types; the config `index`/`geometry`
  groups (via the factory). That's all — it is a leaf compute module.
- **Driven by (does not call):** M01 EpisodeStore, which holds the `MutableVectorIndex`
  and calls `sync_approved`/`drop` inside its write lock and `rebuild_from_store` on boot.
- **Must NOT know about (the boundaries):**
  - **The status state machine / approval policy.** M02 never reads `status`, `pending`,
    `approved_by`, or any admission concept — it only receives *already-approved*
    `(id, value)` pairs from the Store. The approved-only guarantee is enforced *upstream*
    (the Store only ever calls `sync_approved` for an approved row); M02's structural
    contribution is offering no public `add` to bypass that. Boundary name:
    **admission boundary** (M01/M06 own it).
  - **Text, content_hash, blobs, fetch.** M02 indexes `value` vectors only; it never sees
    verbatim text. Boundary name: **content boundary** (BlobStore / fetch own it).
  - **The entropy gate / abstention decision.** M02 returns scores; the *return-vs-abstain*
    call is C4/M04's. Boundary name: **confidence boundary**.
  - **Embedding policy (PCA/native dim).** M02 takes a finished d-dim value; it never
    embeds or projects. Boundary name: **encode boundary** (M-embedder owns it).

## 6. Failure-mode logging (structured JSON; secrets/PII never logged — values are
opaque float vectors, ids are integers, both safe)

| Boundary | Level | Context fields |
|---|---|---|
| `build_index` unknown backend | **error** then raise | `vector_index_backend`, valid set, `geometry.d` |
| `sync_approved` dim mismatch | **error** then raise `ValueError` | `episode_id`, `got_shape`, `dim` |
| `search` dim mismatch on `value_q` | **error** then raise `ValueError` | `query_shape`, `dim`, `N` |
| `search` on empty/absent index | **debug** then `[]` | `k`, `N=0` (abstain-clean, not an error) |
| `rebuild_from_store` start/finish | **info** | `n_indexed`, `dim`, `elapsed_ms` (long-running; spec §6 perf-log) |
| `rebuild_from_store` row dropped (bad value) | **warn** | `episode_id`, `got_shape` (recoverable: skip + continue, never abort the rebuild) |
| `drop` of absent id | **debug** | `episode_id` (no-op, expected during prune races) |
| `is_authoritative is False` consulted on hot path | **warn** (once) | `backend` — surfaces an ANN swap so it is never silent |

No secret/credential path exists here (no text). Logging the *count* of indexed rows and
the rebuild latency satisfies the global standard's success-checkpoint + performance
requirements without leaking content.

## 7. Port disposition vs spec §10

**PORT+SIMPLIFY** of `storage/vector_index.py` (the §10 row collapses the dense
ranker/Store index into this search half). Concretely:
- **Lift:** `ExhaustiveVectorIndex`'s stacked-matrix store, swap-remove, single lock.
- **Flip (the bug fix):** rank by **signed** cosine `q·x` (port from
  `NativeVectorIndex`, vector_index.py:149-158) and **return the score**, replacing the
  absolute `|q·x|` ranking (vector_index.py:74) that would surface an exact-negation
  vector — a real ported defect, fixed here, not carried.
- **Delete (the structural fix):** the `approx_threshold` flip lives at the *caller*
  (episodic.py:277); M02 removes it entirely from the authoritative adapter so exhaustive
  cannot silently become ANN (spec §4.3). `approx_threshold`/`candidate_k` survive only as
  ANN-adapter knobs.
- **Raise the boundary:** replace public `add`/`remove` with Store-driven
  `sync_approved`/`drop`/`rebuild_from_store` fed by `scan_approved` (locked C5 decision —
  no caller mutation, no two-writer drift).
- **Keep behind the port:** `HNSWVectorIndex`/`LSHVectorIndex` as config-selected
  `is_authoritative=False` adapters.
- **Drop:** `NativeVectorIndex` as a separate class (its cosine ranking folds into the
  default adapter); the `SparseVec` "keys may be signed" `|q·x|` machinery entirely.

## 8. TEST CONTRACT (test-first; see `test_contract` field for the compact list)

Every invariant in §2 and every boundary in §6 maps to a named test; full functional
coverage means no path in §2/§6 is unasserted.

**Happy path:** `test_search_ranks_by_descending_cosine` (known unit vectors → exact
cosine order; asserts the full ordered id list), `test_search_returns_cosine_scores_not_bare_ids`
(each `hit[1] == np.dot(value_i, value_q)` within 1e-6), `test_search_topk_truncates_len_min_k_n`.

**Every failure mode (§6):** `test_empty_index_search_returns_empty_no_raise`,
`test_k_nonpositive_returns_empty`, `test_dim_mismatch_query_raises_valueerror`,
`test_factory_unknown_backend_fails_fast`, `test_drop_absent_is_noop`,
`test_rebuild_skips_bad_value_row` (a wrong-shape row is warned + skipped, rebuild still
returns the rest).

**Every invariant (§2):** I1→`test_search_ranks_by_descending_cosine` +
`test_anticorrelated_value_never_surfaces_first`; I2→`test_search_returns_cosine_scores_not_bare_ids`;
I3→`test_search_topk_truncates_len_min_k_n`/`test_k_nonpositive_returns_empty`/`test_empty_index_search_returns_empty_no_raise`;
I4→`test_exhaustive_is_authoritative_true`/`test_no_approx_threshold_attribute_on_exhaustive`/`test_recall_exact_above_legacy_threshold`/`test_growing_n_never_flips_path`;
I5→`test_dim_mismatch_query_raises_valueerror`; I6→`test_only_approved_values_searchable`/`test_caller_has_no_add_remove_verb`;
I7→`test_rebuild_from_store_is_deterministic`/`test_rebuild_matches_incremental_sync`/`test_sync_approved_idempotent_upsert`.

**Swap seam:** `test_hnsw_adapter_satisfies_protocol`,
`test_factory_unknown_backend_fails_fast`.

**MUTATION tests (RULE 2) — two, each on the keystone defects this module exists to kill:**
1. *Fault:* change the exhaustive `search` ranking from signed `q·x` to `np.abs(q·x)`
   (re-introduce vector_index.py:74). *Must go RED:*
   `test_anticorrelated_value_never_surfaces_first` (the `-v` id appears at rank 1).
   *Restore → GREEN.* This proves the cosine-sign correctness is actually tested, not
   assumed.
2. *Fault:* prepend `if len(self._ids) > 10_000: return self._ann.candidates(value_q, 512)`
   to the exhaustive `search` (re-introduce the episodic.py:277 trap). *Must go RED:*
   `test_no_approx_threshold_attribute_on_exhaustive` (an `_ann`/threshold appears) and
   `test_recall_exact_above_legacy_threshold` (recall drops below 1 at N>10k). *Restore →
   GREEN.* This proves the authoritative-exhaustive guarantee is enforced by tests, not
   by a default value.

**§6 acceptance-gate mapping:** spec §4.3 "make exhaustive authoritative — short-circuit
the approx path so growing N can never silently flip it; exact recall, the eval assumes
exact" is the gate M02 is responsible for. It is proven by
`test_exhaustive_is_authoritative_true` + `test_recall_exact_above_legacy_threshold`
(planted gold is rank-1 in a 12,324-vector index) + `test_growing_n_never_flips_path`
(identical exact result either side of the legacy 10k threshold), with mutation test #2
proving those tests have teeth.


---

## Design review (independent pass)

**Verdict:** STRONG DESIGN, NEAR BUILD-READY — sign-off blocked by a small set of test-contract gaps, not by structure. This is a genuinely DEEP module (APOSD #4): a narrow read surface (search/__len__/is_authoritative/dim) hides exact cosine-kNN, deterministic cache reconstruction, and a structural fix to the approx_threshold trap. Three design moves are correct and verified against the reference: (1) Flip |q·x|→signed q·x — the abs-ranking at vector_index.py:74 (np.argpartition(-np.abs(scores),...)) is a real ported defect that surfaces an exact-negation vector at rank-1; M02 ports the cosine-correct ranking from NativeVectorIndex (vector_index.py:142-158). (2) Delete the ANN branch — episodic.py:277 (if len(ids) > self.approx_threshold:, default 10_000 at episodic.py:72) is the live trap that reads recall=0 on the already-past-threshold 12,324 corpus (spec §4.3); M02 removes the branch from the authoritative adapter entirely, making is_authoritative a typed property the hot path asserts rather than a default value that can drift — this is 'define errors out of existence' (APOSD #11) applied structurally. (3) Return SearchHit (id+cosine) instead of bare ids — verified leakage fix: the reference candidates() returns list[int] (vector_index.py:66/142), forcing the C4 gate to re-dot; M02's score-return removes that back-channel (APOSD #6, agent-native §6). Information leakage is genuinely near-zero: M02 explicitly disclaims the admission/content/confidence/encode boundaries and owns no status, text, or abstention concept. The swap seam is real (one config key index.vector_index_backend through build_index that fails-fast, is_authoritative as the explicit-opt-out-of-exactness flag). The Test Contract maps every §2 invariant and §6 boundary to a named test with concrete assertions and includes two well-aimed mutation tests on the keystone defects. It does NOT yet reach build-ready because ~6 functional paths are unasserted and two contract claims cannot actually be tested at M02's boundary as worded.

**Scores (1–10):**
- design_complexity: 3
- cognitive_load: 3
- information_leakage: 2
- extensibility_fit: 8
- agent_navigability: 8
- contract_enforcement: 7
- test_coverage: 7

**Red flags:**
- Prose-Only Contract on tricky semantics @ §2-I1 / §3 'signed matvec + argpartition top-k' — the within-k descending-sort requirement (the load-bearing detail that makes the partition path correct) lives only in prose; no test pins it for the k<N branch. root: obscurity → silent drift (a partition-only impl ranks correctly across the cut but unordered inside it, and the C4 gate consumes mis-ordered scores).
- Nonobvious Code / Missing Feedback Signal @ §8 mutation test #2 — test_no_approx_threshold_attribute_on_exhaustive is a STATIC structural assertion (an _ann/threshold attribute appears). The planted fault `if len(self._ids) > 10_000: return self._ann.candidates(...)` only trips it because it names self._ann; a self-contained inline fallback with no new attribute would NOT trip this test. root: obscurity → the spec overstates this test's teeth. The behavioral companion test_recall_exact_above_legacy_threshold DOES catch any flip, so the mutation is covered — but the §8 claim that test_no_approx_threshold_attribute_on_exhaustive enforces I4 is overclaimed; re-anchor I4's mutation red-trigger to the behavioral test.
- Special-General Mixture (latent, contained) @ §4 'recall.candidate_k and recall.approx_threshold are read by the ANN adapters only and are inert/ignored in the exhaustive adapter' — config keys whose meaning depends on which adapter is active is a mild general/special blend. It is correctly contained (the exhaustive adapter has no code consulting them, per the §4.3 structural fix) and not a build blocker, but no test asserts the exhaustive adapter truly ignores a set candidate_k/approx_threshold. root: dependency → add test_exhaustive_ignores_ann_knobs to convert the prose 'inert' claim into an enforced one.
- Missing Feedback Signal @ §6 'is_authoritative is False consulted on hot path → warn (once)' — this boundary row has no named test in §8. The warn-once behavior (the one signal that surfaces an ANN swap so exactness loss is never silent) is unasserted. root: obscurity → add test_non_authoritative_backend_warns_once.

**Test gaps:**
- No test asserts the k<N argpartition path returns hits sorted descending by cosine (within-k ordering); test_search_ranks_by_descending_cosine may only exercise the k>=N argsort-all branch.
- No NaN / non-finite value or query test — a NaN score silently corrupts argpartition/argsort and can surface a garbage hit at rank-1, violating never-hallucinate on M02's own hot path.
- No value-copy / aliasing test (mutate source array after sync_approved → search must be unchanged); the copy contract is unstated, and row_codec hands out .copy() precisely because np.frombuffer is a read-only view.
- No direct test of the dim property (returns geometry.d) or of __len__ correctness after sync/drop/rebuild interleavings (only mismatch raises are tested).
- test_only_approved_values_searchable asserts a property (status filtering) M02 structurally cannot see — untestable at this boundary; the real enforceable contract is test_caller_has_no_add_remove_verb.
- No test that the exhaustive adapter ignores a non-default recall.candidate_k / recall.approx_threshold (the 'inert' claim in §4 is prose-only).
- No test for the is_authoritative-False warn-once hot-path log (§6 boundary row 8).
- No test for sync_approved upsert of an existing id with a NEW value (the idempotent-upsert test name exists but the assertion target — same id, changed vector, search reflects the new vector — is not spelled out; risk it only checks len stability, not value replacement, which the reference does at vector_index.py:38).
- No concurrency/thread-safety test exercising the single threading.Lock under interleaved sync_approved/drop vs search, despite the lock being part of the §3 adapter contract and the Store driving population inside its write lock.
- No test that drop during a search-in-progress (swap-remove invalidating a row mid-matvec) is safe — the swap-remove + single-lock design is correct but unasserted under contention.

**Must-fix:**
- TEST GAP — within-k ordering is unasserted and the spec under-specifies it. I1 requires the returned list be sorted by descending cosine, but §3 describes only 'argpartition top-k'. np.argpartition does NOT order the partitioned slice; the reference must re-sort (vector_index.py:155-157: order = order[np.argsort(-scores[order])]). test_search_ranks_by_descending_cosine asserts 'the full ordered id list' but if it is run with k>=N it exercises the argsort-all branch and never proves the k<N partition path is internally sorted. Add an explicit test: k < N, assert result is sorted descending by hit[1] AND equals the true top-k order — otherwise a partition-only implementation passes the suite while returning mis-ordered hits to the entropy gate.
- TEST GAP / INVARIANT — NaN / non-finite score handling is untested and never-hallucinate-relevant. A NaN in value_q or a stored value makes the matvec produce NaN scores, which silently corrupt argpartition/argsort ordering and can surface a garbage hit at rank-1 (a hallucinated 'confident' result). M02 owns the abstain-clean contract on its hot path. Add test_nan_query_or_value_is_rejected_or_excluded (ValueError at the boundary, or scrubbed) and a matching §6 log row; today a non-finite value is an unknown-unknown.
- TEST/CONTRACT LIE — test_only_approved_values_searchable cannot be honestly tested at M02's boundary. §2-I6 and §5 say M02 never sees status; so 'a pending value handed to search must be absent' is unprovable here — M02 has no admission concept to filter on. As worded this is a contract that can lie (agent-native §6). Either delete the test and rely solely on test_caller_has_no_add_remove_verb (the real structural contribution) or move test_only_approved_values_searchable to M01's contract where status exists. Keep I6's claim to exactly what M02 enforces: 'no public mutation verb on the read port'.
- TEST GAP — value aliasing / copy-on-ingest is unspecified and unasserted. row_codec's bytes_to_np returns np.frombuffer(...).copy() because frombuffer is a read-only view (row_codec.py:38); but M02's spec never states whether sync_approved/rebuild_from_store copy the incoming Value before stacking into the matrix. If they store the caller's array by reference, a Store that reuses a decode buffer or a later in-place normalization silently mutates indexed geometry. Add test_sync_approved_copies_value (mutate the source array after sync; assert search results unchanged) and state the copy contract in §2.
