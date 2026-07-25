# RETIREMENT-LINE-AND-CACHE-MINIMALITY — plan

Status: **CONFIRMED — ready to implement end to end.**

The one open decision this plan carried, **A-3** (the dead `canonical_ref` ledger-rider scope), was
ruled by the operator: **A-3a — wire it to the memory's own line.** A-3b (delete the chain) is
recorded as considered-and-rejected in §5.5 and is not to be built. No open choices remain; every
item below is to be built as written.

**AMENDED after adversarial review** — see `RETIREMENT-LINE-AND-CACHE-MINIMALITY-REVIEW.md`
for the evidence behind every correction. Amendments are marked **[A-n]**. Three of this
plan's original claims were refuted and are corrected in place; the consolidation set grew
from four forked facts to twelve.

Closes the two blocking defects from the BRANCH-SCOPE-AND-DEMAND end-to-end verification,
BUG-067, and the doc/enum gaps found alongside them — by **removing** duplicated owners
rather than adding new ones.

---

## 0. Corrections to the brief and to this plan's own first draft (the tree wins)

Verified against the working tree. Each changes what the plan must do.

| # | Claimed | The tree says | Consequence |
|---|---|---|---|
| **C-1** | "`make check` is currently GREEN" | **RED.** `uv run --extra dev pytest -m "not embed"` exits 1 on `tests/store/test_change_evidence_store.py::test_evidence_rows_for_filters_to_the_named_kinds_in_order` — it asserts the 3-tuple `(kind, actor, ts)` while the partially-applied fix widened `evidence_rows_for` to a 4-tuple. Format / lint / `mypy --strict` all pass. Reproduced by the reviewer. | The partial edit is **incomplete, not merely partial**. Step 0 of any build is closing that. |
| **C-2** | "`hive/app/mcp_server.py` … was mid-edit on the third" | mcp_server is **fully wired**: `_gate_own_lines` (`:612`) exists, is imported, is typed, and is passed as `own_lines=` at `:668`. The only missing piece anywhere is the one un-updated store test. | Nothing is half-written. The question is whether the *design* is right. |
| **C-3** | Issue 3: "`retirement_evidence`'s docstring still says drift verdicts are read at each repo's canonical tip" | The **docstring was already fixed** (`retirement.py:183-184`). Five *other* statements are stale and worse — see §4.3. | Issue 3 is real but mis-located. |
| **C-4** **[A-6]** | *(this plan's own §4.2, first draft)* "a recall with `repos=["alpha@feature"]` after `hive repo remove alpha` serves **`fresh`**, forever" | **REFUTED — unreachable.** `anchors.py:normalize_repos:153-156` raises `BadAnchors("unknown repo … not in the registry")` for any name absent from the live registry, and `mcp_server._handle_recall:846-849` returns `{"status":"refused"}` **before** `attach_drift` is ever called (`:908`). `_known_repos:532-536` reads the live registry every call. | The defect is real; the trigger is **re-registration**, not deregistration. BUG-068's symptom, its reproduction, and J3's contract all change. §4.2. |
| **C-5** **[A-7/A-8]** | *(this plan's own §5.1/§7, first draft)* clause 1b fires for a repo "fed by CI receipts but never `hive repo add`-ed"; J1 drives an own-line retirement "with a real sync tick" | **Both REFUTED.** (a) `anchors.py:110-113` refuses an unknown repo on **both** anchored write paths, so a never-registered repo has **zero** `episode_anchors` rows and clause 1b is equally dead. (b) The daemon's ledger leg censuses the **canonical tracked branch only** — `sync.py:360` `branch = _tracked_branch(row.canonical_ref)`, `:556` `_build_receipt(..., branch, ...)`, `:605-608` `--ref branch`. **Every daemon-written `verify_*` row is stamped with the producing repo's canonical ref.** | N2 becomes the *deregistered* repo. A non-canonical `ref` stamp is reachable **only** through the manual `hive ingest` door, so J1's second scenario must drive that door. §5.1, §7. |
| **C-6** **[A-3]** | *(not previously noticed)* "the recall rider … derives only from rows measured on the repo's canonical line" (THEORY §5) | **False in production.** `container.make_server()` (`container.py:194-201`) never passes `canonical_ref`, so `HiveMCPServer.canonical_ref` is always `""`, `mcp_server.py:917` always passes `None`, and `last_verification`'s whole ref-scoping arm — **including its Law-7 marker at `store_sqlite.py:956`** — is structurally unreachable. Five tests in `tests/store/test_last_verification.py:137-192` certify it anyway. | The BUG-059/BUG-064 advertised-but-unreachable pattern, **fourth occurrence**, on the exact method this plan wants to share a parser with. Must be decided, not left. §5.5. |
| **C-7** **[A-11]** | *(not previously noticed)* — | `drift.py:86-88` `STALE_TIER` and `retirement.py:64-66` `_QUALIFYING_DRIFT` are the **same three-member frozenset**, defined independently on opposite sides of the hexagonal boundary, each commented as a paraphrase of the other. | The single fact both the retirement gate and the branch-routing softener stand on has two owners. §5.5. |

Two premises in the issue list are handled in place: **issue 6's premise that `ref_tips` is a
migration scar** (it is not — §6.2), and **issue 1's constraint that the store must not parse
the payload** (the meta-envelope law does not reach `evidence_events.payload` — §5.2).

---

## 1. What is wrong, and the one idea that fixes it

Almost every defect below is the **same defect**: a fact that should have one owner acquired
two, and only one of the two got fixed. This tree was built by 9 agents in walled-off scopes,
and an implementer who cannot touch another chunk's file re-implements what it needs on its own
side of the wall. **[A]** The first draft of this plan found four such forks. There are twelve.

| # | The forked fact | Owner A | Owner B (+C) |
|---|---|---|---|
| 1 | "is this memory's anchor dead?" | clause 1a (`retirement.py:206-213`) | clause 1b (`:228-251`) — only 1a was made line-aware |
| 2 | the verify-payload `ref` **grammar** | `change_evidence.py:686` (**writer**) | `store_sqlite.py:958` + `retirement.py:146` (readers) |
| 3 | "which tip do I judge repo R at?" | `drift._tip_for:195-203` | `mcp_server.py:598-606` (raw SQL inline) |
| 4 | the drift wire vocabulary | `drift.WIRE_VERDICTS:60` | `tool_defs.py:144-146` (literal) |
| 5 **[A]** | "which verdicts mean *the anchor moved*" | `drift.STALE_TIER:86` | `retirement._QUALIFYING_DRIFT:64` |
| 6 **[A]** | "this memory's own LINE, per repo" | `mcp_server._gate_drift_verdicts:598` | `mcp_server._gate_own_lines:625` — **ten lines apart, different fallbacks** |
| 7 **[A]** | "read one meta kv" | `drift._meta_value:184` | `sync._meta_get:942`, `census_health.py:125`, `mcp_server.py:601`, `mcp_server.py:632` |
| 8 **[A]** | the not-retired work-list predicate | `store_sqlite.py:1137` | `:1160`, `:1455` — and `anchor_carriers`' docstring claims it is "fixed in ONE place" |
| 9 **[A]** | "repo R's canonical line" | `repos.canonical_ref` (`drift.py:326`) | `sync:<repo>:tracked_ref` (`mcp_server.py:632`) — they differ when `--branch` was omitted |
| 10 **[A]** | the gate's qualifying-signal description | `retirement.py:9-11` | `contract.py:72` ("at the **canonical tip**") — **served to every agent, every session** |
| 11 **[A]** | the demand bar `demand_m` | `lifecycle.py:277` (`n_other`, per BUG-066) | `mcp_server.py:1297` (`len(misses)`, writer-inclusive) |
| 12 **[A]** | the write-side `repos` grammar | `tool_defs._REPOS_PROPERTY:71` (no branch form) | `contract.WRITE_VS_CAPTURE:49` (`'name@branch'`) — **composed into the same string** |

Plus one **dead-surface chain** (not a fork — an unreachable feature): the server-level
`canonical_ref` ledger-rider scope. §5.5.

**The single design idea: give each of those facts exactly one owner, and delete the second
copy.** Every blocking behaviour change falls out of that. The plan adds exactly two new lines
of production behaviour (`ref_tips_prune` in the tick; `repo_remove` sweeping its feed-derived
caches). Net production surface is **smaller** after this change than before it.

---

## 2. Partial-edit assessment — **KEEP the direction, RESHAPE all three files**

The partial work is a **sound starting point**. It correctly identified that the ref must travel
and that the domain must decide relevance; 95 of its 96 associated tests pass. It should **not**
be unwound. It should be reshaped.

### 2.1 What is in the three files right now

**`hive/domain/retirement.py`** — a complete, working clause-1b line filter: `EvidenceRow` gained
`payload: str = ""`; `_row_fields` (`:109`) reads a 3- or 4-sequence; `_row_line` (`:132`)
`json.loads`es the payload for `body["ref"]`; `retirement_evidence` gained
`own_lines: Optional[Iterable[str]]`; clause 1b (`:228-251`) collapses it to
`attributable = lines if len(lines) == 1 else frozenset()` and filters `verify_stale` rows,
leaving `verify_current` rows **unfiltered** (correctly — a current row only ever refuses).
Carries a Law-7 marker on the multi-repo widening.

**`hive/adapters/store_sqlite.py`** — `evidence_rows_for` (`:1015`) returns the RAW payload as a
4th column, with a docstring citing the meta-envelope law as the reason.

**`hive/app/mcp_server.py`** — complete: `_gate_own_lines` (`:612`) reads `episode_refs`, returns
`None` when nothing is declared, else resolves each anchored repo's line as *declared ref, else
`sync:<repo>:tracked_ref` read by raw SQL*.

### 2.2 Verdict: keep, with four corrections

| # | Correction | Why (and what it deletes) |
|---|---|---|
| **K-1 → A-2** **[A]** | Move the ref parse to the module that **writes** the payload. New pure helper `hive/domain/change_evidence.py:verify_payload_ref(payload) -> str`, beside `render_verify_payload`. `store_sqlite` imports it and uses it for **both** `evidence_rows_for` (projecting a typed `ref`) and `last_verification`. `EvidenceRow.payload` → `EvidenceRow.ref`; `retirement._row_line` and its `json` import are **deleted**. | The first draft put the reader in the adapter. That relocates the fork (domain-retirement↔adapter → domain-change_evidence↔adapter) instead of removing it: `render_verify_payload` (`change_evidence.py:670-691`) is what emits the `"ref"` key, under its own Law-7 marker. Co-locating reader and writer gives the grammar **exactly one home**; the adapter depending on the domain is the correct direction (THEORY §2), and `retirement.py` ends just as free of JSON as before. Also **corrects `evidence_rows_for`'s docstring**: the meta-envelope law does **not** reach `evidence_events.payload` (§5.2), and leaving that citation propagates a misapplied law. |
| **K-2 → A-1** **[A]** | **Delete** `_gate_own_lines`' `tracked_ref` fallback and its collapse-set logic. Final rule: anchor-less ⇒ `None`; **≠ 1 anchored repo** ⇒ `frozenset()`; exactly one anchored repo ⇒ `frozenset({declared_ref})` when it declared one, else `None`. | The first draft's one-line fix (unknown line ⇒ `frozenset()`) is *safe* but keeps 18 lines, a raw SQL read, fork #6, fork #7 and fork #9. The collapse rule already under-claims on the empty and multi-line cases; the only over-claim window is "N repos where all but one resolve to the same line and the rest are unknown", which the new rule closes **by construction** rather than by a guard. It also **defines the precondition out of existence** (principle #11): there is no tri-state to reason about. Cost: a memory anchored in ≥2 repos that declare the *same* line loses ledger-clause retirement — an accepted, recorded coverage loss in the safe direction (§9), narrowed further by C-5 (a same-line multi-repo row needs a manual receipt). |
| **K-3** | `_gate_drift_verdicts` calls `hive.app.drift.tip_for(store, repo, declared_ref)`; its raw `SELECT value FROM meta …` is **deleted**. `_tip_for` → public `tip_for`. | `drift._tip_for` and the gate's inline branch are semantically identical. The plan being executed asserted "one tip resolver, one owner" in its own §3.B; it shipped two. **[A]** With A-1, the `tracked_ref_key` import goes too — both new raw meta reads leave the boundary, not one. |
| **K-4** **[A]** | Preserve the `verify_current` arm **unfiltered** and its comment, verbatim. | Load-bearing: filtering current rows would let an off-line stale row out-rank an on-line current one — the over-claiming direction. Called out explicitly so no implementer "tidies" it. |

### 2.3 How to unwind — NOT TAKEN, recorded as contingency only

**The operator confirmed this plan, so the partial edit is KEPT and RESHAPED per §2.2. Do not
unwind.** This section survives only so a future reader can reverse course without guessing, and
because reversing safely is non-obvious given nothing was committed before `923c2d4`.

Recorded because it was asked for, not because it is recommended. **Never `git checkout` /
`git reset` / `git stash`** — one commit (`923c2d4`) protects the whole 9-chunk build. Unwinding
is four *forward* edits, each reverting to a state readable in `git show 1f14e61:<path>`:

1. `store_sqlite.py::evidence_rows_for` → the 3-column `SELECT kind, actor, ts` and
   `list[tuple[str, str, int]]`. This alone turns `make check` green.
2. `retirement.py` → delete `_row_line`, the `json` import, `EvidenceRow.payload`, `own_lines`
   and its docstring bullet, and clause 1b's `attributable` block.
3. `mcp_server.py` → delete `_gate_own_lines`, the `own_lines=` argument, the `tracked_ref_key` import.
4. `tests/mcp/test_retirement_gate_boundary.py:207-278` → the three added tests do not touch
   clause 1b; leave them.

Unwinding costs the same edit count as correcting and re-opens blocking defect 1. It is the
worse option.

---

## 3. Design review (required step, folded in)

`/software-design-review` was run twice — once by the author (Mode B, four decisions) and once
by the reviewer over the whole amended end state. The reviewer's scores, red-flag list and
contract-health table live in `RETIREMENT-LINE-AND-CACHE-MINIMALITY-REVIEW.md` §2. Scores are
reasoned judgments anchored to that skill's rubric, **not** measurements.

**System context assumed:** hexagonal with an AST-gated pure `hive/domain/`; `app/` is transport
+ boundary and may read the concrete store (the "sync/census_health raw-read idiom"); the
retirement gate is fail-closed and under-claims by contract; `drift.py` self-declares as "the
single owner of the drift wire semantics"; `tool_defs.py` self-declares that its enums are
"PROJECTED from the registry"; the instruction layer is `THEORY.md` + `INTERACTIONS.md` +
`BUGS.md`, all version-controlled and enrolled in `/audit-docs`.

**Decisions, re-decided where the review overturned them:**

- **D2 — where the verify-payload `ref` parse lives. [A] RE-DECIDED.**
  *Option A (as-built): domain-retirement parses.* Information leakage **6/10**.
  *Option B (first draft): adapter parses.* Information leakage scored **2/10** — but the score
  was wrong: Option B leaves the grammar's writer in `domain/change_evidence.py` and its reader
  in `adapters/store_sqlite.py`, which is still "two modules share knowledge that should be
  hidden" (**5/10**).
  *Option C (**winner**): the module that renders the payload owns reading it.* Information
  leakage **2/10** genuinely — the `"ref"` key appears in exactly one module. Cognitive load
  **2/10** (`EvidenceRow.ref: str` is a precise mental image; the domain gate needs no JSON).
  Extensibility **8/10** — the D3-option-C follow-up (stamp `repo` into the payload) then lands
  in one file. Cites **Different layers, different abstractions** (#9), **Pull complexity
  downward** (#10), **Separate what matters from what doesn't** (#16).
  **Winner: C**, because it is the only option that actually achieves the property B was scored
  for, at the same edit count.

- **D3 — the `own_lines` contract. [A] RE-DECIDED.**
  *Option A (first draft): `Optional[frozenset[str]]` tri-state with a collapse rule.*
  *Option C: stamp `repo` into `render_verify_payload` and match `(repo, ref)` pairs* — defines
  the ambiguity out of existence (#11) and is the better end state, but only helps rows written
  after the change and edits a content-keyed dedup surface carrying its own Law-7 marker.
  **Named as the follow-up in §9**, not smuggled in.
  *Option D (**winner**): keep the tri-state TYPE, delete the multi-line machinery.* `None` /
  `frozenset({ref})` / `frozenset()` are still the three states, but they are now produced by
  three one-line rules instead of a set-collapse over a meta fallback. Beats A on cognitive load
  (**2/10** vs 5/10), information leakage (deletes forks #6/#7/#9), and safety (never
  over-claims), and loses only the multi-repo-same-line case.
  **Winner: D**, with the type tightened to `Optional[frozenset[str]]` so `mypy --strict`
  carries the tri-state instead of prose.

- **D5 — the dead ledger-rider scope (C-6). [A] NEW.**
  *Option A: delete the whole chain* (server param, port kwarg, filter, marker, five tests, the
  THEORY §5 sentence). More minimal; nothing breaks. But it ratifies a serve-path advisory that
  labels a memory "stale" on a line it never declared — blocking defect 1's advisory twin,
  contradicting `branch_route_verdict`'s own posture.
  *Option B (**winner, ruled**): wire it to the memory's own line.* A-1 and A-2 build both
  ingredients anyway, so the incremental cost is small and it closes a real serve-path defect.
  **Winner: B**, with A as the named fallback if the human prefers strictly minimum surface —
  but **one of the two must ship**. Leaving an unreachable parameter whose behaviour THEORY
  asserts as live is the worst of the three and is the exact class BUG-059/064 exist to prevent.

- **D6 — should clause 1a and 1b merge into one reader? [A] NEW, and the first draft's reason
  was wrong.** The first draft rejected the merge on audit explainability; that is weak (a merged
  reader could still emit `drift:<verdict>` vs `verify_stale`). The real reason is **cohesion**:
  1a is a set of already-line-resolved verdict strings folded by membership; 1b is a row stream
  needing a recency comparison, a line filter and an under-claim rule. One function with two
  disjoint bodies is worse than two functions. **Keep two clauses; unify only the fact they
  share** (fork #5, A-11).

---

## 4. Blast radius, per issue

Every entry was found by scanning the tree, not by trusting the issue list.

### 4.1 Issue 1 (BLOCKING) — clause 1b is not line-aware

**Production:** `hive/domain/retirement.py` (`EvidenceRow`, `_row_fields`, clause 1b, the module
docstring, the `:204` comment, `_QUALIFYING_DRIFT` → public) · `hive/domain/change_evidence.py`
(**[A]** new `verify_payload_ref`) · `hive/adapters/store_sqlite.py` (`evidence_rows_for`,
`last_verification`) · `hive/app/mcp_server.py` (`_gate_own_lines`, `_gate_drift_verdicts`,
`_retirement_eligibility`, the `tracked_ref_key` import) · `hive/app/drift.py`
(`_tip_for` → `tip_for`; **[A]** `STALE_TIER` deleted) · **[A]** `hive/app/contract.py`
(`REMEDIATION_NOTICE`).
**Not touched:** `change_evidence.ingest` (the writer is correct — it already stamps
`provenance.ref`), `hive/domain/ports.py` for `evidence_rows_for` (there is **no** port; the gate
reads the concrete store, so no Protocol widens). **[A]** `ports.py` IS touched if D5 option B
ships (`LastVerificationReader`).

**Tests:** `tests/store/test_change_evidence_store.py` (**currently RED**) ·
`tests/domain/test_retirement.py` (zero `own_lines` coverage; `git diff` on it is **empty**) ·
`tests/domain/test_change_evidence.py` (**[A]** `verify_payload_ref`) ·
`tests/mcp/test_retirement_gate_boundary.py` · `tests/contract/test_retirement_gate_e2e.py`
**(FROZEN)** · `tests/contract/test_retirement_gate.py` **(FROZEN)** · `tests/app/test_drift.py`.

**Docs:** `INTERACTIONS.md` [M2] + [C6] · `BUGS.md` · `THEORY.md` §3 + §5 · `CHANGELOG.md` ·
`llms-full.txt`.

**Why the current test surface missed it:** three independent gaps.
(a) `tests/mcp/test_retirement_gate_boundary.py::test_declared_line_fresh_blocks_even_when_canonical_is_stale`
**seeds** `drift_put`/`ref_tips_put` and never runs a tick, so no `verify_stale` row is written
and clause 1b never executes. (b) The frozen twin
`tests/contract/test_retirement_gate_e2e.py:172::test_a_branch_scoped_memory_is_judged_at_its_own_line`
drives only the *permissive* direction and asserts `drift:anchor_missing` — clause 1a — so it
passes with or without the bug (confirmed by reading it). (c)
`tests/domain/test_retirement.py` was never edited.

### 4.2 Issue 2 (BLOCKING) — `ref_tips` is unbounded, **and survives into a re-registered incarnation** **[A] REWRITTEN**

Confirmed as filed: `grep -rn ref_tips_prune hive/` matches only the definition. The scan found a
second, sharper failure — but **not the one the first draft described.**

`store_sqlite.py:repo_remove` deletes the `repos` row and `meta WHERE key LIKE 'sync:<name>:%'`
(BUG-060) — but **not** `ref_tips`, `anchor_drift`, or `ref_requests`. Its docstring justifies
keeping them ("the rebuildable drift cache … not feed state"). That was true before `ref_tips`
existed.

**What the first draft got wrong.** It claimed a recall with `repos=["alpha@feature"]` *after*
`hive repo remove alpha` serves `fresh` forever. **That is unreachable** —
`normalize_repos:153-156` refuses an unregistered name and `_handle_recall:846-849` returns
`refused` before `attach_drift` runs. An unscoped recall correctly reads `unverifiable` (its
canonical watermark *was* deleted).

**The real defect is RE-registration** — precisely the flow BUG-060 was filed for, and the one
`skills/hive-connect-repo` §4 recommends:

```
hive repo remove alpha             # sync:alpha:* swept; ref_tips / anchor_drift SURVIVE
hive repo add <url> --name alpha   # alpha is known again -> normalize_repos PASSES
recall repos=["alpha@feature"]
  -> tip_for(alpha,"feature") -> the surviving ref_tips row -> a DEAD incarnation's SHA
  -> drift_get(alpha, dead_tip, anchors) -> the surviving anchor_drift rows -> "fresh"
```

The canonical path is immune (BUG-060 deleted `sync:alpha:last_tip`, so it reads `unverifiable`
until the first tick). The **branch** path is not, because `ref_tips` is the branch twin of
`last_tip` living in a table the sweep does not reach. It survives the first tick whenever the
new remote cannot resolve `feature` (no `ref_tips_put`, so the dead row is never overwritten).
That is a serve-path false-`fresh` of exactly the BUG-060 class, introduced by this build, on the
table whose design was chosen to avoid it. **Not covered by BUG-067 as filed; logged as BUG-068
with the corrected symptom.**

**Production:** `hive/app/sync.py:_materialize_drift` (the missing per-tick prune) ·
`hive/adapters/store_sqlite.py:repo_remove` (the missing deregistration sweep + its now-false docstring).
**Tests:** `tests/store/test_repo_registry_store.py` · `tests/store/test_drift_cache_store.py` ·
`tests/sync/test_contract_drift.py` · `tests/contract/test_branch_scope_e2e.py` **(FROZEN)**.
**Docs:** `BUGS.md` (BUG-067 → SOLVED; BUG-068 new) · `INTERACTIONS.md` [C1] · `CHANGELOG.md`.

**Why the test surface missed it:** `test_repo_remove_forgets_the_feed_state`
(`tests/store/test_repo_registry_store.py:169`) asserts the `meta` sweep only — written for
BUG-060, before `ref_tips` existed. `ref_tips_prune` has two green unit tests, which is exactly
what made its zero call sites invisible: the verb is *tested*, just never *used*.

### 4.3 Issue 3 (MINOR) — stale docstrings, two of them load-bearing

Corrected location (C-3). **[A]** Six statements are false against the code, not four:

| Location | Says | Truth |
|---|---|---|
| `retirement.py:14-15` | clause 1b is "reachable for any memory the census verified, **anchored or not**" | **False.** `ingest` joins only `anchored_episodes()`, which filters `ea.anchor != ''`; every verify row is written inside the `matches` loop. Verified: **no production writer** can put a `verify_*` row on an anchor-less episode. |
| `retirement.py:204` | "clause 1a: materialized drift at the **canonical tip**" | False since `_gate_drift_verdicts` resolves the declared line. |
| `tests/domain/test_retirement.py:1-9`, `:146-150` | "drift at the **canonical tip**"; "any censused memory" | Same, and the second is the BUG-059/064 certify-the-unreachable pattern. |
| `INTERACTIONS.md` **[C6]** | verify riders "are written **ONLY on a `pre_merge` ingest**"; the daemon's ledger leg "never marks staleness by itself" | **False in both halves, and load-bearing.** `change_evidence.py:809-820` gates the riders on the **version stamp alone, ANY phase**, under a Law-7 marker (*"restoring the old `phase == "pre_merge"` condition here is the named rider mutation"*), and `sync.py:557` ingests `phase="post_merge"`. Independently re-verified by the reviewer. [C6] is the exact fact a reader needs to judge clause 1b. |
| **[A]** `contract.py:72` | the gate verifies "anchor drift **at the canonical tip**" | **False since BUG-064** — the gate resolves the memory's OWN declared line. **This is the widest-reach stale statement in the tree: `REMEDIATION_NOTICE` is served to every agent on every stale hit.** It rides the uncapped result channel (THEORY §5), so correcting it costs no budget. |
| **[A]** `THEORY.md` §5 | "the recall rider … derives only from rows measured on the repo's canonical line" | **False in production** (C-6). Either the code moves to match it (D5-B) or the sentence goes (D5-A). |

### 4.4 Issue 4 (MINOR) — advertised drift enum has no mechanical tie

Confirmed and **worse than stated**: `tests/contract/test_verdict_writer_coverage.py:266` diffs
the produced set against `hive.app.drift.WIRE_VERDICTS` — it never reads `tool_defs` at all. So
intent I3 is enforced in only one direction; adding a member to the advertised prose today reds
nothing.

**Production:** `hive/app/tool_defs.py:144-146`. **[A]** Plus `_REPOS_PROPERTY:71-73`, whose
description ("registered repo names", no branch form) contradicts the `repos=['name@branch']`
tagging directive composed into the **same served string** at `:89`/`:118`.
**Tests:** `tests/contract/test_verdict_writer_coverage.py` **(FROZEN)** ·
`tests/contract/test_served_contract.py` **(FROZEN)** · `tests/mcp/test_tool_surface.py`.

### 4.5 Issues 5 & 6 — see §6. No production files change.

---

## 5. The design

### 5.1 Minimality verdict on clause 1b — **LOAD-BEARING. Keep it, and make it line-aware.** **[A] REWRITTEN**

Stated first because everything else depends on it. The two clauses cover essentially the same
**population** (both anchored-only — §4.3), but they do **not** cover the same **facts**, and the
decisive reason is neither budget nor coverage. It is **baseline provenance**:

- **Clause 1a** reads `anchor_drift` — `hive-edge verify` comparing the anchor's current tree
  state against its **stored, server-minted `combdrift/fp`** carrier (`sync.py:_verify_anchor`).
  That baseline lives in the store and is written by the backfill.
- **Clause 1b** reads `evidence_events` — the census engine re-deriving its baseline **from the
  range's own base tree**: `hive/combdrift/change.py:88`
  `old_token = fingerprint_anchor(base_tree, anchor)`.

**1a's baseline is corruptible by its own backfill; 1b's is not.** That is the whole argument, and
it makes N1 a *correctness* case rather than a coverage one.

**N1 — the un-minted anchor (permanent, primary, and airtight).**
For an anchor with an empty `fp_meta` carrier, `sync.py:880/882` send neither `--fp` nor
`--subgraph-fp`; `edge/cli.py:230` maps `""` → `fingerprint=None`; and
`combdrift/resolution.py:206` returns `found=True, reason=REASON_OK` **before**
`_compare_fingerprint` (`:243`), the sole producer of `REASON_SIGNATURE_CHANGED`. So
`anchor_changed` is **unconstructable** for an un-minted anchor, and `blast_radius_changed`
likewise (`edge/cli.py:280`). Only `anchor_missing` survives. Worse: `_backfill` (`sync.py:381`)
runs **before** `_materialize_drift` (`:387`) in the same tick, and mints from the post-`reset
--hard <tip>` tree (`:639`, `:645`) — so a signature break that lands before the first backfill is
**baselined away permanently** and clause 1a reads `fresh` forever. Clause 1b still catches it.
**[A] This is itself a latent defect and is logged as BUG-071 (OPEN), with clause 1b named as its
compensating control.**

**N2 — the DEREGISTERED repo (permanent). [A] CORRECTED.**
The first draft said "a repo fed by CI receipts but never `hive repo add`-ed". **Refuted:**
`anchors.py:110-113` refuses an unknown repo on both anchored write paths, so such a repo has zero
`episode_anchors` rows and clause 1b is equally dead. The case that *does* hold is the
**deregistered** repo: `repo_remove` drops `sync:<name>:%` (killing 1a's tip forever) while
`episode_anchors` is deliberately kept, so `hive ingest` of CI receipts stays fully live on 1b.
`hive/tools/censusctl.py` consults no registry (verified: no `repos` read anywhere on that path;
identity comes from the receipt's own `--repo-id`).

**N3 — budget starvation (verified, and worse than stated).**
`_materialize_drift` is capped at `cfg.drift_per_tick` (`sync.py:738/741-746/752`, default 200)
and its cache key is `PRIMARY KEY(repo, tip_sha, anchor)` (`store_sqlite.py:112-115`), so a new
tip empties `have` and `missing` becomes *every* anchor. The ceiling is (#live tips × #anchors),
since `tips` is canonical + declared + demanded (`sync.py:718-731`). The ledger leg
(`sync.py:521-577`) has **no cap, no slice, no counter**; one receipt covers the whole
`base..tip`; rows are cumulative and read with no tip predicate. Clause 1b is the **O(changed)**
signal; clause 1a the **O(all anchors × live tips)** one.

**[A] A stated limit of clause 1b (new, load-bearing).** The daemon's ledger leg censuses the
**canonical tracked branch only** (`sync.py:360`, `:556`, `:605-608`), so every daemon-written
`verify_*` row is stamped with the producing repo's canonical ref. Consequences: (i) post-fix,
clause 1b contributes nothing to a memory that declared a *non-canonical* line, on the daemon
path — its N1/N3 value is for canonical-line memories, which is the overwhelming majority;
(ii) a non-canonical `ref` stamp is reachable **only** through the manual `hive ingest` door;
(iii) the corrected module docstring must say this, so no future reader believes 1b protects
branch-scoped memories.

**Therefore:** deleting clause 1b would silently lose retirement coverage for exactly the memories
most likely to be wrong (freshly written, freshly broken, on a corrupted baseline). Keep it; make
it judge at the declared line. What we delete instead is the **duplicated ownership** (§1), which
is where the actual redundancy lives.

*(Considered and rejected: folding 1a and 1b into one `staleness_evidence` reader — D6. Rejected
on cohesion, not on audit explainability.)*

### 5.2 Blocking defect 1 — the fix

**Why the meta-envelope law does not apply.** THEORY §5 scopes itself in its own heading —
*"The meta envelope law (episode `meta` tags)"* — and closes with an explicit reach statement:
*"Reach: the episode-meta envelope only — receipt/provenance token carriers are out of scope."*
Structurally it could not apply anyway: clause 2 requires every value to be a self-describing
token `<engine>-<kind>/<N>:<body>`, and clause 7 enumerates the four registered keys, all episode
`meta` keys. `evidence_events.payload` is a JSON object rendered by
`domain/change_evidence.py:render_verify_payload`, with no version token and no registry row.
**[A]** `evidence_rows_for`'s docstring currently cites the law and must stop — `anchor_carriers`'
citation of the same law is correct (`fp_meta` *is* an episode-meta carrier) and stays.

**`hive/domain/change_evidence.py`** **[A]** — new pure helper, beside `render_verify_payload`:

```python
def verify_payload_ref(payload: str) -> str:
    """The LINE a ``verify_*`` row was measured on — the ``ref`` this module's own
    ``render_verify_payload`` stamps into the body. "" for anything undecidable: an
    empty, unparseable or non-object body, or a legacy pre-stamp row that carries no
    ref (render emits the key only when non-empty — its Law-7 marker). The SINGLE
    owner of READING what render_verify_payload WRITES; every consumer (the retirement
    gate's feed, the recall rider's scoping arm) goes through here. Total, never raises.
    PURE: stdlib json only."""
```

**`hive/adapters/store_sqlite.py`**

- `evidence_rows_for` returns `list[tuple[str, str, int, str]]` = `(kind, actor, ts, ref)`, with
  `ref = verify_payload_ref(row["payload"])`. Docstring: *"``ref`` is the measured LINE, projected
  by ``change_evidence.verify_payload_ref`` — the gate decides RELEVANCE, the payload grammar
  stays in the one module that writes it. "" = unknown line; a kind with no ref (the hurt kinds)
  always reads ""."*
- `last_verification`'s inline `payload.get("ref")` is replaced by the same helper, keeping its
  **absence rule** intact (`if canonical_ref and row_ref and row_ref != canonical_ref: continue`).
  Policy diverges by design; the *reading* does not. Its Law-7 marker stays verbatim.

**`hive/domain/retirement.py`**

- `EvidenceRow.payload: str = ""` → `EvidenceRow.ref: str = ""`, docstring: *"the LINE the row was
  measured on ("" = unknown, supplied by the adapter — the domain never parses a payload)."*
- `_row_line` and `import json` **deleted**.
- `_row_fields` returns `(kind, actor, ts, ref)`; a 3-sequence still yields `ref=""`.
- `own_lines: Optional[Iterable[str]]` → `Optional[frozenset[str]]` so `mypy --strict` carries the
  tri-state instead of prose. Semantics restated: `None` ⇒ unfiltered (byte-identical to
  pre-build behaviour); a **non-empty** set ⇒ only rows on those lines are attributable; the
  **empty** set ⇒ nothing is attributable (under-claim).
- Clause 1b's filter: `if attributable is not None and (not ref or ref not in attributable): continue`.
- **[A]** The multi-line collapse (`lines if len(lines) == 1 else frozenset()`) is **deleted** —
  the boundary now produces at most one line by construction (A-1). The existing Law-7 marker on
  the multi-repo widening is **re-homed onto `_gate_own_lines`**, where the rule now lives.
- **[A]** `_QUALIFYING_DRIFT` → public `QUALIFYING_DRIFT`, with a comment naming the shared
  reasoning: *"the wire verdicts that mean the anchor MOVED — one tier, two policies: it qualifies
  a retirement here and it is what `drift.branch_route_verdict` may soften to `branch_scoped` for
  an off-line consumer. A member that means 'moved' must do both or neither."*
- The `verify_current` arm stays **unfiltered**, with its comment (K-4).

**`hive/app/drift.py`**

- `_tip_for` → **`tip_for`** (public; docstring gains "the single owner of 'which tip do I judge
  repo R at', shared by the recall enrichment and the retirement gate"). No behaviour change.
- **[A]** `STALE_TIER` **deleted**; `from hive.domain.retirement import QUALIFYING_DRIFT` and
  `base in QUALIFYING_DRIFT` at `:141`. Direction is forced: `hive/domain/` may not import
  `hive.app` (verified: zero such imports), so the shared tier must live in the domain.

**`hive/app/mcp_server.py`**

- `_gate_drift_verdicts` — replace the inline branch + raw `SELECT value FROM meta` with
  `tip = tip_for(self.store, repo, declared.get(repo, ""))`. Deletes 6 lines and one raw-SQL read.
- **[A]** `_gate_own_lines` — final shape:

```python
def _gate_own_lines(self, ep: Episode) -> Optional[frozenset[str]]:
    """§3.2 clause 1b's line filter: the line whose ledger evidence is THIS memory's
    own. A verify payload stamps the LINE it was measured on but NOT the repo, so a
    row is attributable only when the memory has exactly ONE anchored repo:
      - no anchors           -> None  (clause 1b is unreachable for an anchor-less
                                       memory: change_evidence.ingest joins only
                                       anchored_episodes(), which filters anchor != '')
      - != 1 anchored repo   -> frozenset()  (nothing attributable — under-claim)
      - 1 repo, declared ref -> frozenset({ref})
      - 1 repo, no declared  -> None  (it named no line, so canonical rows ARE its own;
                                       byte-identical to pre-declared-line behaviour)
    Under-claim, never over-claim. Raises freely — the caller owns the fail-closed.
    // marker: returning a non-empty set for a memory with MORE THAN ONE anchored repo
    // is the named mutation — the payload carries no repo, so another repo's staleness
    // on a same-named line would retire it (reds
    // tests/domain/test_retirement.py::test_two_anchored_repos_attribute_nothing and
    // tests/mcp/test_retirement_gate_boundary.py::test_multi_repo_memory_is_never_
    // attributable)."""
```

  This deletes the `tracked_ref` raw meta read, the `tracked_ref_key` import, the
  `or set(declared)` fallback (dead once anchor-less returns early), and the collapse-set logic.

**`hive/app/contract.py`** **[A]** — `REMEDIATION_NOTICE`: "anchor drift at the canonical tip" →
"anchor drift on the memory's own declared line". Uncapped channel, no budget movement.

**Fail-closed audit of the whole path** (required by the issue's constraints):
`episode_refs` read raises → `_retirement_eligibility`'s `except` → `_INELIGIBLE` → noop.
`tip_for` raises → same. `evidence_rows_for` raises → same. `verify_payload_ref` never raises
(total). A row whose ref is unparseable under an engaged filter → skipped. A memory with **no**
declared ref → `own_lines is None` → clause 1b unchanged from today, so the "overwhelming
majority keeps being judged at canonical" constraint holds **by construction**, not by care.

### 5.3 Blocking defect 2 / BUG-067 — the fix

**Two call sites, both one line, closing two different failures.**

1. **Correctness (the re-registration leak, §4.2).** `repo_remove` sweeps the repo's
   feed-derived caches in the **same tx** as the registry row: `DELETE FROM ref_tips WHERE repo=?`,
   `DELETE FROM anchor_drift WHERE repo=?`, `DELETE FROM ref_requests WHERE repo=?`. The
   docstring's claim that these "are not feed state and stay" is replaced by the honest
   invariant: *deregistration forgets the feed **and everything derived from it**; only the
   memories, their scope (`episode_anchors` / `episode_refs`), and the append-only ledger
   survive.* Justified by Law 5 — all three are rebuildable caches, and a re-registered repo
   re-materializes them on its first tick, which is exactly what a fresh registration is defined
   to do.
   **[A] Justification corrected:** this is not defence against a post-deregistration read
   (impossible — `normalize_repos` refuses the name), it is defence against a **re-registration**
   read. **[A] Breadth ruling:** only `ref_tips` is strictly required (without a tip, no
   `anchor_drift` row is reachable). Sweeping all three is two extra `DELETE`s that **define the
   error out of existence** (#11) — no future reader has to reason about whether an orphaned row
   is reachable. Kept.
   **[A] `episode_refs` is deliberately NOT swept** — it is *memory* data (the line the writer
   declared), not feed data. `repo_remove`'s docstring and `INTERACTIONS.md` [C1] both state its
   retention as deliberate; deleting it would destroy user-declared scope. There is no twin leak.
2. **Hygiene (BUG-067 as filed).** `sync._materialize_drift` calls
   `self._store.ref_tips_prune(name, keep_refs=[ref for _n, ref, _sha, _ts in resolved_refs])`
   under the lock, immediately beside the existing `drift_prune(name, tips, keep_anchors=anchors)`.
   `resolved_refs` is already the exact set the tip list was built from (`sync.py:719-736`), so no
   new computation is introduced. A ref that fails to resolve this tick loses its watermark → the
   next read is `unverifiable` (fail-safe) → it re-resolves next tick.

**Considered and rejected:** *delete `ref_tips_prune` as dead code and accept the growth.* More
minimal-looking and wrong: it leaves the re-registration defect unaddressed and makes `ref_tips`
the only rebuildable cache with no bound, contradicting Law 5 and the sibling `drift_prune`.
**Also rejected:** re-modelling `ref_tips` as `sync:<repo>:ref:<ref>:tip` meta keys so the
BUG-060 sweep covers it for free — a variable-cardinality key family breaks
`sync_keys.FLEET_KEY_BUILDERS`' fixed-field model and the 3-part grammar `census_health` groups
on, and it still needs a per-ref reconcile.

### 5.4 Issue 4 — **close it. It is the module's own stated convention, and it is free.**

**Project**, do not test-patch. `tool_defs.py`'s docstring already says its enums are "PROJECTED
from the registry so the advertised … cannot drift from the served/stored vocabulary," and it
already does exactly this for `hive.domain.kinds`.

Verified by execution: `" | ".join(WIRE_VERDICTS)` produces
`'fresh | anchor_changed | anchor_missing | blast_radius_changed | branch_scoped | unverifiable | n/a'`,
**present verbatim** in the shipped `hive_recall` description. The projection is byte-identical —
`METADATA_FIELD_LIMIT` is untouched and the golden served contract does not move. No import cycle
(`drift.py` imports only `hive.app.anchors`, `hive.app.sync_keys`, and — after A-11 —
`hive.domain.retirement`; `tool_defs.py` imports none of those).

This is **not** over-engineering: today `test_verdict_writer_coverage` proves *WIRE_VERDICTS ⊆
emittable* but nothing proves *advertised = WIRE_VERDICTS*, so I3 holds in one direction only.
The projection closes the other by construction (Law 2) at the cost of one f-string.

**[A] Plus `_REPOS_PROPERTY` (A-13).** Its description ("Repo scope without a code anchor:
registered repo names.") contradicts the `repos=['name@branch']` directive composed into the same
served string. It gains the `'name'` / `'name@branch'` form. **The tempting deletion — dropping
the duplicated directive from `WRITE_VS_CAPTURE` — is REFUSED**:
`tests/contract/test_served_contract.py:171`/`:186` (FROZEN) assert `@branch` in
`WRITE_VS_CAPTURE` *and* in both tool descriptions, explicitly rejecting "buried only in the
schema's field description". Changing a test to permit a refactor is not on the table. Property
descriptions are not `t["description"]`, so the cap test (`:88`) does not see them: **zero budget
cost** on `hive_write`, which sits at **2015/2048**.

### 5.5 **[A] NEW — the consolidation set the first draft missed**

Each is a fact with two owners found by the whole-diff sweep. Evidence in the review §3.

**A-3 — the dead ledger-rider scope (C-6). DECIDED: A-3a — wire it.**
`HiveMCPServer.canonical_ref` (`mcp_server.py:366`, `:416`) is never passed by
`container.make_server()` (`container.py:194-201`, which says so in a comment), so
`last_verification(canonical_ref=…)` (`store_sqlite.py:926`), its `ports.py:136` kwarg, its filter
and its Law-7 marker at `:956` are structurally unreachable, and five tests
(`tests/store/test_last_verification.py:137-192`) certify the unreachable. THEORY §5 asserts the
scoped behaviour as shipped.

**The operator ruled A-3a. Build this; there is no remaining choice in this item.**

Wire it. Replace the server-level label with the memory's own line — the same `episode_refs` read
A-1 already performs — so the rider stops labelling a memory "stale" on a line it never declared.
`LastVerificationReader` takes a per-episode ref map instead of one global label (a *refinement*
of the existing defaulted kwarg, not a new port). The absence rule and its Law-7 marker move with
it, intact.

Log **BUG-070** and reconcile the THEORY §5 sentence to the now-live behaviour.

*Considered and rejected — A-3b (delete the whole chain: parameter, port kwarg, filter, marker,
five tests, and the THEORY §5 sentence, stating the rider as unscoped).* It is the strictly
smaller change and nothing breaks, but it ratifies a serve-path advisory that labels a memory
"stale" on a line it never declared — blocking defect 1's advisory twin, contradicting
`branch_route_verdict`'s own posture. A-1 and A-2 build both ingredients (the memory's own line;
a typed `ref`) regardless, so wiring costs little. Leaving the parameter unreachable while THEORY
asserts it live was ruled out from the start: that is the exact BUG-059/064 defect class.

**A-4 — the not-retired work-list predicate, spelled three times.**
`store_sqlite.py:1137` (`anchors_lacking_fp`), `:1160` (`anchor_carriers`), `:1455`
(`declared_refs` — added by this build). `anchor_carriers`' own docstring claims it is *"kept next
to it so BUG-065 is fixed in ONE place, not two near-identical joins"* — a lying contract, since
there are three copies sharing no symbol. Fix: one module-level SQL-fragment constant beside the
file's existing `_RECALL_PREDICATE` (`:63`, the same idiom), referenced by all three. That makes
the docstring true by construction.
*Considered and rejected:* collapsing `anchors_lacking_fp` into `anchor_carriers` + a caller-side
filter — it pushes two filters upward onto callers (against #10) and breaks the tuple shape two
test modules consume.

**A-11 — the "anchor moved" tier (C-7).** `drift.STALE_TIER` deleted; `retirement.QUALIFYING_DRIFT`
made public and imported. §5.2.

**A-12 — the demand bar counts two different quantities.** `lifecycle.py:277` now gates on
`n_other < demand_m` (BUG-066); `mcp_server.py:1297` still gates on `len(misses) < demand_m` —
writer-inclusive, scope-unfiltered, cosine-unfiltered — while `_solo_hint`'s docstring claims to
model the gate ("promotion is silently inert"). At the shipped `demand_m=1` that claim is false
whenever the single window identity is not the candidate's writer. Fix: **delete the floor** —
`if not misses: return None` — since the clause below it (`len({m.agent_id}) > 1 ⇒ None`) is what
actually detects identity collapse. Byte-equivalent at `demand_m=1`; removes `demand_m` from the
boundary entirely; `tests/mcp/test_solo_hint.py` (updated this build to the new bar) stays green.
Telemetry-only — it gates nothing.

**A-13 — the write-side `repos` grammar.** §5.4.

**Named, recorded, NOT actioned** (pre-existing, off the defect path, or unsafe to delete):
- "read one meta kv" has five implementations (`drift._meta_value`, `sync._meta_get`,
  `census_health.py:125`, and the two this change deletes). The store exposes `meta_set` with no
  `meta_get`, which is the cause. Filed as a follow-up (§6.3), not pulled into scope.
- `store_sqlite.py:310-319` (`stage`) carries a first-wins-per-repo tie-break made unreachable by
  `normalize_repos:157-160`. **Not deleted**: `episode_refs`' PK is `(episode_id, repo)`, so
  removing the guard turns an unreachable case into an `IntegrityError` mid-transaction.
- `drift.py:335`'s `or (name not in canonical and canonical)` arm is unreachable on the
  production path (its only caller passes `normalize_repos` output). Pre-existing (`941a5a1`).
- `mcp_server.py` is 1860 lines / 30 methods and gains no decomposition here. THEORY §8.1 names
  the god-boundary as a *conscious* tension; splitting it on the back of a two-defect fix is the
  tactical-drift pattern this review exists to catch. Deferred deliberately.

---

## 6. Issues 5 and 6 — explicit scope calls

### 6.1 Issue 5 — content-addressed drift cache key: **OUT OF SCOPE**

The diagnosis is correct: `anchor_drift`'s PK is `(repo, tip_sha, anchor)`, `tip_sha` is only a
cache key, and every commit invalidates every verdict for the repo.

Out of scope for three reasons, in order of weight:

1. **The cheap version is unsound in the forbidden direction.** Verified at the computation:
   `edge/cli.py:_subgraph_member_tokens` (`:588-620`) builds the member set as
   `{seed} ∪ _forward_ids(...) ∪ radius.callers ∪ radius.dependents` — an explicit union cone
   **including reverse reach**. A change in a *caller* moves the token without touching the
   anchor's own blob, so a blob- or subtree-keyed cache would serve a stale **`fresh`** — the one
   direction the drift system may never take (`drift.py`: "never false-fresh"). Making it sound
   requires computing the neighbourhood at the new tip, which is the expensive part.
2. **No correctness defect exists today.** A cold cache reads `unverifiable`, the documented
   fail-safe. This is latency/throughput; the two blocking items here are correctness.
3. **Blast radius.** It would reach `sync.py`, `store_sqlite.py` (a new PK ⇒ a new table ⇒ the
   very migration question issue 6 raises), `drift.py`, `hive/matrix`, and the frozen materializer
   tests — several times this plan's total surface.

**Follow-up to file (not to do now):** *"Make the drift work list O(changed): derive the per-tick
anchor set from the ledger leg's own `base..tip` receipt (the daemon already passes `--propagate`,
`sync.py:609`, so the receipt already carries blast-radius neighbours) and carry verdicts forward
for anchors provably outside that set."* **[A] Record two traps, not one:** (i) the correctness
obligation is **the neighbourhood, not the file**; and (ii) `_subgraph_member_tokens` runs at
`depth=_UNBOUNDED_DEPTH` while `census build --propagate` is depth-bounded, so a work list derived
from a depth-N propagation **cannot** certify an anchor outside it as unchanged under an unbounded
fingerprint — **the two depths must be unified first.** That raises the follow-up's cost and
confirms nothing in it is cheap enough to pull forward. Interaction worth recording: §5.1's N3
says clause 1b is currently compensating for this gap, so closing issue 5 would *reduce* — never
remove — clause 1b's load.

### 6.2 Issue 6 — the two-table split and a real migration path: **OUT OF SCOPE, and the premise is half wrong**

**The premise, corrected.** `episode_refs` is not a scar at all: an episode can declare one ref
per repo, so the fact is genuinely 1:N. And `ref_tips` is not a scar either — even if
`anchor_drift` *had* a `ref` column, you would still need somewhere to record "ref R resolved to
SHA S" **before any verdict exists**, because that is precisely the BUG-063 fail-safe. Verified at
`sync.py:730-736`: `ref_tips_put(resolved_refs)` runs **before** the verify batch, under its own
Law-7 marker naming `test_unmaterialized_branch_tip_is_unverifiable_not_fresh`. A verdict cache
cannot store a fact that exists in the absence of verdicts. `ref_tips` is a **watermark**, a
different kind of thing. The split is a **choice**, correctly made.

What *is* a real scar is narrower and is fixed in §5.3: `ref_tips` is the branch twin of a fact
(`sync:<repo>:last_tip`) that lives in `meta`, so it silently fell outside the `sync:<name>:%`
sweep BUG-060 installed. The lesson to record is "a new per-repo fact must be added to
deregistration's forget-list," not "we need migrations."

**The migration question.** Out of scope because **nothing in this plan needs it** — the change
adds no column to any existing table, and the previous build's end-to-end verifier already proved
on a real pre-build store that `CREATE TABLE IF NOT EXISTS` upgrades a live v3 volume with no
reset. Building it now would be speculative generality against a hypothetical future change, on a
system whose stated posture is "Migration is explicit and refuses old-format tables at boot — **no
silent migration, ever**" (THEORY §5). Changing that posture is a ratified product decision.

**Follow-up to file:** *"An explicit, operator-invoked `hive migrate` verb: backup-first, refuses
to run implicitly at boot, one ordered and individually-tested step per schema change, exit-code
contract like every other operator tool."* Record that the v3 cutover cost a real dogfood store,
so the cost of *not* having it is known and non-zero — a deferral with a price, not a dismissal.

### 6.3 **[A]** Follow-up — `store.meta_get`

*"Add a `meta_get(key) -> str | None` verb to the store and collapse the five hand-rolled
`SELECT value FROM meta WHERE key=?` reads (`drift._meta_value`, `sync._meta_get`,
`census_health.py:125`, and the two this change deletes) onto it. The asymmetry — a `meta_set`
with no `meta_get` — is what forces every reader to reach into `store.conn`."* Out of scope here:
the two copies this build introduced are deleted by A-1 + K-3, and the remaining three are
pre-existing and off the defect path.

---

## 7. Intents and traceability

Every intent becomes an end-to-end behavioural contract test driving **real** production writers —
real git origin, real `hive-edge mint`/`verify` subprocesses, real MCP handler, real
`SyncService.tick()`, real census ingest. **Nothing seeded**: the reason blocking defect 1 survived
the last build is that its only covering test seeded `drift_put`/`ref_tips_put` and never ran a
tick. **[A] Hard rule:** no test in this change may seed `drift_put` / `ref_tips_put` / `meta_set`
/ `insert_audit` to manufacture the condition **under test**. Seeded helpers stay legal only for
*arranging* an unrelated precondition. Where a real sync tick structurally cannot produce the
condition (J1's own-line retirement — C-5), the test drives the **real** `hive.census.cli build
--ref <line>` CLI and the **real** `ChangeEvidenceService.ingest` door instead.

**Frozen-suite boundary.** `tests/contract/**` is FROZEN in the active build
(`BRANCH-SCOPE-AND-DEMAND-PLAN.md:257`, `frozen_paths`).

| Inside `tests/contract/**` (frozen-suite author only) | Outside (any implementer) |
|---|---|
| `test_retirement_gate_e2e.py` (J1, J2) · `test_branch_scope_e2e.py` (J3) · `test_verdict_writer_coverage.py` (J5) · `test_served_contract.py` (J5 budget, J7) | `tests/domain/test_retirement.py` (J1, J8) · `tests/domain/test_change_evidence.py` (J1 helper) · `tests/store/test_change_evidence_store.py` (J1 projection) · `tests/store/test_last_verification.py` (J7) · `tests/store/test_repo_registry_store.py` (J3) · `tests/store/test_drift_cache_store.py` (J3) · `tests/mcp/test_retirement_gate_boundary.py` (J1, J2) · `tests/mcp/test_solo_hint.py` (J7) · `tests/sync/test_contract_drift.py` (J4) · `tests/app/test_drift.py` (`tip_for`, J8) |

| # | Intent | Contract (given / when / then) | Scenarios | Tests |
|---|---|---|---|---|
| **J1** | The retirement gate judges a branch-scoped memory's **ledger** evidence at its own declared line, exactly as it already judges its materialized drift. | **Given** `hive_write(repos=["alpha@feature"], anchors=[{alpha, app.py::greet}])` on a real origin, **when** a real sync tick over a real breaking commit on `main` writes a `verify_stale` row stamped `"ref":"main"`, **then** `hive_prune` is a benign **noop** and trust stays non-deprecated. **[A] And when** a REAL `census build --ref feature` receipt over a real break on `feature` is ingested through the REAL `ChangeEvidenceService.ingest` door (the daemon censuses the canonical branch only — C-5), **then** it retires with `signals` containing `verify_stale`. | declared≠row-ref + stale ⇒ noop (real tick); declared==row-ref + stale ⇒ retire (real manual ingest); **no** declared ref + stale on canonical ⇒ retire (today's behaviour, unchanged); declared ref + newer `verify_current` on another line ⇒ noop (the unfiltered-current arm); ref-less legacy row + declared ref ⇒ noop (under-claim); **[A]** two anchored repos ⇒ **nothing attributable**, noop even when both declare the same line (A-1's recorded coverage loss, pinned so it is a decision not an accident); `episode_refs` read faults ⇒ noop | `tests/contract/test_retirement_gate_e2e.py::test_a_ledger_stale_row_on_another_line_never_retires_a_declared_line_memory` **(F)**, `::test_a_ledger_stale_row_on_the_memorys_own_line_still_retires_it` **(F)**; `tests/domain/test_retirement.py::test_own_lines_*` (truth table); `tests/mcp/test_retirement_gate_boundary.py::test_multi_repo_memory_is_never_attributable`; `tests/domain/test_change_evidence.py::test_verify_payload_ref_round_trips_render_verify_payload` |
| **J2** | Clause 1b's population is anchored-only, mechanically — no advertised-but-unreachable behaviour survives (the BUG-059/064 seam). | **Given** an anchor-less `hive_write`, **when** a real tick ingests a real receipt over a real change, **then** the episode has **zero** `verify_*` rows and `hive_prune` on it is a noop. | anchor-less memory + real breaking change; scope-only (`repos=['alpha']`, no anchors) memory + same change | `tests/contract/test_retirement_gate_e2e.py::test_an_anchorless_memory_never_acquires_a_verify_row` **(F)**; `tests/domain/test_retirement.py::test_verify_ledger_clause_reaches_general_memories` **corrected** — renamed `test_verify_clause_is_anchor_agnostic_in_the_pure_function`, its false system-fact comment replaced with the production reachability rule and a pointer to the e2e twin |
| **J3** **[A]** | Deregistering a repo forgets the feed **and everything derived from it**, so a **re-registered** name can never serve a verdict from its previous incarnation. | **Given** a registered repo with materialized branch verdicts, **when** `hive repo remove alpha` runs **and `hive repo add … --name alpha` re-registers it against a remote with no `feature` branch**, **then** a recall with `repos=["alpha@feature"]` serves `unverifiable`, not `fresh` — and `ref_tips`/`anchor_drift`/`ref_requests` held zero rows for `alpha` from the moment of removal. | **[A]** re-registration then branch-scoped recall (the serve-path defect); branch-scoped recall while deregistered ⇒ **`refused`**, not `fresh` (the corrected pre-condition — C-4); unscoped recall after deregistration ⇒ `unverifiable`; re-register ⇒ re-materializes from scratch; other repos untouched; **memories, `episode_anchors` and `episode_refs` survive** (BUG-060's kept half, and the reason `episode_refs` is NOT swept) | `tests/contract/test_branch_scope_e2e.py::test_a_reregistered_repo_never_serves_a_verdict_from_its_previous_incarnation` **(F)**; `tests/store/test_repo_registry_store.py::test_repo_remove_forgets_the_feed_state` **extended** |
| **J4** | A ref that stops being canonical, declared, or demanded loses its watermark on the same tick it leaves the work list. | **Given** three real demanded branches with watermarks, **when** two are deleted on the real remote and a tick runs, **then** `ref_tips` holds exactly the surviving ref (+ canonical), and a recall on a deleted branch reads `unverifiable`. | deleted branch; retired-episode's declared ref (`declared_refs` already excludes it); demand aged past the 7-day window; a transient resolve failure ⇒ watermark dropped, re-resolved next tick (fail-safe, not fail-wrong) | `tests/sync/test_contract_drift.py::test_ref_tips_are_reconciled_against_the_resolved_ref_set`; `tests/store/test_drift_cache_store.py` (existing `ref_tips_prune` units, now with a live call site) |
| **J5** | The advertised drift vocabulary and the emittable drift vocabulary are the **same object** — I3 enforced in both directions. | **Given** the `hive_recall` tool description, **when** the suite runs, **then** the advertised enum is `WIRE_VERDICTS` by construction and every member is produced by a real writer driven end to end. | all 7 members; a member added to `WIRE_VERDICTS` with no writer must red; every served field still ≤ `METADATA_FIELD_LIMIT` | `tests/contract/test_verdict_writer_coverage.py::test_every_advertised_drift_verdict_has_a_production_writer` **(F, unchanged — it becomes complete)**; `tests/contract/test_served_contract.py` **(F)**; `tests/mcp/test_tool_surface.py` |
| **J6** | Every statement the instruction layer makes about the retirement gate and the census feed is true of the code. | **Given** `/audit-docs --changed` over the touched set, **when** it runs, **then** no strict contradiction remains in `INTERACTIONS.md`, `THEORY.md`, `BUGS.md`, `contract.py`, or the module docstrings named in §4.3. | [C6] rider gate; [M2] gate signals; [C1] deregistration; `THEORY.md` §5 (both the meta-envelope reach sentence and the ledger-rider sentence); `contract.py:72`; `retirement.py` docstrings | `/audit-docs --changed` + review; no new test |
| **J7** **[A]** | No advertised-but-unreachable surface survives this change — the fourth application of the BUG-059/064 seam. | **Given** the server built by the real `container.make_server()`, **when** a memory declaring `alpha@feature` acquires a canonical-line `verify_stale` row from a real tick, **then** the served `last_verified` rider must **not** report it stale on the consumer's behalf. And: the `repos` schema property must state the same grammar the description directs. | rider on a declared-line memory vs a canonical-line memory; the absence rule (legacy ref-less row still counts); the `repos` property names `'name@branch'`; `_solo_hint` no longer reads `demand_m` | `tests/store/test_last_verification.py` **rewritten to the shipped contract** (the five tests currently certify an unreachable path); `tests/mcp/test_tool_surface.py::test_repos_property_names_the_branch_form`; `tests/mcp/test_solo_hint.py`; `tests/contract/test_served_contract.py` **(F)** |
| **J8** **[A]** | One fact, one owner — the forks this change closes cannot silently re-open. | **Given** the assembled system, **when** the suite runs, **then** `drift` and `retirement` agree on the "anchor moved" tier **by identity, not by value**; there is exactly one tip resolver; there is exactly one verify-payload `ref` reader; and the not-retired work-list predicate is one string. | `drift.STALE_TIER` must not exist (`hasattr` is False) and `branch_route_verdict` must route exactly `QUALIFYING_DRIFT`; `mcp_server` contains **zero** `FROM meta` reads (AST assertion, the `test_sync_keys.py` idiom); `store_sqlite` interpolates the predicate constant in all three work-list verbs; `retirement` imports no `json` | `tests/app/test_drift.py::test_the_anchor_moved_tier_has_one_owner`; `tests/mcp/test_tool_surface.py::test_the_boundary_holds_no_raw_meta_reads`; `tests/store/test_store_sqlite.py::test_the_work_list_predicate_has_one_spelling`; `tests/test_purity.py` (unchanged, still enforces the domain) |

**Not a test change anyone may make:** `tests/store/test_change_evidence_store.py:153` is currently
**red**. It is not "a test to relax" — it asserts the *old* projection shape of a verb whose
production contract deliberately changed. The corrected behaviour it must pin is the new one:
`(kind, actor, ts, ref)` with `ref` projected by `verify_payload_ref` and `""` for a payload that
carries none. Its sibling assertions (kind filtering, insertion order, per-episode isolation,
empty-kinds ⇒ `[]`) are unchanged and must stay. **[A]** The same rule applies to the five
`tests/store/test_last_verification.py::test_canonical_ref_*` tests: they are not relaxed, they are
**re-pointed at the shipped contract** — the A-3a per-episode ref map.

---

## 8. Implementation order

Each step is safe because the step before it is inert or independent.

**Step 1 — make the gate red for the right reason, then green.** *(outside frozen paths)*
Correct `tests/store/test_change_evidence_store.py` to the `(kind, actor, ts, ref)` contract **and**
apply **A-2** (`change_evidence.verify_payload_ref` + `evidence_rows_for` projection +
`last_verification` sharing it; `EvidenceRow.ref`; delete `_row_line` and the `json` import; correct
`evidence_rows_for`'s meta-envelope citation). Safe because the projection is a superset of today's
read and `_row_fields` still accepts every legacy shape.
**Exit:** `make check` green — the canonical gate restored before any behaviour changes.

**Step 2 — the failing behaviour, pinned first.** *(frozen: J1/J2 modules)*
Author the J1/J2 contract tests driving a real census ingest — including **[A]** the manual
`census build --ref feature` + `ChangeEvidenceService.ingest` leg for the own-line direction —
observe **red**, capture the red output as evidence. No `hive/` file is touched. Required because
the previous build's failure was precisely a green test over a seeded feed.

**Step 3 — blocking defect 1.** *(outside frozen paths)*
**A-1** (`_gate_own_lines`' final shape; delete the `tracked_ref` read, the `tracked_ref_key`
import, the `or set(declared)` fallback and the collapse-set logic; re-home the Law-7 marker),
**K-3** (`tip_for` public; `_gate_drift_verdicts` calls it; delete the raw meta read), **A-11**
(`QUALIFYING_DRIFT` public; delete `STALE_TIER`), the `own_lines` type tightening, and **[A]**
`contract.py`'s `REMEDIATION_NOTICE`. Add the `tests/domain/test_retirement.py` truth table and the
boundary tests.
**Turns green:** J1, J2. Partially J8.

**Step 4 — blocking defect 2 / BUG-067.** *(frozen: J3's e2e; the rest outside)*
`repo_remove`'s same-tx sweep + docstring; `_materialize_drift`'s `ref_tips_prune` call. Different
files from Step 3, no shared symbol.
**Turns green:** J3, J4.

**Step 5 — issue 4 + the served-grammar fix.** *(outside frozen paths)*
Project the drift enum in `tool_defs.py` from `WIRE_VERDICTS` (verified byte-identical, so
`test_served_contract` and the golden description do not move). **[A]** Plus **A-13**
(`_REPOS_PROPERTY` names the branch form — zero budget cost).
**Completes:** J5.

**Step 6 — [A] the consolidation set.** *(outside frozen paths, except J7's frozen twin)*
**A-3** (A-3a, ruled — wire the rider to the memory's own line), **A-4** (the predicate constant), **A-12**
(`_solo_hint`'s floor). Independent of Steps 3–5 — different files, no shared symbol.
**Turns green:** J7, the rest of J8.

**Step 7 — the instruction layer, in the same change.**
- `INTERACTIONS.md` — **[C6]** rewritten to the stamp-only/any-phase truth with the marker quoted;
  **[M2]** gains "the ledger form is judged at the memory's own declared line, and only when the
  memory is anchored in exactly one repo" with the `mcp_server.py:_gate_own_lines` anchor and its
  DETECT tag; **[C1]** gains the widened deregistration forget-list **and** the explicit note that
  `episode_refs`/`episode_anchors` are kept. **[A]** [E5]/the recall-rider entry gains the rider's
  line scoping: no interaction is added or removed, but the rider's *outcome* changes, so its
  entry is edited in place.
- `BUGS.md` — BUG-067 → SOLVED; **BUG-068** logged and solved for the **re-registration**
  serve-path leak (§4.2, corrected symptom), category DATA_WIRING, cross-referencing BUG-060;
  **BUG-069** logged and solved for clause 1b (blocking defect 1), category LOGIC_DEFECT, with the
  N1/N2/N3 reachability analysis recorded so a future minimality pass does not delete the clause;
  **[A] BUG-070** logged for the unreachable ledger-rider scope (C-6), category
  CONTRACT_VIOLATION, cross-referencing BUG-059/064, solved by wiring the rider (A-3a);
  **[A] BUG-071** logged **OPEN** for the un-minted-anchor baseline corruption (§5.1 N1), category
  LOGIC_DEFECT, with clause 1b named as the compensating control and the `_backfill`-before-
  `_materialize_drift` tick order recorded as the aggravating factor.
- `THEORY.md` — §3's retirement paragraph gains "on the memory's own declared line"; **[A]** §5's
  ledger-rider sentence reconciled to the wired, own-line-scoped rider (A-3a); **[A]** §5's meta-envelope
  reach sentence left alone (it is already correct — it is what proves the law does not reach
  `evidence_events.payload`).
- Module docstrings from §4.3; `CHANGELOG.md`; `llms-full.txt` / `README.md` / `HIVE-ADMIN.md` /
  `OPERATIONS.md` wherever the gate signals or deregistration semantics appear; then
  `/audit-docs --changed`; then `graphify update .`.

**Step 8 — gates.** `make check`, then `/verify`: this change has a runtime surface, so
`/update-dogfood-server` and drive the real flow — confirm a real `hive repo remove` leaves zero
`ref_tips`/`anchor_drift`/`ref_requests` rows, that a branch-scoped recall on the removed name is
**refused**, and that after re-registering the same name it reads `unverifiable` rather than
`fresh`.

**Build mode.** Steps 1, 3, 4, 5, 6 are small and mostly independent; Step 2 and the J3/J7 frozen
twins are the only frozen-path work. **[A]** The amended scope is larger than the first draft's
(six production steps instead of five, twelve forks instead of four) but each step is still a
localized deletion — well inside a single agent's context. Dispatch one implementer, with the
frozen-suite tests authored by the frozen-suite author.

---

## 9. Risks, named, with mitigations

| Risk | Mitigation |
|---|---|
| **[A] A-1 loses ledger-clause retirement for a memory anchored in ≥2 repos that declare the same line.** | **Accepted and recorded — this is a deliberate coverage-for-safety trade, not an oversight.** Under-claiming is the gate's stated safe direction; clause 1a still judges each repo correctly and per-repo, so no memory becomes unretirable. C-5 narrows the loss further: a same-line multi-repo row can only come from a manual receipt. Pinned by a named J1 scenario so it is a decision, not an accident, and marker-guarded so widening it reds a test. The real fix is D3 option C (stamp `repo` into `render_verify_payload`), filed as a follow-up. |
| **Deleting `anchor_drift` on deregistration loses cached work an operator may not expect to lose.** | Rebuildable by construction (Law 5); re-materializes on the re-registered repo's first tick — which is what "first sync baselines the current tip" already promises. The alternative (keeping it) is what makes the false-`fresh` serve reachable. Stated in `CHANGELOG.md` and `INTERACTIONS.md` [C1] so it is a behaviour, not a surprise. |
| **Widening `evidence_rows_for` breaks an out-of-tree consumer.** | Exactly one production caller (`mcp_server.py:654`, verified by grep) and no `ports.py` Protocol. `retirement._row_fields` still accepts the 3-sequence form, so every existing fake and hand-built row keeps working. |
| **`verify_payload_ref` shared between the gate and the rider couples two policies.** | It owns only the *reading*; the two *policies* stay separate and are asserted separately — the rider keeps the absence rule (a ref-less row counts), the gate keeps the under-claim (a ref-less row does not qualify). Both are pinned by their own tests, so a future edit that collapses them goes red. |
| **[A] Moving the payload reader into `hive/domain/` re-imports `json` into the pure core.** | `change_evidence.py` **already** imports `json` (it renders the payload). The purity gate forbids `sqlite3 | torch | subprocess | os | git | time`, not `json`. `retirement.py` *loses* its `json` import. Net: one fewer domain module knows about JSON. |
| **`ref_tips_prune` drops a watermark on a transient `_rev` failure.** | The failure direction is `unverifiable`, never a wrong verdict, and the next tick re-resolves. `_materialize_drift` only runs when the canonical `_rev` already succeeded, so a total git failure never reaches the prune. |
| **Projecting the drift enum changes a served string and moves the `METADATA_FIELD_LIMIT` budget.** | Verified byte-identical by executing `" | ".join(WIRE_VERDICTS)` against the shipped description. **[A]** Measured headroom recorded: `hive_write` 2015/2048, `SERVER_INSTRUCTIONS` 2003/2048 — A-13 adds only to a *property* description, which the cap test does not read. The frozen `test_served_contract` budget assertions are the backstop and stay untouched. |
| **[A] A-3a changes a served field (`last_verified`) on the recall path.** | It only ever *withholds* a stale stamp measured off the memory's own line — the same direction `branch_route_verdict` already takes for drift, and never an upgrade to "current". Accepted by the operator in ruling A-3a; J7 pins the withholding behaviour end to end so a regression is caught rather than served. |
| **[A] `drift.py` importing from `hive/domain/` couples the boundary to the domain.** | That is the *correct* direction (THEORY §2: app depends on domain, never the reverse). The reverse — `retirement.py` importing `drift.STALE_TIER` — is what the purity/hexagonal rule forbids, which is why the tier must live in the domain. |
| **Clause 1b's N1/N2/N3 justification is not itself enforced, so a future pass deletes the clause.** | Recorded in `BUGS.md` (BUG-069) and in `retirement.py`'s corrected module docstring, which names un-minted anchors and deregistered repos as the cases clause 1a cannot reach — plus **[A]** BUG-071, which makes the un-minted-anchor gap a tracked open bug rather than folklore. |
| **The whole-tree cache key (issue 5) stays open, so clause 1a keeps lagging on busy repos.** | Deliberate (§6.1), and clause 1b covers it meanwhile — a further reason not to delete 1b. Filed as a follow-up with **both** traps named (neighbourhood-not-file; unbounded-vs-bounded depth). |

---

## 10. Definition of done

1. `make check` green — format, lint, `mypy --strict`, full suite — **from the currently-red
   baseline**, with the red baseline recorded so the transition is evidence, not assertion.
2. The J1–J8 contract tests observed **red before** the corresponding production edit and green
   after; the red output kept.
3. **[A] No test in this change manufactures the condition under test by seeding**
   `drift_put` / `ref_tips_put` / `meta_set` / `insert_audit`. Every asserted fact is produced by a
   real production writer: a real git origin, real `hive-edge mint`/`verify` subprocesses, a real
   `SyncService.tick()`, the real MCP handler, or the real `census build` + `ChangeEvidenceService.ingest`
   door. Seeded helpers may only *arrange* an unrelated precondition.
4. **[A] Law 7 discharged on four markers, not two** — break each, watch the named test go red,
   restore: (a) `_gate_own_lines`' multi-repo arm (re-homed from `retirement.py`);
   (b) `render_verify_payload`'s conditional-`ref` marker (A-2 must not disturb it);
   (c) `last_verification`'s absence-rule marker (moved **intact** onto the per-episode ref map —
   A-3a re-homes this marker, it must not be dropped); (d) `_materialize_drift`'s
   `ref_tips_put`-before-verify marker (untouched
   by the new prune call — prove it).
5. **[A] Every deleted symbol verified unreferenced** (`grep` + `graphify query`) with the suite
   green after removal: `retirement._row_line`, `retirement`'s `json` import, `drift.STALE_TIER`,
   `mcp_server`'s `tracked_ref_key` import, `_gate_own_lines`' `or set(declared)` fallback and
   collapse-set logic, both `mcp_server` raw `FROM meta` reads, `tool_defs`' enum literal,
   and `_solo_hint`'s `demand_m` read. (The `canonical_ref` chain is **not** deleted — A-3a
   re-shapes it into the per-episode ref map; only the server-level global label goes.)
6. `/verify` driven against the live dogfood server: a real `hive repo remove` leaves zero
   `ref_tips` / `anchor_drift` / `ref_requests` rows for that name; a branch-scoped recall on the
   removed name is **refused**; after re-registering the same name it reads `unverifiable`, never
   `fresh`; no store reset.
7. Step 7's instruction layer reconciled **in the same change**, `/audit-docs --changed` clean over
   `THEORY.md` (§3 + §5), `INTERACTIONS.md` ([C1]/[C6]/[M2]), `BUGS.md`, `contract.py` and the four
   module docstrings; `graphify update .` run.
8. **Net production line count DOWN**, counted and recorded. Deleted: `_row_line`, the domain's
   `json` import, two raw meta reads, `_gate_own_lines`' fallback + collapse logic,
   `drift.STALE_TIER`, `tool_defs`' enum literal, `_solo_hint`'s floor, three duplicated SQL
   predicates, and `HiveMCPServer.canonical_ref`'s server-level global label (A-3a replaces it
   with the per-episode ref map rather than deleting the chain). Added: `verify_payload_ref`, one
   `ref_tips_prune` call, three `DELETE` statements, one predicate constant, one f-string.
