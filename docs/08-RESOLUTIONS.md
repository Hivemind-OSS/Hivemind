# AUTHORITATIVE RESOLUTIONS (override any contradicting module-spec text)

## Cluster A — Move-#6 typed seams

Let me verify one detail: how `recall_margin` flows so I can pin the family resolution and the surfacer signature consistently. I have the M04 surfacer `order(scored, utility_of)`, M08 `rerank(query, cands)` and `utility_map(family)`. Now I'll write the resolutions.

# MOVE-#6 Typed-Seam Resolutions (C9/C10)

**Status: AUTHORITATIVE.** These override any contradicting text in M04, M08, M09, M11, or M03. Each carries the decision, the exact Python signature/formula, the single owner module + file path, and the pinning test (name + exact assertion + RULE-2 mutation). Source-grounded against `01-DECISIONS.md` pins, the §3 DDL, §4.7 guardrails, §11 join, the §10 reuse map, and `federation/controller.py:199-330` + `ops/telemetry.py:1-160`.

---

## Resolution 1 — UtilitySurfacer contract collision

**DECISION.** ONE component, ONE signature, ONE owner. The build-plan name `rerank(query, cands)` is **DELETED**; the contracts name `order(...)` survives but is **extended with `family_scope`** (the missing dimension from #2). The M08 claim of ownership and its `rerank(query, cands)` surface are deleted; the M04 claim that the surfacer is "M04-internal" is deleted. The surfacer is a free-standing pure domain object that **both** M04 (which calls it) and M08 (which supplies its inputs) depend on — it belongs to neither's internals.

**OWNER (single).** `hive/domain/surfacer.py` — class `UtilitySurfacer`. Not M04-internal, not M08-internal. M04's `RecallPipeline` *injects and calls* it; M08's `UtilityStore` *feeds* it the `utility_map`. Delete every other declaration site.

**EXACT SIGNATURE.**

```python
# hive/domain/surfacer.py
# O(n log n) time (stable sort), O(n) space — n = len(scored), bounded by recall_top_n.
class UtilitySurfacer:
    def __init__(self, *, enabled: bool, epsilon_explore: float,
                 f_min: float, f_max: float, rng: random.Random) -> None: ...

    def order(
        self,
        scored: Sequence[Scored],          # gate-passed candidates, base order
        utility_map: Mapping[int, float],  # {episode_id: f_input} for THIS family only,
                                           #   confident-only (sparse posteriors absent)
        *,
        family_scope: str,                 # the live query's family (#2); logged, not re-keyed
    ) -> list[Scored]:
        """Stable-sort scored by `weight * f(util)` DESC; ties keep base order.
        f(u) = f_min + (f_max - f_min) * u, clamped to [f_min, f_max].
        eid absent from utility_map  -> f == 1.0 (identity; un-confident never moves rank).
        enabled is False             -> return list(scored) byte-identical (Phase-1 inert).
        epsilon-explore: with prob epsilon_explore (rng.random() < eps) return
            list(scored) unchanged for THIS call (guardrail-1; eid utilities ignored)."""
```

- Base multiplier is `Scored.weight` (the immutable capture weight, §3) — **never** `alpha`/`sim`. The reference's `alpha·(1+max(0,u))` (`service.py:184`) is the un-cripple target.
- `f` band is `[f_min, f_max] = [0.5, 1.5]`; `f(0.5)=1.0`, `f(1.0)=1.5`, `f(0.0)=0.5` (linear, §4.7 invariant #7). `f<1` DEMOTES.
- `utility_map` is already filtered to the live family by the caller (#2) and to confident-only by the store (M08 invariant #4). The surfacer does **no** family fold and **no** CI gate — those are upstream.

**PINNING TEST.** `tests/domain/test_surfacer.py::test_weight_is_base_multiplier_not_alpha`

> Plant two gate-passed `Scored`: A `(eid=1, weight=2.0, sim=0.9)`, B `(eid=2, weight=1.0, sim=0.95)`, with `utility_map={}` (both identity f=1.0), `enabled=True`, `epsilon=0.0`. **Assert** `order(...)[0].episode_id == 1` (weight 2.0×1.0=2.0 > weight 1.0×1.0=1.0) — proving the base multiplier is `weight`, not `sim` (which would have ranked B first).

**RULE-2 MUTATION.** In `order`, change the base multiplier from `s.weight * f` to `s.sim * f`. **`test_weight_is_base_multiplier_not_alpha` MUST go red** (B ranks first on sim 0.95). Restore → green. (This is the third surfacer mutation, alongside the existing `test_mutation_floor_negative_utility` from M04 §8.3.)

---

## Resolution 2 — Query→family resolution (information-leakage close)

**DECISION.** The single owner of deriving the **live query's** `family_scope` is the **`RecallPipeline`** (`hive/domain/recall.py`). It computes the family from an **agent-declared context label** threaded through `recall()`, with a deterministic fallback, and passes it into both `utility_store.utility_map(family_scope=...)` and `surfacer.order(..., family_scope=...)`. **No cross-family fold** is used (max/sum double-credit — explicitly rejected); a single live family selects exactly one slice of the posterior map.

**OWNER (single).** `RecallPipeline.recall()` in `hive/domain/recall.py`. The query-family derivation is a private pure function `_resolve_query_family(agent_ctx) -> str` on that module. It is the only place the live family is computed.

**EXACT COMPUTATION.** `recall()` gains a required keyword `agent_ctx`. The family string is built by the **same three-axis grammar the producer uses at link time** (§11 `git-remote × language × coarse-workflow`), so query-side and credit-side families are byte-comparable:

```python
# hive/domain/recall.py
def recall(self, query: str, *, agent_id: str, agent_ctx: AgentContext) -> RecallResult: ...

@dataclass(frozen=True, slots=True)
class AgentContext:
    repo_remote: str          # the agent's active git remote (normalized; "" if none)
    language: str             # dominant language of the active file/session ("" if none)
    workflow: str             # coarse label: "bugfix"|"dep-upgrade"|"general"

def _resolve_query_family(ctx: AgentContext) -> str:
    # O(1). Byte-identical grammar to producer family_scope (§11): "<remote>|<lang>|<workflow>".
    # Empty axes collapse to the literal "*" so an unscoped query selects the
    # cross-repo aggregate slice that nothing credits -> utility_map returns {} -> f==1.0.
    return f"{ctx.repo_remote or '*'}|{ctx.language or '*'}|{ctx.workflow or 'general'}"
```

- The label is **agent-declared context, not query text** — `hive-init` teaches the agent to pass its repo/language/workflow (same facts the producer later reads from git), so the family the query resolves to is the family the eventual commit will credit. A memory proven on `repoX|python|bugfix` is **never** boosted for a `repoY|...` query because the keys differ and `utility_map` returns no entries for `repoX`'s posteriors under the `repoY` key.
- When the agent declares nothing, the family resolves to `*|*|general`, which no credit event writes (the producer always derives concrete axes), so `utility_map` is empty and the surfacer is identity — **safe degradation, no leakage**.

**PINNING TEST.** `tests/domain/test_recall_family.py::test_query_family_pins_known_context`

> **Assert** `_resolve_query_family(AgentContext("github.com/acme/web","python","bugfix")) == "github.com/acme/web|python|bugfix"` AND that `recall()` calls `utility_store.utility_map` with exactly that `family_scope` (spy the fake store). Cross-family isolation: with a confident posterior stored under `"github.com/acme/web|python|bugfix"` and a query whose ctx resolves to `"github.com/acme/api|go|general"`, **assert** the boosted eid is NOT reordered (the boost does not cross families).

**RULE-2 MUTATION.** Change `_resolve_query_family` to return a constant `"*|*|general"` (drop the context axes). **`test_query_family_pins_known_context` MUST go red** (family no longer matches; the confident posterior's boost leaks to the wrong query / fails the exact-string assert). Restore → green.

---

## Resolution 3 — C9 reuse overstated → reclassify attributor as BUILD-NEW

**DECISION.** The §10/M08 claim that `apply_outcome` (controller.py:199) is "PORT — the already-wired credit path" is **DOWNGRADED**. The attributor is **BUILD-NEW over a thin watermark shell**. The **only** genuine port from `controller.py` is the `_last_drain_ts` watermark + the `advance_watermark` no-double-credit discipline (controller.py:250-320, including the strict-greater `o.ts <= watermark` guard and the `advance_watermark=False` multi-agent floor). Everything `apply_outcome` *does* with an outcome is deleted and rebuilt.

**EXPLICITLY DELETED (not ported):**
- **The `weight` write is DELETED.** `controller.py:235` `ep_mem.reconsolidate(... weight += alpha_u·utility ...)` is FORBIDDEN (§3: weight immutable). Replaced by a write to the BUILD-NEW `utility(wins, losses)` Beta-Bernoulli table via `EpisodeStore.update_utility` (M03).
- **The self-report gate is DELETED.** `controller.py:222` `if outcome.useful != "yes": return 0` (keyed on `RecallOutcome.useful` derived from self-reported `utility`/`outcome` vocab) is FORBIDDEN (verifiable-credit-only). Credit sign comes **only** from C10's `SettledOutcome.reward_sign ∈ {−1,+1}`.
- **The margin discard is DELETED.** `controller.py:237` `for (h, _entropy, _margin) in outcome.recalled` throws away `recall_margin`. The new attributor splits **by** `recall_margin`.

**MARKED BUILD-NEW (no reference exists):**
- **Margin-split** — `Attributor.split` distributes `reward_magnitude` across co-injected episodes proportional to `recall_margin` (conserved: `Σ deltas == reward_magnitude`).
- **`(episode_id, family_scope)` Beta-Bernoulli posterior** — the `utility(wins, losses, n_sources, version)` table; positive reward → `wins += share`, negative → `losses += share`.
- **Asymmetric clawback** — `+0.2` provisional/settled vs `−1.0` clawback come pre-signed from C10; the attributor applies the sign with no symmetric path.
- **Demotion** — handled downstream by the surfacer (#1); the attributor only writes posteriors.

**OWNER.** Pure policy: `hive/domain/attribution.py` (`Attributor.split`, `PredictionBiasMonitor` — see #6). Persistence: `hive/store/sqlite_utility_store.py` + M03 `update_utility`. The ported watermark shell: the producer-tick drain in `hive/domain/produce.py`/the C10 driver (re-keyed to the producer tick per Fix #1).

**EXACT SIGNATURE.**

```python
# hive/domain/attribution.py  —  pure, no SQL/git/clock
@dataclass(frozen=True, slots=True)
class CreditDelta:
    episode_id: int
    family_scope: str
    d_wins: float      # >=0
    d_losses: float    # >=0
    source_agent: str

class Attributor:
    def split(self, outcome: SettledOutcome,
              exposed: Sequence[tuple[int, float]],   # [(episode_id, recall_margin)]
              isolation: AbstractSet[int]) -> list[CreditDelta]:
        """Conserved margin-split. O(n) time/space.
        share_i = reward_magnitude * margin_i / Σ margins   (all-zero margins -> uniform 1/n).
        reward_sign == +1 -> d_wins=share_i, d_losses=0 ;  -1 -> swapped.
        episode_id in isolation -> excluded entirely (guardrail-2, #5).
        POST: Σ(d_wins+d_losses) == reward_magnitude (rel-tol 1e-9)."""
```

**PINNING TEST.** `tests/domain/test_attribution.py::test_credit_writes_posterior_never_weight`

> Drive a `+1` `SettledOutcome(magnitude=0.2)` over `exposed=[(1,0.6),(2,0.4)]` through `split` + `UtilityStore.apply_credit`. **Assert** `utility[(1,fam)].wins == 0.12` and `utility[(2,fam)].wins == 0.08` (margin-split, conserved to 0.2), `losses == 0.0`, and `episodes[1].weight` is **unchanged** from capture (the store fake has no `update` path to weight — `hasattr` check: `UtilityStore` exposes no weight setter).

**RULE-2 MUTATION.** Re-introduce the reference mechanic: make `apply_credit` also call a (newly added) `store.bump_weight(eid, +share)`. **`test_credit_writes_posterior_never_weight` MUST go red** (weight moved). Restore → green. Plus the existing M08 mutation (iv) `weight += alpha_u·utility` → `test_weight_never_written` red.

---

## Resolution 4 — Epsilon on the wrong config group

**DECISION.** Guardrail-1 ε is a **recall-path** knob (a fraction of *recalls* ignore utility). Add **`recall.epsilon_explore`** (validated `> 0`, consumed by the surfacer). The producer's `producer.epsilon_explore` is **renamed to `producer.assoc_epsilon`** and **kept only** for the association over-attribution discount in §11 (window-default association is `recall_margin`-discounted *and* ε-explored to absorb over-attribution). M11's validation of `producer.epsilon_explore > 0` (the misgrounded blocker) is **deleted**; M11 validates `recall.epsilon_explore > 0` instead.

**OWNER.**
- `recall.epsilon_explore` — defined in M11 config group `recall.*`; consumed by `UtilitySurfacer` (#1) which receives it at construction and is wired by `RecallPipeline`.
- `producer.assoc_epsilon` — defined in M11 config group `producer.*`; consumed by the C10 `OutcomeJoiner` association step only.

**EXACT CONFIG + VALIDATION.**

```python
# hive/config.py  (M11)
@dataclass(frozen=True)
class RecallConfig:
    H_frac_max: float = 0.5
    recall_top_n: int = 10
    epsilon_explore: float = 0.1      # guardrail-1; surfacer ignores utility on Bernoulli(eps)

@dataclass(frozen=True)
class ProducerConfig:
    ...
    assoc_epsilon: float = 0.1        # window-association over-attribution discount ONLY

def __post_init__(self):
    if not (self.recall.epsilon_explore > 0.0):
        raise ValueError("recall.epsilon_explore must be > 0 (guardrail-1: novel memories need exposure)")
    # NOTE: producer.epsilon_explore validation DELETED. assoc_epsilon may be 0 (discount off) — not a guardrail.
```

The surfacer (#1) reads `recall.epsilon_explore` via its `epsilon_explore` ctor arg, wired by `RecallPipeline` from `cfg.recall.epsilon_explore`.

**PINNING TEST.** `tests/config/test_epsilon_placement.py::test_recall_epsilon_validated_positive`

> **Assert** `Config.load(..., recall={"epsilon_explore": 0.0})` raises `ValueError` mentioning `recall.epsilon_explore`; AND `Config.load(..., producer={"assoc_epsilon": 0.0})` succeeds (no guardrail on the association discount); AND `hasattr(cfg.producer, "epsilon_explore") is False` (the misplaced field is gone).

**RULE-2 MUTATION.** Move the `> 0` check back to `producer.epsilon_explore` (re-add the field, drop the recall check). **`test_recall_epsilon_validated_positive` MUST go red** (`recall.epsilon_explore=0.0` now constructs; `producer` has no `assoc_epsilon`/`epsilon_explore` mismatch). Restore → green. (Supersedes M11 mutation-test premise; M11 blocker #2 closed.)

---

## Resolution 5 — Isolation-slice writer (guardrail-2)

**DECISION.** Isolation membership is assigned **deterministically at `approve()` time** (admission/capture), by hashing `episode_id` into a unit interval and comparing against `utility.isolation_frac` (default `0.05`). It is persisted on the `utility` row's `isolation` column (the column already exists per the M08 DDL but had no writer). The writer is **`EpisodeStore.approve()`** (M03) — the same single transaction that flips `status→approved`, so membership is fixed once and never re-rolled.

**OWNER (single writer).** `hive/store/sqlite_episode_store.py` — `approve()`. It computes membership and writes the initial `utility` row (or stamps the flag the Attributor reads). The Attributor (#3) reads it via `UtilityStore.isolation_episode_ids()` and excludes those eids in `split`.

**EXACT FORMULA.**

```python
# hive/store/sqlite_episode_store.py  —  inside approve(), per id, in the same tx
# Deterministic, stable across restarts (no RNG): hash the id, take a fixed fraction.
def _is_isolation(episode_id: int, isolation_frac: float) -> bool:
    # O(1). sha256 is uniform -> the low 53 bits / 2^53 is ~U[0,1); membership is a
    # fixed prefix of that uniform, so exactly ~isolation_frac of ids are held out,
    # and an id's membership NEVER changes (no re-roll -> stable drift control).
    h = int.from_bytes(hashlib.sha256(str(episode_id).encode()).digest()[:7], "big")
    return (h / float(1 << 56)) < isolation_frac
```

Config key: `utility.isolation_frac: float = 0.05` (M11, tier A). `approve()` reads it and stamps `utility.isolation = 1` for held-out ids.

**PINNING TESTS.**

`tests/store/test_isolation.py::test_isolation_membership_assigned_at_fraction`
> Approve 10,000 episodes at `isolation_frac=0.05`. **Assert** the count of rows with `isolation==1` is within `0.05 ± 0.01` of the total (≈500), AND that membership is deterministic: re-running `_is_isolation(eid, 0.05)` for any eid returns the same boolean (idempotent — no RNG).

`tests/domain/test_attribution.py::test_isolation_ids_never_credited`
> An exposed eid that is in `isolation_episode_ids()` produces **no** `CreditDelta` from `split` (guardrail-2: the loop never reweights the held-out slice).

**RULE-2 MUTATION.** Set `isolation_frac` handling so membership is assigned at **0%** (force `_is_isolation` to always return `False`). **`test_isolation_membership_assigned_at_fraction` MUST go red** (zero held-out → drift control absent → count 0, not ≈500). Restore → green. Second mutation: remove the `isolation` exclusion in `Attributor.split` → `test_isolation_ids_never_credited` goes red.

---

## Resolution 6 — Prediction-bias monitor (guardrail-3) made real

**DECISION.** The phantom config-key-+-WARN-log is replaced by a **real method** on the pure attribution domain object: `PredictionBiasMonitor.divergence(family_scope, window) -> float` in `hive/domain/attribution.py`. It computes the gap between **recalled-utility** (what the posterior predicted at recall time, from the exposure ledger × current posterior) and **realized-outcome** (the settled reward that actually landed for those traces in the window). It is the **§12 Phase-2 readiness instrument** — the sole owner of that signal. The "or scope it out" hedge is **removed**: it lives here, mandatory.

**OWNER (single).** `hive/domain/attribution.py` — `PredictionBiasMonitor`. Pure (clock injected for the window). Reads `UtilityStore.posterior` + the settled outcomes the producer tick passes in; computes the divergence; the producer tick logs WARN when `|divergence| > prediction_bias_threshold`.

**EXACT SIGNATURE + FORMULA.**

```python
# hive/domain/attribution.py  —  pure; clock injected, no SQL/git
class PredictionBiasMonitor:
    def __init__(self, store: UtilityStore, *, clock: Clock) -> None: ...

    def divergence(self, family_scope: str, window_s: int) -> float:
        """Mean signed gap between predicted utility and realized reward over the window.
        O(k) for k settled outcomes in window.
        For each settled outcome o in [now-window_s, now] for this family:
          predicted_i = posterior_mean(eid, family) for each exposed eid   # Beta mean = a/(a+b)
          realized    = +1 if o.reward_sign>0 else 0                       # map to [0,1] like the mean
          gap_i       = predicted_i - realized
        divergence = mean(gap_i) over all (outcome, exposed-eid) pairs ; 0.0 when window empty.
        Positive -> ranker over-predicts utility relative to reality (stale, codebase moved)."""
```

Config: `utility.prediction_bias_window_s` (default `604800` = 7d) and `utility.prediction_bias_threshold` (default `0.25`), M11 tier A.

**PINNING TEST.** `tests/domain/test_prediction_bias.py::test_divergence_flags_stale_ranker`

> Plant a family with high posterior mean (`wins=9,losses=1` → predicted ≈0.9) but feed 10 settled outcomes in-window all with `reward_sign=−1` (realized=0). **Assert** `divergence(family, window) ≈ 0.9` (within 1e-6) — a large positive gap flags the stale ranker. Empty window → `divergence == 0.0`.

**RULE-2 MUTATION.** Change the formula from `predicted - realized` to `predicted - predicted` (i.e. drop the realized term / always 0). **`test_divergence_flags_stale_ranker` MUST go red** (divergence collapses to 0, the monitor is blind). Restore → green.

---

## Resolution 7 — Ledger-location contradiction → collapse telemetry sink into the hive DB

**DECISION.** The separate-DB telemetry sink (`ops/telemetry.py`, `~/cortex/telemetry.db`) is **DROPPED as a port**. `exposure`, `task_outcomes`, and `utility` all live in the **single hive WAL DB** (M03-owned, §3 DDL), on the one named WAL volume, under the one single-writer CAS lane. The `exposure` table carries `task_ref` in-file (per §3 DDL). The whole producer tick — settlement sweep → emit → drain/credit → posterior write — is **ONE transaction** on **ONE database**.

**CHOSEN BOUNDARY.**
- **Deleted:** `TelemetrySink` as a separate-SQLite port; `default_telemetry_path()`; the `~/cortex/telemetry.db` file; `apply_outcomes_from_sink`'s cross-DB `sink.read(since=...)` drain semantics.
- **Kept (ported into the hive DB):** the text-free-by-construction guard (`_HEX256` digest check, column allowlist) is **re-applied to the in-file `exposure` writes** — the secret-safe invariant is preserved, just relocated. The `_last_drain_ts` watermark moves to `SqliteMeta` (`persistence.py:1148`) in the same DB.
- **Recall writes exposure** directly to `exposure` via `ExposureLedger.record_exposure` (M04 seam), with `task_ref=NULL`. **Producer back-fills** `exposure.task_ref` + writes `task_outcomes` rows at link time.

**ONE-TX SETTLEMENT→CREDIT PATH.**

```
OutcomeProducer.step(now)  —  one SQLite transaction on the hive WAL DB:
  1. associate : producer reads in-window traces, UPDATE exposure SET task_ref=<SHA>,
                 INSERT task_outcomes(task_ref, trace_id, family_scope, ...) [PK (task_ref,trace_id)]
  2. settle    : sweep task_outcomes WHERE state='provisional' AND settle_at<=now -> 'settled_pos'
  3. clawback  : revert/blame-overlap -> 'clawed_back', reward=-1.0
  4. emit+drain: for each newly-settled/clawed row with ts > meta._last_drain_ts:
                   SettledOutcome -> Attributor.split (by exposure.recall_margin, #3)
                   -> UtilityStore.apply_credit (utility wins/losses, #3)
                 advance meta._last_drain_ts = max(seen ts)   (no-double-credit, ported watermark)
  COMMIT  (single-writer CAS lane; the whole tick is atomic)
```

There is no second DB, no `sink.record`/`sink.read` round-trip, no cross-file watermark. The §11 "written to the telemetry sink keyed by task_ref … drains it" prose is reinterpreted: the "sink" IS the in-DB `task_outcomes` table; the "drain" IS step 4 in the same tx.

**OWNER.** M03 `EpisodeStore` owns all three tables + `_last_drain_ts` in `SqliteMeta` + the single-writer tx. The C10 `OutcomeProducer.step()` drives the tick within that tx. M04's `ExposureLedger` seam writes `exposure` rows into the same DB.

**PINNING TEST.** `tests/store/test_single_db_ledgers.py::test_producer_tick_is_one_transaction_one_db`

> **Assert** the resolved `Config` exposes **no** `telemetry.db` / `default_telemetry_path` (the separate sink is gone), AND that after `OutcomeProducer.step(now)` over a settled trace, `exposure.task_ref`, the `task_outcomes` row, and the `utility` posterior bump are **all** visible in the **same** DB connection within one committed tx — and that injecting a failure in the posterior write (step 4) **rolls back** the `task_outcomes` settlement (step 2) too (atomicity: partial credit is impossible).

**RULE-2 MUTATION.** Split step 4 (the drain/credit) out of the producer tick's transaction into its own connection/commit (re-introducing the two-DB / two-tx boundary). **`test_producer_tick_is_one_transaction_one_db` MUST go red** (the injected posterior-write failure no longer rolls back the settlement — `task_outcomes` shows `settled_pos` while `utility` is unbumped, the exact double-credit/torn-write window the collapse closes). Restore → green.

---

### Cross-resolution consistency notes (for the integrators)

- **#1 + #2 + #4** compose into one surfacer call: `surfacer.order(scored, utility_map=store.utility_map(family_scope=fam, confident_only=True), family_scope=fam)` where `fam = _resolve_query_family(agent_ctx)` (#2) and the surfacer was constructed with `epsilon_explore=cfg.recall.epsilon_explore` (#4).
- **#3 + #5 + #6** all land in `hive/domain/attribution.py` (pure) + `hive/store/sqlite_utility_store.py` (persistence) + M03 `approve()`/`update_utility` (writers). No git, no clock, no SQL in the pure layer.
- **#7** makes M03 the sole owner of `exposure`/`task_outcomes`/`utility` in one DB; this **supersedes** M03 §7's self-contradicting "BUILD-NEW (exposure PORT+EXTEND)" row — it is BUILD-NEW in the single hive DB, with the telemetry text-free guard ported as a write-time assertion, not a separate file.
- M08's `rerank(query, cands)` surface, M08's claim to own the surfacer, M11's `producer.epsilon_explore > 0` validation, and any reference to `telemetry.db` are **deleted** project-wide.

## Cluster B — Substrate

I now have every authoritative fact needed. Producing the five substrate resolutions.

---

# Hivemind v-min — SUBSTRATE Resolutions (C-series, owned half)

Authoritative resolutions for the five flagged substrate contract holes. Each gives: **decision**, **exact signature/formula/byte-layout**, **single owner module + file**, and **the test that pins it (name + assertion + RULE-2 mutation)**. Where a resolution contradicts existing module text, the override is stated explicitly. These are LOCKED — implementers follow these verbatim over any conflicting prose in `M01/M02/M03/M04` or the spec §10 table.

---

## C4 — NormalizedEntropyGate: RELABEL `PORT as-is` → **PORT+EXTEND** (softmax-over-sims, β enters constructor)

### Decision
The spec §10 row (`gate_bundle.py:70 → PORT as-is — re-tune β only`) and M04 §2.2/§7 (`PORT AS-IS … code byte-identical, only beta/h_frac_max change`) are **FALSE and are overridden**. Verified against the reference:
- `gate_bundle.py:48` `_softmax_mass(cands)` reads `float(c.alpha)` off candidate **objects** — softmax mass *already computed upstream* by the cascade.
- `gate_bundle.py:82` `__init__(self, h_frac_max)` has **NO `beta` parameter**.
- The v-min M04 surface is `evaluate(sims: list[float])` over **raw cosine ∈ [−1,1]** with a `geometry.beta` knob (spec §4.1, §5-D2).

Converting raw cosines into a probability distribution requires a **softmax with the re-tuned β** — that is NEW code, not a port. Relabel **C4 = PORT+EXTEND**: port `_softmax_mass`'s two fallbacks verbatim, but (a) change its input from `Candidate.alpha` objects to a `mass = softmax(β·sim)` transform over raw sims, and (b) add `beta` to the constructor.

### Owner
`hive/domain/recall.py` — class `NormalizedEntropyGate` (pure domain; no I/O). The transform helper `_softmax_mass_from_sims` lives in the same file.

### Exact signature + transform (LOCKED)
```python
class NormalizedEntropyGate:
    def __init__(self, h_frac_max: float, beta: float) -> None:
        # beta enters HERE (was absent in the reference __init__).
        # h_frac_max: validated finite (ported guard, gate_bundle.py:83-88).
        # beta: validated finite AND > 0 (a non-positive beta inverts/flattens
        #   the mass, breaking the abstain decision) — fail-fast at construction.
        ...
        self.h_frac_max = float(h_frac_max)
        self.beta = float(beta)

    def evaluate(self, sims: list[float]) -> tuple[bool, float, float]:
        # returns (suppress, entropy_norm, top_margin) — same OUTPUT contract as the reference.
        ...
```

The sims→mass transform that REPLACES the reference's `[float(c.alpha) for c in cands]` line (`gate_bundle.py:58`), **preserving both reference fallbacks verbatim**:

```
mass = softmax(beta * sim) over the candidate sims, i.e.
    raw_i   = exp(beta * sim_i  -  beta * max_j sim_j)   # max-shift for numerical stability
    total   = Σ_i raw_i
    p_i     = raw_i / total

FALLBACK-1 (non-positive floor, ported from gate_bundle.py:59):
    after computing raw_i, any non-finite raw_i (overflow/NaN) is floored to 0.0 before summing.
FALLBACK-2 (degenerate-uniform, ported VERBATIM from gate_bundle.py:61-66):
    if total <= 0.0:  return [1.0/n]*n  (n = len(sims); [] if n==0)
    # a zero-information set reads as maximally uncertain → suppress, never a spurious peak.
```

Everything downstream of `mass` is **byte-identical to the reference** (`gate_bundle.py:108-128`): `mass_sorted`, `top_margin = mass_sorted[0] - mass_sorted[1]`, `n_eff = Σ(p>0)`, `H = -Σ p ln p`, `entropy_norm = H/ln(n_eff)` (with the `n_eff<=1 ⇒ 0.0` ln-1 guard), the `[0,1]` clamp, `suppress = entropy_norm > h_frac_max`, and the fail-closed `except → (True, 1.0, 0.0)`. The `top_margin` is now a **softmax-mass gap over β·sims** — this is the per-call confidence scalar; the per-episode `recall_margin` written to the ledger is a separate quantity owned by M04 (not this resolution's scope).

**Why max-shift:** raw cosine ∈ [−1,1] and β can be O(16–32) (spec §4.1 notes β=16 today, β=32 for the old sparse range), so `exp(β·sim)` without the max-shift overflows float for the top candidate and silently NaNs the distribution — exactly the case FALLBACK-1's non-finite floor exists to catch, but the max-shift makes it not arise for the common path.

### Test that pins it
| Test | Assertion | File |
|---|---|---|
| `test_gate_softmax_mass_uses_beta` ★ NEW | For a fixed sims vector `[0.9, 0.3, 0.1]`, the mass distribution computed at `beta=32` is **strictly more peaked** (lower `entropy_norm`) than at `beta=4`; and the mass equals `softmax(beta*sim)` elementwise within `1e-6`. Pins that β is actually applied (not ignored) and enters via the constructor. | `tests/domain/test_entropy_gate.py` |
| `test_gate_degenerate_uniform_fallback` | all-equal sims OR a sims vector that floors to zero mass ⟹ `entropy_norm ≈ 1.0 > h_frac_max` ⟹ `suppress True` (FALLBACK-2 preserved). | same |
| `test_uniform_high_entropy_suppresses` (M04 existing) | retained, now over raw sims. | same |

**RULE-2 mutation (retain the M04 invert-mutation AND add the β-mutation):**
1. **Invert (retained, M04 §8.2):** flip `entropy_norm > h_frac_max` → `<`. `test_uniform_high_entropy_suppresses` MUST go red; restore → green.
2. **β-drop (NEW, mandated by this relabel):** replace `beta * sim_i` with `sim_i` (drop β from the transform). `test_gate_softmax_mass_uses_beta` MUST go red (the `beta=32` and `beta=4` distributions become identical, so the strict-peakedness assertion fails); restore → green. Proves β is on the tested code path, not dead.

### Spec edit (LOCKED wording for the §10 C4 row)
Override `HIVEMIND_VMIN_SPEC.md:732`:
> | Abstention gate (C4) | EXISTS | `serving/gate_bundle.py:70` `NormalizedEntropyGate` | **PORT+EXTEND** — port `_softmax_mass`'s non-positive-floor + degenerate-uniform fallbacks; CHANGE `evaluate(cands.alpha)` → `evaluate(sims: list[float])` with `mass = softmax(β·sim)` (max-shifted); ADD `beta` to `__init__`. Re-tune `β`/`H_frac_max` for the dense-cosine [−1,1] range (§5-D2/D3). **Not byte-identical** — the softmax-over-sims step is new code. |

And M04 §2.2 / §7 disposition table: strike "PORT AS-IS … code byte-identical" → "PORT+EXTEND (softmax-over-sims with β in the constructor; output contract unchanged)".

---

## C2 — ProjectionHead.to_bytes / from_bytes: **BUILD-NEW** with an exact byte layout (W_version preserved)

### Decision
M01 §4/§7 claims to **PORT** `ProjectionHead.to_bytes()/from_bytes()`. **FALSE — overridden to BUILD-NEW.** Verified: `embedder.py` has only `ProjectionHead.random(...)` and `.pca(...)` factories (lines 65-112) and **no codec at all**. The only serialization in the reference is `migration.py:157-181`: `np.save(buf, head.W)` → `base64.b64encode` → `json.dumps({"d_in","d_out","W"})`. That codec **DROPS `W_version`** — version is the meta-key suffix (`reembed_head_{version}`) only, and `load_head` *injects* it from the caller (`migration.py:181-182`). This is the exact silent-corruption the migration gate (§6.1#4) exists to catch, and it is on the frozen-head-at-construction critical geometry path.

BUILD-NEW a self-describing binary codec on `FrozenPcaHead` that **embeds `w_version` in the blob itself**, so the version can never desync from the W it describes.

### Owner
`hive/adapters/embedding/head.py` — `FrozenPcaHead.to_bytes(self) -> bytes` and `@classmethod FrozenPcaHead.from_bytes(cls, raw: bytes) -> FrozenPcaHead`. The codec is owned by the embedding adapter (M01); the Store (M03) owns only the `meta` kv row that holds the bytes (key `reembed_head_<w_version>`), never the codec.

### Exact byte layout (LOCKED — fixed-size 24-byte header + raw W, little-endian)
```
offset  size   field          encoding
------  ----   -----          --------
0       4      MAGIC          b"HVH1"  (ASCII "HiVe Head", format tag)
4       2      FMT_VERSION    uint16 LE = 1  (codec format version; bump if layout changes)
6       2      DTYPE_CODE     uint16 LE = 1  (1 == float32; the ONLY value W may use, asserted)
8       4      W_VERSION      uint32 LE  (== head.version == geometry.W_version — THE field the reference dropped)
12      4      D_OUT          uint32 LE  (rows of W; == d == 256)
16      4      D_IN           uint32 LE  (cols of W; == native_dim == 384)
20      4      reserved       uint32 LE = 0  (pad to 24; future flags)
24      N      W_BYTES        d_out * d_in * 4 bytes, float32 LITTLE-ENDIAN, C-order (row-major)
```
- Total length MUST equal `24 + d_out*d_in*4`; `from_bytes` asserts this exactly (a truncated/over-long blob raises `HeadCodecError`, never a silent partial load).
- `to_bytes`: `struct.pack("<4sHHIII I", MAGIC, 1, 1, w_version, d_out, d_in, 0)` + `np.ascontiguousarray(W, dtype="<f4").tobytes()`. No `np.save` (its `.npy` header is variable-length and re-encodes shape/dtype redundantly — we own the header, so we drop it).
- `from_bytes`: validate MAGIC (else `HeadCodecError`), validate `FMT_VERSION==1` and `DTYPE_CODE==1`, read the four uint32 dims, then `np.frombuffer(raw[24:], dtype="<f4").reshape(d_out, d_in).copy()` (`.copy()` because `frombuffer` is a read-only view — matches `row_codec.py:38`'s contract). Reconstruct `FrozenPcaHead(W=W, d_in=d_in, d_out=d_out, version=w_version)`.
- **Endianness is pinned little-endian** (`<f4`, `<` struct prefix) so the blob is portable across the build host and the container regardless of native byte order.

### Test that pins it
| Test | Assertion | File |
|---|---|---|
| `test_head_bytes_roundtrip_preserves_w_version` ★ NEW (mandated) | Build a head with `version=7`, `d_in=384`, `d_out=256`, random W. Then `h2 = FrozenPcaHead.from_bytes(h.to_bytes())`. Assert **all three**: (1) `h2.w_version == 7` (the reference-dropped field survives), (2) `np.array_equal(h2.W, h.W)` — **bit-identical** (not atol — exact, since float32 LE round-trips losslessly), (3) `h2.d_in == 384 and h2.d_out == 256`. | `tests/adapters/test_head_codec.py` |
| `test_head_bytes_bad_magic_raises` | corrupt byte 0 ⟹ `from_bytes` raises `HeadCodecError`, no silent garbage head. | same |
| `test_head_bytes_truncated_raises` | drop the last 4 bytes of W ⟹ length-check raises `HeadCodecError`. | same |

**RULE-2 mutation (mandated, geometry critical path):**
- **W_version-drop fault:** in `to_bytes`, hard-code the `W_VERSION` header field to `0` (re-introduce the reference's drop-the-version defect). `test_head_bytes_roundtrip_preserves_w_version` MUST go red on assertion (1) (`h2.w_version == 0 != 7`); restore → green. Proves the version is genuinely carried in the blob, not injected from a caller.
- **Endianness fault (supplementary):** change `<f4` to `>f4` in `to_bytes` only. On a little-endian host `from_bytes` (still `<f4`) reads byte-swapped floats ⟹ `np.array_equal` in assertion (2) goes red; restore → green. Proves the endianness pin is load-bearing.

### Spec edit (M01 §4, §7)
Override M01 §4 ("Format: `ProjectionHead.to_bytes()`") and §7 ("PORT … to_bytes/from_bytes"): mark **BUILD-NEW**, cite the 24-byte `HVH1` layout above, and state explicitly "the reference `migration.py:157-181` codec is NOT ported — it drops W_version into the meta key, the exact failure this codec closes."

---

## C3 — INDEX/STORE crash-recovery: rewrite the guarantee (status is durable truth; index is a rebuilt warm cache)

### Decision
M03 §1.2/§2 and M02 §1 state "in-tx index drive: `index.add` *inside the same transaction that flips `status→approved`*, so the table and the searchable set can never diverge." **This is unenforceable and is overridden.** Verified: the reference `ExhaustiveVectorIndex` (`vector_index.py:22-78`) is an **in-RAM numpy `self._matrix` + `self._ids` + `threading.Lock`** — a process-memory object that **cannot join a SQLite transaction**. A `COMMIT` durably persists the `status='approved'` row; the in-RAM `index.add` mutation is NOT part of that durability. A process kill between `COMMIT` and `index.add` leaves the row approved-on-disk but absent-from-index (a missed recall, not a hallucination — but still a divergence the prose claims is impossible).

**The real guarantee (LOCKED):**
1. **`status='approved'` in SQLite is the single durable truth** of what is recallable. The `_RECALL_PREDICATE = "status='approved'"` SELECT is the authoritative source.
2. The in-RAM `VectorIndex` is a **best-effort warm cache**, deterministically **rebuilt from `scan_approved()` on every boot** (and after any `W_version` migration). It is never the source of truth.
3. The in-tx `index.add` is **demoted from a correctness guarantee to a latency optimization** (avoids a full rebuild after each approval in the warm process). Its failure is recoverable by rebuild, not a data-loss.

This means recall correctness depends on **the SELECT predicate**, not on index/table atomicity. The never-hallucinate invariant is unaffected (a pending row is *unrepresentable* in `scan_approved`, so it can never enter the cache); only the never-*miss* property is now "eventually consistent after boot-rebuild," which is the honest guarantee for an in-RAM index.

### Owner
`hive/adapters/store_sqlite.py` (M03 `SqliteEpisodeStore`) — owns `rebuild_index_from_store()` and calls it in `__init__`/boot. The in-tx `index.add` stays in `approve()` as the warm-cache optimization.

### Exact contract
```python
def approve(self, ids, approver, now) -> list[int]:
    # 1. SQLite tx: CAS UPDATE status='approved' (DURABLE TRUTH). COMMIT.
    # 2. AFTER commit, best-effort: for each admitted id, index.add(id, value).
    #    If this raises or the process dies here, the row is still approved on disk
    #    and will be re-indexed by rebuild_index_from_store() on next boot.
    ...

def rebuild_index_from_store(self) -> int:
    # index := deterministic f(scan_approved()). Idempotent: clears then repopulates.
    # Called at construction (boot) and post-migration. Returns n_indexed.
    # This — NOT in-tx atomicity — is the divergence-recovery guarantee.
    ...
```

### Test that pins it
| Test | Assertion | File |
|---|---|---|
| `test_index_rebuilds_from_approved_only` ★ NEW (mandated) | Stage 3 rows, approve 2 (leave 1 pending). Construct a **fresh empty index** + a fresh Store over the SAME db file (simulating a boot after the in-RAM index was lost). After `rebuild_index_from_store()`: `len(index) == 2`; the 2 approved ids are searchable; the pending id is **absent**. Proves the cache reconstructs from durable truth, approved-only, with no in-tx dependency. | `tests/adapters/test_store_sqlite.py` |
| `test_rebuild_is_idempotent` ★ NEW (mandated) | Call `rebuild_index_from_store()` twice. Assert `len(index)` and the full `search(q, k)` result (ids + cosines) are identical after the 2nd call — no double-insert, no duplicate ids. | same |
| `test_rebuild_recovers_after_crash_between_commit_and_add` NEW | Monkeypatch `index.add` to raise on the 2nd id inside `approve()`. Assert: both rows ARE `status='approved'` on disk (the commit landed), the index is missing the 2nd id; then `rebuild_index_from_store()` makes both searchable. Pins that commit-without-add is recoverable, not corruption. | same |

**RULE-2 mutation (mandated):**
- **Predicate-bypass fault:** in `rebuild_index_from_store`, feed it `scan_all()` (all rows) instead of `scan_approved()`. `test_index_rebuilds_from_approved_only` MUST go red (the pending id becomes searchable — a never-hallucinate breach); restore → green. Proves the rebuild feeds from the approved-only predicate, not the whole table. (This is M03's existing M2 mutation, now also anchored to the rebuild path.)

### Spec edit (M03 §1.2, §2 `rebuild_index_from_store`, §3 design-review must-fix #4)
Strike the wording "calls `index.add` *inside the same transaction that flips status*, so the table and the searchable set can never diverge." Replace: "`status='approved'` is the durable recall truth; the in-RAM index is a derived warm cache rebuilt from `scan_approved()` on boot. The in-tx `index.add` is a best-effort latency optimization — a crash between COMMIT and add is recovered by `rebuild_index_from_store()`, the actual divergence-recovery guarantee." Resolves the design-review must-fix "is index.add inside the SAME SQLite tx (impossible for an in-memory numpy index)".

---

## C5 — SECRET-SAFE floor bypass on import: route import writes through the SecretScanner + the NEW status schema

### Decision
**Two confirmed bypasses, both overridden:**

**(a) `reembed_from_text` does NOT scan.** Verified `migration.py:184-375`: it decodes blob text (`migration.py:251`, `blob.decode("utf-8")`) and re-embeds with **zero `scanner.scan()` call** anywhere in the path. The spec §6.1#5a invariant ("a raw secret is refused/redacted BEFORE staging") is satisfied at `hive_write` (M05) but **NOT** on the import path. Since the one-time corpus import (§12) re-embeds **archived rows** that predate any scan, a planted `AKIA…`/`sk-…` in an archived insight flows straight into the fresh store's blob + episodes + 30-day backups. **"Pending" does not satisfy the floor** — pending rows ARE persisted to disk and ARE backed up (spec §3, §4.6 `backup_keep=30`); the floor is "never persisted," not "not-yet-recallable."

**(b) `reembed_from_text` cannot write the new `status` column.** Verified: `status` appears **0× in `persistence.py`** (the reference predates the admission state machine), and `reembed_from_text` writes via `svc.episode_store.put(fresh)` (`migration.py:350`) — the OLD schema with no status. Porting it verbatim on the write side would insert rows with no status (or a default that bypasses `pending`).

**Resolution:**
1. **Route every imported row through `SecretScanner.scan(text)` before stage** — either inline in `import_corpus` or as an explicit pre-pass over the decoded blob texts. `refuse` → drop the row (logged, counted `import_secret_refused_total`); `redact` → stage the redacted body + re-derive `content_hash = sha256(redacted)`.
2. **`import_corpus` writes via the NEW M03 schema** (`stage()` → `status='pending'`, `version=0`) under a **named import-admin identity** (`proposed_by="import-admin"`), NOT a verbatim `put(fresh)`. Imported rows are pending until human approval — they participate in the same admission state machine as any write. (This preserves the reference's `Capability.CONSOLIDATE`/admin-identity gate at `migration.py:219-223` but binds it to the import-admin role.)
3. **`reembed_from_text` is WRAPPED, not ported verbatim, on the write side.** The geometry-rewrite *math* (native encode → fresh PCA head → re-project, `migration.py:283-353`) is ported; the *write* goes through the new `status`-aware store path. The W_version reembed of an ALREADY-IMPORTED store does **not** re-scan (text is already scanned + already persisted-as-approved — re-scanning a clean store is wasted work and could falsely redact a benign high-entropy token); it only re-projects `value`, leaving `text`/`content_hash`/`status` untouched. **Only the import (old→new ingest) scans**, because that is the one path bringing un-scanned external text across the boundary.

### Owner
`hive/ops/migration.py` — `import_corpus(...)` (the one-time §12 ingest) owns the scan pass and the `stage()` writes under import-admin identity. `reembed_from_text(...)` (the W_version migration of an already-clean store) is the geometry-only rewrite and does NOT scan. The `SecretScanner` port is injected (owned by M05/`scanner_regex.py`); migration depends on the port, not the regexes.

### Exact contract
```python
def import_corpus(self, rows: Iterable[ArchivedRow], *,
                  scanner: SecretScanner,
                  import_admin: Identity) -> ImportReport:
    # For each archived row:
    #   verdict = scanner.scan(row.text)           # FLOOR — before any persist
    #   if verdict.action == "refuse":  skip + log import_secret_refused_total; continue
    #   body = verdict.redacted_text if action=="redact" else row.text
    #   store.stage(StagedEpisode(text=body, content_hash=sha256(body),
    #               status="pending", proposed_by=import_admin.agent_id, ...))
    # Imported rows are PENDING (human-approved like any write), NEVER auto-approved.
    # Returns ImportReport(n_staged, n_refused, n_redacted).
```

### Test that pins it
| Test | Assertion | File |
|---|---|---|
| `test_import_scans_secrets` ★ NEW (mandated) | Plant an archived row whose text contains `AKIA` + an `sk-` token. Run `import_corpus`. Assert: that row is either **refused** (absent from the store, blob never written, `n_refused==1`) OR **redacted** (the staged body contains no `AKIA`/`sk-` substring, `content_hash == sha256(redacted_body)`). Verify the secret string appears in **neither** the `episodes.text`, the blob store, nor any log line. | `tests/ops/test_import.py` |
| `test_import_writes_pending_under_import_admin` NEW | After `import_corpus`, every imported row has `status=='pending'` and `proposed_by=='import-admin'`; none is recallable until `approve()`. Pins bypass (b): the NEW schema + named identity, not a verbatim `put`. | same |
| `test_reembed_does_not_rescan_clean_store` NEW | Import + approve a clean row; bump W_version via `reembed_from_text`; assert `scanner.scan` is **not** called and `text`/`content_hash`/`status` are unchanged (only `value` re-projected). Pins that re-scan is fenced to import only. | same |

**RULE-2 mutation (mandated):**
- **Scan-disable fault:** in `import_corpus`, comment out the `scanner.scan(...)` call (write the raw text straight to `stage`). `test_import_scans_secrets` MUST go red (the planted `AKIA`/`sk-` token now persists into `episodes.text`/blob); restore → green. Proves the scan is the load-bearing floor on the import path, exactly the §6.1#5a acceptance gate extended to import.

### Spec edit (§10 migration row + M03 §1.4 / design-review must-fix "secret-safe unowned")
Override `HIVEMIND_VMIN_SPEC.md:738` migration row and M01/M03 migration text: "**PORT+SIMPLIFY** the geometry-rewrite math of `ops/migration.py` reembed-from-text; **WRAP (not port) the write side** — import writes go through `SecretScanner.scan` (refuse/redact) and `store.stage()` (`status='pending'`, `proposed_by='import-admin'`) under a named import-admin identity. The reference's scan-less `put(fresh)` write is NOT ported. W_version reembed of an already-clean store re-projects `value` only and does NOT re-scan." This also resolves M03 design-review must-fix "SECRET-SAFE INVARIANT IS UNOWNED": the import path scans, and the storage `stage()` is the last-line backstop.

---

## C1 — EpisodeStore god-port: document the method-group segregation + known future cost (tradeoff note, not a redesign)

### Decision
The wide `EpisodeStore` Protocol (15+ methods spanning episodes / blob / ledger / migration, D1 decision lines 117-141) is an **accepted conscious ISP trade** (D1 winner rationale; M03 §1.1) to keep the §12 single-writer SQLite transaction as **one object** — every mutation (`stage`/`approve`/`reject`, ledger append/settle/clawback, migration rewrite) shares one `BEGIN IMMEDIATE` writer with backoff (`persistence.py:90`), so SQLITE_BUSY never surfaces as a dropped write and the table↔ledger↔index writes are atomic per the single-writer discipline. This is **NOT redesigned** — it is documented with explicit method-group segregation and the named future cost.

### Owner
`hive/domain/ports.py` — the `EpisodeStore` Protocol, organized into four **pre-segregated method groups** (already grouped in the D1 decision doc; this resolution makes the segregation a binding contract so a future extraction tears cleanly).

### Exact segregation (LOCKED — the four groups, in declaration order)
```python
@runtime_checkable
class EpisodeStore(Protocol):
    # ── GROUP 1: EPISODES (state machine) ──────────────────────────────
    def stage(self, ep) -> int: ...
    def approve(self, ids, approver, now) -> list[int]: ...
    def reject(self, ids, keep_rejected=False) -> int: ...
    def get(self, episode_id) -> Optional[Episode]: ...
    def pending(self, since) -> list[StagedEpisode]: ...
    def scan_approved(self, tenant_id) -> Iterator[Episode]: ...   # _RECALL_PREDICATE; sole recall feed
    def by_content_hash(self, h) -> Optional[Episode]: ...
    def rebuild_index_from_store(self) -> int: ...                 # boot/post-migration recovery (C3)
    # ── GROUP 2: BLOB (content-addressed verbatim) ─────────────────────
    def put_blob(self, content) -> bytes: ...
    def get_blob(self, content_hash) -> Optional[bytes]: ...
    # ── GROUP 3: LEDGERS (move-#6) ─────────────────────────────────────
    def record_exposure(self, trace_id, rows) -> None: ...
    def exposed_for(self, trace_id) -> list[tuple[int, float]]: ...
    def link_task(self, trace_id, fact, family, settle_at) -> None: ...
    def due_settlements(self, now) -> list[str]: ...
    def settle(self, task_ref, reward) -> None: ...
    def clawback(self, task_ref, reward) -> None: ...
    def update_posterior(self, episode_id, family, dwins, dlosses, source) -> None: ...
    def posterior(self, episode_id, family) -> Optional[UtilityPosterior]: ...
    def zero_utility_layer(self) -> None: ...                      # guardrail-4 human rollback
    # ── GROUP 4: META + MIGRATION ─────────────────────────────────────
    def meta_get(self, key) -> Optional[str]: ...
    def meta_set(self, key, value) -> None: ...
    # (Migrator.reembed_from_text / import_corpus consume meta_get/set + stage; see C5)
```

### The documented tradeoff note (LOCKED — goes in M03 §1 as a "Known cost" box)
> **Wide-port ISP trade — accepted, with named future cost.** `EpisodeStore` fuses four write-knowledge domains (episode state machine / blob / move-#6 ledgers / migration) into one Protocol to keep the §12 single-writer transaction one object — the atomicity that makes table↔ledger writes consistent under one `BEGIN IMMEDIATE` writer. **Known future cost:** if the ledgers ever migrate to a separate DB (e.g. telemetry-volume isolation, the `~/cortex/telemetry.db` split the reference used), extracting GROUP 3 (`LEDGERS`) into a separate `UtilityLedger`/`ExposureLedger` port is a **multi-call-site edit** — every composition-root wiring, every test fake, and `CreditService`/`ProducerLoop`/`RecallPipeline` that today receive one `EpisodeStore` would receive two ports, and the single-writer atomicity guarantee across the table↔ledger boundary would have to be re-established (cross-DB 2-phase or accepted eventual consistency). The four groups are **pre-segregated in declaration order** specifically so this extraction is a mechanical cut along GROUP 3's boundary, not a re-architecture — but it is still N call-site edits, not free. This is a documented tradeoff, **not** a defect to fix in v-min.

### Test that pins it (segregation as an enforced contract, not prose)
| Test | Assertion | File |
|---|---|---|
| `test_episode_store_method_groups_present` NEW | Assert the `EpisodeStore` Protocol declares exactly the four groups' methods (introspect `EpisodeStore.__protocol_attrs__` or the annotated members) — GROUP 3 (ledger) method names are a known set, so a future extraction can be validated against "did the ledger methods leave as a clean unit." Documents the seam as a machine-checkable boundary. | `tests/domain/test_ports.py` |
| `test_fake_and_sqlite_store_satisfy_protocol` (M03 existing, retained) | both `FakeStore` and `SqliteEpisodeStore` pass `isinstance(x, EpisodeStore)` (runtime_checkable) — proves the wide port has at least two conforming impls so the single-object trade is real, not a one-off. | same |

**RULE-2 mutation:** none required — this is a **documented-tradeoff note, not a state-machine/gate/ranker/credit path**, so the RULE-2 mandate (which targets behavioral correctness paths) does not apply. The behavioral correctness of the methods inside the port is pinned by their own per-group mutation tests (M03 M1–M4: in-tx index drive, `_RECALL_PREDICATE`, settle window, CAS lost-update). C1 adds only the structural segregation assertion above; there is no fault to inject into a documentation contract.

### Spec edit
M03 §1.1 and §7: add the "Known cost" box verbatim above; mark the design-review red flag "Special-General Mixture / wide-port ISP" as **accepted with documented cost** (not a must-fix), cross-referencing the four-group declaration order in `ports.py`.

---

## Summary of overrides applied (for the cross-module reconcile)

| ID | Was | Now (LOCKED) | Owner file |
|---|---|---|---|
| C4 | gate `PORT as-is`, β re-tune only | **PORT+EXTEND**: `evaluate(sims)`, `mass=softmax(β·sim)` max-shifted, β in `__init__`; fallbacks ported verbatim | `hive/domain/recall.py` |
| C2 | head codec `PORT` to_bytes/from_bytes | **BUILD-NEW**: 24-byte `HVH1` header w/ `w_version` field, float32 LE | `hive/adapters/embedding/head.py` |
| C3 | "in index IFF status=approved in same tx" | **status=durable truth; index=warm cache rebuilt from `scan_approved` on boot; in-tx add = best-effort** | `hive/adapters/store_sqlite.py` |
| C5 | reembed `PORT+SIMPLIFY` verbatim write | **import scans via SecretScanner + `stage()` pending under import-admin; reembed wrapped not ported on write side; clean-store reembed does NOT re-scan** | `hive/ops/migration.py` |
| C1 | wide port (smell) | **accepted trade + 4-group segregation + named multi-call-site extraction cost** | `hive/domain/ports.py` |

**New mandated tests:** `test_gate_softmax_mass_uses_beta` (+β-drop mutation), `test_head_bytes_roundtrip_preserves_w_version` (+W_version-drop mutation), `test_index_rebuilds_from_approved_only` + `test_rebuild_is_idempotent` (+predicate-bypass mutation), `test_import_scans_secrets` (+scan-disable mutation), `test_episode_store_method_groups_present`.

**Reference citations (verified this session):** `gate_bundle.py:48,58,61-66,82,91,108-128` (no β, reads `c.alpha`); `embedder.py:65-112` (no codec, only `random`/`pca` factories); `migration.py:157-181` (codec drops W_version → meta key), `:184,219-223,251,350` (no scan, admin-identity gate, `put(fresh)` no status); `persistence.py` (`status` 0×); `vector_index.py:22-78` (in-RAM numpy `_matrix`+`_ids`+`threading.Lock`, cannot join SQLite tx).

## Cluster C — Eval membrane / keystone

Here are the three authoritative resolutions.

---

# Eval-Membrane (C8 / §6.6) — Authoritative Resolutions

Owner module for all three: **`hive/research/` (M10 eval membrane)** — `hive/research/metrics_ir.py` (pure scorers) and `hive/research/keystone.py` (the §6.6 harness). None of this is ever imported by `hive.core` / `hive.adapters` / `hive.serving` (M0 import-linter fence, `test_research_not_imported`).

Conventions used below, all grounded in the spec/reference:
- **Paired per-query delta** = vector `Δ[q] = metric_armA[q] − metric_armB[q]`, one entry per held-out task `q`. Fed to `bootstrap_ci` (M10 §2.2).
- **`bootstrap_ci(deltas, *, n_boot=10_000, alpha=0.05, seed=0) -> (point, lo, hi)`** — percentile bootstrap; **CI-significant improvement iff `lo > 0`** (M10 §2.2, §6.2). This is the *single* significance primitive every arm comparison and both keystone sub-conditions reuse. No new statistics are invented below — both keystone flags are reductions to this one helper.

---

## Resolution 1 — KEYSTONE WIN-CONDITIONS (BLOCKER)

**Decision:** Pin `non_saturating` and `within_family_transfer` as two reductions to the *already-specified* `bootstrap_ci` primitive (no new statistics), computed inside `run_keystone_eval` in `hive/research/keystone.py`. Both are `bool` fields of the frozen `KeystoneResult`, each produced by a named pure helper that takes explicit, fixturable inputs. The four control arms inject their posteriors through **one named seam** (resolved below) so the harness never touches C10 git internals.

### 1a. `non_saturating` — exact formula

**Claim under test (§6.6 (i)):** utility's task-success lift does not flatten as credit volume grows — accrual keeps compounding, it is not a one-shot recency win.

**Formula.** Operate on the **utility arm only**. Bin the held-out task stream into `B` equal-width bins over **cumulative settled-credit volume** `V` (count of `state=settled_pos` rows in the family at the time each task is evaluated — the §11 settled stream, the ungameable axis). For bin `b`, let `s_b` = family-scoped task-success rate (mean over tasks in bin `b`) and `v_b` = bin-center cumulative settled-credit volume.

Fit ordinary least squares `s_b = β0 + β1·v_b`. The slope estimator:

```
β1_hat = Σ_b (v_b − v̄)(s_b − s̄) / Σ_b (v_b − v̄)²        # O(B) time, O(B) space
```

`non_saturating := (β1_lo > 0)` where `(β1_hat, β1_lo, β1_hi)` is a **paired bootstrap over the per-task residual-resampled slope** — i.e. resample tasks with replacement `n_boot=10_000` times, re-bin, re-fit `β1` each draw, take the `[alpha/2, 1−alpha/2]` percentile interval (same `bootstrap_ci` machinery, but the resampled statistic is the OLS slope rather than a raw mean). Reuses the one seed/alpha contract.

**Binning + CI parameters (pinned, fixed before the run per §6.2):**
- `B = 5` equal-width bins over `[0, V_max]` (`keystone.n_volume_bins`, default 5).
- Empty bins (no tasks) are **dropped, not zero-filled** — a zero-filled bin is the ERROR_MASKING trap (a synthetic 0 success fakes a downslope). If fewer than 3 non-empty bins survive, `non_saturating := False` is **not** asserted; the run is marked `inconclusive=True` (too sparse to fit a slope — Resolution 1c's pre-gate path, "inconclusive ≠ negative").
- CI method: percentile bootstrap, `n_boot=10_000`, `alpha=0.05`, `seed=0` (deterministic).
- **Saturating** (the failure we must catch) ⇒ slope ≈ 0 or negative ⇒ `β1_lo ≤ 0` ⇒ `False`.

### 1b. `within_family_transfer` — exact formula

**Claim under test (§6.6 (ii)):** a memory credited on task A *transfers* — it lifts a **held-out task B in the same family** that it was never directly credited on. Measured by **ablation**: present-vs-removed.

**Ablation + lift.** Partition the family's credited memories. For each held-out transfer-eval task `B` (a task whose own direct credit is masked out of the posterior — held-out isolation, §6.6 loop-health), compute recall@5 of arm-utility under two store states:
- **present:** the memory `m` credited on a *different* task A (same family, `A ≠ B`) is in the store with its settled posterior.
- **ablated:** `m`'s posterior is zeroed (utility neutralized to the prior; the episode stays in the store so geometry is unchanged — only the credit signal is removed).

Per-task lift `L[B] = recall@5_present(B) − recall@5_ablated(B)`. Then:

```
within_family_transfer := bootstrap_ci(L, n_boot=10_000, alpha=0.05, seed=0).lo > 0
```

**Pins:**
- Lift metric = **recall@5** (the §6.1 #1 gate metric — same ranker measure, kept consistent so transfer is in the same units as the headline gate). `keystone.transfer_metric_k = 5`.
- Ablation = **posterior-zeroing of the cross-task-credited memory only** (not deletion) — isolates the *credit* signal from the *embedding* signal. Deleting the episode would confound transfer with raw store-size and is forbidden.
- Held-out isolation: task B's own direct credit is masked, so a positive lift cannot be self-credit leaking back (anti-gaming, §6.6).
- Empty `L` (no eligible A≠B pair in the family) ⇒ `inconclusive=True`, **not** `False` (sparse family, Resolution 1c).

### 1c. The fixture seam (4 control arms → hermetic store)

**Decision — named seam:** a **config-selected scoring mode on the recall surface**, `recall.ranking_mode ∈ {utility, utility_off, recency, frequency}`, threaded through the same `_build_eval_service` builder the oracle already uses (M10 §3 swap-seam; the membrane only ever reaches the recall surface, never C10). The four arms differ **only** in this one enum — identical store, identical geometry, identical episodes; the arm changes the rank signal, nothing else. This is the seam §5/§3 hand-waved; it is now named.

The **posteriors themselves** are injected via a **posterior-fixture loader** on the hermetic store: `EpisodeStore`-adjacent `utility` rows are seeded directly from a fixture dict `{(episode_id, family_scope): (wins, losses)}` — the harness writes Beta-Bernoulli `(α, β)` tallies straight into the `utility` table of the temp DB. The keystone **never** runs the C10 git producer; it consumes already-credited `(episode, family)` posteriors as fixture data (M10 §5 boundary: "consumes already-credited posteriors, does not shell out to git"). Each arm reads:

| arm | rank signal `f(·)` the surfacer applies |
|---|---|
| `utility` | `weight × f(utility_posterior)`, f demotes when CI excludes 0 (§4.7) |
| `utility_off` | `weight` only (λ_Q = 0) |
| `recency` | `weight × recency_decay(age)` |
| `frequency` | `weight × log(1 + exposure_count)` |

### 1d. The two named tests (exact assertions)

```python
def test_keystone_non_saturating_required():
    # fixture: utility arm posteriors seeded so per-bin success RISES with
    # cumulative settled volume (β1 strongly > 0). recency/frequency seeded flat.
    res = run_keystone_eval(fixture=rising_accrual_fixture(), seed=0)
    assert res.non_saturating is True
    assert res.killed is False
    # MUTATION-paired negative: a SATURATING fixture (success flat after bin 0)
    res2 = run_keystone_eval(fixture=saturating_fixture(), seed=0)
    assert res2.non_saturating is False          # slope CI lower-bound ≤ 0
    assert res2.win is False                       # win REQUIRES non_saturating

def test_keystone_within_family_transfer_required():
    # fixture: memory m credited on task A (same family) lifts held-out task B's
    # recall@5 by a CI-significant margin when present vs posterior-ablated.
    res = run_keystone_eval(fixture=transfer_fixture(), seed=0)
    assert res.within_family_transfer is True
    assert res.win is True   # beats recency AND frequency AND non_sat AND transfer
    # negative: ablation lift CI straddles 0 (no transfer) ⇒ flag False ⇒ no win
    res2 = run_keystone_eval(fixture=no_transfer_fixture(), seed=0)
    assert res2.within_family_transfer is False
    assert res2.win is False
```

**RULE-2 mutation (mandatory, named):** in `_non_saturating`, replace `β1_lo > 0` with `β1_hat > 0` (point estimate, dropping the CI floor). **Red:** a noisy-but-flat fixture whose point slope is fractionally positive but whose CI straddles 0 now passes → `test_keystone_non_saturating_required`'s `res2` flips to `True` and fails the `is False` assertion. **Restore** → green. Proves the CI floor (not the point estimate) is the load-bearing predicate — the §6.2 ship rule. Symmetrically, in `_within_family_transfer` invert the ablation direction (`ablated − present`) → `test_keystone_within_family_transfer_required` `res` goes red.

---

## Resolution 2 — AUROC PRODUCER-CONSUMER WIRING GAP (§6.1 #2)

**Decision — pick the WIRED option.** `run_longmemeval` emits a **per-question continuous gate confidence** alongside the existing ternary outcome, and a new test feeds that live stream into `abstention_auroc`. The AUROC gate is proven **end-to-end on oracle output**, not fixture-only. (The fixture test `test_auroc_target_band_on_fixture` is retained as a fast unit-level sanity band; the *gate* is the live-wired one.)

**Exact confidence formula.** The natural confidence (M10 §2.2): `conf = 1 − H_norm` where `H_norm = H / ln(N_eff)` is the normalized recall entropy the production gate already computes (the same quantity the `H_frac_max=0.5` abstention gate thresholds on). HIGHER `conf` ⇒ more confident ⇒ less likely to abstain. This is already on the recall result; the oracle currently discards it. The fix surfaces it.

**Wiring — `run_longmemeval` change.** The oracle gains a parallel output vector, one entry per scored/abstention question:

```python
@dataclass
class LMEResult:
    ...
    # NEW — per-question continuous abstention confidence + its is_miss label,
    # in question order. abstention_scores[i] = 1 - H/ln(N_eff) on question i;
    # is_miss[i] = (gold NOT in top-k). The two vectors are the exact
    # (scores, is_miss) pair abstention_auroc consumes — no fixture in between.
    abstention_scores: list[float] = field(default_factory=list)
    is_miss: list[bool] = field(default_factory=list)
```

`conf = 1 − ep_res.h_norm` is read off the recall result on **every** question (both the ranker pass and the abstention pass expose `h_norm`); `is_miss = gold_doc_ids ∉ retrieved[:k]`. The ternary EMPTY/surfaced outcome is **unchanged** — the continuous vector is *additive*, closing the split without disturbing the §6.1 #3 never-hallucinate contract.

**New test (exact):**

```python
def test_run_longmemeval_emits_continuous_abstention_scores():
    res = run_longmemeval(FIXTURE_LME, k=5, h_frac_max=0.5)
    # producer actually emits the continuous stream, one per scored+abstention q
    assert len(res.abstention_scores) == res.scored + res.abstention.n
    assert len(res.is_miss) == len(res.abstention_scores)
    assert all(0.0 <= c <= 1.0 for c in res.abstention_scores)
    # CONSUMER fed DIRECTLY from producer output — no hand-built fixture
    auroc = abstention_auroc(res.abstention_scores, res.is_miss)
    assert 0.70 <= auroc <= 0.84      # §6.1 #2 target band ≈ 0.77, live-wired
```

**RULE-2 mutation (named):** in `run_longmemeval`, emit `conf = H/ln(N_eff)` (drop the `1 −`, inverting the direction so misses get HIGH confidence). **Red:** `test_run_longmemeval_emits_continuous_abstention_scores`'s AUROC drops below 0.70 (toward `1 − 0.77`). **Restore** → green. This is *distinct from* the M10 §8 fault that inverts the rank-sum *inside* `abstention_auroc` — that one proves the scorer; this one proves the **producer→consumer wiring**, which was the gap. Both must be present.

**Owner:** `run_longmemeval` (producer) in `hive/research/eval_membrane.py`; `abstention_auroc` (consumer) in `hive/research/metrics_ir.py`. No separate live-wiring module is introduced — the oracle *is* the live wiring.

---

## Resolution 3 — `admit` `min_gain` DEAD-UNDER-DEFAULT

**Confirmed bug (grounded):** reference `eval_membrane.py:779` `report.mean_jaccard < champion_score + min_gain` with `champion_score` default `1.0` and `jaccard ∈ [0,1]`. The reference's own `tests/test_admission_membrane.py:252 test_min_gain_makes_floor_strict` asserts that with `champion_score=1.0, min_gain=0.01` an **identical** candidate (jaccard=1.0) is **rejected** — i.e. *no candidate can ever pass with `min_gain>0`*. The docstring's "strict-improvement (`>` champion)" is arithmetically impossible: a candidate cannot be more self-consistent with the champion than 1.0. `min_gain` is a dead lever; only `max_regressions` is live. The reference test mislabels a dead lever as "strict floor."

**Decision — REDEFINE `champion_score` as the candidate's measured champion-replay floor (the fix, not the doc).** Strict improvement becomes *expressible*, because the comparison axis is no longer a self-jaccard pinned at 1.0.

**Exact redefinition.** `champion_score` is no longer the champion's self-jaccard (≡1.0). It is the **candidate's measured agreement floor against the champion baseline**, and the admit rule compares the candidate's *replayed* mean_jaccard against the prior champion's *own recorded* floor stored in the baseline artifact. Concretely:

- `export_baseline` already records each query's `retrieved` set. Add a baseline-level field `champion_floor` = the champion's mean_jaccard **against the incumbent it replaced** (the floor that incumbent had to clear to be champion). For a genesis baseline (no prior incumbent), `champion_floor` defaults to `0.0`, not `1.0` — a genesis champion has no floor, so any non-regressing candidate is admissible and `min_gain` measures *real* improvement over `0.0`.
- Admit rule becomes: `report.mean_jaccard >= champion_floor + min_gain`. With `champion_floor` in `[0,1)` for any real incumbent, `min_gain > 0` is **reachable** (a candidate scoring `champion_floor + min_gain ≤ jaccard ≤ 1.0` passes).
- The `report.n > 0` fail-closed guard (M10 §2.4, the single most important guard) and `max_regressions` are **unchanged**.

**New test (exact, the must-fix name):**

```python
def test_admit_positive_min_gain_is_reachable_or_documented_dead(tmp_path):
    # champion_floor is the incumbent's real floor (< 1.0), so min_gain>0 is LIVE.
    base = tmp_path / "champion.ndjson"
    export_baseline(str(base), QUERIES, champion_svc)   # records champion_floor=0.6
    # candidate strictly improves agreement to 0.8 with zero regressions
    assert admit(str(base), better_cand, k=5, min_gain=0.1,
                 max_regressions=0) is True          # 0.8 >= 0.6 + 0.1  → REACHABLE
    # a candidate that only matches the floor does NOT clear a positive min_gain
    assert admit(str(base), floor_cand, k=5, min_gain=0.1,
                 max_regressions=0) is False         # 0.6 < 0.6 + 0.1
    # genesis baseline: champion_floor defaults to 0.0 (NOT 1.0) — min_gain live
    gen = tmp_path / "genesis.ndjson"
    export_baseline(str(gen), QUERIES, genesis_svc)  # no prior incumbent
    assert admit(str(gen), any_nonregressing_cand, k=5, min_gain=0.1,
                 max_regressions=0) is True
```

**RULE-2 mutation (named):** revert `champion_floor` to the hardcoded `1.0` self-jaccard (the original bug). **Red:** `test_admit_positive_min_gain_is_reachable_or_documented_dead`'s first assertion flips to `False` (0.8 < 1.0 + 0.1 is always true → reject), failing the `is True`. **Restore** the measured-floor → green. This proves `min_gain` is a *live* rejection lever, not decorative — and pins the exact regression (`champion_score=1.0`) that the reference shipped.

**Owner:** `admit` + `export_baseline` (the `champion_floor` field) in `hive/research/eval_membrane.py`. The reference's mislabeled `test_min_gain_makes_floor_strict` is **replaced** by the test above (its premise — that a 1.0 floor is correct — is the bug).

---

### Files (all absolute)
- Spec read: `/home/null/Desktop/work/hivemind/HIVEMIND_VMIN_SPEC.md` (§6.1 L401–423, §6.6 L470–506, §11 join L790–827, §12 readiness L852–857).
- Module spec (target of these pins): `/home/null/Desktop/work/hivemind/docs/03-modules/M10-eval.md` (§2.2 AUROC/bootstrap, §2.4 admit, §2.5 keystone, §8 must-fix items 1–5).
- Reference impl carrying the bug + the wiring gap: `/home/null/Desktop/work/AgentCortex/cls_memory/cls_memory/research/eval_membrane.py` (`admit` L727–795, the `champion_score=1.0` default L734 / dead compare L779; `run_longmemeval` L292–447 emits ternary only, no continuous confidence; `abstention_auroc`/`bootstrap_ci`/`run_keystone_eval` confirmed absent).
- Reference test proving the dead lever: `/home/null/Desktop/work/AgentCortex/cls_memory/tests/test_admission_membrane.py:252` (`test_min_gain_makes_floor_strict` — to be replaced).
- New build target for the keystone harness: `hive/research/keystone.py` (`run_keystone_eval`, `KeystoneResult`, `_non_saturating`, `_within_family_transfer`, the `ranking_mode` arm seam, the posterior-fixture loader).

---

## Cluster D — Residuals pinned at verification (AUTHORITATIVE)

The integrated verifier confirmed Clusters A–C close blockers B1–B11, but flagged four quantities referenced by the contracts/build-plan yet left to inference. They are pinned here and **override** any looser wording elsewhere. D1/D3/D4 mostly formalize what the regenerated docs already imply; **D2 is the one genuinely-open hole** (the confidence test that decides whether utility *ever* reorders recall).

### D1 — Per-episode `recall_margin` formula + last-hit edge (the credit-split weight)

For the CONFIDENT hit set sorted by score descending (rank `i`, `0`=top), using the **same** softmax masses the gate computes (`mass_j = softmax(β·sim_j)`, max-shifted, `Σ mass = 1`):

```
margin_i      = mass_i − mass_{i+1}      for i < N−1
margin_{N−1}  = mass_{N−1} − 0           # last returned hit: "next" mass is 0 ⇒ equals its own mass; never negative
```

This is the softmax-mass gap of THIS hit to the next-ranked hit (the on-disk contracts definition, made exact at the tail). The gate's confidence scalar `top_margin` is the `i=0` case. Credit split (`Attributor.split`, A3): `share_i = magnitude · margin_i / Σ_j margin_j`, conserved, with the all-zero fallback `share_i = magnitude/n`.
- **OWNER:** `hive/domain/recall.py::RecallPipeline` computes + logs `recall_margin` to `exposure` (M04); `hive/domain/attribution.py::Attributor.split` consumes it (M08).
- **TEST:** `test_exposure_ledger_written_with_margin` pins exact per-hit values on a 2-hit fixture (masses `[0.7,0.3]` ⇒ margins `[0.4,0.3]`). **MUTATION:** define `margin_i = mass_i` (own mass, not gap) ⇒ fixture value flips ⇒ test RED.

### D2 — Posterior confidence test + priors (the open hole: the gate that decides whether utility EVER reorders recall)

`BetaPosterior` over `(episode_id, family_scope)` with a uniform prior **Beta(1, 1)**, normal-approx (scipy-free, deterministic):

```
a  = prior_a + wins ;   b = prior_b + losses          # prior_a = prior_b = 1.0
u  = mean = a / (a + b)                                # ∈ (0,1); fresh (0,0) ⇒ u = 0.5  (neutral)
sd = sqrt( u·(1 − u) / (a + b + 1) )                   # Beta posterior std-dev
confident  ⇔  |u − 0.5| > Z · sd ,   Z = 1.645         # 90% interval excludes the no-signal point u₀ = 0.5
```

`u₀ = 0.5` is the surfacer's neutral point (`f(0.5)=1.0`). `UtilityStore.utility_map(family_scope, confident_only=True)` returns `{eid: u}` for **confident** posteriors only; un-confident eids are absent ⇒ surfacer leaves them identity (`f=1.0`). A confident-negative (`u < 0.5`) DEMOTES.
- **CONFIG (tier B):** `utility.prior_a = 1.0`, `utility.prior_b = 1.0`, `utility.ci_z = 1.645`.
- **OWNER:** `hive/domain/models.py::BetaPosterior.mean()` + `is_confident(z)`; `hive/store/sqlite_utility_store.py::utility_map(confident_only=True)` applies the gate.
- **TEST:** `test_posterior_confident_only_when_ci_excludes_half` — `(0,0)`→absent; `(8,1)`→`u≈0.82` confident-positive (in map); `(1,8)`→confident-negative (in map, `u<0.5`); `(1,0)`→still absent (CI too wide). **MUTATION:** force `confident=True` always (drop the `Z·sd` test) ⇒ a single-win memory reorders recall ⇒ test RED. This is the guard that "utility does not move ranking until the posterior is confident" (§4.7 invariant).

### D3 — `utility_map` value IS the posterior mean (one-line pin)

`utility_map[eid] = BetaPosterior(eid, family).mean() = a/(a+b)` (D2). This `u ∈ (0,1)` is exactly what the surfacer's `f(u) = f_min + (f_max − f_min)·u` consumes; `u=0.5 ↔ f=1.0` (neutral), `u→1 ↔ f→1.5` (promote), `u→0 ↔ f→0.5` (demote). No other transform.

### D4 — Consolidated config defaults (single source of truth; tiers per spec §4)

Resolves the "scattered inline" gap. These dataclasses in `hive/app/config.py` are the **only** place these values live; domain code reads them, never a literal.

```python
@dataclass(frozen=True)
class RecallConfig:                     # tier B (hot) unless noted
    h_frac_max: float = 0.5             # abstention floor; the ONE derived copy is injected into the gate (B8 single-source)
    beta: float = 16.0                  # tier D — recalibrate on the dense-cosine range (spec §5-D2) before ship
    recall_top_n: int = 10
    epsilon_explore: float = 0.1        # guardrail-1 (A4); validated >0; fraction of recalls that IGNORE utility

@dataclass(frozen=True)
class UtilityConfig:                    # tier B unless noted
    prior_a: float = 1.0                # Beta(1,1) (D2)
    prior_b: float = 1.0
    ci_z: float = 1.645                 # 90% confidence (D2)
    f_min: float = 0.5                  # surfacer band (A1): f(0)=0.5 demote
    f_max: float = 1.5                  #                     f(1)=1.5 promote ; f(0.5)=1.0 neutral
    isolation_frac: float = 0.05        # guardrail-2 held-out slice (A5); assigned at approve()
    utility_rerank: bool = False        # PHASE-1 OFF (observe-not-apply); Phase-2 flips True (tier C, keystone-gated)
    prediction_bias_window_s: int = 1_209_600   # 14d; the §12 Phase-2 readiness monitor (A6)

@dataclass(frozen=True)
class ProducerConfig:                   # tier A/C
    watch_repos: tuple[str, ...] = ()   # absolute paths; empty ⇒ producer idle (logged WARN, loop starved-not-broken)
    poll_interval_s: int = 300          # tick cadence; settlement sweep + outcome-drain ride this tick (Fix #1)
    assoc_window_s: int = 1800          # window-primary commit→trace association
    assoc_epsilon: float = 0.1          # over-attribution discount ONLY (NOT guardrail-1; A4) — may be 0
    stamp_trailer: str = "Hive-Trace"   # optional precision override (exact-trace set at higher credit)
    bugfix_pattern: str = r"^(fix|bug|hotfix|patch):"   # + BUG-NN / regression / crash / race ; clawback candidacy
    require_stamp: bool = False         # MVP false (window association allowed); tighten post-keystone only
    provisional_reward: float = 0.2     # merge ⇒ small provisional + (pre-signed, A3)
    clawback_reward: float = -1.0       # revert / blame-overlap bug-on-files ⇒ large − (pre-signed, A3)
    settle_days: int = 7                # provisional + settles after N clean days
```

- **TEST:** `test_config_defaults_single_source` — every §4.7/§4.8 value resolves from exactly one dataclass field; `grep` of `hive/domain/` finds no inline literal for `settle_days`/`provisional_reward`/`clawback_reward`/`assoc_window_s`/`epsilon_explore`. **MUTATION:** hardcode `settle_at = merge_ts + 7*86400` inside `join.py` ⇒ `test_settle_days_from_config` RED (config override no longer honored).

### Files
- Owners: `hive/domain/recall.py` (D1), `hive/domain/models.py` + `hive/store/sqlite_utility_store.py` (D2/D3), `hive/app/config.py` (D4).
- Consistent with the on-disk `02-CONTRACTS.md` (`BetaPosterior.mean`, `utility_map(confident_only=True)`, `recall_margin` DDL comment) and `05-BUILD-PLAN.md` (P0.2 split, P0.3 CI gate, P1.5 margin).
