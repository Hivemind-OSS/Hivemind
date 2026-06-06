# 04 — Risk Ranking + The #1-Risk Thin Vertical Slice

> Step 4. Move #6 (the verifiable utility loop) is the product's center of gravity (§6.6 keystone, §MVP-scope). It is the one move built *before* #1/#2/#4/#5/#7, and a clean *negative* on it is a successful MVP outcome that deletes six unbuilt moves cheaply. Therefore the риск surface is dominated by the move-#6 join, not by the substrate. This document (1) ranks the risks **riskiest-first**, each with its **cheapest retiring experiment** and an explicit **kill/keep signal**; (2) specifies the **#1-risk thin vertical slice** in full — a single commit → trace → provisional + → settle → credit → surfacer round-trip on a 2-memory store with every adapter faked, asserting **both** surfacer directions (promote on settle, demote on clawback) to prove the un-cripple — with its **7-test contract** and a **6-row RULE-2 mutation matrix**; (3) names the **most-downstream assumptions A1–A5**, ordered most-dangerous-first (A1 = verifiable signal density).

This document reflects the **AUTHORITATIVE RESOLUTIONS**: net MCP surface = exactly 8 tools; the exhaustive index is authoritative and never silently flips to ANN; the move-#6 producer is the D6 synthesis — `OutcomeSource` (PORT, all I/O) + pure clock-injected `OutcomeJoiner` (all §11 policy) + `OutcomeProducer.step(now) -> ProducerTick` (driver). Where M08/M09 prose disagrees with D1/D6 (e.g. "11/12 tools", "the attributor EXISTS/PORT", "the posterior already exists"), D1/D6 win and the disagreement is itself logged below as a risk.

---

## Part 1 — Risk ranking (riskiest-first)

Each risk states: the failure it names, *why it ranks where it does*, the **cheapest retiring experiment** (the smallest thing that converts the unknown into a fact), and the explicit **KILL signal** (this design/assumption is dead) vs **KEEP signal** (proceed). Ranking axis: *how much built and unbuilt work a wrong answer invalidates*, with ties broken by *how silent the failure is* (a silent wrong answer outranks a loud one).

---

### R1 — The §11 trace↔outcome join does not produce dense-enough, correctly-attributed credit (the keystone's instrument is blind or lying)

**The failure.** The §11 bridge (recall→trace→exposure, trace→commit by window/stamp, commit→verifiable-outcome by settle/revert/blame-overlap clawback) is *the* one mechanism specified by neither the prior spec nor the existing code (§11 opening). If it (a) credits too few settled outcomes to power the §6.6 control-arm A/B (sparse credit), or (b) attributes the credit to the *wrong* memories (window over-attribution not absorbed by the margin-discount + ε), or (c) attributes credit but the posterior never moves because the join key / family_scope / sink chain has a silent break — then the keystone eval runs **underpowered or on corrupted data**, and the entire pre-committed kill/keep decision (which gates whether #1/#2/#4/#5/#7 are ever built) is made on noise.

**Why it ranks #1.** Every other risk is a property of a component; R1 is a property of *the experiment the MVP exists to run*. A wrong R1 answer invalidates not just code but the **go/no-go for the next six moves** — the largest blast radius in the system. It is also maximally silent: a join that fires but mis-attributes looks identical to a working join until the §6.6 control arms come back null, by which point Phase-2 work is already underway. This is the §11 mechanism the D6 decision, M08, and M09 all circle; M09's own independent review flags its two highest-risk paths (blame-overlap clawback, posterior credit) as "under-specified at exactly the seams the design otherwise nails." **R1 is the risk this entire document's Part 2 vertical slice exists to retire.**

**Cheapest retiring experiment.** The Part-2 thin vertical slice itself: a single end-to-end round-trip on a **2-memory store, all adapters faked** (FakeOutcomeSource scripts the commit/merge/revert facts, FakeClock advances `settle_days`, FakeEmbedding/FakeIndex/FakeStore hold two approved episodes, FakeTelemetrySink captures the drained reward). Drive `OutcomeProducer.step()` through commit→trace→provisional+→settle→credit, then `OutcomeSurfacer.rerank` — and assert **both** directions (promote after a clean settle, demote after a blame-overlap clawback). No git subprocess, no real model, no wall-clock. This is ~7 tests and runs in milliseconds.

- **KILL signal:** the faked round-trip cannot make the `(episode_id, family_scope)` Beta-Bernoulli posterior move in *both* directions, OR the margin-split does not conserve credit (`Σ(d_wins+d_losses) ≠ reward_magnitude`), OR a clean settle and a clawback are indistinguishable at the surfacer. Any of these means the §11 chain as designed cannot carry the keystone signal — the join must be redesigned before *any* substrate is built.
- **KEEP signal:** both surfacer directions move on the faked round-trip, credit conserves to rel-tol 1e-9, and the 6-row RULE-2 mutation matrix is green-red-green. The instrument is sound; proceed to wire the real `LocalGitOutcomeSource` adapter behind the same `OutcomeSource` port (the swap axis = test axis identity guarantees no core change).

---

### R2 — Move #6 does not compound — the §6.6 keystone is a clean negative (the product's central bet is wrong)

**The failure.** Even with a *correct* join (R1 retired), utility-weighted recall may simply **not beat the recency/frequency control arms** on family-scoped task-success, CI-significant, with non-saturating accrual and within-family transfer (§6.6 win condition). Or the apparent lift may trace to **rewarding the gameable CI-green channel** rather than the ungameable revert/bug-on-files clawback (§6.6 "signal information, not latency"; the symmetric "+1 per CI-green" null hypothesis).

**Why it ranks #2.** This is the keystone itself — §6.6, the experiment the MVP exists to run, whose one scaling claim "was already refuted (0–3)" so the eval is *adversarial by default*. It ranks below R1 only because **R1 is a precondition for R2 being answerable at all**: you cannot read a keystone result until the instrument producing it is trusted. A negative here is not a build failure — per §7, "a clean negative is a successful MVP outcome; it kills six unbuilt moves cheaply." But a *false* positive (lift from the gameable channel) is catastrophic: it greenlights six moves on a phantom.

**Cheapest retiring experiment.** Run the §6.6 keystone on the recommended `fix-failing-CI` family in one service, with the **three mandatory control arms** (utility-off λ_Q=0, recency-weighted, frequency-weighted), only *after* the Phase-2 readiness gate is met (≥ `N_settled` settled outcomes spanning ≥ `M_memories` distinct memories, `N,M` fixed on the corpus *before* the run — §12). The anti-gaming check is mechanical and cheap and can run first: plant a "delete the failing test → CI-green" PR, confirm it earns nothing once the bug-on-files clawback lands on its revert (§6.6 anti-gaming).

- **KILL signal (pre-committed, §6.6):** Q-weighted recall does **not** beat the recency baseline on family-scoped task success, CI-significant, with non-saturating accrual and within-family transfer → **the keystone is dead, and moves #1/#2/#4/#5/#7 are NOT built.** OR the lift exists but the anti-gaming check shows it comes from the CI-green channel, not the clawback → also dead (a win on a gameable signal is not a win).
- **KEEP signal:** Q-weighted recall beats *both* recency and frequency, CI-significant, non-saturating, with within-family transfer, AND the lift survives the ε-slice and held-out-isolation arms, AND it traces to the ungameable clawback. Then Phase 2 flips `channels.utility_rerank=True` and the deferred moves are scheduled.
- **Inconclusive ≠ negative (§6.6):** if too few recalls settle, Q is undertrained → widen the family or extend the window; do **not** read sparsity as refutation. (This is exactly the failure R1 must prevent at the instrument level.)

---

### R3 — The authoritative-exhaustive index silently flips to ANN (a recall result becomes unfalsifiable)

**The failure.** The locked geometry mandates `VectorIndex.is_exact == True`, exhaustive cosine-kNN **AUTHORITATIVE** (D1; spec §4.3 anti-trap). If an operator (or a well-meaning "scale" PR) wires an HNSW/approximate adapter behind the same port, or a config knob (`approx_threshold`) re-enters, then `recall@5` and the normalized-entropy abstention gate (which assumes the top-N are the *true* top-N) become **silently wrong** — the never-hallucinate guarantee is undermined because the gate can abstain on a query whose true nearest neighbor was simply not visited.

**Why it ranks #3.** It corrupts the *substrate* recall path (C1–C4), which every downstream measurement (including R1/R2) rides on. It ranks below R1/R2 because the keystone is the bet and the join is its instrument, but it ranks above the secret/clawback risks because a silent geometry degradation poisons the recall@5 ≥ 0.33 / AUROC ≈ 0.77 gate (spec §6.1) and would make R2's control arms uninterpretable. The D1 design already hardens this structurally (`assert index.is_exact` in `RecallService.__init__`; the §4.3 `approx_threshold` landmine cannot re-enter through a config knob because an HNSW adapter cannot pass `PortContractTests::test_index_is_exact`).

**Cheapest retiring experiment.** `tests/contract/test_index.py::test_index_is_exact[FakeIndex,ExhaustiveIndex]` — the *same* assertion proves both the fake and the REAL index exact at N ≥ 10k, by comparing `search(q,k)` against a brute-force `argsort` of full cosine. Plus the construction-time `assert index.is_exact` in `RecallService.__init__`. Cost: one parametrized contract test, milliseconds on the fake, seconds on the real index at 10k.

- **KILL signal:** any adapter wired into `container.py` whose `is_exact` is `False` reaches `RecallService` (the assertion would fire), OR `ExhaustiveIndex.search` disagrees with brute-force argsort on any query → the index is not authoritative; do not ship it on the recall path.
- **KEEP signal:** `is_exact == True` is contract-tested green on every wired adapter and the real index matches brute-force at 10k. The §4.3 landmine is closed by construction; an approximate index is a *new adapter* that must explicitly opt the recall path into approximate mode (which the MVP never does).

---

### R4 — The secret-scan floor leaks (a credential reaches the store, the index, or a log)

**The failure.** The deterministic `SecretScanner.scan(text)` runs **before** stage (D1 `domain/admission.py`: scan → refuse/redact → blob → stage). If a credential pattern is missed (regex floor too narrow), or if any write path persists/indexes *before* scanning, a secret enters the durable store, the vector index, or — worse — a structured log line (which §6 forbids).

**Why it ranks #4.** It is a hard product invariant (secret-safe) and a real exposure, but it ranks below R1–R3 because (a) it does not invalidate the keystone *experiment*, only the *safety* of the running system, and (b) the D1 design makes the *ordering* structural: in `AdmissionService.write`, `scan()` precedes `put_blob()`/`stage()`, so the residual risk is **regex coverage**, not control-flow ordering. The blast radius is one leaked secret per miss, not a corrupted experiment — serious, but bounded and loud once detected (vs R1's silence).

**Cheapest retiring experiment.** Two tests, both on fakes: `unit/test_admission.py::test_secret_refused_never_staged` (a `refuse` verdict ⇒ `fake_store.stage` call-count == 0 *and* `put_blob` never called — mutation: move `stage()` before `scan()` → red) and a coverage corpus test feeding a fixture of real-shaped credentials (AWS keys, JWTs, `Bearer` tokens, private-key headers, connection strings) and asserting each is `refuse`/`redact`, never `clean`. Plus a log-scrubbing assertion: no `error`/`warn` line ever contains the matched secret, only `matched_kinds`.

- **KILL signal:** any credential in the corpus returns `action == "clean"`, OR `stage`/`put_blob` is reachable before `scan` on any write path, OR a log line carries raw secret text → the floor leaks; widen the regex set and re-prove ordering before any write path ships.
- **KEEP signal:** every corpus credential is refused/redacted, `stage`-before-`scan` is mutation-red, and logs carry only `matched_kinds`/identifiers. The floor holds for the known kinds; treat the corpus as a living allowlist and extend on any new kind seen in the wild.

---

### R5 — Blame-overlap clawback false-positives (a `−1.0` punishes a good memory on a coincidental same-file edit)

**The failure.** Decision B (D6 I3): a bug-fix commit clawbacks **iff** its changed lines overlap the original commit's *introduced* lines (`touched_blame` non-empty); same-file-alone NEVER clawbacks. The `−1.0` is the largest single reward in the schedule, and punishing a *good* memory is "the expensive false-positive direction" (M09 I3). The risk: the `touched_blame` provenance is wrong — wrong original SHA blamed, blame computed at the wrong time (a delayed bugfix arrives after the original SHA is squashed/rebased away), or the bugfix-SHA→original-SHA lookup hits the wrong `task_outcomes` row — so a coincidental edit claws back a memory that was actually helpful.

**Why it ranks #5.** It is a *correctness* defect *inside* the join, so it is a sub-risk of R1 — but it ranks separately and lower because the D6 synthesis already isolates it onto the **pure** `OutcomeJoiner._clawback`, making it the single most-tested path in the system (the §6.1.6 mandated guard `test_bugfix_same_file_no_blame_overlap_does_NOT_clawback` + the M1 blame-overlap mutation). The residual is the *provenance* gap M09's review flags: where the original commit's introduced line-ranges are persisted, and the bugfix→original lookup. The blast radius is per-memory mis-credit, smaller than R1's experiment-wide corruption, but it directly degrades the keystone's ungameable-negative signal (R2).

**Cheapest retiring experiment.** Pure-Joiner tests on hand-built `GitFacts`, no git: GUARD-a `test_bugfix_same_file_no_blame_overlap_does_NOT_clawback` (same file, `touched_blame` empty ⇒ 0 reward, 0 state change) + GUARD-b `test_bugfix_overlapping_blame_claws_back` + the M09-flagged additions: a **two-original-commits-same-file** test (plant two originals touching one file, assert the clawback hits ONLY the row whose introduced lines overlap — closes the cross-commit false-positive), and `test_clawback_blames_original_introduced_lines_after_settlement` (delayed bugfix after `settle_days`, original SHA squashed — the blame target must survive). The RULE-2 mutation: disable blame-overlap (clawback on any same-file match) ⇒ GUARD-a goes red.

- **KILL signal:** GUARD-a passes while the overlap gate is disabled (the test is not load-bearing), OR the two-original test claws back the wrong row, OR the delayed-clawback test cannot recover the original introduced lines (no persisted provenance) → the clawback precision is unprovable; define `touched_blame` storage/recompute and the bugfix→original lookup before the clawback path ships.
- **KEEP signal:** GUARD-a/b green, the mutation flips GUARD-a red and restoration restores green, the two-original and delayed cases attribute to the exactly-correct row, and same-file-disjoint-lines never claws back. The expensive false-positive direction is sealed on the pure side.

---

### R6 — Containerized MCP exposure / WAL single-writer contention / embedder-resident health (the deployment shape fails operationally)

**The failure.** Three coupled operational risks of the single-service container (D1; spec §12): (a) the MCP server exposes the `hive_*` surface beyond loopback or runs as root (violating the non-root `USER` mandate); (b) the in-process `OutcomeProducer.step()` git I/O (subprocess `git log`/`blame`) runs *inside* the single SQLite WAL writer lane and stalls the writer (the §12 in-process cost — the one constraint the sidecar swap would dissolve); (c) the baked CPU bge-small model fails to load or drifts dimension, and `hive_health` cannot detect it because the health probe is not embedder-resident.

**Why it ranks #6.** These are real and must be handled, but they rank low because: the D6 synthesis already mandates that `source.poll()`+`blame_spans()` do all git I/O **OUTSIDE** the write critical section (only `task_outcomes`/sink writes take the short lock); the embedder health is a fail-fast `health()` probe at startup + on `hive_health` (D2 §1 `ProviderHealth`); and the container hardening (non-root, multi-stage, loopback-only) is a Dockerfile/compose concern that fails *loudly* at deploy, not silently in the experiment. None of these corrupt the keystone instrument; they degrade availability, which is recoverable.

**Cheapest retiring experiment.** (a) A container smoke test asserting the running image's `USER` is non-root and the MCP port binds loopback/UDS only. (b) A driver test `test_producer_git_io_outside_write_lock` asserting `step()` performs `poll()`/`blame_spans()` before acquiring the writer lane and only the `task_outcomes`/sink writes are inside it (mutation: move `poll()` inside the lock → a contention/latency assertion fires). (c) `hive_health` returns `ProviderHealth.ok` only when the baked model is loaded, the frozen PCA head is present, and `dims agree` (encode("healthcheck") yields shape `(256,)`, unit-norm); fail-fast at startup on `missing_frozen_head`/`head_geometry_mismatch`.

- **KILL signal:** the image runs as root or binds non-loopback, OR git I/O is inside the write lock and stalls a concurrent write past a latency bound, OR `hive_health` reports `ok` while the model/head is absent → the deployment shape is unsafe; fix before exposing the server.
- **KEEP signal:** non-root + loopback-only verified, git I/O provably outside the lock, `hive_health` fail-fasts on a missing/mismatched head and only reports `ok` on a live encode probe. The single-service shape holds; the sidecar swap remains a future adapter, not a forced refactor.

---

### R7 — Onboarding idempotency / the `Hive-Trace` link can lie (first-run flow or agent-written provenance corrupts state)

**The failure.** Two coupled low-probability risks: (a) `hive_init` is not idempotent — a re-run double-imports the corpus or re-fits the frozen PCA head, producing a duplicated/mis-geometried store; (b) the `Hive-Trace` commit trailer (agent-written, the one new agent behavior, §11 hop 2) is *trusted to set the reward sign or value* rather than only **re-targeting which traces get credit** — letting an agent self-reward (violating verifiable-credit-only).

**Why it ranks last.** Both are genuine but the smallest blast radius and the most-structurally-defended. Verifiable-credit-only is made structural in two places: `SettledOutcome.__post_init__` rejects `reward_sign ∉ {−1,+1}` (M08 invariant 1), and the trailer in `OutcomeJoiner` can only select the trace *set* at `STAMP_WEIGHT` — the sign/value come from the git fact (merge/revert/blame), never the trailer (D6 association policy). So "the link can lie" is reduced to "the agent can choose *which* of its own recalls to credit," which the margin-discount + ε + the ungameable-negative settlement already absorb (§11: "the only thing they relay is the stamp, §9 high-trust"). Onboarding idempotency is a one-time operation (CLAUDE §7: "complexity is less critical than correctness" for cached/once-per-session ops), failing loudly at init, not silently in the experiment.

**Cheapest retiring experiment.** (a) `test_hive_init_idempotent`: run `hive_init` twice on the same store, assert the second run is a no-op (corpus row-count unchanged, frozen head version unchanged, no re-fit). (b) `test_stamp_trailer_cannot_inject_reward`: a `Hive-Trace` trailer on a commit re-targets the trace set but a trailer *claiming success* on an un-merged/reverted SHA produces **no `+`** — the sign still comes from the git fact (mutation: let the trailer set the sign → this test red). Both pure/faked, milliseconds.

- **KILL signal:** a second `hive_init` mutates store state, OR a trailer can produce a positive reward on a commit with no verifiable merge/clean-settle → onboarding or the trust boundary is broken; fix before enrolling repos.
- **KEEP signal:** `hive_init` is a verified no-op on re-run; the trailer only re-targets the trace set and can never set sign/value. The link cannot lie about *whether* a memory helped, only nominate *which* recall to score — and that nomination is absorbed by the discount + ε + ungameable settlement.

---

### Ranking summary

| # | Risk | Blast radius if wrong | Silence | Cheapest retirer | Defended by |
|---|---|---|---|---|---|
| **R1** | §11 join blind/lying | go/no-go for 6 unbuilt moves | **silent** | the Part-2 faked round-trip (7 tests) | this document |
| **R2** | move #6 doesn't compound | the product bet itself | semi-silent (null arms) | §6.6 keystone + 3 control arms | R1 retired first |
| **R3** | exhaustive index → ANN | substrate recall + all gates | **silent** | `test_index_is_exact[fake,real]` | `assert is_exact`, PortContractTests |
| **R4** | secret-scan floor leaks | one secret per miss | loud once found | refuse-never-staged + corpus | scan-before-stage (structural) |
| **R5** | blame clawback false-pos | per-memory mis-credit | semi-silent | GUARD-a + M1 mutation + 2-original | pure `_clawback`, mandated mutation |
| **R6** | container/WAL/health | availability, not experiment | loud at deploy | smoke + io-outside-lock + health | D6 io-outside-lock, fail-fast health |
| **R7** | init idempotency / link lies | one re-run / nominated trace | loud at init | idempotent + no-reward-inject | `__post_init__` sign-reject, stamp=set-only |

---

## Part 2 — The #1-risk thin vertical slice (R1, in full)

### 2.1 What the slice proves and why it is the cheapest possible R1 retirer

The slice drives the **entire §11 chain end-to-end on a 2-memory store with every adapter faked** — no git subprocess, no real embedder, no wall-clock — and asserts **both surfacer directions** (promote on a clean settle, demote on a blame-overlap clawback). Proving both directions is the **un-cripple proof**: the reference surfacer (`service.py:184`, `alpha·(1+max(0,u))`) could *only ever promote*; the M08 design un-cripples it to `alpha·f(utility)`, `f ∈ [0.5,1.5]`, which **demotes** on a confident-negative posterior. A slice that only promoted would not distinguish the new design from the crippled reference — so the demote direction is the load-bearing half. If both directions move on the faked round-trip and credit conserves, the §11 instrument is sound (KEEP); if either direction is stuck or credit leaks, the join is dead as designed (KILL).

This is the **cheapest** R1 retirer because the D6 synthesis makes the swap axis = test axis = pure-policy axis: the entire §11 credit policy lives on the pure clock-injected `OutcomeJoiner`, so the round-trip needs only frozen `GitFacts`, an injected `now`, and a fake sink/store — it runs in milliseconds and exercises every §11 decision (window association, provisional, settle, revert/blame clawback, family derivation, margin-split, posterior, surfacer) with zero I/O.

### 2.2 The store: 2 memories, fully faked

```
episodes (status='approved'):
  E1: id=1, text="bump pytest timeout to fix flaky CI",  value=v1 (256-d, faked unit vec), family_scope="repo-svc:python:fix-ci"
  E2: id=2, text="add retry to the network client",       value=v2,                        family_scope="repo-svc:python:fix-ci"

Both approved (admission upstream). Both in FakeIndex. Both eligible for recall + credit.
```

### 2.3 The faked adapters (no git, no model, no clock)

```python
# tests/fakes/  — all deterministic, no I/O
class FakeEmbedding:            # EmbeddingProvider
    d = 256; native_dim = 384; provider_id = "fake#pca"; w_version = 1
    def encode(self, text): return _hash_unit_vec(text, 256)          # hash→unit vec, stable
    def encode_batch(self, ts): return np.stack([self.encode(t) for t in ts])

class FakeIndex:                # VectorIndex
    is_exact = True
    def __init__(self): self._v = {}
    def add(self, eid, v): self._v[eid] = v
    def search(self, q, k):                                            # EXACT cosine, best-first
        s = sorted(((eid, float(q @ v)) for eid, v in self._v.items()),
                   key=lambda t: -t[1])
        return s[:k]
    def __len__(self): return len(self._v)

class FakeStore:                # EpisodeStore (episodes + exposure + utility ledgers, in-memory dicts)
    # exposure: trace_id -> [(eid, recall_margin)]
    # utility:  (eid, family) -> UtilityPosterior(wins, losses, n_sources, version)
    # task_outcomes: (task_ref, trace_id) -> OutcomeRow(state, settle_at, reward, ...)
    def record_exposure(self, trace_id, rows): self.exposure[trace_id] = list(rows)
    def exposed_for(self, trace_id): return self.exposure.get(trace_id, [])
    def update_posterior(self, eid, fam, dwins, dlosses, source): ...  # Beta α/β accrue
    def posterior(self, eid, fam): return self.utility.get((eid, fam))

class FakeOutcomeSource:        # OutcomeSource (PORT) — scripted GitFacts, NO subprocess, NO .git
    def __init__(self, scripted): self._facts = scripted
    def poll(self, since_ts): return iter([f for f in self._facts if f.author_ts > since_ts])
    def blame_spans(self, sha, paths): return ()                       # pre-attached on facts

class FakeClock:                # injected Callable[[], float]
    def __init__(self, t0): self.t = t0
    def now(self): return self.t
    def advance(self, *, days): self.t += days * 86400

class FakeTelemetrySink:        # captures drained rewards keyed by task_ref
    def __init__(self): self.records = []
    def record(self, reward): self.records.append(reward)
    def read(self, since): return [r for r in self.records if r.ts > since]
```

### 2.4 The round-trip (the 6 hops the slice drives)

```python
clock  = FakeClock(t0=1_000_000)
store  = FakeStore(); store.add_approved(E1, v1); store.add_approved(E2, v2)
index  = FakeIndex(); index.add(1, v1); index.add(2, v2)
embed  = FakeEmbedding()
source = FakeOutcomeSource(scripted=[...])      # facts injected per-test below
sink   = FakeTelemetrySink()

recall   = RecallService(embed, index, store, NormalizedEntropyGate(h_frac_max=0.5),
                         clock, OutcomeSurfacer(store, UtilityConfig()), recall_top_n=10)
joiner   = OutcomeJoiner(exposure=store, outcomes=store, sink=sink,
                         cfg=ProducerConfig(settle_days=7.0, provisional_plus=0.2,
                                            clawback=-1.0, epsilon=0.1, assoc_window_s=1800),
                         clock=clock.now)
producer = OutcomeProducer(source=source, joiner=joiner, controller=FakeController(store, sink),
                           meta=store.meta, agent_ids=lambda: ["agentA"],
                           cfg=joiner.cfg, clock=clock.now)
surfacer = recall.surfacer

# HOP 1  recall → trace            : both E1,E2 confident (gate does NOT abstain) → trace_id=T1,
#                                    exposure recorded margins {1: 0.7, 2: 0.3}
# HOP 2  trace → commit            : FakeOutcomeSource yields MERGE fact at author_ts inside
#                                    assoc_window_s of T1 → window-associates SHA→T1, writes
#                                    provisional task_outcomes row, settle_at = merged_ts + 7d
# HOP 3a settle sweep (PROMOTE)    : clock.advance(days=8); producer.step() → settle_sweep promotes
#                                    provisional→settled_pos, emits Reward(+0.2*margin*(1-ε)) to sink
# HOP 4  drain → credit            : producer.step()'s _drain reads sink, splits +0.2 by margin
#                                    {0.7,0.3}, updates (eid, "repo-svc:python:fix-ci") posterior wins
# HOP 5  surfacer (PROMOTE assert) : once posterior CI excludes 0.5 → f(E1) > 1.0 → E1 ranked up
# HOP 6  clawback (DEMOTE assert)  : second run — BUGFIX fact with touched_blame overlapping E1's
#                                    introduced lines → _clawback emits −1.0 → drain → losses →
#                                    confident-negative posterior → f(E1) < 1.0 → E1 ranked DOWN
```

Both surfacer directions (HOP 5 promote, HOP 6 demote) are asserted on the *same* 2-memory store, proving the un-cripple: the new `f ∈ [0.5,1.5]` moves ranking *both ways*, where the reference `1+max(0,u)` could only ever move it up.

### 2.5 The 7-test contract

Each row: `file::test` — exact assertion — the failure it catches. All run against fakes, milliseconds, no git/model/clock.

| # | `file::test` | Exact assertion | Failure caught |
|---|---|---|---|
| **T1** | `tests/slice/test_join_roundtrip.py::test_commit_window_associates_and_writes_provisional` | After `producer.step()` on a MERGE fact inside `assoc_window_s` of `T1`: `store.task_outcomes[(SHA,T1)].state == "provisional"` AND `.settle_at == merged_ts + 7*86400` AND a row exists for **each** exposed eid {1,2}. | Hop-2 window join broken / settle_at off-by-`settle_days` / dropped a co-injected memory. |
| **T2** | `::test_provisional_settles_after_settle_days_promotes` | `clock.advance(days=8); producer.step()`: row `.state == "settled_pos"`, sink has one `Reward` per eid with `utility == approx(0.2 * margin * (1-0.1))` (E1: 0.2·0.7·0.9, E2: 0.2·0.3·0.9). `settle_sweep` at `settle_at-1` (advance only 6d) emits **nothing**. | A gameable `+` counts *before* reality confirms (settle window not enforced); margin-discount or ε dropped. |
| **T3** | `::test_drain_splits_credit_by_margin_conserves` | After drain: `Σ(d_wins over eids) == approx(reward_magnitude, rel=1e-9)`; `d_wins[E1]/d_wins[E2] == approx(0.7/0.3)`. NOT flat `+1` to both. | The "+1 to all 8" co-occurrence amplification; credit leaks/inflates on the split. |
| **T4 (PROMOTE)** | `::test_confident_positive_posterior_promotes_surfacer` | After enough settled wins that `posterior.ci_excludes_half() is True` with positive mean: `surfacer.utility_map("repo-svc:python:fix-ci")[1] > 1.0` AND `rerank(q,[E1,E2])` ranks E1 above its no-utility position. | The promote direction of the un-cripple is dead (surfacer inert when it should boost). |
| **T5 (DEMOTE)** | `::test_confident_negative_posterior_demotes_surfacer` | After a blame-overlap clawback drives E1's posterior confident-negative: `surfacer.utility_map(...)[1] < 1.0` AND `rerank` ranks E1 **below** E2. | **The headline un-cripple failure**: restoring `1+max(0,u)` (cannot demote) leaves E1 un-demoted — this test is the one that proves the new `f ∈ [0.5,1.5]` actually demotes. |
| **T6 (CLAWBACK)** | `::test_bugfix_blame_overlap_claws_back_only_culprit` | BUGFIX fact with `touched_blame` overlapping **E1's** introduced lines (E2's empty): drain yields `d_losses[E1] == approx(1.0)`, `d_losses[E2] == 0`; a same-file BUGFIX with **empty** `touched_blame` yields `d_losses == 0` for both. | R5 false-positive leaking into the slice; same-file-alone wrongly claws back a good memory. |
| **T7 (ABSTAIN-NO-RESURRECT @ surfacer)** | `::test_surfacer_never_adds_or_resurrects` | `rerank(q, cands=[])` returns `[]` (empty in → empty out); `rerank(q, [E1])` never returns an eid absent from `cands` (E2 cannot appear). With `enabled=False` (Phase 1), `rerank` output order is **byte-identical** to input. | The surfacer resurrecting a gate-emptied candidate list, inventing an eid, or Phase-1 non-inertness (utility applied when it must be observed-not-applied). |

### 2.6 The 6-row RULE-2 mutation matrix

Each row: the **deliberate fault**, the **named test that must go RED**, restore → GREEN. This proves each gate is load-bearing and actually under test (a gate whose test still passes when broken is not tested).

| # | Deliberate fault (where) | Test that goes RED | Why it proves the gate is real |
|---|---|---|---|
| **M1 — blame guard (§6.1.6 mandated)** | In `OutcomeJoiner._clawback`, replace `touched_blame non-empty` with same-file `set(files_touched) & set(introduced_files)` (clawback on any same-file match). | `T6::test_bugfix_blame_overlap_claws_back_only_culprit` (the empty-overlap sub-case now wrongly claws back) | The expensive false-positive direction (R5): proves blame-line overlap, not path equality, is the actual gate. |
| **M2 — surfacer un-cripple** | Restore the reference `f = 1 + max(0, u)` (can only promote). | `T5::test_confident_negative_posterior_demotes` (E1 no longer demotes) | Proves the demote half of `f ∈ [0.5,1.5]` is the load-bearing un-cripple over `service.py:184`. |
| **M3 — margin-split conservation** | In the drain attributor, replace margin-split with flat `+reward` to every exposed eid. | `T3::test_drain_splits_credit_by_margin_conserves` (ratio ≠ 0.7/0.3, Σ ≠ reward) | Proves credit is *split* (co-occurrence-discounted), never *broadcast*. |
| **M4 — settle clock** | Change `settle_at <= now` to `settle_at < 0` (provisional never settles). | `T2::test_provisional_settles_after_settle_days_promotes` (no settled_pos, sink empty) | Proves the N-clean-days settlement window is the anti-gaming barrier (§6.6: CI-green earns nothing until it settles). |
| **M5 — within-tick hop order (T-ORDER)** | In `OutcomeProducer.step`, emit the provisional `+` at ingest time instead of after settle, OR move `_drain` before `settle_sweep`. | `T2` (premature `+` at `now < settle_at`) — and a same-SHA-revert-in-same-tick variant nets to `+` not clawback | Proves the fixed hop order `associate→settle→clawback→emit→drain` prevents a stale gameable-positive surviving a same-tick revert (§6.6 anti-gaming; D6 I2). |
| **M6 — confidence gate** | In `UtilityStore.utility_map(confident_only=True)`, return all posteriors regardless of `ci_excludes_half()`. | `T7`/`T4` variant: a sparse/uncertain posterior leaks an `f ≠ 1.0` into `rerank` and moves ranking before CI excludes 0.5 | Proves utility only moves ranking once the posterior CI excludes the no-signal point — single-source-of-truth at the map, no leak. |

Each row: inject → run → **named test RED** → restore → **GREEN**, reported explicitly (fault introduced, test that caught it, suite green after restoration). M1 is the §6.1.6-mandated blame mutation; M2 is the un-cripple proof that distinguishes this design from the crippled reference.

### 2.7 What green-across-the-matrix retires

If T1–T7 pass and M1–M6 are each green→red→green, then on a faked 2-memory store the §11 chain: associates a commit to its informing trace, writes a provisional, settles it after the clean window (and *only* after), splits the reward by recall_margin with conservation, moves the `(episode, family)` posterior, surfaces a confident posterior **both up (promote) and down (demote)**, claws back only the blame-overlapping culprit, and never resurrects a gate-emptied candidate. That is R1 retired at the instrument level — the join is sound enough to carry the §6.6 keystone signal — at the cost of ~13 fast, I/O-free tests. The real `LocalGitOutcomeSource` then drops in behind the same `OutcomeSource` port with **zero core change** (swap axis = test axis), and R3–R7 are retired by their own (cheaper) experiments above.

---

## Part 3 — Most-downstream assumptions A1–A5

These are the assumptions that, **if wrong, invalidate the most work** — ordered most-dangerous-first. "Downstream" = the further from a controllable knob and the closer to an empirical fact about the world the agent fleet generates, the more dangerous, because no code change rescues a wrong one. A1 is the most dangerous: it is the precondition for the entire MVP being *runnable*, and it is the one assumption purely about the world, not the design.

---

### A1 — Verifiable signal density is sufficient (MOST DANGEROUS)

**The assumption.** The agent fleet, working a chosen family (`fix-failing-CI` in one service), produces **enough machine-verifiable git outcomes** — merges that settle clean, and reverts / bug-on-files clawbacks — to accrue ≥ `N_settled` settled outcomes spanning ≥ `M_memories` distinct memories within the family, *before* the §6.6 keystone runs (§12 Phase-2 readiness gate).

**Why it is the most dangerous.** Everything else in the MVP is a design choice the agent controls; A1 is a property of *the world the fleet operates in*. If verifiable signal is too sparse, the keystone runs **underpowered** and a null result is **misread as a kill** — when §6.6 explicitly says "inconclusive ≠ negative: if too few recalls settle, Q is undertrained; do not read sparsity as refutation." A wrong A1 doesn't just invalidate code — it invalidates *the decision the whole MVP exists to make*, in the most expensive possible direction (killing a live bet because the instrument was starved). It cannot be fixed by any amount of better code; only by a denser family, a wider window, or more fleet activity. It is the assumption R1's instrument and R2's experiment both silently depend on.

**If wrong:** the keystone is unrunnable as scoped; widen the family or extend the window (`producer.assoc_window_s`, `settle_days`) per §12, *never* read sparsity as a negative. The Phase-1 stamp-hit-rate + credit-density logs (`ProducerTick`) are the readiness instrument that detects this *before* the run.

---

### A2 — Git facts are the only credit signal, and they are ungameable by the author

**The assumption.** The verifiable signals (merge / revert / bug-on-files) are git facts the author **cannot write to reward themselves** (§11; spec §6.6 anti-gaming). The agent's only new behavior is the `Hive-Trace` trailer, which re-targets *which* traces get credit but **never sets the reward sign or value**. No trusted MCP message carries an outcome; the L0 `hive_outcome` self-report path is dropped (M08 invariant 1).

**Why it is downstream.** It is the verifiable-credit-only invariant — structurally defended (`SettledOutcome.__post_init__` rejects sign ∉ {−1,+1}; the trailer only selects a trace set at `STAMP_WEIGHT`). If wrong (e.g. an author *can* manufacture a clean-settling merge that rewards a memory they planted), the entire keystone lift becomes self-fulfilling and the §6.6 "lift must trace to the ungameable negative" check fails. Invalidates the credibility of every positive result.

**If wrong:** the keystone positive is uninterpretable (could be gaming). Mitigation: the N-clean-days settlement + the ungameable bug-on-files clawback (§6.6: "delete the failing test → CI-green earns nothing once the clawback lands"); confirm the clawback fires on a reverted PR before trusting any positive (R7's `test_stamp_trailer_cannot_inject_reward`).

---

### A3 — `family_scope` derived at link time correctly scopes transfer

**The assumption.** `family_scope = git-remote × dominant-language × coarse-workflow`, computed by the watcher at link time from git facts (one denormalized O(1) string, no taxonomy, on the credit event not the episode), correctly partitions credit so that a memory proven on `repo-svc:python:fix-ci` boosts *only* same-family queries — and the §6.6 "within-family transfer" win condition is measurable against it.

**Why it is downstream.** It is the keying decision the posterior table, `utility_map(family)`, and the surfacer all depend on — and M08's review flags **family-resolution-at-query-time** as the one genuine information-leakage gap (the surfacer's `rerank(query, cands)` has no family parameter; how the live query's family is resolved is unspecified). If the derivation is too coarse, credit bleeds across families (a Python fix-CI memory boosts a Go dep-upgrade query) and the transfer signal is noise; too fine, and credit density collapses (A1). Invalidates the within-family-transfer half of the win condition.

**If wrong:** the cross-family isolation test (a memory credited on repo-X is NOT boosted for a repo-Y query) fails, or credit density per family falls below the readiness gate. Mitigation: pin the single tested `family_scope` derivation function (D6 §family-derivation) AND specify how the recall path resolves the live query's family into `rerank` — the M08 must-fix. This is also why `fix-failing-CI` (high credit density) is recommended over `dependency-upgrade` (§6.6).

---

### A4 — The downstream attributor + posterior exist and move (the M08/M09 "EXISTS" claim is false as written)

**The assumption (which is WRONG as the reference stands, and must be built).** M09 labels "splits by recall_margin" and "moves the (episode, family) Beta-Bernoulli posterior" as EXISTS/PORT. The reference `controller.py:199 apply_outcome` **discards** `recall_margin`, credits `weight` via `alpha_u·utility` (not a posterior), and has **no** `family_scope`; grep finds zero `task_outcomes`/`family_scope`/`wins`/`losses`/`posterior` in the tree. So the §6.1.6 gate `test_reward_reaches_sink_and_moves_posterior` has **no posterior to move** unless M08 builds it.

**Why it is downstream and dangerous.** The keystone's headline acceptance gate rides on code the PORT does not contain. If the team trusts the "EXISTS" framing, the margin-split + Beta-Bernoulli `utility(episode_id, family_scope, wins, losses)` table + the un-crippled surfacer are *never built*, and the keystone has no instrument. This assumption invalidates Part 2's entire round-trip (T3/T4/T5/T6 have no posterior to assert against). **It is already known-false** — listed here so the build plan treats it as BUILD-NEW (M08's must-fix: own the margin-split + (episode,family) posterior rewrite, or name the module that does).

**If wrong (i.e., taken at face value):** the §6.1.6 gate is unrunnable; the slice's T3–T6 fail-first has no target. Mitigation: M08 explicitly owns the BUILD-NEW `utility`/`utility_sources` tables, the margin-split attributor, the confidence-CI gate, and the `f ∈ [0.5,1.5]` surfacer — the Part-2 slice's FakeStore *is* this posterior, proving the design before the SQLite adapter exists.

---

### A5 — The pure-policy cut holds (the Joiner never touches git/clock, so it is exhaustively testable)

**The assumption.** The D6 synthesis cut — all git/subprocess/blame/squash/locale impurity sealed behind one `OutcomeSource.poll()`, with the entire §11 credit policy on a pure, clock-injected `OutcomeJoiner` (no git/`time.time()`/subprocess import, enforced by the AST import-linter banning adapter/stdlib-clock imports from `hive/domain/**`) — actually holds, so the keystone's highest-risk code (settlement machine + blame-overlap clawback) is mutation-tested on a git-free, time-free object.

**Why it is downstream (least dangerous of the five).** It is a *design* assumption, the most controllable of the five — if it erodes (a clock or git import leaks into the Joiner), `test_purity.py` (the AST import-linter) goes red and the erosion is caught at CI, loudly, before it reaches the experiment. It ranks last because it is the only A-assumption defended by a *compiler-grade structural test* rather than an empirical fact. But it is still load-bearing: if the cut did not hold, the §6.1.6 mutation matrix would land on a unit interleaved with subprocess + watermark side-effects (the exact reason D6 rejected standalone-A), and the mutation tests would be larger and less isolated — degrading every other A-assumption's testability.

**If wrong:** the pure Joiner imports git/clock; mutations become non-isolated; the swap axis ≠ test axis and the "swap needs no core change" claim is unproven. Mitigation: `test_purity.py::test_domain_imports_no_io` as a blocking CI gate (mutation: `import subprocess` into the Joiner → red), and the frozen-dataclass-across-every-boundary discipline making "policy reads impure state" *unrepresentable*, not merely forbidden (D6 precondition-designed-out).

---

### A1–A5 summary

| # | Assumption | If wrong, invalidates | Defended by | Most-dangerous because |
|---|---|---|---|---|
| **A1** | verifiable signal density sufficient | the keystone's *runnability* + go/no-go decision | Phase-1 readiness gate (`N_settled`,`M_memories`); inconclusive≠negative | a property of the *world*, not the design — no code rescues it |
| **A2** | git facts ungameable by author | credibility of every positive keystone result | `__post_init__` sign-reject; stamp=set-only; clawback on revert | self-rewarding makes a positive uninterpretable |
| **A3** | family_scope scopes transfer correctly | the within-family-transfer win condition | single tested derivation fn + query-family resolution (must-fix) | bleed across families turns transfer into noise; too-fine kills density (→A1) |
| **A4** | attributor + posterior EXIST (**false as written**) | the §6.1.6 gate's instrument; Part-2 T3–T6 | M08 BUILD-NEW `utility` table + margin-split + un-cripple | already known-false; "EXISTS" framing would skip the build |
| **A5** | pure-policy cut holds | mutation-test isolation; swap=test axis | `test_purity.py` AST import-linter (blocking CI) | least dangerous — caught loudly at CI, structurally defended |

The throughline: **A1 is the floor.** R1 (Part 2) retires the *instrument's correctness* on faked data; A1 is whether the *world* feeds that instrument enough signal to run R2 (§6.6) at power. A correct join (R1) on a starved family (A1 wrong) still kills the product on noise — which is exactly the misread §6.6 forbids. Build the instrument (Part 2), then verify density before reading the keystone.
