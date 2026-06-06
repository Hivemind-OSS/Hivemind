# M10 Eval membrane  (eval)

**One-line:** The dev-time acceptance/decision harness: a real-corpus retrieval+abstention oracle, capture→replay regression gate, the §6.1 acceptance gates (incl. abstention AUROC and the §6.6 move-#6 keystone control-arm experiment), with bootstrap-CI significance and §6.3 de-confounding baked in — never imported by the runtime server.
**Port disposition:** PORT (per §10 map: "Eval membrane (C8) EXISTS → PORT as-is, dev-time"). Concretely a PORT+EXTEND, naming the reference files. PORT-AS-IS: research/metrics_ir.py (all 6 pure metrics: precision_at_k, recall_at_k, mrr, ndcg_at_k, jaccard_at_k, the _dedup/_topk_unique helpers — lift verbatim, contracts already correct incl. dedup-before-truncate, k<=0 raises, empty-relevant→0.0). PORT+SIMPLIFY: research/eval_membrane.py — keep load_longmemeval/LMEQuestion/LMEResult, run_longmemeval (the per-question hermetic-store oracle + the scored/abstention/skipped closure invariant), the LoCoMo adapter (locomo_to_lme_rows/load_locomo), export_baseline/replay/ReplayReport (capture→replay), and admit() (the eval-gated admission decision). DROP the MemoryAgentBench Selective-Forgetting scorer (run_factconsolidation/SFScore/SFResult/_score_one_chain — depends on supersession/prune/tombstone, which §10 drops for the episodic MVP) and the AbilityScore/ABILITY_OF_TYPE LongMemEval ability rollup (LME-paper-specific, not a v-min gate). REWIRE the hermetic-store builder _build_eval_service to the new hive ports (EpisodeStore + the recall pipeline) instead of CortexConfig.from_flat/AgentCortexService, threading the v-min geometry defaults (st_projection_head=pca, d=256, beta-retuned, embedder=st/bge-small, H_frac_max=0.5). BUILD-NEW (absent from the entire tree — verified by grep: only hits were in dropped federation/): metrics_ir.abstention_auroc (the §6.1 #2 AUROC≈0.77 gate has NO scoring function in the reference) and metrics_ir.bootstrap_ci (the §6.2 "bootstrap CI on every delta, ship only on CI-significant" mandate has NO helper in the reference); plus the §6.6 keystone harness (run_keystone_eval with the utility-off/recency/frequency control arms, pre-committed kill criterion, non-saturating-accrual + within-family-transfer checks) which has no analog (the closest, research/benchmark.py, is a synthetic-topic SLO bench, not a control-arm causal experiment).

---

# M10 — Eval membrane (C8): dev-time acceptance/decision harness

> **Provenance:** ports `research/eval_membrane.py` (795 LOC) + `research/metrics_ir.py`
> (135 LOC) from the `cls_memory` tree, per the §10 map row *"Eval membrane (C8) EXISTS →
> PORT as-is (dev-time)."* Every signature, invariant, and de-confounding rule below is
> grounded in the spec (§6 in full, §11 join, §12 phasing) or a named reference file.

---

## 1. Responsibility (one deep module behind a narrow surface)

The eval membrane is the **single dev-time gate every change must pass to ship**, and the
**experiment harness the MVP exists to run** (§6.6). It is **not a runtime component** —
the shipped server (`hive.core` / `hive.adapters` / `hive.serving`) never imports it
(C8 row in §2: *"Dev-time only, not a runtime component"*).

The meaningful, hidden work behind a small set of functions:

1. **Labeled retrieval+abstention oracle** (`run_longmemeval`): for each natural
   query→gold pair it builds a **fresh hermetic store**, writes that question's haystack,
   recalls, and scores the ranking — with two contracts on one surface: the **ranker pass**
   (gate OFF, measures *"is the gold in top-k"* → recall@5, the §6.1 #1 gate) and the
   **abstention pass** (gate ON at the production floor, measures the **EMPTY refusal** →
   the §6.1 #2 honest-abstention gate). The per-question store is what makes
   *"no haystack leaks between questions"* (§6.3) a tested invariant, not a hope.
2. **Significance machinery** (`bootstrap_ci`, `abstention_auroc`): turns point estimates
   into **CI-significant** ship/no-ship decisions (§6.2) and scores the abstention gate's
   **AUROC at flagging its own top-5 misses** (§6.1 #2, target ≈0.77). *Both are
   BUILD-NEW — neither exists anywhere in the reference tree* (verified: the only
   `auroc|bootstrap` grep hits live in the dropped `federation/`).
3. **Capture→replay regression gate** (`export_baseline`/`replay`/`admit`): deterministic,
   LLM-free — *"did my change move retrieval, and on which queries?"* — with `admit()`
   **failing closed on zero signal** (an empty baseline never auto-passes).
4. **The §6.6 keystone experiment** (`run_keystone_eval`, BUILD-NEW): the move-#6
   control-arm causal test — utility-weighted recall vs **utility-off / recency /
   frequency** — applying the **pre-committed kill criterion**. This is the one
   experiment the whole MVP is built to run; a clean *negative* here kills six unbuilt
   moves cheaply (§7).
5. **§6.3 de-confounding rails** (`strip_stamped_tokens`, `assert_clean_store`,
   `assert_exact_path`, BUILD-NEW helpers): the harness must *guard against the prior
   eval artifacts that faked results* — stamped-token label leak, the `approx_threshold`
   ANN-path-engaging-at-N>10k recall→0 trap, and re-consolidating a persisted store.

The narrow surface (≈12 public functions) hides a large, error-prone body of
hermetic-store construction, metric arithmetic, statistical resampling, and
artifact-guarding. That asymmetry is the point — a deep module.

---

## 2. Public surface + ENFORCED contract

Full signatures in `interface_block`. Contract highlights and the invariants enforced:

### 2.1 Pure metrics (`hive/research/metrics_ir.py`) — PORT-AS-IS
All six are **pure, side-effect-free** maps `(retrieved, relevant) → float`. Enforced
contracts (already correct in the reference; ported verbatim):
- **`k <= 0` RAISES `ValueError`** — never returns 0.0. *Designed-out precondition:* a
  zero here would mask a caller bug (the BUG-001/002/003 lesson). This is the right
  call — keep it a raise, not a doc.
- **dedup-before-truncate**: `_topk_unique` de-duplicates the *full* ranked list then cuts
  at k, so a leading duplicate can never push a distinct relevant item out of the window.
- **empty `relevant` → 0.0** (undefined; caller skips); **`ndcg` IDCG==0 → 0.0**;
  **`jaccard` both-empty → 1.0, one-empty → 0.0**.
- Units: all return a float in `[0,1]`. Complexity: each `O(R + |relevant|)` time, `O(R)`
  space (documented per the global standard).

### 2.2 BUILD-NEW scoring (the §6 gates have no scorer in the tree)
- **`abstention_auroc(scores, is_miss) -> float`** — AUROC via the **Mann-Whitney
  rank-sum identity** = P(score(miss) < score(hit)), ties → 0.5. `scores` is the gate's
  confidence proxy (HIGHER = more confident; the natural choice is `1 - H/ln(N_eff)` so a
  *high* value means *less likely to abstain*); `is_miss[i]` is the event the gate *should*
  abstain on (gold not in top-k). **Degenerate inputs RAISE** (all-miss / all-hit / length
  mismatch) — a 0.5 default would mask an untestable metric, the exact ERROR_MASKING class
  `metrics_ir`'s own docstring forbids. Maps the §6.1 #2 gate (≈0.77).
- **`bootstrap_ci(deltas, *, n_boot=10_000, alpha=0.05, seed=0) -> (point, lo, hi)`** —
  percentile bootstrap on a **paired per-query delta** vector. **CI-significant iff
  `lo > 0` (improvement) or `hi < 0` (regression)** — the §6.2 ship rule (*"a change ships
  only on a CI-significant improvement, not a point estimate"*). **Deterministic** for a
  fixed seed; **empty deltas RAISE**.

### 2.3 The oracle (`run_longmemeval`) — PORT+SIMPLIFY, rewired to hive ports
Narrow surface, rich enforced contract:
- **Per-question hermetic store** (unique temp DB, cleared backend cache) — the leakage
  invariant `test_leakage_each_question_uses_a_fresh_store` asserts on.
- **Two-contract routing**, with the **closure invariant ASSERTED**:
  `n (scored) + abstention.n + Σskipped == total_loaded`. Every loaded question lands in
  exactly one bucket; nothing silently vanishes (anti-ERROR_MASKING).
  - `qtype=="abstention"` → gate **ON** at `h_frac_max` (0.5), ternary scoring: EMPTY recall
    ⇒ `correct` refusal, a confident surface ⇒ `fail` (never pass-by-default).
  - empty `answer_doc_ids` (non-abstention) ⇒ `skipped["no_relevant"]` (counted, not a 0).
  - otherwise ⇒ scored on `precision/recall/mrr/ndcg @k`.
- **Ranker pass keeps the gate OFF** (a retrieval benchmark measures the *ranker*, not the
  inject-confidence decision) — this two-contract split is load-bearing and must survive
  the port.
- `recall_top_n >= k` so P@k/R@k for k>8 are **not silently capped** by a read default.
- **retrieval-only**; an answer-generation arg **RAISES `NotImplementedError`** (LLM-free
  load-bearing path; honesty over a silent no-op).

### 2.4 Capture→replay + admission (PORT)
- `export_baseline` → NDJSON `{query_hash, query, retrieved[:k], latency_ms}` (a dev/CI
  artifact that *does* carry query text — distinct from the §6 text-free telemetry sink).
- `replay` → `ReplayReport(mean_jaccard, top1_stability, latency_delta_ms, n, regressions)`;
  empty/unreplayable baseline → `ReplayReport(1,1,0,0,[])`.
- **`admit` FAILS CLOSED**: `report.n == 0` ⇒ `False` (a gate green on zero signal is
  decorative — the single most important guard). Admit iff `n>0` AND
  `mean_jaccard >= champion_score + min_gain` AND `len(regressions) <= max_regressions`.

### 2.5 Keystone (`run_keystone_eval`) — BUILD-NEW, §6.6
- Returns a **frozen** `KeystoneResult` with the four `ArmResult`s and the decision flags.
- **Win** = utility beats **BOTH recency AND frequency** (`bootstrap_ci.lo > 0` on each
  paired delta) **AND** `non_saturating` **AND** `within_family_transfer`. Beating *nothing*
  (utility-off) is **not** a win — that would be trivial recency, not learning.
- **Pre-committed kill**: utility fails to beat recency CI-significantly ⇒ `killed=True`
  ⇒ moves #1,#2,#4,#5,#7 are NOT built (§6.6 / §7).
- **Phase-2 readiness pre-gate** (§12): `settled < n_settled_min` or distinct
  memories `< m_memories_min` ⇒ `inconclusive=True` (NOT `killed`) — *"inconclusive ≠
  negative"* (§6.6); the remedy is widen the family / extend the window.

---

## 3. Swap seam

This module **is not behind a port and exposes no port** — it is a dev-time consumer that
sits *above* the three runtime ports, reaching them only through the same public surface
the server uses. Concretely it depends on a `svc`/recall surface that yields, per query,
the ranked candidate `(episode_id, score)` list and resolves an `episode_id` to its
`content_hash` (the cross-run-stable identifier capture→replay compares).

Because it consumes the recall surface and the `EpisodeStore.get` read — **not** any
embedder/index/producer adapter directly — the three mandated swaps (embedder, vector
index, outcome producer) are **transparent** to it: swapping `embedding.transport=local`
→ `loopback`, or `index.vector_index_backend` to a future adapter, changes nothing in the
membrane. The membrane **measures** the swap (run the oracle before/after, `bootstrap_ci`
the delta); it never participates in it. This is the correct relationship — the eval
harness must be able to score *any* configuration without being recompiled, which it is.

The one config it asserts on (`assert_exact_path`) is `index.vector_index_backend` being
**authoritative-exhaustive** — guarding the §4.3 `approx_threshold` trap so a measurement
is never silently run on the ANN path (recall→0). That is a *guard*, not a swap.

---

## 4. Data owned

**None.** The membrane owns **no tables and no blobs** — it is read-mostly over hermetic
*temporary* stores it builds and tears down per question (`tempfile.TemporaryDirectory`),
plus dev/CI **NDJSON baseline artifacts** on the local filesystem (not in the DB, not in
any backup-kept path). It writes only to those temp stores and those artifact files.

**Config keys it READS (never writes)** — to construct the hermetic store at the v-min
geometry: `geometry.d` (256), `geometry.beta` (retuned for dense-cosine, §5-D2),
`embedding.st_projection_head` (pca), `embedding.st_model_name` (BAAI/bge-small-en-v1.5),
`embedding.embedder` (st), `recall.H_frac_max` (0.5), `recall.recall_top_n` (≥ k),
`index.vector_index_backend` (exhaustive/authoritative).

---

## 5. Dependencies (and the boundary it MUST NOT cross)

**Depends on:**
- `hive/research/metrics_ir.py` (its own pure arithmetic) — no further deps.
- The **public recall surface + `EpisodeStore.get`** read, via the hermetic-store builder.
  It reaches the system the way an agent would: write → recall → resolve content-hash.
- The config object (read-only) to set the hermetic store's geometry.
- `numpy` (bootstrap resampling) and stdlib (`json`, `tempfile`, `statistics`, `math`,
  `time`, `logging`).

**It MUST NOT know about** (the boundary — named):
- **The runtime server's internals.** The fence is *directional and structural*:
  **`hive.core` / `hive.adapters` / `hive.serving` MUST NOT import `hive.research`.** The
  membrane may import *down* into the public surface; the runtime must never import *up*
  into the membrane. Enforced by the **M0 AST import-linter** (`test_research_not_imported`),
  the same compiler-grade enforcement folded in from the architecture decision — ported
  from the reference's `scripts/check_layers.py` discipline (*"core/ must never import
  research/"*, `research/__init__.py`).
- **The outcome producer's git internals (C10).** The keystone consumes *already-credited*
  `(episode, family)` posteriors and family-scoped task-success — it does not shell out to
  git or re-implement the §11 join. The producer writes outcomes; the membrane scores their
  downstream effect on recall.
- **Secrets / PII** — never written to a baseline artifact or a log (only `query_hash`,
  content-hashes, and metric floats; the baseline's plaintext `query` is dev-corpus text,
  never store secrets).

---

## 6. Failure-mode logging (per the engineering standard; secrets never logged)

| Boundary | Level | Context logged |
|---|---|---|
| Unparseable JSONL line in `load_longmemeval` | **WARN** | `lineno` (skip-and-continue, not abort) |
| `run_longmemeval` closure-invariant breach | **ERROR** → `AssertionError` | `scored / abstention.n / Σskipped / total_loaded` (a question vanished — a real bug) |
| Abstention-pass scoring | **DEBUG** | `question_id`, `source` (EMPTY vs surfaced), ternary outcome |
| `replay`: baseline entry missing `query` (text-free) | **WARN** | *"cannot re-run"* — counted, not silently stable |
| `admit` fails closed on `n==0` | **WARN** | `champion_baseline_path` (anti-ERROR_MASKING) |
| `admit` reject (mean_jaccard / regressions) | **INFO** | the two scores + thresholds (auditable decision) |
| `abstention_auroc` / `bootstrap_ci` degenerate input | **ERROR** → `ValueError` | input length / class balance (an untestable metric refused, not masked) |
| `assert_exact_path` trips (ANN/non-authoritative) | **ERROR** → `ValueError` | configured backend (the §4.3 trap, eval artifact (a)) |
| `assert_clean_store` trips (non-empty) | **ERROR** → `ValueError` | live episode count (eval artifact (c)) |
| Hermetic store build (embedder load / SQLite I/O) | **ERROR** | exception + db_path (boundary failure per global standard) |
| `run_keystone_eval` Phase-2-readiness short | **WARN** | `settled`, `distinct_memories`, the minima ⇒ `inconclusive` (not a negative) |
| Keystone kill criterion fires | **INFO** | the recency-delta CI ⇒ `killed=True` (the pre-committed decision, audit-logged) |

Structured JSON logging, `INFO` default (§4.6). No secret/PII fields; content is
referenced by hash.

---

## 7. Port disposition vs the §10 map

§10 row: **"Eval membrane (C8) EXISTS → `research/eval_membrane.py`,
`research/metrics_ir.py`, `serving/cli/commands/eval.py` → PORT as-is (dev-time)."**
Refined to a precise per-symbol disposition:

| Symbol / area | Disposition | Reference |
|---|---|---|
| All 6 pure metrics + `_dedup`/`_topk_unique` | **PORT-AS-IS** | `research/metrics_ir.py` |
| `LMEQuestion`, `LMEResult`, `AbstentionScore`, `load_longmemeval`, `run_longmemeval` (oracle + closure invariant) | **PORT+SIMPLIFY** (rewire `_build_eval_service` to hive ports; thread v-min geometry; drop the `AbilityScore`/`ABILITY_OF_TYPE` LME-paper rollup) | `research/eval_membrane.py:97-447` |
| `locomo_to_lme_rows`, `load_locomo` | **PORT** | `eval_membrane.py:452-512` |
| `export_baseline`, `replay`, `ReplayReport`, `admit` | **PORT** | `eval_membrane.py:629-795` |
| `run_factconsolidation` / `SFScore` / `SFResult` / `_score_one_chain` (Selective-Forgetting) | **DROP** — depends on supersession/prune/tombstone, which §10 drops for the episodic MVP | `eval_membrane.py:515-624` |
| `abstention_auroc` | **BUILD-NEW** — §6.1 #2 AUROC has no scorer in the tree | — (grep-verified absent) |
| `bootstrap_ci` | **BUILD-NEW** — §6.2 CI-significance has no helper in the tree | — (grep-verified absent) |
| `run_keystone_eval` / `KeystoneResult` / `ArmResult` | **BUILD-NEW** — §6.6 control-arm experiment; closest reference (`research/benchmark.py`) is a synthetic-topic SLO bench, not a control-arm causal test | (benchmark.py for the harness idiom only) |
| `strip_stamped_tokens`, `assert_clean_store`, `assert_exact_path` | **BUILD-NEW** — §6.3 de-confounding rails | — |

Net: ~80% lifts (metrics + oracle + replay + admit), one drop (Selective-Forgetting),
and four BUILD-NEW pieces that are exactly the §6 gates the reference never scored.

---

## 8. TEST CONTRACT (first-class, written test-first)

Full list in `test_contract`. Coverage map (a reviewer can say *no functional path is
untested*):

**Happy path:** `test_run_longmemeval_scores_and_finds_gold`,
`test_admit_identical_candidate_passes`, `test_replay_identical_store_is_perfectly_stable`,
`test_keystone_win_requires_beating_recency_AND_frequency`,
`test_auroc_perfect_separation_is_one`, `test_bootstrap_ci_significant_improvement`.

**Every §6 invariant → its test:**
- §6.1 #1 recall@5 ≥ 0.33 → measured by `run_longmemeval` (oracle wiring proven by
  `test_run_longmemeval_scores_and_finds_gold`, `..._topk_above_8_not_capped`).
- §6.1 #2 abstention AUROC ≈ 0.77 → `test_auroc_target_band_on_fixture` (band [0.70,0.84]).
- §6.1 #3 never-hallucinate / EMPTY → `test_abstention_bucket_gate_enabled_ternary`,
  `test_abstention_confident_surface_is_fail`.
- §6.2 CI-significant ship rule → `test_bootstrap_ci_noise_not_significant`,
  `..._significant_improvement`, `..._deterministic_seed`.
- §6.3 de-confounding → `test_leakage_each_question_uses_a_fresh_store`,
  `test_strip_stamped_tokens_removes_label_leak`, `test_assert_exact_path_raises_on_ann`,
  `test_assert_clean_store_raises_on_nonempty`, `test_locomo_category5_routes_to_abstention`,
  `test_run_longmemeval_skips_empty_relevant`.
- §6.6 keystone → `test_keystone_kill_criterion_fires`,
  `..._sparse_credit_is_inconclusive_not_killed`, `..._non_saturating_required`,
  `..._within_family_transfer_required`, `..._lift_must_trace_to_clawback`.
- Accounting closure → `test_closure_invariant_accounts_every_question`.
- Structural fence (this module's defining invariant) →
  `test_research_not_imported_by_runtime` (M0 AST import-linter).

**Every §6 failure-mode (section 6 above) → a test:** unparseable JSONL skip
(`test_loader_normalizes_aliases` covers the tolerant loader), `k<=0` raises
(`test_*_rejects_nonpositive_k`), empty-relevant 0.0 (`..._empty_relevant_is_zero`),
degenerate AUROC/bootstrap raise (`test_auroc_degenerate_raises`,
`test_auroc_len_mismatch_raises`, `test_bootstrap_ci_empty_raises`), admit fails-closed
(`test_admit_zero_signal_fails_closed`), replay empty baseline
(`test_replay_empty_baseline_reports_zero_queries`), retrieval-only raise
(`test_retrieval_only_false_raises`).

### Acceptance-gate → test mapping (§6.1 / §6.2 / §6.6)
| Gate | Proving test |
|---|---|
| §6.1 #2 honest abstention AUROC ≈0.77 | `test_auroc_target_band_on_fixture` |
| §6.1 #3 never-hallucinate (EMPTY, no resurrect) | `test_abstention_bucket_gate_enabled_ternary` |
| §6.2 ship only on CI-significant delta | `test_bootstrap_ci_noise_not_significant` |
| §6.3 hermetic / no leakage | `test_leakage_each_question_uses_a_fresh_store` |
| §6.6 pre-committed kill criterion | `test_keystone_kill_criterion_fires` |
| §6.6 lift must trace to ungameable clawback | `test_keystone_lift_must_trace_to_clawback` |
| §12 Phase-2 readiness (sparse ≠ negative) | `test_keystone_sparse_credit_is_inconclusive_not_killed` |

### MUTATION TEST (RULE 2) — mandatory, two faults
1. **Fault:** in `run_longmemeval`, build the abstention-pass service with the gate
   **disabled** (`h_frac_max=1.0` instead of the caller's 0.5) — i.e. never abstain.
   **Red:** `test_abstention_bucket_gate_enabled_ternary` (a false-premise question now
   surfaces confidently → `abstention.fail` not `.correct`). **Restore** the caller's
   `h_frac_max` → **green**. Proves the §6.1 #2/#3 abstention gate is *actually exercised*
   in the one pass that tests it.
2. **Fault (metrics):** invert the rank-sum direction in `abstention_auroc` (count
   `score(miss) > score(hit)`). **Red:** `test_auroc_perfect_separation_is_one` (1.0 → 0.0)
   and `test_auroc_target_band_on_fixture`. **Restore** → **green**. Proves the AUROC
   scorer is load-bearing, not decorative.

Both faults map to the §6.4 mandate: *"A gate whose test still passes when broken is not
tested."* The suite is green after restoration.


---

## Design review (independent pass)

**Verdict:** CONDITIONALLY BUILD-READY for the ~80% PORT surface (metrics_ir, run_longmemeval, export_baseline/replay/admit) — those signatures, invariants, and tests are verbatim-grounded in the reference and the existing tests/test_eval_membrane.py, and the design is a genuinely DEEP module (≈12 public functions hiding hermetic-store construction, statistical resampling, and artifact-guarding behind a narrow surface, APOSD #4). NOT yet sign-off-ready for the four BUILD-NEW surfaces (run_keystone_eval, abstention_auroc, bootstrap_ci, the §6.3 rails): grep-verified, ZERO of {keystone, non_saturating, within_family, inconclusive, clawback, auroc, bootstrap} exist anywhere in the reference tree, so the spec's most consequential surface — the §6.6 control-arm causal test the entire MVP exists to run — is also its least operationally defined. Its win/kill flags are asserted on by tests but never computed by a stated function (Prose-Only Contract on Tricky Semantics / Hard to Describe), and the harness has no defined way to fixture the C10-sourced (episode, family) posteriors the keystone consumes. Score split: PORT surface is an 8; BUILD-NEW surface is a 4; blended to the dimension scores below. Resolve the must_fix items and the keystone half reaches the same bar as the ported half.

**Scores (1–10):**
- design_complexity: 4
- cognitive_load: 5
- information_leakage: 3
- extensibility_fit: 7
- agent_navigability: 7
- contract_enforcement: 6
- test_coverage: 6

**Red flags:**
- Hard to Describe @ run_keystone_eval win-criteria (non_saturating, within_family_transfer) — these named flags drive ship/kill decisions but require a paragraph of undefined semantics each to specify; per APOSD red-flags, long-required-documentation-with-no-type signals the THING is too complex/underspecified, not the prose — root: obscurity → unknown-unknowns at build time (the keystone is the highest-stakes surface and the least defined).
- Prose-Only Contract on Tricky Semantics @ §2.5 keystone flags + §6.3 rails (strip_stamped_tokens token-set) — non-trivial decision predicates and a label-strip transform stated only in prose, with tests asserted but no enforced type/regex/oracle backing them; root: obscurity → silent drift, the agent-native 'Missing Feedback Signal' (a test can't go red against an undefined transform).
- Latent contract bug @ admit (eval_membrane.py:779, restated in spec §2.4) — mean_jaccard >= champion_score(=1.0) + min_gain makes any positive min_gain unreachable since Jaccard<=1.0; the docstring's 'strict improvement' claim is arithmetically impossible against a 1.0 self-consistency champion; root: dependency on a confused semantic (champion_score conflates 'candidate floor' with 'champion self-jaccard') → a dead config lever that looks live.
- Producer-Consumer gap @ run_longmemeval → abstention_auroc — the oracle emits a ternary EMPTY/surface outcome but abstention_auroc consumes a continuous 1 - H/ln(N_eff) confidence the oracle never produces; root: dependency split across two surfaces with no wiring → the §6.1 #2 AUROC gate is proven only on a hand-built fixture, never on live oracle output (information that should live in one flow is split).
- Missing Feedback Signal @ keystone arm-input fixturing — §5 fences off C10 git internals (correct) but leaves NO stated seam for injecting the four arms' (episode, family) posteriors into a hermetic test; root: dependency (the keystone needs credited posteriors it is forbidden to compute) with no port named → the keystone is untestable in principle until the fixture seam is specified, which the swap-seam claim in §3 papers over.

**Test gaps:**
- covered above in test_gaps array

**Must-fix:**
- KEYSTONE OPERATIONAL DEFINITION (design, blocking): run_keystone_eval asserts win = utility beats recency AND frequency AND non_saturating AND within_family_transfer, but non_saturating and within_family_transfer have NO stated computation anywhere in the spec or reference (grep-verified absent). Name the exact metric for each: what input vector feeds non_saturating (utility-curve slope over what window? threshold?), and how within_family_transfer is measured (held-out-task recall lift inside the credited family vs. a control family?). Without these, the four keystone tests (test_keystone_non_saturating_required, _within_family_transfer_required) cannot be written test-first — they assert against an undefined oracle. This is the single largest gap: it is the §6.6 keystone, the experiment the whole MVP is built to run.
- KEYSTONE ARM-INPUT FIXTURING (design+test, blocking): §5 correctly fences the membrane from C10 git internals — the keystone consumes 'already-credited (episode, family) posteriors' — but the spec never says HOW the harness injects those posteriors into the four arms (utility / utility-off / recency / frequency) for a hermetic test. The four arms differ only in their ranking signal; the spec must state the seam (a config-selected scoring mode on the recall surface, or a posterior-fixture file the hermetic store loads). Until that seam is named, test_keystone_kill_criterion_fires and test_keystone_lift_must_trace_to_clawback have no constructable fixture and the keystone is untestable in principle. This couples directly to the swap-seam claim in §3.
- ADMIT min_gain CONTRACT IS LATENTLY BROKEN (design, blocking): the ported admit rule (eval_membrane.py:779) is mean_jaccard >= champion_score + min_gain with champion_score default 1.0. Since Jaccard in [0,1], ANY min_gain > 0 makes the pass-branch mathematically unreachable (requires jaccard > 1.0). The reference docstring claims 'strict-improvement (> champion)' but the arithmetic cannot express strict improvement against a self-consistency champion of 1.0 — a candidate is being asked to be MORE self-consistent with the champion than the champion is with itself. The spec §2.4 restates this rule verbatim without flagging it. Either redefine champion_score to be the candidate's floor (not the champion's self-jaccard) or document that min_gain is dead under the default and the only live rejection lever is max_regressions. Add test_admit_positive_min_gain_is_reachable_or_documented_dead.
- AUROC SCORE-DIRECTION CONTRACT MUST BE TYPE/ASSERT-ENFORCED (test+design): §2.2 states scores is a confidence proxy where HIGHER = less likely to abstain and the natural choice is 1 - H/ln(N_eff). But run_longmemeval (the only producer of is_miss events) computes EMPTY/surfaced ternary outcomes, not a continuous 1 - H/ln(N_eff) score — the spec never wires a per-question continuous confidence out of the oracle into abstention_auroc. The §6.1 #2 AUROC≈0.77 gate therefore has a producer-consumer gap: test_auroc_target_band_on_fixture runs on a hand-built fixture, never on oracle output, so the gate that the MVP ships on is never proven end-to-end. Add test_run_longmemeval_emits_continuous_abstention_scores feeding abstention_auroc, or state explicitly that the AUROC is fixture-only and the live wiring is a separate (named) module.
- DE-CONFOUNDING RAILS ARE PROSE-ONLY (test, blocking): strip_stamped_tokens, assert_clean_store, assert_exact_path are BUILD-NEW with tests named but no stated signature or token-pattern. strip_stamped_tokens must define WHICH stamped tokens it strips (the prior eval-artifact label leak (a)) — test_strip_stamped_tokens_removes_label_leak cannot be written without the regex/token-set being part of the contract. Pin the exact stamp format the rail removes; otherwise the test asserts on an undefined transform.
