# M04 Recall pipeline  (recall)

**One-line:** A single deep module `RecallPipeline.recall(query) -> RecallResult` that hides the whole encode→dense-cosine-score→normalized-entropy-abstain→utility-surface→trace+exposure-ledger flow behind one narrow surface, and is the never-hallucinate enforcement point (abstain-no-resurrect is structural, not a flag).
**Port disposition:** PORT+SIMPLIFY serving/sources/native_source.py (drop GMM/Mahalanobis episodic path, drop |q·x| absolute-value ranking → positive-cosine only; rank over the d=256 PCA `value`, not a separate native vector); PORT AS-IS serving/gate_bundle.py::NormalizedEntropyGate (re-tune `geometry.beta`/`recall.H_frac_max` for the dense-cosine [-1,1] range, §5-D2/D3 — code unchanged); PORT+FIX the surfacer at serving/service.py:184 (un-cripple `1+max(0,u)` → `weight × f(utility)` with f able to DEMOTE; Phase-1 inert until posteriors exist); PORT+EXTEND ops/telemetry.py emit_recall_telemetry / TelemetrySink (trace_id + exposure ledger write with episode_ids + recall_margin; add task_ref column wiring is M-store's job, not here). DROP the ExhaustiveVectorIndex |q·x| absolute-value variant and NativeVectorIndex duplication — the M03 Store-owned authoritative index (positive cosine, never-flip) is the single index this module reads via the C3 ranker port; this module never mutates it. Reuses RecencyDecay from serving/recall_pipeline.py only as the default-OFF identity multiplier (Phase-1).

---

# M04 — Recall pipeline (C3 ranker + C4 abstention gate + surfacer)

> Hexagonal placement: this module is the **read half of the pure domain core**
> (`hive/domain/recall.py`). It owns NO I/O — it composes three injected
> collaborators (the `EmbeddingProvider` port, the `RecallIndex` view of the
> M03 Store-owned authoritative index, the `ExposureLedger` write seam) and the
> two pure stage objects (`NormalizedEntropyGate`, `UtilitySurfacer`). It is the
> spec's **never-hallucinate enforcement point** (§2 C4, §6.1#3).

---

## 1. Responsibility (one deep module)

`RecallPipeline.recall(query) -> RecallResult` hides the entire recall flow
behind one narrow surface:

```
query ─▶ embedder.encode  (same chain as capture — M02)        value_q : float32[d], unit-norm
      ─▶ index.search(value_q, top_n)  over APPROVED only        scored  : list[Scored]  (positive cosine)
      ─▶ gate.evaluate(sims)                                     (suppress, entropy_norm, top_margin)
            ├─ suppress  ─▶ ABSTAIN  (hits = (), no resurrect)
            └─ pass      ─▶ surfacer.order(scored, utility_of)   reorder by weight × f(utility)
      ─▶ emit trace_id + ledger.record_exposure([(eid, margin)]) move-#6 exposure capture
      ─▶ RecallResult(trace_id, state, hits, entropy_norm, top_margin)
```

The "meaningful work hidden behind a narrow surface" is the **abstain-no-resurrect
control flow**: the abstention decision is a one-way gate — once `suppress` is True,
there is **no code path** that can repopulate `hits`. This is the product's central
invariant and it is made **structural** (control flow), not a documented convention.

The module is the §10 PORT+SIMPLIFY of `serving/sources/native_source.py` (dense
cosine-kNN, GMM/Mahalanobis dropped) + PORT-AS-IS of
`gate_bundle.py::NormalizedEntropyGate` + PORT+FIX of the `service.py:184` surfacer,
re-housed as one pure module so the whole read path is fake-testable in milliseconds.

---

## 2. Public surface + ENFORCED contract

### 2.1 `RecallPipeline.recall(query: str, *, agent_id: str) -> RecallResult`

**Postconditions (each is a named test):**
- `state == CONFIDENT` ⟺ `len(hits) >= 1` and the gate did not suppress.
- `state in {ABSTAIN, EMPTY_NO_DATA}` ⟹ `hits == ()` — **enforced, not documented**
  (abstain-no-resurrect; §6.1#3). `ABSTAIN` = data existed + gate fired;
  `EMPTY_NO_DATA` = empty index / zero candidates (distinct: entropy_norm 0.0).
- `0.0 <= entropy_norm <= 1.0` always (the agent-visible confidence; normalized
  H/ln(N_eff), **never raw nats** — N-invariant).
- `trace_id` is a fresh uuid4 hex per call (the move-#6 join key; unique per call).
- On `CONFIDENT`, `ledger.record_exposure(trace_id, agent_id, [(eid, margin)…], ts)`
  is called **exactly once**; on a non-CONFIDENT result it is **not** called
  (nothing was injected, so nothing is credited).
- **NEVER raises into the caller.** Any internal failure (embedder, index, gate)
  is logged and degrades to `EMPTY_NO_DATA` — a recall failure must fail closed
  (return nothing), never surface an un-vetted hit.

**Units:** `sim` is cosine ∈ [-1, 1]; `entropy_norm` ∈ [0, 1] dimensionless;
`ts` epoch seconds (int); `recall_margin` = softmax-mass gap to the next hit.

**Precondition DESIGNED OUT, not documented:** the "recall reads APPROVED only"
guarantee is **not** a precondition the caller must satisfy. It is enforced by the
*shape of the seam*: `RecallIndex.search` is the only candidate source and the
M03 Store feeds that index **exclusively from `scan_approved`** (the synthesis
decision: the Store is the sole index mutator; the pipeline has no index-mutation
verb at all). A pending row is therefore *unrepresentable* in `scored`, so no
`status` filter can be forgotten here.

**Precondition DESIGNED OUT:** the never-silently-ANN guarantee. `recall()` asserts
`index.is_authoritative()` (the M03 exhaustive index returns True; an ANN index
returns False ⟹ refuse). This kills the §4.3 `approx_threshold` trap at the type
boundary instead of relying on a config value being set above N.

### 2.2 Stage objects (narrow, pure)

- `NormalizedEntropyGate.evaluate(sims) -> (suppress, entropy_norm, top_margin)` —
  PORT AS-IS. `suppress` iff `entropy_norm > h_frac_max`. Empty ⟹ `(False, 0.0, 0.0)`.
  Internal failure ⟹ fail-closed `(True, 1.0, 0.0)`. `beta` re-tuned for cosine
  ∈ [-1,1] (§5-D2) — the *code* is byte-identical to the reference; only the
  injected `beta`/`h_frac_max` change.
- `UtilitySurfacer.order(scored, utility_of) -> list[Scored]` — PORT+FIX. Stable
  sort by `weight × f(utility)`, `f ∈ [util_lo, util_hi]` with `f < 1` permitted
  (DEMOTES on confident-negative utility — the un-cripple of `1+max(0,u)`).
  `enabled=False` ⟹ pure passthrough (Phase-1 inert). `utility_of(eid) is None`
  ⟹ `f == 1.0` (un-confident posterior never moves ranking, §4.7).

---

## 3. Swap seam

This module **sits behind / depends on** ports rather than being one itself, but it
participates in two of the three mandated swap seams:

- **Embedding provider (M02 port).** `recall()` calls `embedder.encode(query) -> Vec`.
  Swapping local↔loopback adapter requires **no change here** — the port returns the
  final d-dim unit-norm value; the pipeline is agnostic to the transport.
- **Vector index / storage (M03 seam).** `recall()` reads `RecallIndex` (a
  read-only `Protocol`: `search`, `is_authoritative`, `__len__`). To swap exhaustive →
  pgvector/Qdrant, M03 supplies a second `RecallIndex` adapter; **this module needs
  no change** because it never sees `add`/`remove` (the Store owns mutation). A second
  adapter must implement positive-cosine `search` over APPROVED rows and return
  `is_authoritative()` honestly (False ⟹ this module refuses, preserving never-flip).

The **C3 ranker is itself a separate component** (§10 keeps Store and ranker as
distinct ports): the default adapter is the dense positive-cosine kNN over the d=256
PCA `value`, ported from `native_source.py` with the GMM/Mahalanobis path and the
`|q·x|` absolute-value ranking **removed** (positive cosine only — an anti-correlated
vector never surfaces, and there is no signed-key sparse variant to confuse it with).

---

## 4. Data owned

**Owns no tables.** It is pure domain logic. It *reads* (does not own):
- the authoritative vector index (M03 Store owns the table + the index lifecycle);
- the `utility` posterior via the injected `utility_of` callable (M-loop owns it);

It *writes through a seam it does not own*:
- the **exposure ledger** (`exposure` table, M03/§3) via `ExposureLedger.record_exposure`
  — `(trace_id, episode_id, recall_margin, injected_ts)`; `task_ref` stays NULL here
  (the producer C10 back-fills it at commit time, §11). This is the move-#6 capture
  point. Write is **fire-and-forget**: a ledger failure logs WARN and the recall result
  is still returned.

**Config keys READ** (`group.field`, M-config; never written):
| Key | Value | Tier | Use |
|---|---|---|---|
| `recall.H_frac_max` | 0.5 | B | gate suppress threshold (§4.2) |
| `recall.recall_top_n` | 10 | A | hits length only — **never** the abstain decision |
| `geometry.beta` | re-tuned (§5-D2) | B | gate softmax sharpness over cosine range |
| `channels.utility_rerank` | False (Phase-1) | C | → `surfacer.enabled` |
| `producer.epsilon_explore` | 0.1 (Phase-2) | A | guardrail-1; MUST be > 0 when surfacer on |
| `index.vector_index_backend` | exhaustive | C | authoritative; `is_authoritative()` True |

---

## 5. Dependencies

| Depends on | Why | MUST NOT know about |
|---|---|---|
| `EmbeddingProvider` (M02 port) | `encode(query) -> Vec` (same chain as capture) | the embedder transport (local vs loopback), `native_dim`, the PCA fit |
| `RecallIndex` view (M03) | positive-cosine kNN over APPROVED | the SQLite schema, `status` filtering, **any index-mutation verb** (no add/remove — the Store is the sole mutator) |
| `NormalizedEntropyGate` (M04-internal) | abstain decision | nothing — pure |
| `UtilitySurfacer` (M04-internal) | weight × f(utility) order | how the posterior is computed/stored (gets a `utility_of` callable) |
| `ExposureLedger` (M03 write seam) | move-#6 exposure capture | the producer, `task_outcomes`, family_scope (those are downstream) |

**Boundary it must not cross:** it must **not** import storage/SQLite, subprocess,
or the producer. The import-linter test (`test_domain_imports_no_adapters`, folded in
from the layered approach's AST walk) fails CI if `hive/domain/recall.py` imports any
`hive/adapters/*`. This is what keeps the read path fake-testable in milliseconds.

---

## 6. Failure-mode logging (per engineering standard; secrets never logged)

| Boundary | Level | Context logged (no text, no vector, no secret) |
|---|---|---|
| embedder load/encode failure | **error** | `agent_id`, exception, "recall→EMPTY_NO_DATA fail-closed" |
| `index.search` raises | **error** | `agent_id`, `len(index)`, exception, "→EMPTY_NO_DATA" |
| `is_authoritative()` False | **error** | `backend`, "ANN index rejected — never-flip guard" (refuse) |
| gate internal failure | **warn** | `n_cands`, `h_frac_max` → fail-closed SUPPRESS (from ported gate) |
| ABSTAIN decision | **info** | `trace_id`, `entropy_norm`, `top_margin`, `n_cands` (the auditable abstain) |
| CONFIDENT decision | **debug** | `trace_id`, `n_hits`, top `sim` (no text/hashes beyond ids) |
| `ledger.record_exposure` raises | **warn** | `trace_id`, `n_rows`, exception, "ledger drop — recall preserved" |
| EMPTY_NO_DATA (empty index) | **debug** | `agent_id`, `len(index)` |

The query text is **never logged**; only `agent_id`, the `trace_id`, counts, and
bounded scalars. `recall_margin`/`entropy_norm` are bounded floats, safe to log.

**Acceptance gates this module owns:**
- §6.1#2 honest abstention AUROC ≈ 0.77 → `test_entropy_gate.test_abstention_auroc`.
- §6.1#3 never-hallucinate / abstain-no-resurrect → `test_recall_pipeline.test_abstain_no_resurrect`.
- §6.1#1 recall@5 ≥ 0.33 → `test_recall_pipeline.test_happy_path_returns_confident_hits`.
- §6.1#5b pending never recallable (this module's half: reads APPROVED-fed index only)
  → `test_recall_pipeline.test_recall_reads_approved_only`.

---

## 7. Port disposition vs §10 map

| Piece | Reference file | Disposition |
|---|---|---|
| Dense ranker (C3) | `serving/sources/native_source.py` | **PORT+SIMPLIFY** — keep positive-cosine resolve-to-full-candidate; **drop** GMM/Mahalanobis (I12 path), **drop** `|q·x|` abs ranking, **drop** per-agent native index (rank over the d=256 PCA `value` in the M03 authoritative index) |
| Abstention gate (C4) | `serving/gate_bundle.py::NormalizedEntropyGate` | **PORT AS-IS** — cleanest port; only re-tune injected `beta`/`H_frac_max` for the dense-cosine range (§5-D2/D3). Keep the BUG-012 normalized-H and BUG-001 no-resurrect posture |
| Surfacer | `serving/service.py:184` (`1+max(0,u)`) | **PORT+FIX** — un-cripple to `weight × f(utility)` with f able to demote; Phase-1 `enabled=False` inert |
| Exposure emit | `ops/telemetry.py::emit_recall_telemetry` + `TelemetrySink` | **PORT+EXTEND** — keep text-free trace_id + (id, margin) capture; route through `ExposureLedger` seam so the M03 `exposure` table (with `task_ref`) is the sink, not the separate `telemetry.db` |
| Index variants | `storage/vector_index.py` (`Exhaustive`/`Native`/`LSH`/`HNSW`) | **DROP** the `|q·x|` Exhaustive + the duplicate Native index here — this module reads the **single** M03 Store-owned authoritative positive-cosine index via the `RecallIndex` port |
| RecencyDecay | `serving/recall_pipeline.py::RecencyDecay` | **PORT (default-OFF only)** — available as the identity multiplier; not wired into ranking in v-min |
| `ConfidenceBundleBuilder` / `NarrowingCascade` | `gate_bundle.py` / `recall_pipeline.py` | **DROP the multi-channel cascade** — v-min has one source + one gate; the bundle collapses to `RecallResult`. (§8.3 re-introduces fusion AFTER the benchmark gate; the cascade `from_flat` dual-gate is deferred — only one gate exists, so CONFIG_DRIFT is closed by passing the frozen `cfg.recall` object by identity.) |

---

## 8. TEST CONTRACT (test-first; full functional coverage)

> Files: `tests/domain/test_recall_pipeline.py`, `tests/domain/test_entropy_gate.py`,
> `tests/domain/test_surfacer.py`. All run against `FakeEmbeddingProvider`,
> `FakeRecallIndex`, `FakeExposureLedger` — hash-speed, no SQLite, no network.

### 8.1 `test_recall_pipeline.py` (happy + every failure mode + every invariant)
| Test | Exact assertion | Failure it catches |
|---|---|---|
| `test_happy_path_returns_confident_hits` | query near a planted gold ⟹ `state==CONFIDENT`, that eid is `hits[0]`; over the held-out pairs recall@5 ≥ 0.33 | §6.1#1 ranker/encode regression |
| `test_abstain_returns_empty_hits` | uniform candidate set ⟹ `state==ABSTAIN`, `hits==()` | gate not firing / hits leaking past abstain |
| `test_abstain_no_resurrect` ★ | gate `suppress=True` ⟹ `hits==()` AND `ledger.record_exposure` never called | §6.1#3 a fallback rescuing a refused query |
| `test_empty_index_is_empty_no_data` | `len(index)==0` ⟹ `state==EMPTY_NO_DATA`, `entropy_norm==0.0`, `hits==()`, no raise | crash on empty store; ABSTAIN vs EMPTY conflation |
| `test_recall_reads_approved_only` | a pending eid fed-but-not-in `scan_approved` index is absent from `hits` | §6.1#5b pending row leaking into recall |
| `test_authoritative_index_required` | `is_authoritative()==False` ⟹ refuse (raise at construct OR EMPTY_NO_DATA + error log) | §4.3 silent ANN flip / approx_threshold trap |
| `test_trace_id_emitted_and_unique` | two calls ⟹ two distinct uuid4 `trace_id`s | missing/duplicate move-#6 join key |
| `test_exposure_ledger_written_with_margin` | CONFIDENT ⟹ `record_exposure` called once with `rows==[(eid, margin)…]`, `ts`, `agent_id` | broken move-#6 exposure capture |
| `test_exposure_not_written_on_abstain` | ABSTAIN/EMPTY ⟹ `record_exposure` NOT called | crediting memories never surfaced |
| `test_recall_top_n_size_only` | changing `recall_top_n` changes `len(hits)` but NOT the abstain decision (gate sees full scored set) | §4.2 "never affects abstain" invariant |
| `test_ledger_failure_never_breaks_recall` | `record_exposure` raises ⟹ CONFIDENT result still returned, WARN logged | telemetry endangering the hot path |

### 8.2 `test_entropy_gate.py` (PORT-AS-IS coverage + the AUROC gate + mutation)
| Test | Exact assertion | Failure it catches |
|---|---|---|
| `test_uniform_high_entropy_suppresses` | N equal sims ⟹ `entropy_norm≈1.0 > H_frac_max` ⟹ `suppress True` | gate math inverted |
| `test_peaked_low_entropy_passes` | one dominant sim ⟹ low `entropy_norm` ⟹ `suppress False` | over-abstention |
| `test_single_candidate_zero_entropy` | `n_eff==1` ⟹ `entropy_norm==0.0` (ln1 guard), `suppress False` | div-by-zero / NaN |
| `test_gate_fail_closed` | malformed input ⟹ `(True,1.0,0.0)` | error-masking (must abstain, never fabricate) |
| `test_abstention_auroc` | sweep `H_frac_max`/`beta`; AUROC at flagging the gate's OWN top-5 misses ≈ 0.77 on held-out pairs, **no contrived masking** (§6.3) | §6.1#2 dishonest abstention; β-carryover-from-sparse (§5-D2) |
| `test_mutation_invert_entropy_comparison` ★ | flip `entropy_norm > h_frac_max`→`<`; `test_uniform_high_entropy_suppresses` MUST go red; restore ⟹ green | §6.4 RULE 2: proves the abstain decision is the tested path |

### 8.3 `test_surfacer.py` (PORT+FIX coverage + mutation)
| Test | Exact assertion | Failure it catches |
|---|---|---|
| `test_surfacer_disabled_is_passthrough` | `enabled=False` ⟹ order byte-identical to weight/sim order | utility leaking into Phase-1 ranking |
| `test_confident_negative_demotes` | confident-negative utility ⟹ `f<1` ⟹ ranked BELOW an un-credited tie | the crippled `1+max(0,u)` that cannot demote (§10 un-cripple) |
| `test_utility_none_is_identity` | `utility_of→None` ⟹ `f==1.0` ⟹ order unchanged | un-confident posterior moving ranking (§4.7) |
| `test_mutation_floor_negative_utility` ★ | re-introduce `max(0.0,u)` floor in f; `test_confident_negative_demotes` MUST go red; restore ⟹ green | §6.4 RULE 2: proves demotion is wired |

### 8.4 Coverage argument (no functional path untested)
- **Happy path:** 8.1 happy + 8.2 peaked-passes + 8.3 passthrough.
- **Every §6 failure mode:** embedder/index raise → `EMPTY_NO_DATA` (covered via the
  fail-closed path exercised by `test_empty_index_is_empty_no_data` + a raising fake in
  `test_ledger_failure_never_breaks_recall`); gate fail-closed (`test_gate_fail_closed`);
  ledger failure (`test_ledger_failure_never_breaks_recall`); ANN refuse
  (`test_authoritative_index_required`).
- **Every §2 invariant:** abstain⟹empty (`test_abstain_returns_empty_hits`); no-resurrect
  (`test_abstain_no_resurrect`); state discrimination (`test_empty_index_is_empty_no_data`);
  entropy ∈ [0,1] (`test_single_candidate_zero_entropy` + `..._uniform...`); unique trace
  (`test_trace_id_emitted_and_unique`); exposure exactly-once on CONFIDENT / never otherwise
  (`..._written_with_margin`, `..._not_written_on_abstain`); top_n size-only
  (`test_recall_top_n_size_only`); approved-only (`test_recall_reads_approved_only`).
- **Two mutation tests** (★) cover the two state-machine/ranker faults RULE 2 mandates:
  the abstain comparison and the demotion floor. A third mutation (drop `is_authoritative`
  guard ⟹ `test_authoritative_index_required` goes red) covers the never-flip trap.
- **Acceptance gates** §6.1 #1/#2/#3/#5b each map to a named test above.

---

## Design review (independent pass)

**Verdict:** STRONG design, NOT YET build-ready. The architecture is genuinely deep and the rubric's best ideas are present and load-bearing: abstain-no-resurrect is made STRUCTURAL (control flow, not prose — APOSD "define errors out of existence" + agent-native "contracts that can't lie"), the approved-only guarantee is DESIGNED OUT via the seam shape ("the Store is the sole index mutator; the pipeline has no index-mutation verb"), and the never-flip ANN trap is killed at the type boundary via is_authoritative() instead of an approx_threshold config value (kills the §4.3 live-default bug structurally). The import-linter boundary test keeps the read path fake-testable in milliseconds — a real agent-navigability win. Ports are correctly classified against §10. Two genuine mutation tests + a third (drop is_authoritative) hit exactly the gate/ranker/state-machine faults RULE 2 mandates. BUT the spec has THREE unresolved contract holes that block sign-off, all of which masquerade as 'PORT AS-IS' when they are in fact behavior changes: (1) the gate's evaluate(sims) signature diverges from the reference's evaluate(cands: list[Candidate]) — the reference reads c.alpha (softmax mass), the spec feeds raw cosine sims, so 'byte-identical code, only beta changes' is FALSE and the softmax-over-sims step is unspecified; (2) per-episode recall_margin written to the exposure ledger is never defined — the gate produces ONE top_margin scalar but record_exposure wants [(eid, margin)...], and §11 splits credit BY recall_margin, so an under-specified margin silently corrupts the move-#6 credit split, the product's differentiated move; (3) the surfacer key is 'weight × f(utility)' but the reference keys on c.alpha — weight (immutable capture weight) and alpha (softmax mass) are different quantities and the Test Contract never plants the weight×f interaction. The Test Contract is well-structured but has real coverage gaps on the swap seam (no test asserts a second RecallIndex adapter passes), the embedder-raise path (claimed covered but no raising-embedder fake is named), and the recall_margin definition (no test pins what margin value lands in the ledger). Fix the three contract holes + the named test gaps and this is build-ready.

**Scores (1–10):**
- design_complexity: 3
- cognitive_load: 4
- information_leakage: 3
- extensibility_fit: 8
- agent_navigability: 8
- contract_enforcement: 6
- test_coverage: 5

**Red flags:**
- [Prose-Only Contract on Tricky Semantics] @ §2.2 NormalizedEntropyGate 'PORT AS-IS … code byte-identical' + §7 disposition table — why: the claim of byte-identical port hides a real signature change (evaluate(cands)→evaluate(sims)) and a missing softmax step; an agent reading 'PORT AS-IS' will copy gate_bundle.py verbatim and feed it raw cosines where it expects Candidate.alpha, producing wrong entropy with no type error — root: obscurity → confident misuse / silent drift.
- [Information Leakage] @ §2.1 recall_margin in the exposure tuple vs §2.2 gate top_margin — why: the meaning of 'margin' is split across the gate (single top_margin scalar) and the ledger (per-episode recall_margin) and §11 (credit-split weight), three modules sharing one under-defined term; changing how margin is computed forces a coordinated edit across gate, pipeline, and producer — root: dependency (shared knowledge of 'what margin means' split apart) → change amplification into the move-#6 credit path.
- [Hard to Describe] @ §2.2 UtilitySurfacer.order 'weight × f(utility), f ∈ [util_lo,util_hi] with f<1 permitted … utility_of(eid) is None ⟹ f==1.0' — why: the f mapping (how a Beta-Bernoulli posterior or a raw utility maps to [0.5,1.5], when CI 'excludes 0' gates it per §4.7, vs §2.2's simpler 'None⟹1.0') needs multiple interacting clauses to be complete and the §4.7 'posterior CI excludes 0' confidence gate is absent from §2.2 entirely — root: obscurity/complex semantics → the surfacer's actual contract is larger than stated; redesign-the-spec candidate, not a prose patch.
- [Missing Feedback Signal] @ §3 swap seam (second RecallIndex adapter) — why: the port-swappability of the index/ranker is asserted but has no test or runtime assertion an agent could use to self-check that a new adapter satisfies the RecallIndex Protocol contract (positive-cosine search, honest is_authoritative); an agent adding a pgvector adapter gets no failing test if it returns absolute-cosine or lies about authority — root: obscurity → errors surface late, swap mandate unverifiable.

**Test gaps:**
- No test enforces the swap seam: no second/alternate RecallIndex fake proving the Protocol is sufficient and a swap needs 'no change here' (§3 claim unverified) — add test_recall_against_alternate_index_adapter.
- No test injects a RAISING embedder despite §6 logging it and §2.1 postcondition 'NEVER raises into the caller' — §8.4's claim that test_empty_index covers it is false (different code path, before index.search) — add test_embedder_failure_is_empty_no_data.
- No test injects a RAISING index.search separately from empty-index — §6 logs 'index.search raises → EMPTY_NO_DATA' but §8.4 folds it into the empty-index test; an empty index never enters the search try-block — add test_index_search_raise_is_empty_no_data.
- No test pins the per-episode recall_margin VALUE written to the ledger (only that record_exposure is 'called once with rows==[(eid, margin)…]'); the assertion accepts any margin, so a wrong/constant margin (e.g. always top_margin, or always 1.0) passes — this silently corrupts the §11 credit split — add an exact-value assertion.
- No test varies episode weight independently of utility in the surfacer, so 'weight × f' is indistinguishable from 'alpha × f' or 'sim × f' — the base multiplier is unverified (Contract Hole #3).
- No test for the CONFIDENT iff boundary (gate-passes + exactly-one-candidate ⟹ CONFIDENT with that hit; and the reverse directions) — the §2.1 biconditional is only half-covered.
- No mutation test on the exposure exactly-once-on-CONFIDENT / never-otherwise wiring (the move-#6 capture point) — RULE 2 mandates mutation for 'every credit path'; the spec lists two ★ mutations (entropy compare, demotion floor) + a third (is_authoritative) but NONE on record_exposure gating, which is THE move-#6 surface — add test_mutation_exposure_on_abstain (force record_exposure to fire on ABSTAIN ⟹ test_exposure_not_written_on_abstain goes red).
- AUROC gate test (test_abstention_auroc) targets ≈0.77 but the spec gives no tolerance band or held-out corpus fixture definition; an AUROC assertion with no ± band either flakes or is vacuous — pin the band (e.g. AUROC ≥ 0.72) and name the fixture.
- test_recall_reads_approved_only is structurally weak: it hand-builds a FakeRecallIndex without the pending eid, so it tests the FAKE, not the real Store→scan_approved→index wiring; the actual approved-only guarantee lives in M03 (Store as sole mutator). The M04 test can at best assert the pipeline has no index-mutation verb — make that the explicit assertion (e.g. assert not hasattr(pipeline, 'add'); assert RecallIndex Protocol has no add/remove), and cross-reference the M03 test that owns the real guarantee, so the §6.1#5b coverage is honest about which half it proves.

**Must-fix:**
- CONTRACT HOLE #1 — gate signature divergence mislabeled as PORT-AS-IS. §7 and §2.2 claim NormalizedEntropyGate is 'PORT AS-IS, code byte-identical, only beta/h_frac_max change.' FALSE: the reference gate_bundle.py:91 is evaluate(cands: list) reading float(c.alpha) (softmax mass already on Candidate) via _softmax_mass(); the M04 surface is evaluate(sims) over raw cosine ∈[-1,1]. Converting cosines to a probability distribution requires a softmax with the re-tuned beta (geometry.beta, §5-D2) — that step is NEW code, not a port, and is entirely unspecified in §2.2. Specify: the exact sims→mass transform (softmax(beta·sim)?), where beta enters, and that _softmax_mass's non-positive-floor + degenerate-uniform fallback are preserved. Until this is pinned the gate's whole abstain math is undefined.
- CONTRACT HOLE #2 — per-episode recall_margin is undefined. §2.1 emits ledger.record_exposure(trace_id, agent_id, [(eid, margin)…], ts) and §4/§11 split credit BY recall_margin, but the gate produces a SINGLE top_margin scalar (alpha[0]-alpha[1]). There is no defined per-episode margin. §3 schema says recall_margin REAL per (trace_id, episode_id). Define exactly what per-hit margin is written (the hit's own softmax mass? its gap to the next? its rank?) — this value drives the move-#6 credit split, so an unspecified or wrong margin silently biases utility forever. Add a test that pins the exact margin value per row.
- CONTRACT HOLE #3 — surfacer key 'weight × f(utility)' diverges from the ported 'alpha × mult' and is untested. service.py:184 keys on float(c.alpha) (softmax mass); M04 §2.2 keys on 'weight × f(utility)' where weight is the IMMUTABLE capture weight (§3: 'weight is immutable post-capture'), a different quantity than alpha. The Test Contract's test_confident_negative_demotes only checks f<1 demotes 'below an un-credited tie' — it never plants two hits with DIFFERENT weights to prove weight is the base multiplier and not alpha/sim. Specify which scalar is the base (weight vs the ranker's cosine/alpha) and add a test that varies weight independently of utility.
- TEST GAP (swap seam) — the SWAPPABILITY MANDATE is asserted in prose but NO test enforces it. §3 claims a second RecallIndex adapter swaps with 'no change here,' but §8 has no test instantiating the pipeline against a second/alternate RecallIndex fake to prove the Protocol is sufficient. Add test_recall_against_alternate_index_adapter (a second FakeRecallIndex with a different internal representation yields identical RecallResult) — without it the port-swappability is a prose-only contract (agent-native 'Prose-Only Contract' red flag) and the mandate is unverified.
- TEST GAP (embedder-raise path) — §6 logs 'embedder load/encode failure → EMPTY_NO_DATA fail-closed' and §8.4 CLAIMS it is 'covered via the fail-closed path,' but NO named test injects a raising embedder. test_empty_index_is_empty_no_data exercises an EMPTY index, not an embedder exception — these are different code paths (the embedder raise happens BEFORE index.search). Add test_embedder_failure_is_empty_no_data with a FakeEmbeddingProvider that raises, asserting state==EMPTY_NO_DATA, no raise into caller, error logged, and ledger NOT called. The 'NEVER raises into the caller' postcondition (§2.1) is currently unenforced for its primary trigger.
- TEST GAP (CONFIDENT requires gate non-suppress AND ≥1 hit) — the §2.1 postcondition 'state==CONFIDENT ⟺ len(hits)>=1 AND gate did not suppress' has no test for the boundary case: gate passes (suppress False) but scored set is non-empty yet all candidates resolve away (e.g. a hit whose surfacer/resolve yields zero usable hits). Add a test pinning that a non-suppress gate over a 1-candidate set yields CONFIDENT with exactly that hit, and that the iff is bidirectional (no CONFIDENT with empty hits, no ABSTAIN with non-empty hits).
