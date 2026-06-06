# M08 Utility loop  (loop)

**One-line:** A pure batch attributor that joins exposure × verified git outcome, splits credit across co-injected memories by recall_margin, updates a per-(episode_id, family_scope) Beta-Bernoulli posterior in a SEPARATE versioned table, and exposes a confident-CI-gated, ε-randomized utility multiplier f(utility) ∈ [0.5, 1.5] that the C3 surfacer applies ONLY in Phase 2 — never mutating episodes.weight, never crediting on an unverified or agent-self-reported signal.
**Port disposition:** PORT+SIMPLIFY federation/controller.py apply_outcomes_from_sink / apply_outcome (the watermarked, no-double-credit drain), with three hard changes: (1) MOVE the drain OFF service.consolidate()/consolidation timer ONTO the producer tick (C10) — Fix #1 / §4.4; (2) REPLACE the controller's `reconsolidate(... weight += alpha_u·utility)` mechanic — which mutates episodes.weight, FORBIDDEN by §3 ("weight immutable post-capture; loop never writes it") — with a write to the BUILD-NEW `utility(wins,losses)` Beta-Bernoulli table; (3) REPLACE the "credit the helpers, blame no one" success-only gate (controller.py:222) with the §4.7 asymmetric clawback (settled-positive → wins, revert/bug-on-files → losses) and recall_margin credit-split (no flat +1-to-all). PORT+FIX the surfacer federation→serving/service.py:150 UtilityReranker (`alpha·(1+max(0,u))`, controller.py:184/service.py:184) — un-cripple to `alpha · f(utility)` where f ∈ [0.5,1.5] DEMOTES on confident-negative posteriors and is gated by CI-excludes-0 + ε-randomization. BUILD-NEW: the `utility` table (Beta-Bernoulli α/β), the posterior CI gate, the credit-split-by-margin attributor, and the four §4.7 guardrails (ε-randomization, held-out isolation slice, prediction-bias monitor, versioned/zeroable layer). DROP from the ported controller: propose/calibrate_thresholds/assert_action_limits/rollback (the shadow config-tuner — stays gated, NOT in C9; §2 "the config-tuner is not flipped live"). The reference's _last_drain_ts watermark + advance_watermark multi-agent floor discipline are PORTED as-is (re-keyed to the producer tick).

---

# M08 — Utility / Attribution Loop (C9) — Build-Ready Module Spec

## 1. Responsibility (one deep module)

C9 is the **batch attributor + posterior surfacer**: the keystone learning move (#6). Behind a narrow surface it hides all of the credit math the rest of the system must never see:

- **Join** one verified git outcome (a `SettledOutcome` produced by C10) against the exposure ledger for its trace.
- **Split** that outcome's reward across the co-injected memories **by recall_margin** — never flat "+1 to all 8" (spurious reinforcement; §2 "Attribution — co-occurrence-discounted").
- **Update** a **Beta-Bernoulli posterior** keyed `(episode_id, family_scope)` in a **separate, versioned** table — never touching `episodes.weight` (§3: "weight immutable post-capture; the loop never writes it"; guardrail 4: a human can zero the utility layer).
- **Gate** the surfacer so utility only moves ranking once the posterior **CI excludes the no-signal point**, and only on `(1−ε)` of recalls (ε-randomization), and never on the held-out isolation slice.
- **Surface** `alpha · f(utility)` where `f ∈ [0.5, 1.5]` **can demote** — the un-cripple of the reference's `1 + max(0, u)` that could only ever promote.

The meaningful, error-prone work (conserved margin-split, Beta CI gate, asymmetric win/loss accounting, the four self-modifying-loop guardrails, the no-double-credit watermark) is **enclosed**. The surface a caller touches is: `Attributor.split(outcome, isolation) → CreditDelta[]`, `UtilityStore.apply_credit(deltas)`, `UtilityStore.utility_map(family) → {eid: f_input}`, and `UtilitySurfacer.rerank(query, cands)`. Everything else is internal.

This is the product's **center of gravity** (§MVP-scope: "the single keystone the product thesis rests on"). The decomposition keeps the **risky credit policy PURE** (`hive/core/attribution.py` + `surfacer.py`: no SQL, no git, no clock — clock and store injected) so the §6.6 keystone eval and the RULE-2 mutation matrix land on a git-free, time-free object, exactly mirroring the C10 OutcomeSource/OutcomeJoiner cut.

## 2. Public surface + ENFORCED contract

See `interface_block` for exact signatures. Contract highlights (each is a test in §8):

**Invariants (enforced by type/`__post_init__`/test, not prose):**
1. **Verifiable-credit-only** — `SettledOutcome.__post_init__` rejects `reward_sign ∉ {−1, +1}`. A CI-green / `~0` event (§4.7 "high base-rate ⇒ near-zero info") can **never be constructed** as a credit, so it can never reach the posterior. This makes the product invariant ("utility updates only from a machine-checkable git outcome, never from agent self-report") **structural**, not documented. The old L0 `hive_outcome` self-report path does NOT feed C9.
2. **Credit conserved** — `Attributor.split` postcondition: `Σ(d_wins + d_losses) == reward_magnitude` (rel-tol 1e-9). Designed-out the "+1 to all 8" amplification: credit is *split*, never *broadcast*.
3. **weight is immutable** — C9 owns the `utility` table only; it has **no method that writes `episodes.weight`**. The reference's `reconsolidate(weight += alpha_u·utility)` is deliberately NOT ported; the precondition "don't touch weight" is **designed out** by giving C9 no handle to the episodes table's weight column.
4. **Confidence gate** — `f(utility)` is applied by the surfacer **only** for posteriors where `ci_excludes_half()` is True. Enforced at the SOURCE: `utility_map(confident_only=True)` is the only map the surfacer ever sees, so a sparse/uncertain posterior is *absent* from the map (cannot leak — same single-source-of-truth discipline as the `_RECALL_PREDICATE` decision).
5. **ε-randomization > 0** — `UtilitySurfacer` ignores the utility term on a per-call `Bernoulli(ε)` draw; `epsilon_explore` MUST be `> 0` (config `__post_init__` rejects 0). Guardrail 1 — the only way novel memories get exposure and loop-decay is detectable ([[project_channel_flip_gate_dominates]]: one unchecked ranking term starves the rest).
6. **Isolation slice never reweighted** — `Attributor.split` excludes `isolation` ids (guardrail 2); `UtilityStore.isolation_episode_ids()` is the single source.
7. **f-band clamped** — `f ∈ [0.5, 1.5]`; `f(0.5)=1.0` (no-op), `f(1.0)=1.5`, `f(0.0)=0.5`. Bounded reversible step (CACE / guardrail 4).
8. **DEFAULT-PRESERVING** — `enabled=False` (Phase 1) ⇒ `rerank` is byte-identical identity; posteriors accrue but never apply (§12 L1a "utility observed, not applied").

**Units/semantics:** `recall_margin ≥ 0` (rank/score gap, from the exposure ledger). `wins/losses` are Beta α/β delta-sums (real, not integer — margin-split fractional). `reward_magnitude ∈ (0, 1.0]`: provisional-settled `+0.2`, clawback `−1.0` (§4.7). `version` monotone; bump = roll the whole layer back. `n_sources` = DISTINCT corroborating agents (not write count).

**Error semantics:** the attributor is PURE and may raise `ValueError` on malformed value types (caught at construction, before any state change). The store's `apply_credit` is transactional (all-or-nothing, CAS-bounded). The producer-tick drain (ported `apply_outcomes_from_sink`) **NEVER raises into its caller** — a credit failure logs + counts but must not break the producer loop (ported G-MASK boundary catch, controller.py:245).

**Precondition designed OUT:** the reference required the caller to know "only credit on useful==yes, blame no one on no/unknown" (controller.py:222) — a fragile policy split. C9 designs it out: the *producer* (C10) only ever emits `SettledOutcome`s with a resolved `±1` sign (settled-positive or clawed-back), so the attributor has no "unknown" branch to get wrong. The asymmetry lives in C10's state machine (§11), C9 just applies the sign.

## 3. Swap seam

C9 sits **behind** the `UtilityStore` port (the posterior persistence is swappable: SQLite default → pgvector/Redis-backed later with NO core change), and **consumes** the C10 producer via the `SettledOutcome` value type (the producer is swappable behind its own port).

- **Port:** `UtilityStore` Protocol (see interface_block) — `apply_credit / posterior / utility_map / zero_layer / isolation_episode_ids`.
- **Default adapter:** `hive/store/sqlite_utility_store.py` over the §3 `utility` + `utility_sources` tables, sharing the §12 single-writer CAS lock with the rest of the Store.
- **Second adapter must implement:** the five Protocol methods with the same transactional + distinct-agent-counting + confident-only-filter semantics. The PURE `Attributor` and `UtilitySurfacer` need NO change — they speak only `CreditDelta` / `UtilityPosterior` / `{eid: f}` dicts. Proof of zero-core-refactor: `tests/store/test_utility_store.py` runs the SAME contract against a real SQLite adapter AND a `FakeUtilityStore`; the core tests run entirely against the fake. C9's pure layer never imports SQLite.

The surfacer multiplier (`f`) is intentionally **NOT** swappable — it is a fixed, mutation-tested policy (a bounded reversible step is a product invariant, not a tunable adapter), mirroring the "recall boundary is non-swappable by design" decision.

## 4. Data owned

- **Tables (BUILD-NEW):** `utility(episode_id, family_scope, wins, losses, n_sources, version, isolation, cas_version)` PK `(episode_id, family_scope)`; sidecar `utility_sources(episode_id, family_scope, source_agent)` for DISTINCT-agent `n_sources`. DDL in interface_block. C9 is the **sole writer** of both; C3/surfacer is read-only via `utility_map`.
- **Reads (does not own):** the `exposure` ledger (owned by the Store / telemetry — `trace_id → [(episode_id, recall_margin)]`); the `SettledOutcome` stream from C10's sink. C9 never writes `episodes`, `exposure`, or `task_outcomes`.
- **Config:** see interface_block — `channels.utility_rerank` (Phase switch), `utility.{epsilon_explore, prior_a, prior_b, f_min, f_max, promote_min_sources, ci_level, prediction_bias_window}`. Tier: `utility_rerank` = C (restart). The rest = A/next-run. `salience.utility_sigma` is **explicitly not read** (superseded; the §10 map calls it "soft via SalienceConfig.utility_sigma — replaced by BUILD-NEW table").

## 5. Dependencies (and the boundaries it must NOT cross)

**Depends on:**
- `UtilityStore` port (persistence; injected).
- The exposure ledger read surface (margins) — via the value types passed in by the producer tick, NOT a direct telemetry import in the pure core.
- An injected `clock` (for the prediction-bias monitor window) and an injected `rng` (for ε-randomization) — never reads wall-clock or global random directly (keeps the core deterministically testable).

**Must NOT know about (named boundaries):**
- **git / subprocess** — that is C10's sealed `OutcomeSource`. C9 receives an already-resolved `family_scope` and `reward_sign`; it never shells out, never parses porcelain, never runs `git blame`. (The §9 trust boundary stays in C10.)
- **the embedder / PCA / vector index (C1–C3 geometry)** — C9 reorders an already-scored candidate list; it never re-encodes or re-scores by similarity.
- **the recall gate (C4)** — C9 is downstream of abstention; it never resurrects a refused query (abstain-no-resurrect: a candidate list the gate emptied stays empty; the surfacer reorders, never adds).
- **the secret scan / admission (C6)** — C9 only ever credits already-`approved` memories (§8.2 "admission is upstream of the loop").
- **the shadow config-tuner** (`controller.propose/calibrate_thresholds/assert_action_limits`) — DROPPED from this module; C9 credits the outcome path only, it does not flip config live (§2 "the config *controller* stays gated").

## 6. Failure-mode logging (per the engineering standard; secrets never logged)

All structured JSON (`timestamp, level, context, message, error, stack`), context-tagged `cls.attribution`. NO text/secrets — episode_ids, family_scope (a denormalized non-secret string), and bounded floats only.

| Boundary | Level | Logs |
|---|---|---|
| Producer-tick drain start/end | INFO | trace count, deltas applied, episodes credited (success checkpoint — proves the loop is LIVE, anti-dead-code; ports `cls_controller_drain_runs_total`). |
| No-double-credit skip (`o.ts ≤ watermark`) | DEBUG | skipped ts, watermark (proves the strict-greater guard fired). |
| `SettledOutcome` malformed (`__post_init__` raises) | ERROR | reward_sign/magnitude, task_ref, trace_id, stack. Boundary failure: a bad outcome from C10. The drain catches, logs, increments an error counter, and CONTINUES (one bad outcome must not stall the loop). |
| `apply_credit` CAS conflict / retry | WARN | episode_id, family_scope, attempt (recoverable concurrency; ports the CAS retry path). |
| `apply_credit` transaction rollback | ERROR | the failing delta batch (ids+family only), error, stack. |
| Confidence gate excludes a posterior | DEBUG | episode_id, family, ci_lo, ci_hi (why it didn't apply — traceability for the keystone eval). |
| ε-randomization fired (utility ignored this call) | DEBUG | trace_id, epsilon (guardrail-1 audit; lets the eval measure the ε-slice). |
| Prediction-bias monitor divergence | WARN | recalled-utility vs realized-outcome gap over the window (guardrail-3: "ranker stale, codebase moved underneath it"). |
| Producer-enrolled but `watch_repos` empty / sink absent | WARN | "utility loop starved, not broken" (§4.8 — distinguishes starved from crashed). |
| `zero_layer` invoked | INFO | old version, new version, rows zeroed (guardrail-4 human rollback audit). |

## 7. Port disposition vs §10 map

| Sub-part | Reference file | Disposition |
|---|---|---|
| Drain / no-double-credit watermark | `federation/controller.py:250` `apply_outcomes_from_sink` (+ `apply_outcome:199`, `peek_max_seen_ts:318`) | **PORT+SIMPLIFY** — keep the `_last_drain_ts` strict-greater guard + `advance_watermark` multi-agent floor; **MOVE off `service.consolidate()` (service.py:942) onto the producer tick** (Fix #1/§4.4); **REPLACE** the `reconsolidate(weight+=...)` mechanic with `utility`-table writes; **REPLACE** the success-only gate with §4.7 asymmetric clawback + margin-split. |
| Surfacer | `serving/service.py:150` `UtilityReranker` (`alpha·(1+max(0,u))`) | **PORT+FIX** — un-cripple `f` to `[0.5,1.5]` (DEMOTES); add ε-randomization + confidence gate. |
| Utility posterior table | — (only soft `SalienceConfig.utility_sigma`) | **BUILD-NEW** — Beta-Bernoulli `utility` + `utility_sources`. |
| Confidence-CI gate, margin-split, 4 guardrails | — | **BUILD-NEW** — the pure `Attributor` + `UtilitySurfacer` policy. |
| Config tuner (`propose/calibrate/assert_action_limits/rollback`) | `controller.py:118–196,338` | **DROP from C9** (stays gated, §2; not part of the credit path). |
| `reconsolidate` (key-drift CAS) | `core/episodic.py:422` | **DROP** — C9 does not drift keys or touch weight; only the `update_cas` *idiom* is reused by the store adapter. |

## 8. TEST CONTRACT (test-first)

See `test_contract` for the full file/function list with exact assertions + the failure each catches. Summary of coverage:
- **Happy path:** margin-split credit conserved + proportional; confident posterior demotes/promotes; drain on producer tick moves the posterior.
- **Every §6 failure mode:** malformed `SettledOutcome` rejected; CAS conflict retried; rollback atomic; double-credit guarded; starved-loop WARN; prediction-bias flagged.
- **Every §2 invariant:** verifiable-credit-only (`reject_zero_sign`), credit conserved (`split_conserves`), weight-immutable (`test_weight_never_written` + mutation), confidence gate (`ci_includes_half` + mutation), ε > 0 (`epsilon_ignores_utility` + mutation), isolation excluded, f-band clamped, DEFAULT-PRESERVING.
- **MUTATION matrix (RULE 2):** (i) margin-split→uniform → `test_split_proportional_to_margin` red; (ii) gate→always-True → `test_ci_includes_half_when_sparse` red; (iii) restore old `1+max(0,u)` → `test_confident_negative_demotes` red; (iv) re-introduce `weight += alpha_u·utility` → `test_weight_never_written` red; (v) `o.ts <= watermark` → `<` → `test_drain_no_double_credit_on_watermark` red; (vi) delete ε branch → `test_epsilon_ignores_utility` red. Each restores → green.
- **§6 acceptance gate mapping:** §6.1#6(c) "the outcome-drain fires on the producer tick with the consolidation timer disabled" → `tests/loop/test_drain_on_producer_tick.py::test_drain_fires_on_producer_tick_consolidation_disabled` (the Fix-#1 dead-loop guard). The §6.1#6 posterior-moves clause → `test_phase1_observed_not_applied` (accrual) + `test_confident_negative_demotes` (apply). The §4.7 "posterior CI excludes 0" confidence gate → `test_ci_includes_half_when_sparse`. No functional path is untested.

---

## Design review (independent pass)

**Verdict:** STRONG DESIGN, NOT BUILD-READY. The decomposition is genuinely good — a deep module that encloses the error-prone credit math (margin-split, Beta CI gate, asymmetric clawback, 4 guardrails) behind a 4-method surface, with a pure git-free/time-free core (Attributor + UtilitySurfacer) that makes the §6.6 keystone eval and RULE-2 mutation matrix land cleanly (APOSD #4 deep module, #10 pull-complexity-downward, #16 separate-what-matters). The product invariants are mostly made STRUCTURAL rather than prose: verifiable-credit-only via SettledOutcome.__post_init__ rejecting reward_sign∉{−1,+1} (designs out the reference controller.py:222 'useful==yes' policy split — a real fragility I confirmed at controller.py:199-225); weight-immutable by giving C9 no handle to episodes.weight (the reference's reconsolidate weight+=alpha_u·utility at episodic.py:455 is correctly NOT ported); confidence gate enforced at source via utility_map(confident_only=True); ε>0 rejected at config __post_init__. Extensibility is excellent (8): the UtilityStore Protocol + FakeUtilityStore dual-contract gives true zero-core-refactor swappability per the SWAPPABILITY mandate. BUT three things block sign-off: (1) the interface_block and test_contract artifacts the spec defers to on nearly every contract and EVERY test claim DO NOT EXIST (docs/03-modules is empty) — so 'rich enforced contract' and 'test-first full coverage' are asserted, not verifiable, which is exactly the Prose-Only-Contract failure the rubric weights heaviest; (2) a real information-leakage gap: the surfacer is keyed (episode_id, family_scope) but rerank(query, cands)/utility_map(family) never specify how the live query's family is resolved at recall time — the reference reranker keys on eid alone (service.py:166, _recall_utility_map returns dict[int,float]), so family-resolution-at-query-time is an unspecified cross-boundary decision; (3) two named guardrails (held-out isolation slice §4.7-2, prediction-bias monitor §4.7-3) appear in logging and one method (isolation_episode_ids) but have NO owning mechanism, NO test, and isolation's selection/membership source is undefined. Fix the artifact absence + family-resolution + the two phantom guardrails and this is build-ready.

**Scores (1–10):**
- design_complexity: 4
- cognitive_load: 6
- information_leakage: 4
- extensibility_fit: 8
- agent_navigability: 5
- contract_enforcement: 5
- test_coverage: 5

**Red flags:**
- Prose-Only Contract on Tricky Semantics @ entire spec (every 'See interface_block'/'See test_contract' reference) — the most error-prone parts (Beta CI gate, margin-split conservation, asymmetric reward, 4 guardrails) are described in prose and deferred to two artifacts that don't exist — root: obscurity → an agent building from this will invent signatures/assertions that silently drift from intent; this is the dominant flag.
- Information Leakage @ UtilitySurfacer.rerank(query,cands) ↔ recall path — the (episode_id, family_scope) keying decision is reflected in the store (PK), the utility_map(family) signature, AND implicitly in the surfacer, but no module owns 'which family is this query' — root: dependency → a change to family granularity forces a coordinated edit across C3 recall and C9 surfacer that the spec doesn't acknowledge.
- Hard to Describe @ §2 'Error semantics' paragraph — apply_credit is 'transactional (all-or-nothing, CAS-bounded)' AND the producer-tick drain 'NEVER raises into its caller' AND the attributor 'may raise ValueError ... caught at construction before any state change': three different error regimes across one surface, stated only in prose with the cross-cutting ordering ('before any state change') uncaptured by any type — root: obscurity/complex semantics → an agent must hold the whole error-ordering contract in head with no enforced signal; candidate for define-errors-out-of-existence (the construction-time ValueError already does this for SettledOutcome; the drain's swallow-and-continue should be a named Result/counter type, not prose).
- Missing Feedback Signal @ prediction-bias monitor and isolation slice — two of the four mandatory guardrails exist only as a WARN log row and a config key with no method, no return type, no test — root: obscurity → an agent cannot self-check whether these guardrails are even wired; a guardrail with no test is dead-code-by-default (the exact §6.1#6(c) 'prove the loop is not silently dead' concern, recursively unmet for the guardrails themselves).
- Special-General Mixture (latent) @ n_sources / promote_min_sources — §2 says wins/losses are real-valued margin-split α/β deltas, but n_sources is a DISTINCT-agent integer gating 'high-trust', and the spec never states how promote_min_sources interacts with ci_excludes_half() (is it AND-ed into the confidence gate, or separate?) — root: dependency → two confidence mechanisms (CI width and source count) with an unspecified composition rule embedded in one posterior; one general gate vs a special source-count case mixed without separation.

**Test gaps:**
- No test_contract file exists at all — the §8 summary lists test NAMES and a mutation matrix but not the exact assertions or the failure each catches; per the LOCKED test-first mandate the failing assertions must be written BEFORE implementation and they are not present to review.
- credit-conserved invariant: §2 states Σ(d_wins+d_losses)==reward_magnitude (rel-tol 1e-9) and §8 names test_split_proportional_to_margin, but there is NO named property/fuzz test that conservation holds across RANDOM margin vectors and reward magnitudes (e.g. hypothesis over N injected memories, arbitrary margins incl. zeros and ties) — a single hand-picked case can pass while the split leaks credit on a tie or all-zero-margin exposure (undefined: how is reward split when every recall_margin==0?).
- abstain-no-resurrect / never-hallucinate at the surfacer: §5 asserts 'the surfacer reorders, never adds' and 'a candidate list the gate emptied stays empty', but §8 lists NO test that feeds rerank an EMPTY candidate list and asserts EMPTY out, and NO test that rerank cannot introduce an eid absent from cands. This is the product's headline invariant at C9's boundary and it is untested.
- approved-only / secret-safe invariants at C9: §5 claims C9 only credits already-approved memories ('admission is upstream'), but there is NO test that a credit/exposure referencing a PENDING (status!='approved') episode_id is rejected or a no-op. If the exposure ledger can contain a pending eid (race: recalled-then-not-yet... actually approved-only recall prevents this, but the test that the surfacer/store NEVER emits f for a non-approved eid is absent). No secret-safe test is needed at C9 (correctly upstream) — but the spec should state that explicitly as a non-applicability, not silently.
- family-resolution: no test exercises a query whose family differs from a candidate's credited family — i.e. that a memory proven on repo-X/python/fix-ci is NOT surfaced-boosted for a repo-Y query (the core family-scope guarantee). §8 has confident_negative_demotes but nothing asserting cross-family ISOLATION of the boost.
- f-band boundary tests: §8 asserts f-band clamped but lists no test pinning f(0.5)=1.0, f(1.0)=1.5, f(0.0)=0.5 as explicit equality assertions, nor a clamp test for utility outside [0,1] (what does f do at posterior mean 1.0 vs >1? the band is [0.5,1.5] on f, but the input domain of utility is unstated).
- DEFAULT-PRESERVING (enabled=False) is claimed 'byte-identical identity' but §8 names test_phase1_observed_not_applied for ACCRUAL only; there is no test asserting rerank output == input list order EXACTLY when enabled=False (the reference proves this via inert passthrough at service.py:170; the port must re-prove it, and the mutation 'flip enabled default to True' → that test red is not in the §8 matrix).
- prediction-bias and isolation guardrails: zero tests (consequence of their being phantom mechanisms) — both are §4.7-mandatory.
- n_sources / promote_min_sources gate: §2 lists n_sources='DISTINCT corroborating agents' and config has promote_min_sources, but §8 has NO test that a posterior with <promote_min_sources distinct agents is withheld from the confident map (the cross-agent-corroboration robustness gate is untested), and no test that two writes from the SAME agent count as n_sources==1 (the distinct-counting semantic).
- CAS-conflict retry: §6/§8 mention a CAS conflict WARN and 'retried', but no named test asserts the retry actually CONVERGES (apply_credit under simulated concurrent version bump eventually commits) vs just logs — the reference reconsolidate has max_retries=64 and raises CASConflictError on exhaustion (episodic.py:431); the port's exhaustion behavior and its test are unspecified.

**Must-fix:**
- ARTIFACTS MISSING — BLOCKING: the spec defers to `interface_block` (exact signatures, DDL, config keys) and `test_contract` (file::test + exact assertion + failure caught) on essentially every contract and EVERY test, but docs/03-modules is empty and neither artifact exists. Per agent-native.md §6 a prose-only contract on tricky semantics is the worst case for an agent; the entire test-first mandate (LOCKED DECISION) and 'enforced contract over prose' rest on two unsupplied files. Supply the literal interface_block (Protocol + @dataclass with __post_init__ bodies + DDL with PK/CHECK constraints + config dataclass) and the literal test_contract (every test name, its exact assertion, the failure it catches) before sign-off — the summaries in §2/§8 are not substitutes.
- FAMILY-RESOLUTION-AT-QUERY-TIME UNSPECIFIED — leakage: posteriors are keyed (episode_id, family_scope) and utility_map(family)→{eid:f} takes a family, but UtilitySurfacer.rerank(query, cands) has no family parameter and the spec never says how the surfacer learns the live query's family. The reference reranker (service.py:166, _recall_utility_map) keys on eid ONLY — there is no family dimension at recall. Either (a) the recall path must derive/pass a family_scope into rerank (new cross-module data dependency that must be named and contract-tested), or (b) define the fold across families (max? sum? per-family map selection) — currently undefined, and §8 has no test for it. This is the one genuine Information Leakage flag: a decision (how family scopes the live query) smeared across C3-recall and C9-surfacer with no single owner.
- ISOLATION SLICE has no owning mechanism: guardrail-2 (§4.7) requires a held-out slice the loop NEVER reweights; the spec exposes UtilityStore.isolation_episode_ids() and excludes those ids in Attributor.split, but never defines HOW an episode enters isolation (random assignment at capture? a flag? what fraction?), who writes it, or its persistence. invariant #6 ('isolation excluded') is listed as tested but with no membership-source there is nothing to assert against. Specify isolation membership assignment + storage + the exact test that proves a credit to an isolation id is dropped (and the mutation: remove the exclusion → test red).
- PREDICTION-BIAS MONITOR (guardrail-3) is phantom: it appears only as a WARN log row (§6) and a config key (prediction_bias_window) — there is no method on any surface that computes recalled-utility-vs-realized-outcome divergence, no owner, and no test. §4.7 calls it mandatory and §12 makes it the Phase-2 readiness instrument. Either give it a real method on the surface (e.g. UtilityStore.prediction_bias(family, window)→float) with a contract test, or explicitly scope it out of M08 and name the module that owns it; a mandatory guardrail that exists only in a log line is a Missing Feedback Signal.
- F-BAND / CI-GATE NUMERICS UNSPECIFIED: invariant #7 fixes f(0.5)=1.0, f(1.0)=1.5, f(0.0)=0.5 — three points — but never gives the function between them (linear? f(p)=0.5+p?), and ci_excludes_half() is named but the posterior-mean→f input mapping and the CI computation (Beta quantile? normal approx? which ci_level tail?) are undefined. A mutation-tested fixed policy (the spec's own claim that f is non-swappable BECAUSE it is mutation-tested) cannot be mutation-tested without its exact formula. Pin the closed-form f and the exact CI method in interface_block.
