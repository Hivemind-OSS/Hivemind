# RETIREMENT-LINE-AND-CACHE-MINIMALITY — plan

Status: **awaiting human confirmation — no code until confirmed.**

Closes the two blocking defects from the BRANCH-SCOPE-AND-DEMAND end-to-end verification,
BUG-067, and the doc/enum gaps found alongside them — by **removing** two duplicated owners
rather than adding a third.

---

## 0. Three corrections to the brief (the tree wins)

Verified against the working tree before designing. Each changes what the plan must do.

| # | The brief says | The tree says | Consequence |
|---|---|---|---|
| **C-1** | "`make check` is currently GREEN" | **RED.** `uv run --extra dev pytest -m "not embed"` exits 1 on `tests/store/test_change_evidence_store.py::test_evidence_rows_for_filters_to_the_named_kinds_in_order` — it asserts the 3-tuple `(kind, actor, ts)` while the partially-applied fix widened `evidence_rows_for` to a 4-tuple. Format / lint / `mypy --strict` all pass. | The partial edit is **incomplete, not merely partial**: it left the canonical gate red. Step 0 of any build is closing that. |
| **C-2** | "`hive/app/mcp_server.py` … was mid-edit on the third" | mcp_server is **fully wired**: `_gate_own_lines` (`mcp_server.py:612`) exists, is imported, is typed, and is passed as `own_lines=` at `mcp_server.py:668`. The only missing piece anywhere is the one un-updated store test. | Nothing is half-written. The question is whether the *design* is right, not whether it compiles. |
| **C-3** | Issue 3: "`retirement_evidence`'s docstring still says drift verdicts are read at each repo's canonical tip" | The **docstring was already fixed** (`retirement.py:183-184` reads "at each repo's OWN line's tip"). Three *other* statements are stale and worse: the module docstring's clause-1b claim (`retirement.py:14-15`), the clause-1a inline comment (`retirement.py:204`), and `CONTEXT/INTERACTIONS.md` **[C6]**. | Issue 3 is real but mis-located, and [C6] is a *load-bearing* lie (see §4.3). |

Two further contradictions, in the issue premises rather than the file list, are handled
in-place: **issue 6's premise that `ref_tips` is a migration scar** (it is not — §6.2), and
**issue 1's constraint that the store must not parse the payload** (the meta-envelope law
does not reach `evidence_events.payload` — §5.2).

---

## 1. What is wrong, and the one idea that fixes it

Four of the five real defects below are the **same defect**: a fact that should have one
owner acquired two, and only one of the two got fixed.

| Symptom | The forked owner |
|---|---|
| Blocking defect 1 — a branch-scoped memory is pruned on `main`'s staleness | "is this memory's anchor dead?" is owned by clause 1a **and** clause 1b; only 1a was made line-aware |
| The partial fix's own shape | "what line was this verify row judged on?" is now parsed in `store_sqlite.last_verification` **and** in `domain/retirement._row_line` |
| Two tip resolutions | "which tip do I judge repo R at?" is owned by `drift._tip_for` **and** re-implemented inline in `mcp_server._gate_drift_verdicts` (raw SQL against `store.conn`) |
| Issue 4 — advertised vs emittable drift enum | the wire vocabulary is owned by `drift.WIRE_VERDICTS` **and** re-spelled as a literal in `tool_defs.py` |

**The single design idea: give each of those four facts exactly one owner, and delete the
second copy.** Every blocking behaviour change falls out of that; the plan adds one genuinely
new line of production behaviour (`ref_tips_prune` in the tick) and one genuinely new
production deletion (`repo_remove` sweeping its feed-derived caches). Net production surface
is **smaller** after this change than before it.

---

## 2. Partial-edit assessment — **KEEP the direction, RESHAPE two of the three files**

The partial work is a **sound starting point**. It correctly identified that the ref must
travel and that the domain must decide relevance; the tree is coherent and 95 of its 96
associated tests pass. It should **not** be unwound. It should be reshaped on three points,
each of which makes the change smaller.

### 2.1 What is actually in the three files right now

**`hive/domain/retirement.py`** — a complete, working clause-1b line filter:
- `EvidenceRow` gained a 4th field `payload: str = ""` (the raw JSON body).
- `_row_fields` (`:109`) reads a 3- **or** 4-sequence, attr-shaped object, or `EvidenceRow`.
- `_row_line(payload)` (`:132`) — **new**: `json.loads` the payload, return `body["ref"]` when it
  is a non-empty string, else `None`. The module now imports `json`.
- `retirement_evidence` gained `own_lines: Optional[Iterable[str]] = None`; clause 1b (`:228-251`)
  collapses it to `attributable = lines if len(lines) == 1 else frozenset()`, filters
  `verify_stale` rows by `_row_line(payload) in attributable`, and — correctly, deliberately,
  with a comment — leaves `verify_current` rows **unfiltered**, because a current row only ever
  refuses. Carries a Law-7 marker naming the multi-repo widening as the re-opening mutation.
- Module docstring updated to "at the memory's OWN line" for clause 1a; clause 1b's docstring
  bullet added. **Not** updated: the `:14-15` "anchored or not" claim and the `:204` "canonical
  tip" comment.

**`hive/adapters/store_sqlite.py`** — `evidence_rows_for` (`:1015`) returns
`list[tuple[str, str, int, str]]`, adding the RAW `payload` column, with a docstring saying the
store deliberately does not parse it. (The rest of this file's diff — `episode_refs`, `ref_tips`,
`anchor_carriers`, `drift_prune(keep_anchors=)`, the `trust != 'deprecated'` predicates — is the
9-chunk build, not the partial fix, and is not in question.)

**`hive/app/mcp_server.py`** — complete, not mid-edit: `_gate_own_lines` (`:612`) reads
`episode_refs`, returns `None` when nothing is declared, and otherwise resolves each anchored
repo's line as *declared ref, else `sync:<repo>:tracked_ref` read by raw SQL*; `_retirement_eligibility`
passes it. Imports `tracked_ref_key` (`:55`).

### 2.2 Verdict: keep, with three corrections

| # | Correction | Why (and what it deletes) |
|---|---|---|
| **K-1** | Move the ref parse from the domain into the adapter. `evidence_rows_for` returns `(kind, actor, ts, ref)`; `EvidenceRow.payload: str` becomes `EvidenceRow.ref: str`; `_row_line` and the `json` import are **deleted** from `hive/domain/retirement.py`. | The adapter *already* parses this exact payload for this exact key in `last_verification` (`store_sqlite.py:952-962`) and in `promotion_provenance`. The partial fix created a second parser of the same payload grammar on the other side of a layer boundary — **Information Leakage + Repetition**, and a payload-schema bump would then require a coordinated two-module edit. See §5.2 for why the brief's meta-envelope objection does not apply. |
| **K-2** | `_gate_own_lines` drops its `sync:<repo>:tracked_ref` fallback's *silent* arm: an anchored repo that resolves to **no** known line must make the whole set unattributable (`frozenset()`), not contribute nothing. | Today an unknown `tracked_ref` **shrinks** the line set, which can turn a 2-line (unattributable) memory into a 1-line (attributable) one — the **over-claiming** direction, the one direction a fail-closed gate may never take. Reachable exactly when the fallback's source is missing: before a repo's first tick, and permanently after `repo_remove` (which deletes `sync:<name>:%`). One line. |
| **K-3** | `_gate_drift_verdicts` stops re-implementing tip resolution: it calls the (now public) `hive.app.drift.tip_for(store, repo, declared_ref)` and its raw `SELECT value FROM meta …` is **deleted**, along with the `tracked_ref_key` import if K-2 leaves it unused. | `drift._tip_for` and the gate's inline branch are semantically identical (`ref_tip(repo, ref)` when a ref is declared, else `sync:<repo>:last_tip`). The plan being executed asserted in its own §3.B that this is "one tip resolver, one owner"; it shipped two. |

### 2.3 How to unwind, if the human decides to unwind instead

Recorded because it was asked for, not because it is recommended. **Never `git checkout` or
`git stash`** — the whole 9-chunk build is uncommitted. Unwinding is four *forward* edits,
each reverting to a state readable in `git show HEAD:<path>` (read-only, safe):

1. `hive/adapters/store_sqlite.py::evidence_rows_for` → restore the 3-column `SELECT kind, actor, ts`
   and the `list[tuple[str, str, int]]` return type + its original docstring. This alone turns
   `make check` green.
2. `hive/domain/retirement.py` → delete `_row_line`, the `json` import, `EvidenceRow.payload`,
   the `own_lines` parameter and its docstring bullet, and the `attributable` block in clause 1b
   (restoring the unconditional `stale_ts` fold). Restore `_row_fields` to the 3-tuple form.
3. `hive/app/mcp_server.py` → delete `_gate_own_lines`, the `own_lines=` argument, and the
   `tracked_ref_key` import.
4. `tests/mcp/test_retirement_gate_boundary.py` → the three tests added at `:207-278` do not
   touch clause 1b and stay green either way; leave them.

Unwinding costs the same edit count as correcting, and re-opens blocking defect 1. It is the
worse option.

---

## 3. Design review (required step, folded in)

Ran `/software-design-review` in Mode B against the four decisions below. Scores are reasoned
judgments anchored to that skill's rubric, **not** measurements.

**System context assumed:** hexagonal with a purity-gated `hive/domain/`; `app/` is transport +
boundary and may read the concrete store (the established "sync/census_health raw-read idiom");
the retirement gate is fail-closed; `hive/app/drift.py` self-declares as "the single owner of the
drift wire semantics"; `tool_defs.py` self-declares that its enums are "PROJECTED from the registry
so the advertised … cannot drift from the served/stored vocabulary."

**Options considered (the two that were close):**

- **D2 — where the verify-payload `ref` parse lives.**
  *Option A (as-built): domain parses the raw payload.* Scores: information leakage **6/10**
  ("several modules share knowledge that should be hidden") — the payload's `"ref"` key is now known
  in `domain/retirement.py` and `adapters/store_sqlite.py`; extensibility **5/10**.
  *Option B: adapter parses, projects a typed `ref`.* Information leakage **2/10** ("each design
  decision lives in exactly one module"); cognitive load **2/10** (`EvidenceRow.ref: str` creates a
  precise mental image — *Vague Name* avoided; the domain needs no JSON knowledge at all);
  extensibility **8/10**. Cites **Pull complexity downward** (#10), **Different layers, different
  abstractions** (#9), **Separate what matters from what doesn't** (#16).
  **Winner: B**, because it beat A on the highest-signal dependency flag while *removing* code
  (the domain's `json` import and `_row_line`) rather than adding any.

- **D3 — the `own_lines` contract.**
  *Option A: `Optional[frozenset[str]]` tri-state (None = unfiltered / non-empty = filter /
  empty = nothing attributable), collapse rule in the domain.*
  *Option C: stamp `repo` into `render_verify_payload` and match `(repo, ref)` pairs* — this
  **defines the ambiguity out of existence** (#11) and is genuinely the better end state.
  It loses **on scope, not on merit**: it only helps rows written after the change, it edits a
  content-keyed dedup surface that carries its own Law-7 mutation marker, and the ambiguity it
  removes is already handled by an under-claim (the safe direction). **Named as the follow-up in
  §9**, not smuggled in.
  **Winner: A**, with the type tightened from `Optional[Iterable[str]]` to `Optional[frozenset[str]]`
  so `mypy --strict` carries the tri-state contract instead of prose (agent-native §6: *prefer
  enforced contract over prose*).

**Red flags the review found in the current tree, all closed by this plan:**

1. **Repetition / Information Leakage** @ `domain/retirement.py:132` + `adapters/store_sqlite.py:952`
   — one payload grammar, two parsers, across a layer boundary. root: dependency → change amplification. *(K-1)*
2. **Repetition** @ `mcp_server.py:598-603` vs `drift.py:195-203` — one tip-resolution rule, two
   implementations, one of them raw SQL in the boundary. root: dependency → change amplification. *(K-3)*
3. **Repetition** @ `tool_defs.py:145-146` vs `drift.py:WIRE_VERDICTS` — one vocabulary, two
   spellings, no mechanical tie. root: dependency → change amplification. *(§5.4)*
4. **Stale Instruction Layer (lying contract)** @ `CONTEXT/INTERACTIONS.md` **[C6]**,
   `retirement.py:14-15`, `retirement.py:204`, `tests/domain/test_retirement.py:1-9`. root: obscurity
   → **unknown unknowns**. Highest-priority finding: [C6] tells a reader the daemon can never write
   a `verify_stale` row, which is exactly the fact needed to judge clause 1b. *(§4.3)*
5. **Missing Feedback Signal** @ `tests/domain/test_retirement.py` — `git diff` on that file is
   **empty**: the partial fix shipped three new domain behaviours (line filter, collapse rule,
   unattributable-ref under-claim) with **zero** unit coverage, and the defaulted 4th field is what
   let the old tests stay green. *(§7)*
6. **Test that certifies the unreachable** @ `tests/domain/test_retirement.py:146`
   `test_verify_ledger_clause_reaches_general_memories` — its comment asserts "any censused memory,"
   which is false: `anchored_episodes()` filters `ea.anchor != ''`, so no production writer can ever
   put a verify row on an anchor-less episode. The BUG-059 / BUG-064 pattern, third occurrence. *(§7)*

---

## 4. Blast radius, per issue

Every entry below was found by scanning the tree, not by trusting the issue list.

### 4.1 Issue 1 (BLOCKING) — clause 1b is not line-aware

**Production:** `hive/domain/retirement.py` (`EvidenceRow`, `_row_fields`, `_row_line`,
`retirement_evidence` clause 1b, module docstring, `:204` comment) · `hive/adapters/store_sqlite.py`
(`evidence_rows_for`) · `hive/app/mcp_server.py` (`_gate_own_lines`, `_gate_drift_verdicts`,
`_retirement_eligibility`, the `tracked_ref_key` import) · `hive/app/drift.py` (`_tip_for` → `tip_for`).
**Not touched:** `hive/domain/change_evidence.py` (the writer is correct — it already stamps
`provenance.ref` into the payload), `hive/domain/ports.py` (there is **no** port for
`evidence_rows_for`; the gate reads the concrete store, so no Protocol widens).

**Tests:** `tests/store/test_change_evidence_store.py` (**currently RED**) ·
`tests/domain/test_retirement.py` (zero `own_lines` coverage; `_gate` helper at `:41` does not pass it) ·
`tests/mcp/test_retirement_gate_boundary.py` · `tests/contract/test_retirement_gate_e2e.py` **(FROZEN)** ·
`tests/contract/test_retirement_gate.py` **(FROZEN)** · `tests/app/test_drift.py` (`tip_for` rename).

**Docs:** `CONTEXT/INTERACTIONS.md` [M2] + [C6] · `CONTEXT/BUGS.md` (new entry) · `CONTEXT/THEORY.md`
§3 retirement paragraph ("a newest-verify-stale ledger form" gains "on the memory's own line") ·
`CHANGELOG.md` · `llms-full.txt` if it restates the gate's signals.

**Why the current test surface missed it:** three independent gaps, each verifiable.
(a) `tests/mcp/test_retirement_gate_boundary.py::test_declared_line_fresh_blocks_even_when_canonical_is_stale`
**seeds** `drift_put`/`ref_tips_put` and never runs a sync tick, so no census `verify_stale` row is
ever written and clause 1b never executes. (b) The contract twin
`tests/contract/test_retirement_gate_e2e.py::test_a_branch_scoped_memory_is_judged_at_its_own_line`
drives only the *permissive* direction (dead on its own line ⇒ must retire), which passes with or
without the bug. (c) `tests/domain/test_retirement.py` was never edited, so the pure clause has no
line-aware case at all. The combination means the only test that could have caught it would have had
to drive a **real census ingest** — which is precisely the "component tests are necessary but not
sufficient" gap §7 closes.

### 4.2 Issue 2 (BLOCKING) — `ref_tips` is unbounded, **and leaks across deregistration**

Confirmed as filed: `grep -rn ref_tips_prune hive/` matches only the definition. But the scan found
the leak is **larger and sharper** than "bounded growth."

`hive/adapters/store_sqlite.py:repo_remove` deletes the `repos` row and `meta WHERE key LIKE
'sync:<name>:%'` (BUG-060) — but **not** `ref_tips`, `anchor_drift`, or `ref_requests`. Its docstring
explicitly justifies keeping them ("the rebuildable drift cache … not feed state"). That justification
was true before `ref_tips` existed and is false now:

- `drift.attach_drift` builds `canonical` from the **live registry** (`drift.py:326`); a deregistered
  repo is simply absent from it.
- `_drift_for_hit` therefore computes `routed = bool(branch) and branch != canonical.get(repo, "")`
  → **True for any queried branch** of a deregistered repo → `tip_for(repo, branch)` → the surviving
  `ref_tips` row → `drift_get` at that tip → the surviving `anchor_drift` rows.
- Result: a recall with `repos=["alpha@feature"]` after `hive repo remove alpha` serves **`fresh`**,
  forever, for a repo the server no longer tracks — while the unscoped recall on the same memory
  correctly serves `unverifiable` (its canonical watermark *was* deleted). No tick will ever prune
  those rows, because the daemon iterates the registry.

That is a serve-path correctness defect of exactly the BUG-060 class, introduced by this build, on
the table whose design was chosen to avoid it. It is **not** covered by BUG-067 as filed and must be
logged as its own bug.

**Production:** `hive/app/sync.py:_materialize_drift` (the missing per-tick prune) ·
`hive/adapters/store_sqlite.py:repo_remove` (the missing deregistration sweep + its now-false docstring).
**Tests:** `tests/store/test_repo_registry_store.py` (`test_repo_remove_forgets_the_feed_state` is the
right home) · `tests/store/test_drift_cache_store.py` · `tests/sync/test_contract_drift.py` ·
`tests/contract/test_branch_scope_e2e.py` **(FROZEN)** · `tests/sync/test_contract_mirror.py`.
**Docs:** `CONTEXT/BUGS.md` (BUG-067 → SOLVED; a new bug for the deregistration leak) ·
`CONTEXT/INTERACTIONS.md` [C1] (deregistration's stated effect) · `CHANGELOG.md`.

**Why the test surface missed it:** `test_repo_remove_forgets_the_feed_state`
(`tests/store/test_repo_registry_store.py:169`) asserts the `meta` sweep only — it was written for
BUG-060, before `ref_tips` existed, and nothing extended it when the new table landed. `ref_tips_prune`
has two green unit tests, which is exactly what made its zero call sites invisible: the verb is
*tested*, just never *used*.

### 4.3 Issue 3 (MINOR) — stale docstrings, one of them load-bearing

Corrected location (see C-3). Four statements are false against the code:

| Location | Says | Truth |
|---|---|---|
| `hive/domain/retirement.py:14-15` | clause 1b is "reachable for any memory the census verified, **anchored or not**" | **False.** `ChangeEvidenceService.ingest` joins only `anchored_episodes()`, which filters `ea.anchor != ''`. Clause 1b's population is *identical* to clause 1a's. This claim is why nobody questioned the overlap. |
| `hive/domain/retirement.py:204` | "clause 1a: materialized drift at the **canonical tip**" | False since `_gate_drift_verdicts` resolves the declared line. |
| `tests/domain/test_retirement.py:1-9` | "Clause coverage: drift at the **canonical tip**" | Same. |
| `CONTEXT/INTERACTIONS.md` **[C6]** | verify rider rows "are written **ONLY on a `pre_merge` ingest**"; the daemon's ledger leg "never marks staleness by itself" | **False and load-bearing.** `change_evidence.py:816-820` gates the riders on the **version stamp alone, any phase**, and carries a Law-7 marker: *"restoring the old `phase == "pre_merge"` condition here is the named rider mutation."* The daemon's `post_merge` ingest **is** the rider's normal carrier in v3. |

### 4.4 Issue 4 (MINOR) — advertised drift enum has no mechanical tie

Confirmed and **worse than stated**: `tests/contract/test_verdict_writer_coverage.py:266` diffs the
produced set against `hive.app.drift.WIRE_VERDICTS` — it never reads `tool_defs` at all. So intent I3
("every drift verdict advertised in `tool_defs` is emittable, enforced mechanically") is enforced in
only one direction. Adding a member to the advertised prose today reds nothing.

**Production:** `hive/app/tool_defs.py` (the `hive_recall` description literal at `:144-146`).
**Tests:** `tests/contract/test_verdict_writer_coverage.py` **(FROZEN)** ·
`tests/contract/test_served_contract.py` **(FROZEN)** (the `METADATA_FIELD_LIMIT` budget) ·
`tests/mcp/test_tool_surface.py`.

### 4.5 Issues 5 & 6 — see §6. No production files change.

---

## 5. The design

### 5.1 Minimality verdict on clause 1b — **LOAD-BEARING. Keep it, and make it line-aware.**

Stated first because everything else depends on it. The honest answer is *not* the tempting one:
the two clauses cover the **same population** (both anchored-only — §4.3 proves the docstring's
"anchored or not" is false), but they do **not** cover the same *facts*, because they are computed
by different engines from different inputs.

- **Clause 1a** reads `anchor_drift` — `hive-edge verify` comparing the anchor's current tree state
  against its **stored, server-minted `combdrift/fp`** (`sync.py:_verify_anchor`).
- **Clause 1b** reads `evidence_events` — `change_evidence.classify_verify` reading the census
  receipt's **diff analysis** (`exists_after` / `drift ∈ {breaking, removed}`) over `base..head`.

Three cases where 1b fires and 1a **structurally cannot**. Named concretely, as required:

**N1 — the un-minted anchor (permanent, and the primary case).**
`sync._repo_fps` documents that an empty carrier "contributes empty tokens (**verify then judges
existence alone**)." So for an anchor the server has not yet minted, `hive-edge verify` can produce
`anchor_missing` but **never `anchor_changed`** — it has no baseline signature to compare against.
Mint backfill is capped at `backfill_per_tick` and only runs on `anchors_lacking_fp`, so a
freshly-written memory's anchor is un-minted until at least the next tick. If a signature-breaking
change lands in that window, clause 1a is blind *and stays blind*: the next backfill mints the
**post-break** signature as the baseline, so verify reads `fresh` from then on and the break is lost
permanently. The census receipt over `base..head` sees it in the diff and writes `verify_stale`.
**Only clause 1b can retire that memory.**

**N2 — the receipt-only repo (permanent).**
`hive ingest` / `censusctl` consult **no** registry (`hive/tools/censusctl.py` — the repo identity
comes from the receipt's own `--repo-id`; grep confirms no `repos`-table read on that path). A repo fed
by CI receipts but never `hive repo add`-ed therefore has no `sync:<repo>:last_tip`, no `ref_tips`,
and no `anchor_drift` rows — clause 1a is structurally dead for it, while clause 1b is fully live.
The same holds after `hive repo remove` (BUG-060 deletes `sync:<name>:%`, while `evidence_events` is
deliberately kept as honest history).

**N3 — budget starvation (a window that issue 5 makes long).**
`_materialize_drift` is capped at `drift_per_tick` verify spawns per repo per tick and its cache key
is the whole-tree `tip_sha`, so **every commit invalidates every verdict** and the whole anchor set
must be re-verified. The ledger leg is neither capped nor invalidated: one receipt covers the whole
range and its rows are cumulative. Clause 1b is the **O(changed)** signal; clause 1a is the
**O(all anchors)** signal. On any repo with more anchors than the per-tick cap, or one committing
faster than the poll interval, 1a systematically lags.

**Therefore:** deleting clause 1b would silently *lose* retirement coverage for exactly the memories
most likely to be wrong (freshly written, freshly broken). Keep it; the fix is to make it judge at
the declared line. What we delete instead is the **duplicated ownership** (§2.2, K-1/K-2/K-3), which
is where the actual redundancy lives.

*(Considered and rejected: folding 1a and 1b into a single `staleness_evidence` reader. It would erase
the audit's ability to record **which** signal authorized a retirement — `drift:<verdict>` vs
`verify_stale`, asserted by substring in CT-7 — which is a Law-3 explainability loss for a cosmetic
gain, and the two feeds have genuinely different shapes and fail-safe rules.)*

### 5.2 Blocking defect 1 — the fix

**`hive/adapters/store_sqlite.py`**

```python
def _payload_ref(payload: str) -> str:
    """The LINE a ledger row was measured on — the ``ref`` the census stamps into a
    ``verify_*`` payload (change_evidence.render_verify_payload). "" for anything
    undecidable: an empty, unparseable or non-object body, or a legacy pre-stamp row
    that carries no ref. The SINGLE owner of that read — last_verification's scoping
    arm and the retirement gate's feed both go through here. Total, never raises."""
```

`evidence_rows_for` becomes:

```python
def evidence_rows_for(
    self, episode_id: int, kinds: Sequence[str]
) -> list[tuple[str, str, int, str]]:
    """The retirement gate's ledger feed: the target's ``(kind, actor, ts, ref)``
    rows restricted to ``kinds``, in insertion order. ``ref`` is the measured LINE,
    parsed defensively out of the payload here (the promotion_provenance idiom, and
    last_verification's twin) — the gate decides RELEVANCE, the adapter does the
    reading, and the payload grammar stays in exactly one module. "" = unknown line;
    a kind with no ref (the hurt kinds) always reads "". Read-only, no DDL."""
```

`last_verification`'s inline `payload.get("ref")` is replaced by `_payload_ref(...)`, keeping its
**absence rule** (a ref-less row counts) intact — its filter is `if canonical_ref and row_ref and
row_ref != canonical_ref: continue`, which is a different *policy* over the same *reading*. Policy
diverges by design; the reading does not.

**`hive/domain/retirement.py`**

- `EvidenceRow.payload: str = ""` → `EvidenceRow.ref: str = ""`, docstring updated: *"the LINE the
  row was measured on ("" = unknown, supplied by the adapter — the domain never parses a payload)."*
- `_row_line` and `import json` **deleted**.
- `_row_fields` returns `(kind, actor, ts, ref)`; a 3-sequence still yields `ref=""` (backwards
  compatible with every fake and with the attr-shaped form).
- `own_lines: Optional[Iterable[str]]` → `Optional[frozenset[str]]`, so `mypy --strict` carries the
  tri-state instead of prose. Semantics unchanged and restated in the docstring:
  `None` ⇒ unfiltered (the memory declared no line — the overwhelming majority, byte-identical to
  pre-build behaviour); a **non-empty** set ⇒ only rows on those lines are attributable; the **empty**
  set ⇒ nothing is attributable (under-claim).
- Clause 1b's filter becomes `if attributable is not None and (not ref or ref not in attributable): continue`
  — an unknown ref under an engaged filter under-claims, unchanged from the partial fix's behaviour.
- The `verify_current` arm stays **unfiltered**, with its comment. This is load-bearing and must
  survive review: filtering current rows would let an off-line stale row out-rank an on-line current
  one — the over-claiming direction.
- The existing Law-7 marker on the multi-repo widening is **preserved verbatim**.

**`hive/app/drift.py`** — `_tip_for` → **`tip_for`** (public; docstring gains "the single owner of
'which tip do I judge repo R at', shared by the recall enrichment and the retirement gate"). Internal
call sites updated. No behaviour change.

**`hive/app/mcp_server.py`**

- `_gate_drift_verdicts` — replace the inline branch + raw `SELECT value FROM meta` with
  `tip = tip_for(self.store, repo, declared.get(repo, ""))`. Deletes 6 lines and one raw-SQL read
  from the boundary.
- `_gate_own_lines` — final shape:

```python
def _gate_own_lines(self, ep: Episode) -> Optional[frozenset[str]]:
    """§3.2 clause 1b's line filter: the line(s) whose ledger evidence is THIS
    memory's own. None ⇒ the memory declared no line and every row reads exactly
    as it did before declared lines existed. Otherwise: per repo the memory is
    ANCHORED in, the ref it declared for that repo, else that repo's canonical
    tracked branch — and if ANY anchored repo resolves to no known line, or the
    repos resolve to more than one line, the result is the EMPTY set: a verify
    payload stamps the line but not the REPO, so a memory whose repos are judged
    at different (or unknown) lines can attribute no row at all. Under-claim, never
    over-claim. Raises freely — the caller owns the fail-closed.
    // marker: returning a partial set when a repo's line is UNKNOWN (rather than
    // frozenset()) is the named mutation — it shrinks a 2-line memory to 1 line and
    // lets another repo's staleness retire it."""
```

  with an early `if not ep.anchors: return None` (clause 1b provably cannot fire for an
  anchor-less memory — §4.3 — so the filter has nothing to do and the majority path stays
  byte-identical), and the unknown-line arm returning `frozenset()` (K-2).

**Fail-closed audit of the whole path** (required by the issue's constraints):
`episode_refs` read raises → `_retirement_eligibility`'s `except` → `_INELIGIBLE` → noop.
`tip_for` raises → same. `evidence_rows_for` raises → same. `_payload_ref` never raises (total).
A row whose ref is unparseable under an engaged filter → skipped. A memory with **no** declared ref →
`own_lines is None` → clause 1b unchanged from today, so the "overwhelming majority keeps being judged
at canonical" constraint holds by construction, not by care.

### 5.3 Blocking defect 2 / BUG-067 — the fix

**Two call sites, both one line, closing two different failures.**

1. **Correctness (the deregistration leak, §4.2).** `repo_remove` sweeps the repo's feed-derived
   caches in the **same tx** as the registry row:
   `DELETE FROM ref_tips WHERE repo=?`, `DELETE FROM anchor_drift WHERE repo=?`,
   `DELETE FROM ref_requests WHERE repo=?`. The docstring's claim that these "are not feed state and
   stay" is replaced by the honest invariant: *deregistration forgets the feed **and everything derived
   from it**; only the memories, their scope, and the append-only ledger survive.* Justified by Law 5 —
   all three are rebuildable caches, and a re-registered repo re-materializes them on its first tick,
   which is exactly what a fresh registration is defined to do. Sweeping all three (rather than
   `ref_tips` alone) **defines the error out of existence**: no reader has to reason about whether an
   orphaned row is still reachable.
2. **Hygiene (BUG-067 as filed).** `sync._materialize_drift` calls
   `self._store.ref_tips_prune(name, keep_refs=[ref for _n, ref, _sha, _ts in resolved_refs])`
   under the lock, immediately beside the existing `drift_prune(name, tips, keep_anchors=anchors)`.
   `resolved_refs` is already the exact set the tip list was built from, so no new computation is
   introduced. A ref that fails to resolve this tick loses its watermark → the next read is
   `unverifiable` (fail-safe) → it re-resolves next tick.

**Considered and rejected:** *delete `ref_tips_prune` as dead code and accept the growth.* That is the
more minimal-looking option and it is wrong: it leaves the deregistration defect unaddressed, and it
would make `ref_tips` the only rebuildable cache in the store with no bound, contradicting Law 5 and
the sibling `drift_prune`. **Also rejected:** re-modelling `ref_tips` as `sync:<repo>:ref:<ref>:tip`
meta keys so the BUG-060 sweep covers it for free — a variable-cardinality key family breaks
`sync_keys.FLEET_KEY_BUILDERS`' fixed-field model and the 3-part grammar `census_health` groups on, and
it still needs a per-ref reconcile.

### 5.4 Issue 4 — **close it. It is the module's own stated convention, and it is free.**

Recommendation: **project**, do not test-patch. `tool_defs.py`'s own docstring already says its enums
are "PROJECTED from the registry so the advertised … cannot drift from the served/stored vocabulary,"
and it already does exactly this for `hive.domain.kinds`. Not doing it for the drift vocabulary is an
inconsistency with the file's stated rule, not a deliberate exception.

Verified: `" | ".join(WIRE_VERDICTS)` produces
`'fresh | anchor_changed | anchor_missing | blast_radius_changed | branch_scoped | unverifiable | n/a'`,
which is **present verbatim** in the shipped `hive_recall` description (checked by executing both).
So the projection is byte-identical — `METADATA_FIELD_LIMIT` is untouched and the golden served
contract does not move. No import cycle: `drift.py` imports only `hive.app.anchors` and
`hive.app.sync_keys`; `tool_defs.py` imports neither.

This is **not** over-engineering, and the reason is I3: today `test_verdict_writer_coverage` proves
*WIRE_VERDICTS ⊆ emittable* but nothing proves *advertised = WIRE_VERDICTS*, so I3 holds in one
direction only. The projection closes the other direction by construction (Law 2: unconstructable
beats detected) at the cost of one f-string, and makes the existing frozen test the complete
enforcement of I3.

---

## 6. Issues 5 and 6 — explicit scope calls

### 6.1 Issue 5 — content-addressed drift cache key: **OUT OF SCOPE**

The diagnosis is correct: `anchor_drift`'s PK is `(repo, tip_sha, anchor)`, `tip_sha` is only a cache
key, and every commit therefore invalidates every verdict for the repo, making the work O(all anchors)
per commit under a fixed `drift_per_tick` budget.

Out of scope for three reasons, in order of weight:

1. **The cheap version is unsound in the forbidden direction.** Keying on the anchor's own blob or
   subtree would carry a verdict forward across a commit that did not touch that blob — but
   `blast_radius_changed` is a function of the anchor's **dependency neighbourhood**
   (`matrix/subgraph_fp`), which spans other files. A change in a *caller* can flip the radius tier
   without touching the anchor's blob, so a blob-keyed cache would serve a stale **`fresh`**. That is
   the one direction the drift system may never take (`drift.py`: "never false-fresh"). Making it
   sound requires computing the neighbourhood at the new tip — which is the expensive part you were
   trying to skip.
2. **No correctness defect exists today.** A cold cache reads `unverifiable`, which is the documented
   fail-safe. This is a *latency and throughput* optimisation, and the two blocking items here are
   correctness.
3. **Blast radius.** It would reach `sync.py`, `store_sqlite.py` (a new PK ⇒ a new table ⇒ the very
   migration question issue 6 raises), `drift.py`, `hive/matrix`, and the frozen materializer tests —
   several times this plan's total surface.

**Follow-up to file (not to do now):** *"Make the drift work list O(changed): derive the per-tick
anchor set from the ledger leg's own `base..tip` receipt (which already computes touched subjects and,
under `--propagate`, their blast-radius neighbours) and carry verdicts forward for anchors provably
outside that set."* Note in the follow-up that the correctness obligation is **the neighbourhood, not
the file** — that is the trap. Interaction worth recording: §5.1's case N3 says clause 1b is currently
compensating for this gap, so closing issue 5 would *reduce* — never remove — clause 1b's load.

### 6.2 Issue 6 — the two-table split and a real migration path: **OUT OF SCOPE, and the premise is half wrong**

**The premise, corrected.** `episode_refs` is not a scar at all: an episode can declare one ref per
repo, so the fact is genuinely 1:N and could never have been a column on `episodes` regardless of
migration. And `ref_tips` is not a scar either — even if `anchor_drift` *had* a `ref` column, you would
still need somewhere to record "ref R resolved to SHA S" **before any verdict exists**, because that is
precisely the BUG-063 fail-safe (a budget-starved tick must leave the tip *known* so the reader gets
`unverifiable` instead of inheriting an older tip's `fresh`). A verdict cache cannot store a fact that
exists in the absence of verdicts. `ref_tips` is a **watermark**, a different kind of thing from
`anchor_drift`, and the plan's own §3.A said so. The split is a **choice**.

What *is* a real scar is narrower and is fixed in §5.3: `ref_tips` is the branch twin of a fact
(`sync:<repo>:last_tip`) that lives in `meta`, so it silently fell outside the `sync:<name>:%` sweep
that BUG-060 installed. The lesson to record is "a new per-repo fact must be added to deregistration's
forget-list," not "we need migrations."

**The migration question.** Building an in-place migration path is out of scope because **nothing in
this plan needs it** — the change adds no column to any existing table, and the previous build's
end-to-end verifier already proved on a real pre-build store that `CREATE TABLE IF NOT EXISTS` upgrades
a live v3 volume with no reset. Building it now would be speculative generality against a hypothetical
future change, on a system whose stated posture is "Migration is explicit and refuses old-format tables
at boot — **no silent migration, ever**" (THEORY §5). Changing that posture is a ratified product
decision, not a side effect of closing two defects.

**Follow-up to file:** *"An explicit, operator-invoked `hive migrate` verb: backup-first, refuses to run
implicitly at boot, one ordered and individually-tested step per schema change, exit-code contract like
every other operator tool."* Record that the v3 cutover cost a real dogfood store, so the cost of *not*
having it is known and non-zero — this is a deferral with a price, not a dismissal.

---

## 7. Intents and traceability

Every intent below becomes an end-to-end behavioural contract test that drives **real** production
writers — real git origin, real `hive-edge mint`/`verify` subprocesses, real MCP handler, real
`SyncService.tick()`, real census ingest. **Nothing seeded**: the reason blocking defect 1 survived
the last build is that its only covering test seeded `drift_put`/`ref_tips_put` and never ran a tick.

**Frozen-suite boundary.** `tests/contract/**` is FROZEN in the active build. Tests are split
explicitly below:

| Inside `tests/contract/**` (frozen-suite author only) | Outside (any implementer) |
|---|---|
| `test_retirement_gate_e2e.py` (J1, J2) · `test_branch_scope_e2e.py` (J3) · `test_verdict_writer_coverage.py` (J5) · `test_served_contract.py` (J5 budget) | `tests/domain/test_retirement.py` (J1 unit) · `tests/store/test_change_evidence_store.py` (J1 projection) · `tests/store/test_repo_registry_store.py` (J3) · `tests/store/test_drift_cache_store.py` (J3) · `tests/mcp/test_retirement_gate_boundary.py` (J1, J2) · `tests/sync/test_contract_drift.py` (J4) · `tests/app/test_drift.py` (`tip_for`) |

| # | Intent | Contract (given / when / then) | Scenarios | Tests |
|---|---|---|---|---|
| **J1** | The retirement gate judges a branch-scoped memory's **ledger** evidence at its own declared line, exactly as it already judges its materialized drift. | **Given** `hive_write(repos=["alpha@feature"], anchors=[{alpha, app.py::greet}])` on a real origin, **when** a real sync tick over a real breaking commit on `main` writes a `verify_stale` row stamped `"ref":"main"`, **then** `hive_prune` is a benign **noop** and trust stays non-deprecated; **and when** the same break happens on `feature`, **then** it retires with `signals` containing `verify_stale`. | declared≠row-ref + stale ⇒ noop; declared==row-ref + stale ⇒ retire; **no** declared ref + stale on canonical ⇒ retire (today's behaviour, unchanged); declared ref + newer `verify_current` on **another** line ⇒ noop (the unfiltered-current arm); ref-less legacy row + declared ref ⇒ noop (under-claim); two anchored repos on different lines ⇒ noop; anchored repo with an unknown line ⇒ noop (K-2); `episode_refs` read faults ⇒ noop | `tests/contract/test_retirement_gate_e2e.py::test_a_ledger_stale_row_on_another_line_never_retires_a_declared_line_memory` **(F)**, `::test_a_ledger_stale_row_on_the_memorys_own_line_still_retires_it` **(F)**; `tests/domain/test_retirement.py::test_own_lines_*` (the 8-row truth table); `tests/mcp/test_retirement_gate_boundary.py::test_unknown_anchored_repo_line_makes_nothing_attributable` |
| **J2** | Clause 1b's population is anchored-only, mechanically — no advertised-but-unreachable behaviour survives (the BUG-059/064 seam, third application). | **Given** an anchor-less `hive_write`, **when** a real tick ingests a real receipt over a real change, **then** the episode has **zero** `verify_*` rows and `hive_prune` on it is a noop. | anchor-less memory + real breaking change; scope-only (`repos=['alpha']`, no anchors) memory + same change | `tests/contract/test_retirement_gate_e2e.py::test_an_anchorless_memory_never_acquires_a_verify_row` **(F)**; `tests/domain/test_retirement.py::test_verify_ledger_clause_reaches_general_memories` **corrected** — the pure function's totality is still worth pinning, but its comment must stop asserting a false system fact; renamed `test_verify_clause_is_anchor_agnostic_in_the_pure_function` with the production reachability rule named and pointed at the e2e twin |
| **J3** | Deregistering a repo forgets the feed **and everything derived from it**; nothing from a dead incarnation is ever served or read again. | **Given** a registered repo with materialized branch verdicts, **when** `hive repo remove alpha` runs, **then** `ref_tips`/`anchor_drift`/`ref_requests` hold zero rows for `alpha` **and** a recall with `repos=["alpha@feature"]` serves `unverifiable`, not `fresh`. | branch-scoped recall after deregistration; unscoped recall after deregistration (unchanged); re-register ⇒ re-materializes from scratch; other repos untouched; the memories and their scope survive (BUG-060's kept half) | `tests/contract/test_branch_scope_e2e.py::test_a_deregistered_repo_never_serves_a_verdict_from_its_previous_incarnation` **(F)**; `tests/store/test_repo_registry_store.py::test_repo_remove_forgets_the_feed_state` **extended** |
| **J4** | A ref that stops being canonical, declared, or demanded loses its watermark on the same tick it leaves the work list. | **Given** three real demanded branches with watermarks, **when** two are deleted on the real remote and a tick runs, **then** `ref_tips` holds exactly the surviving ref (+ canonical), and a recall on a deleted branch reads `unverifiable`. | deleted branch; retired-episode's declared ref (`declared_refs` already excludes it); demand aged past the 7-day window; a transient resolve failure ⇒ watermark dropped, re-resolved next tick (fail-safe, not fail-wrong) | `tests/sync/test_contract_drift.py::test_ref_tips_are_reconciled_against_the_resolved_ref_set`; `tests/store/test_drift_cache_store.py` (existing `ref_tips_prune` units, now with a live call site) |
| **J5** | The advertised drift vocabulary and the emittable drift vocabulary are the **same object** — I3 enforced in both directions. | **Given** the `hive_recall` tool description, **when** the suite runs, **then** the advertised enum is `WIRE_VERDICTS` by construction and every member is produced by a real writer driven end to end. | all 7 members; a member added to `WIRE_VERDICTS` with no writer must red; every served field still ≤ `METADATA_FIELD_LIMIT` | `tests/contract/test_verdict_writer_coverage.py::test_every_advertised_drift_verdict_has_a_production_writer` **(F, unchanged — it becomes complete)**; `tests/contract/test_served_contract.py` **(F)**; `tests/mcp/test_tool_surface.py` (the description still names the enum) |
| **J6** | Every statement the instruction layer makes about the retirement gate and the census feed is true of the code. | **Given** `/audit-docs --changed` over the touched set, **when** it runs, **then** no strict contradiction remains in `INTERACTIONS.md`, `THEORY.md`, `BUGS.md`, or the module docstrings named in §4.3. | [C6] rider gate; [M2] gate signals; [C1] deregistration; `retirement.py` module docstring; `retirement.py:204` | `/audit-docs --changed` + review; no new test |

**Not a test change anyone may make:** `tests/store/test_change_evidence_store.py:153` is currently
**red**. It is not "a test to relax" — it asserts the *old* projection shape of a verb whose production
contract deliberately changed. The corrected behaviour it must pin is the new one:
`(kind, actor, ts, ref)` with `ref` parsed from the payload and `""` for a payload that carries none.
Its sibling assertions (kind filtering, insertion order, per-episode isolation, empty-kinds ⇒ `[]`)
are unchanged and must stay.

---

## 8. Implementation order

Each step is safe because the step before it is inert or independent. Steps 1–2 are the only ones
that must precede anything else.

**Step 1 — make the gate red for the right reason, then green.** *(outside frozen paths)*
Correct `tests/store/test_change_evidence_store.py` to the `(kind, actor, ts, ref)` contract **and**
apply K-1 (`_payload_ref` + `evidence_rows_for` projection + `last_verification` sharing it;
`EvidenceRow.ref`; delete `_row_line` and the `json` import). Safe because the projection is a
superset of today's read and the domain's `_row_fields` still accepts every legacy shape.
**Exit:** `make check` green — the canonical gate is restored before any behaviour changes.

**Step 2 — the failing behaviour, pinned first.** *(frozen: J1/J2 modules)*
Author the J1/J2 contract tests driving a real census ingest, observe **red**, and capture the red
output as evidence. No `hive/` file is touched in this step. Required because the previous build's
failure was precisely a green test over a seeded feed.

**Step 3 — blocking defect 1.** *(outside frozen paths)*
K-2 (`_gate_own_lines`: `not ep.anchors ⇒ None`; unknown line ⇒ `frozenset()`), K-3 (`tip_for` made
public in `drift.py`; `_gate_drift_verdicts` calls it; delete the raw meta read and, if now unused,
the `tracked_ref_key` import), and the `own_lines` type tightening. Add the
`tests/domain/test_retirement.py` truth table and the boundary tests.
**Turns green:** J1, J2. Independent of Steps 4–6.

**Step 4 — blocking defect 2 / BUG-067.** *(frozen: J3's e2e; the rest outside)*
`repo_remove`'s same-tx sweep + docstring; `_materialize_drift`'s `ref_tips_prune` call. Safe in
either order relative to Step 3 — different files, no shared symbol.
**Turns green:** J3, J4.

**Step 5 — issue 4.** *(outside frozen paths)*
Project the drift enum in `tool_defs.py` from `WIRE_VERDICTS`. Verified byte-identical, so
`test_served_contract` and the golden description do not move.
**Completes:** J5.

**Step 6 — the instruction layer, in the same change.**
- `CONTEXT/INTERACTIONS.md` — **[C6]** rewritten to the stamp-only/any-phase truth with the marker
  quoted; **[M2]** gains "the ledger form is judged at the memory's own declared line" with the
  `hive/app/mcp_server.py:_gate_own_lines` anchor and its DETECT tag; **[C1]** gains the widened
  deregistration forget-list. No interaction is added or removed, so no new entry is needed —
  three are edited in place.
- `CONTEXT/BUGS.md` — BUG-067 → SOLVED with the two call sites; **BUG-068** logged and solved for the
  deregistration serve-path leak (§4.2), category DATA_WIRING, cross-referencing BUG-060; **BUG-069**
  logged and solved for clause 1b (blocking defect 1), category LOGIC_DEFECT, with the N1/N2/N3
  reachability analysis recorded so a future minimality pass does not delete the clause.
- `CONTEXT/THEORY.md` §3 — the retirement paragraph's "a newest-verify-stale ledger form" gains
  "on the memory's own declared line."
- Module docstrings from §4.3; `CHANGELOG.md`; `llms-full.txt` / `README.md` / `HIVE-ADMIN.md` /
  `OPERATIONS.md` wherever the gate signals or deregistration semantics appear; then
  `/audit-docs --changed` over the touched set; then `graphify update .`.

**Step 7 — gates.** `make check`, then `/verify`: this change has a runtime surface (a live store
whose deregistration now deletes rows, a real branch-scoped prune), so `/update-dogfood-server` and
drive the real flow — confirm a real `hive repo remove` leaves zero `ref_tips`/`anchor_drift` rows and
that a branch-scoped recall on the removed name reads `unverifiable`.

**Build mode.** Steps 1, 3, 4, 5 are small and mostly independent; Step 2 and the J3 e2e are the only
frozen-path work. This is well inside a single agent's context — dispatch one implementer, with the
frozen-suite tests authored by the frozen-suite author.

---

## 9. Risks, named, with mitigations

| Risk | Mitigation |
|---|---|
| **Deleting `anchor_drift` on deregistration loses cached work an operator may not expect to lose.** | It is a rebuildable cache by construction (Law 5) and re-materializes on the re-registered repo's first tick — which is what "first sync baselines the current tip" already promises. The alternative (keeping it) is what makes the false-`fresh` serve reachable. Called out in `CHANGELOG.md` and `INTERACTIONS.md` [C1] so it is a stated behaviour, not a surprise. |
| **Widening `evidence_rows_for` breaks an out-of-tree consumer.** | It has exactly one production caller (`mcp_server:654`, verified by grep) and no `ports.py` Protocol. `retirement._row_fields` still accepts the 3-sequence form, so every existing fake and hand-built row keeps working. |
| **`_payload_ref` shared between the gate and the rider couples two policies.** | It owns only the *reading*; the two *policies* stay separate and are asserted separately — the rider keeps the absence rule (a ref-less row counts), the gate keeps the under-claim (a ref-less row does not qualify). Both are pinned by their own tests, so a future edit that collapses them goes red. |
| **A multi-repo memory with divergent declared lines can never be retired by clause 1b.** | Accepted, recorded, and marker-guarded — under-claiming is the gate's stated safe direction, and clause 1a still judges each repo correctly and per-repo. The real fix is stamping `repo` into `render_verify_payload` (§3, D3 option C), filed as a follow-up rather than smuggled in. |
| **`ref_tips_prune` drops a watermark on a transient `_rev` failure.** | The failure direction is `unverifiable`, never a wrong verdict, and the next tick re-resolves. `_materialize_drift` only runs at all when the canonical `_rev` already succeeded, so a total git failure never reaches the prune. |
| **Projecting the drift enum changes a served string and moves the `METADATA_FIELD_LIMIT` budget.** | Verified byte-identical by executing `" | ".join(WIRE_VERDICTS)` against the shipped description. The frozen `test_served_contract` budget assertions are the backstop and must stay untouched. |
| **Clause 1b's N1/N2/N3 justification is not itself enforced, so a future pass deletes the clause.** | Recorded in `CONTEXT/BUGS.md` (BUG-069) and in `retirement.py`'s corrected module docstring, which will name un-minted anchors and receipt-only repos as the cases clause 1a cannot reach — the "why," which is exactly what a docstring is for. |
| **The whole-tree cache key (issue 5) stays open, so clause 1a keeps lagging on busy repos.** | Deliberate (§6.1), and clause 1b is what covers it in the meantime — which is a further reason not to delete 1b. Filed as a follow-up with the neighbourhood-soundness trap named. |

---

## 10. Definition of done

1. `make check` green — format, lint, `mypy --strict`, full suite — **from the currently-red baseline**,
   with the red baseline recorded so the transition is evidence, not assertion.
2. The J1–J5 contract tests observed **red before** the corresponding production edit and green after;
   the red output kept.
3. Law 7 discharged on the two new markers (`_gate_own_lines`'s unknown-line arm; the preserved
   multi-repo widening marker): break each, watch the named test go red, restore.
4. `/verify` driven against the live dogfood server: a real `hive repo remove` leaves zero
   `ref_tips` / `anchor_drift` / `ref_requests` rows for that name, and a branch-scoped recall on the
   removed name reads `unverifiable`; no store reset.
5. Step 6's instruction layer reconciled **in the same change**, `/audit-docs --changed` clean,
   `graphify update .` run.
6. Net production line count **down**: `_row_line` + the domain's `json` import + the gate's raw meta
   read + `tool_defs`' enum literal deleted; `_payload_ref`, one `ref_tips_prune` call, and three
   `DELETE` statements added.
