# M09 Outcome producer  (produce)

**One-line:** An in-process, single-writer git watcher tick (`OutcomeProducer.step(now) -> ProducerTick`) that seals ALL git I/O behind one `OutcomeSource.poll()` port and routes every §11 credit decision (window/stamp association, asymmetric reward, settlement sweep, revert + blame-overlap-confirmed bug-fix clawback, squash-resolution, family_scope derivation) through one pure, clock-injected `OutcomeJoiner` state machine, emitting rewards to the telemetry sink keyed by `task_ref` — the swap axis = the test axis = the pure-policy axis.
**Port disposition:** BUILD-NEW (the one genuinely new component). Per the §10 move-#6 map: Producer (C10) ABSENT → BUILD-NEW; `task_outcomes` ABSENT → BUILD-NEW (producer state machine). It DEPENDS ON ported substrate and must NOT reimplement it: the telemetry sink it writes to is PORT (`ops/telemetry.py:TelemetrySink.record/read`, §10 "Exposure ledger PARTIAL → PORT+EXTEND add task_ref"); the drain it triggers is PORT (`federation/controller.py:250 apply_outcomes_from_sink` ← currently called from `serving/service.py:942` inside `consolidate()` — Fix #1/§4.4 REQUIRES moving that call onto this producer's tick); the watermark kv it uses is PORT (`storage/persistence.py:1148 SqliteMeta.get/set`); the single-writer CAS discipline is PORT (`storage/persistence.py:581 update_cas` idiom — rowcount-checked `UPDATE ... WHERE`). No `git`/`subprocess`/producer code exists in the reference tree (verified: `find -iname '*producer*|*git*|*watch*'` → none), so the GitFacts parser + OutcomeJoiner state machine are wholly new. The shadow config-controller (`controller.py shadow_mode=True`) stays gated and is NOT touched.

---

# M09 — Verifiable-outcome producer + trace↔outcome join (C10 + §11)

> **The riskiest new build.** Per the winning producer decision (synthesis):
> **B's cut** (`OutcomeSource`/`GitFacts` with all I/O sealed, feeding a **pure
> `OutcomeJoiner`** that owns all §11 policy) **driven by A's orchestration**
> (`OutcomeProducer.step(now) -> ProducerTick` as the in-process single-writer tick
> that owns the fixed hop order, the typed audit record, per-repo liveness, and the
> Fix-#1 drain). Swap axis = test axis = pure-policy axis.

---

## 1. Responsibility (one deep module behind a narrow surface)

This module is the **bridge** of move #6: it stamps a *verifiable git outcome* onto
the *recall trace* that informed it, and writes the §4.7 asymmetric reward to the
telemetry sink. Everything upstream (recall→trace→exposure) and downstream
(sink→attribution→posterior→surfacer) already exists (§11); this module is the one
new component.

It hides a large amount of meaningful, error-prone work behind a one-method tick:
- **commit→trace association** — window-primary (`assoc_window_s=1800`) discounted by
  `recall_margin` + ε, with a `Hive-Trace` commit-trailer **override** at higher credit;
- the **asymmetric reward state machine** — merge ⇒ small provisional `+`
  (`settle_at = merge_ts + settle_days`); a **settlement sweep** that settles `+`
  only after N clean days; **revert** ⇒ immediate large `−`; **bug-fix commit** ⇒
  large `−` **confirmed by `git blame` LINE overlap, not same-file** (Decision B);
- **squash-merge resolution** (follow branch→merge, resolve the squashed SHA so the
  join key + blame target survive a squash that would otherwise drop the trailer);
- **`family_scope` derivation at link time** (`git-remote × language × coarse-workflow`);
- the **Fix-#1 drain** (`apply_outcomes_from_sink`) moved off the deleted consolidation
  timer onto this tick.

The **deep cut**: the entire impure, per-deployment-variable, porcelain-fragile surface
(subprocess, blame, squash, locale, detached HEAD) is sealed behind **one** `OutcomeSource.poll()`;
every §11 *credit decision* lives on a **pure, clock-injected** `OutcomeJoiner` that is
fully testable with no live git repo. The system's highest-risk code (the settlement
machine + blame-overlap clawback — the §6.1.6 mutation target) never touches the thing
that varies per deployment.

The **3 mandatory §6.1.6 guard tests live here** (false-positive blame, squash survival,
drain-on-tick) plus the RULE-2 blame-overlap mutation.

---

## 2. Public surface + ENFORCED contract

**Surface (narrow):** the integration surface is exactly `OutcomeProducer.step(now) -> ProducerTick`.
Everything else (`OutcomeSource`, `OutcomeJoiner`, the frozen fact/row/emit dataclasses)
is the internal seam. The MCP server has **no new tool** — this is a background tick
driven by the in-process scheduler at `poll_interval_s` (§12 process model).

**Frozen dataclasses across every boundary** (`frozen=True, slots=True`): `CommitFact`,
`SourcePoll`, `RecallWindow`, `OutcomeRow`, `JoinerEmit`, `ProducerTick` (signatures in
the interface block). Immutability is the enforced contract — a fact handed to the pure
Joiner cannot be mutated by it, so determinism is structural, not disciplinary.

**Invariants (enforced, not prose):**
- **I1 — `step()` NEVER raises** (T-LIVENESS). Per-repo and per-policy failures are
  caught, counted into `ProducerTick.errors`, and logged; the single writer is never
  stalled by one bad repo (§4.8 "loop starved, not broken").
- **I2 — Fixed hop order:** `associate → settle → clawback → emit → drain`, asserted.
  A provisional `+` and a revert of the same SHA arriving in the *same tick* must net to
  the clawback (anti-gaming §6.6 — a stale `+` surviving a revert is the exact gameable
  positive the loop exists to prevent).
- **I3 — Clawback requires blame-line overlap** (Decision B). A bug-fix commit clawbacks
  **iff** its modified lines overlap the original commit's *introduced* lines
  (`touched_blame`); same-file-alone NEVER clawbacks. This is the load-bearing
  precision (`−1.0` punishing a good memory is the expensive false-positive direction).
- **I4 — Settlement is monotone + idempotent:** only `provisional → settled_pos` emits a
  `+`; a clawed-back row never settles; a re-tick never re-emits a settled row
  (no double-credit).
- **I5 — `task_outcomes` PK is `(task_ref, trace_id)`**; re-association upserts, never
  duplicates.
- **I6 — Single-writer:** all `task_outcomes` mutations + the drain run under the store's
  CAS/tx discipline (ported `update_cas` idiom); the producer is the only writer of
  `task_outcomes` and shares the one SQLite writer lane (§12 — no 2nd writer).
- **I7 — Trailer override is authoritative when present:** a `Hive-Trace` trailer replaces
  the window set at higher credit; `require_stamp=True` drops window association entirely.
- **I8 — `family_scope` is derived per credit event, never read from an episode column**
  (§3 — episodes have no family_scope).

**Units / semantics:** all timestamps epoch **seconds (int)**; `recall_margin ∈ [0,1]`;
`provisional_reward` default `0.2` (window 0.1–0.3); `clawback_reward` fixed `−1.0`;
`settle_days` `7` (3–14); `epsilon_explore` `0.1` (must stay `>0` — guardrail 1).

**Error semantics:** boundary failures are **counted + logged, never raised** (I1).
A disabled/absent sink ⇒ emits are dropped (logged), tick still advances. Empty
`watch_repos` ⇒ idle + one WARN (not an error).

**Precondition DESIGNED OUT (not documented):** "the Joiner must not be called with a
live git handle / wall clock." Designed out by construction — the Joiner's only inputs
are frozen dataclasses + an explicit `now: int`; there is no git or `time.time()` import
in the domain module (enforceable by the AST import-linter that bans adapter/stdlib-clock
imports from `hive/domain/**`). The split-brain risk (policy reading impure state) is
**unrepresentable**, not merely forbidden.

---

## 3. Swap seam

**Port:** `OutcomeSource` (`runtime_checkable Protocol`), one method
`poll(repo_cursors: dict[str,str]) -> SourcePoll`. This is the SWAPPABILITY-MANDATE port
for the **outcome producer**: ALL git/subprocess impurity lives behind it.

- **Default adapter (BUILD-NEW):** `GitCliSource(watch_repos, bugfix_pattern)` — shells
  `git log/show/blame` with a **hard subprocess timeout**, classifies `kind`, resolves
  squash via first-parent/`--merges`, and **pre-attaches `touched_blame`** on the impure
  side so the Joiner stays pure (Decision B resolution of the "blame is git, overlap is
  policy" seam).
- **Second adapter (proves no core change):** `WebhookOutcomeSource` — the sidecar/webhook
  producer the §12 compose file is staged for. It implements `poll()` over a buffered
  webhook queue and emits the **same** `CommitFact`s. The `OutcomeJoiner` and
  `OutcomeProducer.step()` are **byte-unchanged** — the swap is one config key
  (`producer.source = git_cli | webhook`) routed through a fail-fast factory plus one new
  adapter file. **Proof the swap needs no core change:** the named §4.7/§8.1 future signals
  (CI-status, deploy-success, incident clawback) bolt onto a new Source with ZERO change to
  the state machine because the Joiner is signal-source-agnostic (it consumes `CommitFact`,
  not git).

The Joiner is **not** behind a port (it is the pure core); the Producer tick is **not**
behind a port (it is the driving adapter, on the same footing as the MCP surface per the
hexagonal decision).

---

## 4. Data owned

**Owns `task_outcomes`** (DDL is §3 of the spec; reproduced in the interface block):
PK `(task_ref, trace_id)`, plus `idx_task_outcomes_settle (state, settle_at)` so the
settlement sweep is `O(due rows)` not `O(all rows)`.

**Owns `exposure.task_ref`** wiring (the `ALTER`/extend on the ported `ops/telemetry.py`
exposure ledger — §10 PORT+EXTEND "add task_ref").

**Owns the per-repo cursor watermark** in `SqliteMeta`:
`producer_repo_cursor:<repo> -> last-seen SHA` (ported kv, `persistence.py:1148`).

**Config read (new group `producer.*` — §4.8):** `watch_repos`, `poll_interval_s` (300),
`assoc_window_s` (1800), `stamp_trailer` (`Hive-Trace`), `bugfix_pattern`
(`^(fix|bug|hotfix|patch):` + `BUG-NN`/regression/crash/race), `require_stamp` (False),
`settle_days` (7), `provisional_reward` (0.2), `clawback_reward` (−1.0),
`epsilon_explore` (0.1). The `producer.*` group is **build-new** (verified absent from
`config.py`); the §6.1 onboarding handshake must NOT couple to it (per the hive_init
decision — watch-enrollment is a separate concern).

---

## 5. Dependencies (and the boundary it must NOT cross)

**Depends on (ported):**
- the **telemetry sink** (`ops/telemetry.py:TelemetrySink.record/read`) — writes
  `JoinerEmit` rewards keyed by `task_ref`; **PORT+EXTEND** to carry `task_ref`/family on
  the outcome rows.
- the **drain** (`controller.py:apply_outcomes_from_sink`) — the producer *triggers* it on
  its tick (Fix #1); it does NOT reimplement crediting.
- the **store CAS/tx** (`persistence.py:update_cas` idiom) — single-writer discipline.
- the **`SqliteMeta` kv** (`persistence.py:1148`) — cursor + drain watermark.

**Must NOT know about (named boundaries):**
- **the embedder / PCA / ranker / gate (C1–C4)** — the producer never embeds, never
  recalls; it consumes `trace_id`s the recall path already issued.
- **the Beta-Bernoulli posterior math + the surfacer multiplier** — it emits a reward to
  the sink; the *attributor* (`apply_outcomes_from_sink`) splits by `recall_margin` and
  moves the `(episode, family)` posterior. The producer must NOT write `utility` rows or
  `weight` directly.
- **the MCP tool layer** — no new tool; the producer is a background tick, not a request
  handler.
- **the pure Joiner must NOT know `git`/`subprocess`/`time`** — the domain↔adapter
  boundary the AST import-linter enforces.

---

## 6. Failure-mode logging (structured, secrets never logged)

| Boundary | Level | Context (no secrets — SHAs/paths only) |
|---|---|---|
| `OutcomeSource.poll` per-repo failure (unreadable repo, bad path) | **WARN** | repo, error type+msg, `errors` counter; tick continues (I1) |
| subprocess timeout in `GitCliSource` | **WARN** | repo, command class (`log`/`blame`), timeout_s; counted into `errors` |
| `watch_repos == []` | **WARN** (once per tick) | "producer idle — loop starved, not broken" (§4.8) |
| commit parse / porcelain drift (unclassifiable, locale, detached HEAD) | **WARN** | sha, repo, raw `kind` attempt; commit skipped, `errors+=1` |
| squash resolution failed | **WARN** | merge sha, repo; falls back to merge-sha join, logged |
| sink `record` failure (disabled/IO) | **WARN** | task_ref, reward sign; emit dropped (sink is fire-and-forget) |
| drain (`apply_outcomes_from_sink`) raised internally | **WARN** | swallowed (drain never raises by its own contract), `drained=0` |
| **success checkpoints** | **INFO** | per tick: a JSON `ProducerTick` (associated/settled/clawed_back/drained/stamp_hits/window_assoc/errors/poll_commits) — the §12 Phase-2 readiness signal (stamp-hit-rate + credit density) |
| clawback fired | **INFO** | task_ref, original sha, kind (revert/bugfix), overlap line-count |

`ProducerTick` IS the structured-failure-logging contract (global §6): one typed,
JSON-serializable record per tick carrying every counter the readiness gate and the
ops dashboard need. **No commit body, no secret, no recalled text is ever logged** —
only SHAs, repo names, paths, counts, and reward signs.

---

## 7. Port disposition vs §10

**BUILD-NEW** (the one genuinely new component): Producer (C10) and `task_outcomes` are
both ABSENT in the tree (§10 move-#6 map; verified — no `git`/`subprocess`/`*producer*`
files exist). It **composes ported pieces** it must not reimplement:
- telemetry sink — **PORT+EXTEND** (`ops/telemetry.py`, add `task_ref`),
- drain — **PORT** (`controller.py:250`), **with the Fix-#1 relocation**: the
  `apply_outcomes_from_sink` call currently inside `consolidate()` (`service.py:942`) MOVES
  onto this producer's tick (§4.4 — dropping the consolidation timer must not silently kill
  the loop),
- CAS/tx — **PORT** (`persistence.py:581`),
- `SqliteMeta` kv — **PORT** (`persistence.py:1148`).
The shadow config-controller (`controller.py shadow_mode=True`) stays **PORT, gated** — not
flipped by this module.

---

## 8. TEST CONTRACT (first-class, written test-first)

Full file/test list with exact assertions + the failure each catches is in the
`test_contract` field. Coverage map (a reviewer can say "no functional path is untested"):

**Happy path:** `test_window_primary_associates_in_window_traces`,
`test_provisional_settles_after_settle_days`, `test_reward_reaches_sink_and_moves_posterior`
(end-to-end §6.1.6 join round-trip).

**Every §2 invariant:**
- I1 → `test_step_never_raises_on_bad_repo`, `test_poll_never_raises_on_missing_repo`,
  `test_subprocess_timeout_counts_error`.
- I2 (fixed hop order / same-tick net) → `test_hop_order_settle_then_clawback_nets`.
- I3 (blame overlap) → GUARD a `test_same_file_no_blame_overlap_no_clawback`,
  GUARD b `test_blame_overlap_fires_clawback`.
- I4 (settle monotone/idempotent) → `test_settle_is_idempotent`,
  `test_clawed_back_row_never_settles`.
- I5 (PK upsert) → `test_task_outcomes_pk_upsert`.
- I6 (single-writer/cursor) → `test_repo_cursor_advances_monotonically`.
- I7 (trailer override / require_stamp) → `test_stamp_trailer_overrides_window`,
  `test_require_stamp_drops_window_assoc`, `test_out_of_window_traces_not_associated`.
- I8 (family derivation) → `test_family_scope_derived_at_link_time`.

**Every §6 failure mode** is hit by: `test_step_never_raises_on_bad_repo`,
`test_subprocess_timeout_counts_error`, `test_poll_never_raises_on_missing_repo`,
`test_empty_watch_repos_idle_warns`, `test_squash_merge_resolves_original_sha`,
`test_tick_returns_typed_audit` (the structured-log record).

**The 3 mandatory §6.1.6 guard tests (named, here):**
- (a) false-positive blame → `[GUARD a] test_same_file_no_blame_overlap_no_clawback`
  (a coincidental same-file edit that does NOT overlap blame lines must NOT clawback).
- (b) squash survival → `test_squash_revert_resolved` + `test_squash_merge_resolves_original_sha`
  (the join + blame target survive a squash-merge).
- (c) drain-on-tick → `[GUARD c / Fix #1] test_drain_fires_on_producer_tick_with_consolidation_disabled`
  (the outcome-drain fires on the producer tick with the consolidation timer disabled —
  proves the loop is not silently dead).

**MUTATION test (RULE 2, §6.1.6 mandated):**
`[MUTATION] test_mutation_disable_blame_overlap` — DELIBERATE FAULT: change
`OutcomeJoiner.clawback()`'s bugfix branch from "clawback iff `touched_blame` overlaps the
original introduced lines" to "clawback on any same-file match" (disable blame-overlap).
**MUST go red:** `test_same_file_no_blame_overlap_no_clawback` (GUARD a) — it now wrongly
clawbacks the coincidental same-file edit. **Restore → GREEN.** This proves the
blame-overlap gate is load-bearing and actually under test (a gate whose test still passes
when broken is not tested).

**§6.1.6 acceptance gate ownership:** this module owns the **Producer join round-trip**
gate; `test_reward_reaches_sink_and_moves_posterior` proves the full chain
(commit → window/stamp link → `task_outcomes` → provisional → settle/clawback → sink →
drain → `(episode, family)` posterior moves), and the three guard tests + the mutation
discharge the gate's mandatory sub-requirements.

**Source contract tests are parametrized** over the real `GitCliSource` (against a tmp git
fixture: a repo with a merge, a squash-merge, a revert, and a bugfix-overlapping-blame
commit) **and** a `FakeOutcomeSource` (frozen `CommitFact` lists) — the same port, so the
swap seam is the test seam. The Joiner suite runs entirely on frozen dataclasses + injected
`now` (no git, no clock) — millisecond, deterministic, the full §11 policy under mutation.


---

## Design review (independent pass)

**Verdict:** STRONG ARCHITECTURE, NOT YET BUILD-READY. The core decomposition is the best thing in this spec: the hexagonal cut — all git/subprocess/blame/squash/locale impurity sealed behind one `OutcomeSource.poll()`, feeding a pure clock-injected `OutcomeJoiner` that owns 100% of §11 credit policy, driven by a single-writer `OutcomeProducer.step(now)->ProducerTick` — is a textbook DEEP module (APOSD #4,#6): a one-method integration surface hiding the system's highest-risk code (settlement state machine + blame-overlap clawback). The 'swap axis = test axis = pure-policy axis' identity is genuinely elegant and earns the high extensibility/navigability scores: frozen dataclasses across every boundary make determinism STRUCTURAL not disciplinary (APOSD #11 'define errors out of existence' — the split-brain 'policy reads impure state' risk is made unrepresentable via the AST import-linter, not merely forbidden in prose). I1 (step never raises) and the Fix-#1 drain relocation are correctly identified as the loop-keeping invariants. HOWEVER, three load-bearing correctness gaps block sign-off, and ALL THREE are masked by the spec's framing of the downstream as 'EXISTS' when the reference tree proves it does not. Verified against /home/null/Desktop/work/AgentCortex/cls_memory/cls_memory/federation/controller.py:199 (`apply_outcome`) and :250 (`apply_outcomes_from_sink`): the ported drain DISCARDS `recall_margin` (`for (h, _entropy, _margin) in outcome.recalled`), credits `weight` via `alpha_u·utility` — NOT a Beta-Bernoulli posterior — and has NO `family_scope` keying; grep confirms zero occurrences of `task_outcomes`, `family_scope`, `wins/losses`, or `posterior` anywhere in the tree. So the §6.1.6 acceptance test M09 claims to own — `test_reward_reaches_sink_and_moves_posterior` — has no posterior to move, and the module both disclaims ownership of that attributor AND asserts it 'EXISTS'. That is an Information-Leakage red flag at the module's downstream boundary (the credit-split + posterior decision is smeared across an undefined 'attributor' that the PORT does not contain) and a test-coverage hole (the end-to-end gate is unrunnable as scoped). Combined with the `touched_blame` provenance gap (I3 has no defined storage/recompute path for the ORIGINAL commit's introduced lines at delayed-clawback time) and the bugfix-SHA→original-SHA lookup gap (the PK is keyed by the original SHA but the clawback event arrives on a DIFFERENT SHA with no index to find the row), the riskiest-in-the-build module has its two highest-risk paths (blame-overlap clawback, posterior credit) under-specified at exactly the seams the design otherwise nails. Fix the scoping/provenance and this is a 9/10 design.

**Scores (1–10):**
- design_complexity: 4
- cognitive_load: 5
- information_leakage: 4
- extensibility_fit: 8
- agent_navigability: 7
- contract_enforcement: 7
- test_coverage: 5

**Red flags:**
- Information Leakage @ §5 downstream boundary ('the attributor splits by recall_margin and moves the posterior; producer must NOT write utility/weight') — the credit-split + posterior decision is described as living in a ported component that, verified at controller.py:199, does NOT split by margin (discards `_margin`) and does NOT write a posterior; the decision is smeared across an undefined attributor. root: dependency → the §6.1.6 end-to-end test depends on code the PORT does not contain; change to the credit math forces coordinated edits in an unnamed module.
- Hard to Describe @ §2 I3 + §3 GitCliSource 'pre-attaches touched_blame' — the precondition 'overlap the original commit's INTRODUCED lines' needs multiple clauses (which SHA, blamed when, stored where, recomputed how at delayed-clawback time) that the interface does not capture; the introduced-line provenance for a clawback arriving on a different SHA is exactly the 'must be called after X but the data came from Y weeks ago' shape the flag warns of. root: obscurity → the load-bearing −1.0 precision path is under-specified at its single highest-risk seam.
- Special-General Mixture @ §11 `family_scope` derivation (git-remote × language × coarse-workflow, with bugfix-pattern⇒fix-ci/bugfix, manifest-only⇒dep-upgrade) — a special-case workflow taxonomy is hard-coded inside the otherwise-general link-time derivation; the same `bugfix_pattern` is the clawback trigger AND a family-classification input, coupling two policies. root: dependency → a change to bugfix_pattern silently shifts BOTH credit-trigger and family-bucketing; the coupling is not called out as an invariant.
- Missing Feedback Signal @ §8 `test_reward_reaches_sink_and_moves_posterior` — the headline end-to-end gate has no implementation target (no posterior exists in the tree and M09 disclaims owning the attributor), so the test cannot fail-first per TDD; an agent writing M09 gets no red→green signal on the module's own acceptance criterion. root: dependency → the gate's pass/fail rides on out-of-module, not-yet-built code, defeating the contract-as-feedback intent.
- Nonobvious Code @ §2 I2 'a provisional + and a revert of the same SHA in the same tick must net to the clawback' — the hop order `associate→settle→clawback→emit→drain` is asserted, but WHY net-to-clawback is correct (rather than emit-both-and-let-the-sink-net) requires tracing the anti-gaming §6.6 rationale; the ordering is an implicit correctness contract whose violation is silent. Mitigated by `test_hop_order_settle_then_clawback_nets`, but the test name does not encode the same-SHA-same-tick condition. root: obscurity → an agent reordering hops for readability could silently re-open the gameable stale-+ window.

**Test gaps:**
- The §6.1.6 keystone gate `test_reward_reaches_sink_and_moves_posterior` asserts the posterior MOVES, but no posterior/family_scope/wins-losses exists in the PORT (verified absent). The test is unrunnable as scoped — it needs either a stub posterior fixture owned by M09 or explicit reassignment to the attributor module. As written it is a coverage claim with no implementation to exercise.
- No test for the bugfix-SHA→original-SHA LOOKUP itself: every clawback test (GUARD a/b, mutation) presumes the original `task_outcomes` row is FOUND from the bugfix commit, but no test plants two original commits touching the same file and asserts the clawback hits ONLY the row whose introduced lines overlap. This is where a false-positive across commits (not just same-file) hides.
- No test for `touched_blame` provenance at DELAYED clawback: the named tests blame at link/merge time, but the dangerous case is a bug-fix arriving after `settle_days` when the original SHA may be squashed/rebased away. Add `test_clawback_blames_original_introduced_lines_after_settlement` — the squash-survival test (`test_squash_merge_resolves_original_sha`) covers the JOIN key but not the BLAME-TARGET survival for a delayed bugfix.
- Mutation coverage is one-directional: only the false-POSITIVE blame path is mutation-proven. No mutation proves (a) the TRUE clawback fires (mutate clawback to no-op → `test_blame_overlap_fires_clawback` must go red — not named as a mutation), (b) settlement monotonicity (mutate `settle()` to settle clawed-back rows → `test_clawed_back_row_never_settles` red), (c) the same-tick net (mutate hop order → `test_hop_order_settle_then_clawback_nets` red).
- No SECRET-SAFE assertion in the test contract. §6 claims 'no commit body, no secret, no recalled text is ever logged — only SHAs/paths/counts', but no test asserts it. A commit message can contain a token; `ProducerTick`/WARN logs must be proven secret-free. Add `test_producer_tick_and_logs_carry_no_commit_body_or_secret` asserting the serialized record + log lines contain only SHAs/paths/counts/signs.
- No APPROVED-ONLY / pending-write interaction test. The producer credits traces that recalled episodes; if a recall could expose a pending (unapproved) episode the credit path could move utility on never-approved memory. A test should assert the producer never creates/credits a `task_outcomes`/utility row for a trace whose exposed episodes were pending — closing the approved-only invariant at the credit boundary.
- No VERIFIABLE-CREDIT-ONLY negative test: the product invariant is 'utility updates ONLY from a machine-checkable git outcome, never agent self-report'. The `Hive-Trace` trailer is agent-written (I7) — there should be a test that the TRAILER only re-targets WHICH traces get credit, and can NEVER set the reward SIGN or VALUE (a trailer claiming success must not produce a +); `test_stamp_trailer_overrides_window` proves re-targeting but not that the trailer cannot inject reward.
- Swap-seam parity is asserted structurally (parametrized over `GitCliSource` + `FakeOutcomeSource`) but no test asserts the two sources produce BYTE-IDENTICAL `JoinerEmit` for the same logical commit set. Add `test_webhook_and_gitcli_emit_identical_joiner_output` (or fake-vs-git on a shared CommitFact fixture) — otherwise 'swap needs no core change' is unproven, only claimed.
- No test for `require_stamp=True` DROP combined with an unstamped commit that IS in-window: `test_require_stamp_drops_window_assoc` exists but should also assert the unstamped in-window commit produces ZERO task_outcomes rows (not just 'window not used') — the abstain-no-resurrect analog for credit (a dropped association must not be rescued by the window path).

**Must-fix:**
- SCOPE THE DOWNSTREAM HONESTLY (blocks the §6.1.6 gate). The spec labels 'splits by recall_margin' and 'moves the (episode, family) Beta-Bernoulli posterior' as EXISTS/PORT, but controller.py:199 `apply_outcome` discards `_margin`, credits `weight` not a posterior, and has no family_scope; grep finds no `task_outcomes`/`family_scope`/`wins`/`losses`/`posterior` in the tree. Either (a) expand M09's PORT+EXTEND to explicitly own the margin-split + (episode,family) posterior rewrite of `apply_outcome` (and name the new `utility(episode_id, family_scope, wins, losses)` write path), or (b) name the SEPARATE module that owns it and make `test_reward_reaches_sink_and_moves_posterior` that module's gate — but it cannot be asserted EXISTS here. As written the module's own acceptance gate is unrunnable.
- DEFINE `touched_blame` PROVENANCE FOR DELAYED CLAWBACK (blocks I3 + GUARD a + the mutation test). Decision B requires overlap against the ORIGINAL commit's *introduced* lines. At merge time the Source can blame the original SHA, but a bug-fix commit arrives days/weeks later on a different SHA; the spec never says where the original commit's introduced line-ranges are persisted (`task_outcomes` DDL stores only `files_touched`, not line ranges) nor that the Source re-blames the original SHA at clawback time. Without this, GUARD a (`test_same_file_no_blame_overlap_no_clawback`) and the mutation test have no data to compute overlap from. Add a stored introduced-lines set (or a defined recompute-from-original-SHA path) to the interface block and the DDL.
- DEFINE THE bugfix-SHA→original-SHA LOOKUP (blocks I3 + I5 + clawback O()). PK `(task_ref, trace_id)` is keyed by the ORIGINAL commit SHA, but a clawback is TRIGGERED by a different commit (the bugfix/revert SHA). The spec gives `idx_task_outcomes_settle(state, settle_at)` for the sweep but no index/path from a bugfix commit's touched files+lines back to the candidate original rows. Specify the lookup (e.g. an index on `files_touched`/repo, or a files→task_ref reverse map) and its complexity, or the clawback is an unbounded scan and I3 is untestable for performance.
- ADD A NEGATIVE/EMPTY-OVERLAP MUTATION SURVIVOR + A SECOND MUTATION TARGET. The single named mutation (disable blame-overlap → GUARD a goes red) only proves the false-POSITIVE direction is tested. RULE 2 / §6.1.6 also need a mutation proving the TRUE-positive clawback and the settlement monotonicity are load-bearing: e.g. mutate `settle()` to settle a clawed-back row (must turn `test_clawed_back_row_never_settles` red) and mutate the hop order to emit-before-clawback (must turn `test_hop_order_settle_then_clawback_nets` red). Name them with the exact fault + the test that catches each.
- SPECIFY THE PORT+EXTEND DELTA ON `ops/telemetry.py` AS AN ENFORCED CONTRACT. `RecallOutcome` (telemetry.py:70) has no `task_ref` and no `family`; the module says 'add task_ref' but the drain reads `o.utility`/`o.recalled`, not a JoinerEmit. Pin the exact extended dataclass fields + the `record()` signature change + the migration of the existing sink schema (telemetry.py:116 DDL), so the 'reward reaches sink keyed by task_ref' assertion has a typed target rather than prose.
