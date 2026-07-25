# RETIREMENT-LINE-AND-CACHE-MINIMALITY — adversarial review record

Reviewed: `docs/PLANS/RETIREMENT-LINE-AND-CACHE-MINIMALITY-PLAN.md` against the tree at
`923c2d4` (branch `wip/branch-scope-and-demand`). No production code written. The plan has
been amended in place; this file is the evidence behind every amendment.

Rubric: `CONTEXT/THEORY.md` §9. Ledger: `CONTEXT/BUGS.md`. Interaction contract:
`CONTEXT/INTERACTIONS.md`.

---

## 0. Headline

The plan is **substantially right and materially incomplete**. Its central idea — close the
defects by *deleting the second owner of a forked fact* — is correct and is the right idea.
Four of its five load-bearing claims survive. But:

- it found **four** forked facts; there are **twelve**, plus a chain of dead surface — and two
  of the eight it missed sit inside the two methods it is rewriting;
- **its headline symptom for blocking defect 2 is not reachable** — `normalize_repos`
  refuses an unregistered repo name before `attach_drift` is ever called, so the
  "deregistered repo serves `fresh` forever" scenario cannot occur. The defect is real; the
  trigger is **re-registration**, not deregistration;
- **its J1 contract test is not satisfiable as written** — the daemon's ledger leg censuses
  only the canonical tracked branch, so no sync tick can ever produce a `verify_stale` row
  stamped with a non-canonical ref;
- one of its two clause-1b justifications (the receipt-only repo) **does not hold**;
- and the design decision it scored most confidently (D2) picks *an* owner, not *the* owner.

Amended, the design is minimal and production-safe. Unamended it would have shipped a
correct fix with a fifth fork left in place, an unreachable contract test, and a BUGS.md
entry describing a symptom that cannot happen.

---

## 1. Verdicts on the five load-bearing claims

### V1 — "clause 1b is LOAD-BEARING; keep it" — **UPHELD** (justification corrected)

**Verdict: keep clause 1b.** Deleting it would silently lose retirement coverage. But the
plan's case is one-third wrong and its strongest argument is one it never made.

| Case | Verdict | Evidence |
|---|---|---|
| **N1 — un-minted anchor** | **VERIFIED, and stronger than stated** | `sync.py:880/882` send neither `--fp` nor `--subgraph-fp` for an empty carrier; `edge/cli.py:230` maps `""` → `fingerprint=None`; `combdrift/resolution.py:206` short-circuits with `found=True, reason=REASON_OK` **before** `_compare_fingerprint` (`:243`), the sole producer of `REASON_SIGNATURE_CHANGED`. So `anchor_changed` is *unconstructable* for an un-minted anchor, and `blast_radius_changed` likewise (`edge/cli.py:280` `if subgraph_fp and not off_set`). `anchor_missing` remains reachable (`resolution.py:171-183`). Re-baselining confirmed: `sync.py:639` `reset --hard <tip>` then `:645` mint → the token is minted from the **post-break** tree, and `_backfill` (`sync.py:381`) runs **before** `_materialize_drift` (`:387`) in the same tick. |
| **N2 — receipt-only repo** | **REFUTED as stated** | `anchors.py:110-113` raises `BadAnchors("unknown repo … not in the registry")` on **both** anchored write paths (`mcp_server.py:718` write, `:803` capture) — the only feeders of the two `INSERT INTO episode_anchors` statements. A never-registered repo therefore has **zero** anchored episodes, so `change_evidence.py:586` (`if row_repo != repo: continue`) yields zero matches and **clause 1b is equally dead**. The asymmetry survives only in the **deregistered** variant: `repo_remove` drops `sync:<name>:%` (killing 1a's tip forever) while `episode_anchors` is kept, leaving `hive ingest` of CI receipts fully live on 1b. |
| **N3 — budget asymmetry** | **VERIFIED, and worse than stated** | Cap: `sync.py:738/741-746/752`, default 200 (`config.py:262`). Whole-tree key: `anchor_drift PRIMARY KEY(repo, tip_sha, anchor)` (`store_sqlite.py:112-115`), skip reads `WHERE repo=? AND tip_sha=?` (`:1299`), so a new tip ⇒ `missing = anchors` (all). The ceiling is (#live tips × #anchors), not #anchors — `tips` is canonical + declared + demanded (`sync.py:718-731`). Ledger leg: `sync.py:521-577` — **no cap, no slice, no counter**; one receipt per `base..tip`; rows are cumulative and read with no tip predicate (`store_sqlite.py:1036-1039`). `ingested_ranges` is a duplicate-range guard, not a rate cap. |
| **P — "1b's population is identical to 1a's"** | **PARTIAL** | Anchored-only: **VERIFIED** (`change_evidence.py:805` → `store_sqlite.py:1010` `WHERE e.status='approved' AND ea.anchor != ''`; second belt at `change_evidence.py:588`; every verify row is written strictly inside the `matches` loop at `:827`). No production writer can put a verify row on an anchor-less episode — `classify_verify` (`:478-480`) is the sole writer; every other reference in `hive/` is a read. **But not identical:** 1a's work list excludes `trust='deprecated'` (`store_sqlite.py:1162`) while `anchored_episodes` includes all trust states (`:1002`), and 1a additionally requires a resolvable tip (`mcp_server.py:604-605`). |

**The argument the plan should have made (and now does).** Population identity was never the
question. The decisive fact is that the two clauses **derive their baseline from different
places**:

- clause 1a compares the working tree against the **stored, server-minted `combdrift/fp`**
  carrier — a baseline that lives in the store and can be re-minted from a broken tree;
- clause 1b's engine re-derives its baseline **from the range's own base tree** —
  `hive/combdrift/change.py:88` `old_token = fingerprint_anchor(base_tree, anchor)`.

That is why N1 exists at all, and it is not a budget or a coverage argument — it is a
*correctness* one: 1a's baseline is corruptible by its own backfill; 1b's is not. Merging the
clauses would delete the only signal that survives a poisoned baseline.

**On "one clause, two sources" (the brief's alternative).** Rejected, and not for the reason
the plan gave. The plan's reason (audit explainability) is weak — a merged reader could still
emit `drift:<verdict>` vs `verify_stale`. The real reason is cohesion: the two feeds share
nothing but the word "staleness". 1a is a set of already-line-resolved verdict strings folded
by membership; 1b is a row stream requiring a recency comparison, a line filter, and an
under-claim rule. One function with two disjoint bodies is worse than two functions.

**What the plan missed and now records (new, load-bearing).** The daemon's ledger leg runs
`census build --ref <branch>` where `branch = _tracked_branch(row.canonical_ref)` — the repo's
**canonical** line (`sync.py:360`, `:556`, `:605-608`). Therefore **every `verify_*` row the
daemon writes is stamped with the producing repo's canonical ref.** A non-canonical `ref`
stamp can arrive *only* through the manual `hive ingest` door. Three consequences:

1. Post-fix, clause 1b contributes **nothing** to a memory that declared a non-canonical
   line, on the daemon path. Its N1/N3 value is entirely for canonical-line memories — which
   is the overwhelming majority, so the clause still earns its keep, but the docstring must
   not imply it protects branch-scoped memories.
2. The plan's J1 second scenario ("the same break happens on `feature` ⇒ retires") is
   **not drivable by a real sync tick**. See §4.
3. It shrinks the `own_lines` filter's job to something far simpler than the build assumed —
   see amendment **A-1**.

### V2 — "Issue 5 (content-addressed drift key) is OUT OF SCOPE" — **UPHELD**

The refutation is sound and I confirmed it at the computation.
`hive/edge/cli.py:_subgraph_member_tokens` (`:588-620`) builds the member set as
`{seed} ∪ _forward_ids(...) ∪ radius.callers ∪ radius.dependents` — an explicit **union cone
including reverse reach**. A change in a *caller* moves the token without touching the
anchor's own blob. A blob- or subtree-keyed cache would therefore carry a `fresh` verdict
across a commit that flipped the radius tier — a false `fresh`, the one direction
`hive/app/drift.py` forbids ("never false-fresh"). Making it sound requires computing the
neighbourhood at the new tip, which is the expensive part. **Confirmed; keep out of scope.**

Reason 2 (no correctness defect today — a cold cache reads `unverifiable`) and reason 3
(blast radius) both hold.

**On the proposed follow-up.** Deriving the work list from the ledger leg's own `base..tip`
receipt is sound *in principle* — the daemon already passes `--propagate` (`sync.py:609`), so
the receipt already carries blast-radius neighbours. I add one trap the plan does not name,
which would re-open false-fresh if missed: **`_subgraph_member_tokens` runs at
`depth=_UNBOUNDED_DEPTH`, while `census build --propagate` is depth-bounded.** A work list
derived from a depth-N propagation cannot certify an anchor outside it as unchanged under an
unbounded fingerprint. The follow-up is only sound if the two depths are unified first. That
raises its cost, and confirms the exclusion is the right call — nothing in it is cheap enough
to pull forward.

### V3 — "Issue 6 is OUT OF SCOPE and its premise is half wrong" — **SPLIT: mechanism UPHELD, symptom REFUTED**

**Half A — "`ref_tips` is not a migration scar": UPHELD.** A verdict cache genuinely cannot
hold "ref R resolved to SHA S" before any verdict exists, and that pre-write is the BUG-063
fail-safe. Confirmed at `sync.py:730-736`: `ref_tips_put(resolved_refs)` runs **before** the
verify batch, under a Law-7 marker naming `test_unmaterialized_branch_tip_is_unverifiable_not_fresh`
as the catching test. `episode_refs` is likewise not a scar — one ref per repo per episode is
genuinely 1:N. The split is a choice, correctly made.

**Half B — "the real scar is that `ref_tips` fell outside BUG-060's `sync:<name>:%` sweep":
mechanism UPHELD, stated symptom REFUTED.**

The plan's §4.2 asserts: *"a recall with `repos=["alpha@feature"]` after `hive repo remove
alpha` serves **`fresh`**, forever."* **This is unreachable.**
`hive/app/anchors.py:normalize_repos` (`:153-156`) raises
`BadAnchors("unknown repo … not in the registry")` for any name absent from the live registry,
and `mcp_server._handle_recall` (`:846-849`) turns that into `{"status": "refused"}` **before**
`attach_drift` is ever called (`:908`). `_known_repos` (`:532-536`) reads the live registry on
every call. So after `repo_remove alpha`, a branch-scoped recall on `alpha` is refused, not
served. The unscoped recall degrades correctly to `unverifiable` (the canonical watermark was
swept by BUG-060).

**The defect is real but its trigger is RE-registration** — precisely BUG-060's own flow,
which `skills/hive-connect-repo` §4 explicitly recommends:

```
hive repo remove alpha            # sync:alpha:* swept; ref_tips/anchor_drift SURVIVE
hive repo add <url> --name alpha  # alpha is known again
recall repos=["alpha@feature"]    # normalize_repos now PASSES
  -> _tip_for(alpha,"feature") -> surviving ref_tips row -> a DEAD incarnation's SHA
  -> drift_get(alpha, dead_tip, anchors) -> surviving anchor_drift rows -> "fresh"
```

The canonical path is immune (BUG-060 deleted `sync:alpha:last_tip`, so it reads
`unverifiable` until the first tick). The **branch** path is not, because `ref_tips` is the
branch twin of `last_tip` and lives in a table the sweep does not reach. The failure survives
the first tick whenever the new remote cannot resolve `feature` (no `ref_tips_put`, so the
dead row is never overwritten). That is a genuine BUG-060-class false-`fresh`, introduced by
this build, and it must be logged — with the correct symptom.

**`episode_refs` twin-leak hypothesis: REFUTED.** `episode_refs` is *memory* data, not feed
data — the line the writer declared. `repo_remove`'s docstring (`store_sqlite.py:1262`) and
`INTERACTIONS.md` [C1] both state its retention as deliberate ("episode scope rows are KEPT,
so a re-registered repo picks its memories straight back up"), and deleting it would destroy
user-declared scope, not a cache. It is on the right side of the line the plan draws. There is
no twin leak.

**Migration deferral: UPHELD.** Nothing in the change adds a column to an existing table, and
the previous build's verifier proved `CREATE TABLE IF NOT EXISTS` upgrades a live v3 volume
with no reset. THEORY §5's "no silent migration, ever" is a ratified posture; changing it is
not a side effect of closing two defects.

### V4 — "the meta-envelope law does not reach `evidence_events.payload`" — **UPHELD** (D2's owner is still wrong)

**The law does not reach it.** `CONTEXT/THEORY.md` §5 scopes itself in its own heading —
*"The meta envelope law (episode `meta` tags)"* — and closes with an explicit reach statement:
*"Reach: the episode-meta envelope only — receipt/provenance token carriers are out of
scope."* Structurally it could not apply anyway: clause 2 requires every value to be a
self-describing token `<engine>-<kind>/<N>:<body>`, and clause 7 enumerates the four registered
keys (`combdrift/fp`, `matrix/subgraph_fp`, `git/branches`, `hive-sync/minted`) — all episode
`meta` keys. `evidence_events.payload` is a JSON object rendered by
`domain/change_evidence.py:render_verify_payload`, carries no version token, and has no
registry row. An adapter that `json.loads` it violates no clause of the law.

The store's *own* docstrings are what created the confusion: `evidence_rows_for`
(`store_sqlite.py:1024`) and `anchor_carriers` (`:1152`) both cite "meta-envelope law" as the
reason they return a raw body. For `anchor_carriers` that citation is correct (`fp_meta` **is**
an episode-meta carrier). For `evidence_rows_for` it is a **misapplied law** and must be
corrected in the same change, or the next reader inherits the same wrong constraint.

**But D2's winner is only half right.** Option B (adapter parses, projects a typed `ref`) does
beat Option A on the dependency flag — and then stops one module short. The payload's grammar
is **written** by `hive/domain/change_evidence.py:render_verify_payload` (`:670-691`). Moving
the reader into `hive/adapters/store_sqlite.py` relocates the fork from
*domain-retirement ↔ adapter* to *domain-change_evidence ↔ adapter*; it does not remove it.
One module still writes the `"ref"` key and a different module still reads it, across a layer
boundary, which is the definition of Information Leakage.

**The single owner is the module that renders the payload.** See amendment **A-2**.

### V5 — "four facts, two owners" — **UPHELD at every cited location; the diagnosis is INCOMPLETE**

All four forks confirmed, all four proposed owners correct under the hexagonal rule:

| Fork | Confirmed at | Single owner | Correct? |
|---|---|---|---|
| dead-anchor | `retirement.py:206-213` (1a) + `:228-251` (1b) | keep both clauses, one line-resolution | ✔ (V1) |
| ref parsing | `store_sqlite.py:955-965` + `retirement.py:132-147` | **not** the adapter — `change_evidence.py` (A-2) | partial |
| tip resolution | `drift.py:195-203` + `mcp_server.py:598-606` | `drift.tip_for` | ✔ |
| wire vocabulary | `drift.py:60-68` + `tool_defs.py:144-146` | `drift.WIRE_VERDICTS` | ✔ |

Byte-identity of the enum projection independently confirmed: `" | ".join(WIRE_VERDICTS)`
reproduces the shipped literal exactly, so `METADATA_FIELD_LIMIT` and the golden served
contract do not move. `evidence_rows_for` has exactly **one** production caller
(`mcp_server.py:654`) and no `ports.py` Protocol — the plan's risk row is correct.

**The plan stops at four. There are at least nine.** §3 below.

---

## 2. Design review (`/software-design-review`, Mode B, whole amended design)

Scores are **reasoned judgments anchored to the skill's rubric — not measurements.**

**System context assumed.** Hexagonal with an AST-gated pure `hive/domain/`
(`tests/test_purity.py`); `hive/app/` is transport + boundary and may read the concrete store
(the established "sync/census_health raw-read idiom"); `hive/adapters/` owns all I/O and
already parses `evidence_events.payload` in two places; the retirement gate is fail-closed and
under-claims by contract; `hive/app/drift.py` self-declares as "the single owner of the drift
wire semantics"; `tool_defs.py` self-declares that its enums are "PROJECTED from the registry";
instruction layer = `CONTEXT/THEORY.md` + `CONTEXT/INTERACTIONS.md` + `CONTEXT/BUGS.md`, all
version-controlled and enrolled in `/audit-docs`.

**Verdict.** As amended, the change is a net **deletion** of surface that closes two blocking
defects, and it repairs three lying contracts an agent would otherwise have followed
confidently. As originally written it would have left the highest-signal red flag in the tree
(Scattered Truth in the two methods it rewrites) and shipped a false BUGS.md entry.

**Scores** (amended end state)

- **Design complexity: 4/10** — "mostly sound; a few shallow spots or minor leaks; no change
  amplification." After A-1/A-2/A-3 each of the nine forked facts has one owner, and no
  change here requires a coordinated multi-module edit. It is not 2/10 because
  `mcp_server.py` (1860 lines, 30 methods) remains the boundary god-module and gains no
  decomposition — a conscious, pre-existing tension (THEORY §8.1). Principles: **#4 modules
  should be deep**, **#10 pull complexity downward**.
- **Cognitive load: 3/10** — "small, learnable surface; minor inconsistencies." A-1 replaces a
  tri-state `Optional[frozenset[str]]` whose rules needed five clauses of prose with a rule a
  reader can state in one sentence, and deletes the `tracked_ref` fallback entirely. Residual:
  the gate still has two staleness clauses a reader must hold at once. Principles:
  **#5 design the interface for the common case**, **#16 separate what matters**.
- **Information leakage: 3/10** — "one or two mild, contained leaks." A-2 puts the verify-payload
  grammar's reader beside its writer, so the `"ref"` key lives in exactly one module; A-4
  makes the not-retired work-list predicate one constant. Residual leak: the drift wire
  vocabulary is still *projected* into a served string rather than owned by one object — a
  one-way mechanical tie, not a shared symbol. Principles: **#9 different layers, different
  abstractions**, **#8 separate general and special-purpose code**.
- **Extensibility / system fit: 8/10** — "most likely changes are localized behind stable
  interfaces; respects conventions." The next likely change is D3-option-C (stamp `repo` into
  `render_verify_payload`), and after A-1/A-2 it lands in **one** module and *deletes* the
  ambiguity rather than adding a branch. Not 9 because that change still touches a
  content-keyed dedup surface carrying its own Law-7 marker. Principles:
  **#15 increments should be abstractions, not features**, **#11 define errors out of existence**.
- **Agent-navigability: 8/10** — "mostly self-contained modules, traceable dependencies,
  enforced contracts on the tricky parts, a current single-source instruction layer." The
  amendment converts four prose contracts into enforced ones (a projected enum, a typed `ref`
  field, a shared SQL predicate constant, a mechanically-tied `WIRE_VERDICTS`) and repairs
  three stale ones. Not 9 because `mcp_server.py` is a 1860-line load-everything file and the
  boundary still reaches into `store.conn` by convention rather than through a named verb.
  Criteria: agent-native §6 (**prefer enforced contract over prose**), §4 (**instruction layer
  single-source and current**).

**Red flags found in the current tree** (all closed by the amended plan)

0. **Repetition across the hexagonal boundary** @ `drift.py:86-88` vs `retirement.py:64-66` —
   root: dependency → change amplification. "Which wire verdicts mean *the anchor moved*" is
   the single fact both the retirement gate (does it qualify?) and the branch-routing softener
   (may it be downgraded to `branch_scoped`?) stand on, and it is spelled as two independent
   frozensets on opposite sides of the layer boundary, each with a comment that paraphrases
   the other. **The most consequential fork in the tree**, and neither the plan nor the
   end-to-end verifier found it. A member added to one and not the other silently makes a
   verdict retire-worthy but un-softenable, or vice versa. Closed by A-11.
1. **Stale Instruction Layer (lying contract)** @ `CONTEXT/INTERACTIONS.md` [C6] — root:
   obscurity → **unknown unknowns**. [C6] tells a reader the daemon "never marks staleness by
   itself" and that riders land "ONLY on a `pre_merge` ingest". `change_evidence.py:809-820`
   gates them on the version **stamp alone, any phase**, under a Law-7 marker
   (*"restoring the old `phase == "pre_merge"` condition here is the named rider mutation"*),
   and `sync.py:557` ingests `phase="post_merge"`. **Independently verified — the plan's claim
   is correct, and this is the highest-priority finding**: [C6] is the exact fact a reader
   needs to judge clause 1b, and it is false in both halves.
2. **Stale Instruction Layer** @ `hive/app/contract.py:72` — root: obscurity → unknown unknowns.
   `REMEDIATION_NOTICE` tells **every agent, every session** that the gate verifies "anchor
   drift **at the canonical tip**". False since BUG-064 — the gate resolves the memory's own
   declared line. **The plan missed this; it is the widest-reach stale statement in the tree.**
   It rides the uncapped result channel (THEORY §5), so correcting it costs no budget.
3. **Scattered Truth / Repetition** @ `mcp_server.py:598-606` vs `:625-634` — root: dependency
   → change amplification. "This memory's own line, per repo" is computed **twice, ten lines
   apart, with different fallbacks** (`sync:<repo>:last_tip` vs `sync:<repo>:tracked_ref`).
   The plan's K-3 fixes one and leaves the other. Closed by A-1.
4. **Repetition** @ `store_sqlite.py:1137` vs `:1160` vs `:1455` — root: dependency → change
   amplification. The BUG-065 not-retired predicate `e.status='approved' AND e.trust !=
   'deprecated'` is spelled in **three** SQL strings. `anchor_carriers`' own docstring claims
   *"kept next to it so BUG-065 is fixed in ONE place, not two near-identical joins"* — a
   **lying contract**: this build added the third copy (`declared_refs`), not adjacent, sharing
   no symbol. Closed by A-4.
5. **Information Leakage** @ `retirement.py:132` + `store_sqlite.py:955` — root: dependency →
   change amplification. One payload grammar, two parsers, across a layer boundary — *and a
   third module writes it*. Closed by A-2 (not by K-1 as written).
6. **Test that certifies the unreachable + Missing Feedback Signal** @
   `tests/store/test_last_verification.py:137-192` (five tests) + `hive/domain/ports.py:130-136`
   + `store_sqlite.py:925-965` + `mcp_server.py:366/416/917` — root: obscurity → unknown
   unknowns. `HiveMCPServer.canonical_ref` is a constructor parameter `container.make_server()`
   **never passes** (`container.py:194-198` says so in a comment), so
   `last_verification(canonical_ref=self.canonical_ref or None)` is **always `None`** and the
   entire ref-scoping arm — including its Law-7 marker at `store_sqlite.py:956` — is
   structurally unreachable in production. `THEORY.md` §5 asserts the scoped behaviour as
   shipped (*"the recall rider … derives only from rows measured on the repo's canonical
   line"*). **This is the BUG-059/BUG-064 pattern, fourth occurrence, on the exact method K-1
   wants to share a parser with.** The plan missed it entirely. Closed by A-3.
7. **Repetition** @ `drift.py:187`, `sync.py:946`, `census_health.py:125`, `mcp_server.py:601`,
   `mcp_server.py:632` — root: dependency → change amplification. "Read one meta kv" has five
   implementations; the store exposes `meta_set` with **no `meta_get`**, which is what forces
   every reader to hand-roll raw SQL. Two of the five are new in this build. A-1 + K-3 delete
   both new ones; the pre-existing three are named as a follow-up, not pulled into scope.
8. **Missing Feedback Signal** @ `tests/domain/test_retirement.py` — root: obscurity →
   unknown unknowns. `git diff 1f14e61..HEAD -- tests/domain/test_retirement.py` is **empty**
   (verified). Three new domain behaviours shipped with zero unit coverage; the defaulted 4th
   field is what let the old tests stay green.
9. **Stale Instruction Layer** @ `tests/domain/test_retirement.py:1-9` and `:146-150` — the
   module docstring says "drift at the **canonical tip**", and
   `test_verify_ledger_clause_reaches_general_memories`' comment asserts "any censused memory,"
   which no production writer can produce (**claim P, verified**).
10. **Special-General Mixture (pre-existing, out of scope)** @ `drift.py:335` —
    `if branch == canonical.get(name, "") or (name not in canonical and canonical)`. The
    `name not in canonical` arm is **unreachable on the production path**: `attach_drift`'s
    only caller (`mcp_server.py:908`) passes `scope_pairs` from `normalize_repos`, which
    already refused unknown names. It fires only when `repo_registry()` itself raised. Named,
    not fixed — pre-existing (`941a5a1`), off the defect path.

**Contract health**

| Module / API | Surface | Contract | Note |
|---|---|---|---|
| `retirement.retirement_evidence` | wide (7 kwargs) | enforced (frozen carriers, total, typed) | A-1 replaces the prose tri-state with a one-sentence rule; the `own_lines` **precondition is defined out of existence** rather than documented (principle #11) |
| `store.evidence_rows_for` | narrow | prose → **enforced** after A-2 (`EvidenceRow.ref: str`) | its "meta-envelope law" citation is a misapplied law and is corrected |
| `store.last_verification` | narrow + **one dead kwarg** | prose, **and the prose lies** | A-3: the kwarg has no production caller; decide it, don't document it |
| `drift.tip_for` | narrow (3 args) | enforced (`str \| None`, fail-safe named) | correct owner; making it public is right |
| `tool_defs` drift enum | n/a | **prose-only, no mechanical tie** | closed by projection; I3 becomes bidirectional |
| `store` meta access | asymmetric (`meta_set`, no `meta_get`) | none | the root cause of red flag 7 |

---

## 3. Whole-diff consolidation sweep (`git diff 1f14e61..HEAD`, 43 modified + 9 new)

Findings the plan did not have. Each is verified at both sites in the real files.

**Nine new findings**, each verified at both sites in the real files.

| # | Fact with two owners | Site A | Site B (+C) | Single correct owner | Proof deletion is safe |
|---|---|---|---|---|---|
| **S0** | "which wire verdicts mean *the anchor MOVED*" | `drift.py:86-88` `STALE_TIER = {anchor_missing, anchor_changed, blast_radius_changed}` (**new this build**) | `retirement.py:64-66` `_QUALIFYING_DRIFT = {"anchor_missing", "anchor_changed", "blast_radius_changed"}` | the **domain** — `hive/domain/` may not import `hive.app` (verified: zero such imports), so the shared tier must live in `retirement.py` and `drift.py` must import it | `STALE_TIER` has exactly one consumer (`drift.py:141`); `_QUALIFYING_DRIFT` exactly one (`retirement.py:209`). The two comments are paraphrases of each other and name the same three exclusions (`fresh`/`branch_scoped`/`unverifiable`). Replacing the `drift.py` definition with an import changes no value. **This is the highest-value new find: it is the single fact both the retirement gate and the branch-routing softener stand on, and it is spelled twice across the hexagonal boundary.** |
| **S1** | "this memory's own LINE, per repo" | `mcp_server.py:598-606` (`_gate_drift_verdicts`: declared → `ref_tip`, else `canonical_tip_key` meta) | `mcp_server.py:625-634` (`_gate_own_lines`: declared → ref name, else `tracked_ref_key` meta) | `episode_refs` alone; each consumer applies its own fallback in its own currency (`tip_for` for SHAs; **no fallback at all** for line names — A-1) | A-1 deletes site B's fallback outright; the only behaviour it changes is the multi-repo case, which moves from over-claim to under-claim |
| **S2** | "read one meta kv" | `drift.py:184-192` `_meta_value` | `sync.py:942-948` `_meta_get`; `census_health.py:125`; `mcp_server.py:601`; `mcp_server.py:632` | a `meta_get` verb on the store (the asymmetry with `meta_set` is the cause) | this build's two new copies are deleted by K-3 + A-1; the three pre-existing ones are a named follow-up, not scope |
| **S3** | the not-retired work-list predicate | `store_sqlite.py:1137` (`anchors_lacking_fp`) | `store_sqlite.py:1160` (`anchor_carriers`), `store_sqlite.py:1455` (`declared_refs` — **new in this build**) | one module-level SQL fragment constant in `store_sqlite.py` | all three are the identical string; `anchor_carriers`' docstring already asserts single-ownership, so the constant makes the doc true by construction (A-4) |
| **S4** | the verify-payload `ref` key | `change_evidence.py:686` (**writer**) | `store_sqlite.py:958` (reader), `retirement.py:146` (reader) | `change_evidence.py` — the module that renders the payload (A-2) | the domain already owns the grammar; the adapter may depend on the domain (dependency rule), the reverse is forbidden |
| **S5** | "which line is repo R's canonical line" | `repos.canonical_ref` via `store.repo_registry()` — `drift.py:326` | `sync:<repo>:tracked_ref` meta — `mcp_server.py:632` | `sync_keys.tracked_ref_key` is the **resolved** answer, `repos.canonical_ref` the **declared** one; they differ whenever the operator omitted `--branch` | A-1 removes the gate's need for either, so no reconciliation is required — the fork is deleted rather than adjudicated |
| **S6** | "is the ledger rider scoped to a line?" | `THEORY.md` §5 (asserts scoped) + `ports.py:130-136` + `store_sqlite.py:934-965` + 5 tests | `container.py:194-198` (**never passes it**) → `mcp_server.py:917` always `None` | one answer, decided (A-3) | `grep -rn "canonical_ref=" hive/` → the only call is `mcp_server.py:917`; `HiveMCPServer(` is constructed in exactly one place (`container.py:201`) |
| **S7** | the gate's qualifying-signal description | `contract.py:72` ("anchor drift at the **canonical tip**") | `retirement.py:9-11`, `INTERACTIONS.md` [M2], the code | `retirement.py`'s module docstring; everything else restates it | the served string is uncapped (THEORY §5), so correcting it moves no budget |
| **S8** | *(unused surface)* `HiveMCPServer.canonical_ref` ctor param | `mcp_server.py:366`, `:416` | — | delete or wire (A-3) | zero production callers |
| **S9** | *(unused surface)* `last_verification(canonical_ref=)` + its `ports.py` kwarg + its Law-7 marker | `store_sqlite.py:926`, `ports.py:136` | — | delete or wire (A-3) | reachable only from S8 |
| **S10** | the demand bar `demand_m` | `lifecycle.py:276-277` `n_other = sum(… m.agent_id != candidate_writer); if n_other < self.demand_m` (**redefined this build, BUG-066**) | `mcp_server.py:1297` `if len(misses) < int(self.autonomy.demand_m)` — writer-**inclusive**, scope-unfiltered, cosine-unfiltered | `domain/lifecycle.py:DemandRule` owns "is there enough demand?"; the boundary diagnostic must not re-derive the bar | `_solo_hint`'s own docstring claims to model the gate (*"promotion is silently inert"*). At the new `demand_m=1` that is **false** whenever the single window identity is not the candidate's writer. The floor is redundant with the clause below it (`len({m.agent_id}) > 1 ⇒ None`); replacing it with `if not misses` is **byte-equivalent at the shipped default** and deletes `demand_m` from the boundary. `tests/mcp/test_solo_hint.py` was updated this build to the `demand_m=1` bar and stays green |
| **S11** | the write-side `repos` grammar | `tool_defs.py:71-73` `_REPOS_PROPERTY` — *"Repo scope without a code anchor: registered repo names."* (**no branch form**) | `contract.py:49` `WRITE_VS_CAPTURE` — *"Tag it: repos=['name@branch'] …"* (**new this build**), composed into the SAME served string at `tool_defs.py:89`/`:118` | `_REPOS_PROPERTY` is the `repos` argument's own schema home and must state the branch form; `WRITE_VS_CAPTURE` keeps the directive | The two contradict each other inside one description. **The obvious deletion is REFUTED**: `tests/contract/test_served_contract.py:171`/`:186` (FROZEN) assert `@branch` in `WRITE_VS_CAPTURE` *and* in both tool descriptions, explicitly rejecting "buried only in the schema's field description". So the fix is additive on the uncapped side only: property descriptions are not `t["description"]`, so the cap test (`:88`) does not see them — **zero budget cost** on `hive_write`, which is at **2015/2048** |

Shapes that came back **clean**: no boundary layer re-implements a *domain* function (the raw
SQL in `app/` duplicates *store verbs*, not domain logic); no evidence-kind literal is
duplicated outside the known sites; `split_scope`/`normalize_repos` have exactly one owner each
(`hive/app/anchors.py`) and every consumer routes through them; every added `try/except`
fail-open/fail-closed policy is stated once at its own layer, in deliberately opposite
directions.

**Two findings I examined and did NOT promote to amendments:**

- `store_sqlite.py:310-319` (`stage`) carries a `seen_repo_names` dedup and a documented
  *"first non-empty ref per repo name wins"* tie-break that `anchors.normalize_repos:157-160`
  makes unreachable (it refuses a repo named twice). **Not deleted:** `episode_refs`' PK is
  `(episode_id, repo)` (`store_sqlite.py:104-106`), so removing the dedup converts an
  unreachable case into an `IntegrityError` inside a transaction — strictly worse. Only the
  docstring's framing (a *policy* for a case that cannot arise) is soft. Recorded, not actioned.
- `drift.py:335`'s `or (name not in canonical and canonical)` arm is unreachable on the
  production path (its only caller passes `normalize_repos` output, which already refused
  unknown names); it fires only when `repo_registry()` itself raised. Pre-existing (`941a5a1`),
  off the defect path. Recorded, not actioned.

**Shape of what landed.** The new tables and verbs are the minimal set, with one exception.
`episode_refs`, `ref_tips`, `ref_requests`, `anchor_carriers`, `declared_refs`, `requested_refs`,
`ref_tip`, `ref_tips_put`, `touch_ref_request`, `drift_prune(keep_anchors=)`,
`branch_route_verdict` all have live production consumers. `ref_tips_prune` had **zero** (BUG-067,
closed by this plan). The exception is S8/S9 — a pre-existing server-level `canonical_ref` label
that is the wrong *shape* for a fleet server tracking N repos with N canonical refs, and that
nothing has ever passed.

---

## 4. Where minimality, production-safety and maintainability conflict — and the rulings

**Ruling 1 — `_gate_own_lines`: minimality and production-safety both beat coverage. (A-1)**
The three pull apart here. K-2's one-line fix is *safe* but keeps an 18-line method with a raw
SQL read, a second canonical-line resolver (S1/S5), and a tri-state whose rules need five
clauses of prose. The simplification (A-1) is smaller **and** strictly safer — it never
over-claims where K-2 could — but it loses one case K-2 retains: a memory anchored in ≥2 repos
that declare the *same* line, retired via the ledger clause. Given (a) the gate's stated safe
direction is under-claim, (b) the plan already accepts "multi-repo with divergent lines can
never be retired by 1b" as a named risk, and (c) every daemon-written row is canonical-stamped
so this case needs a manual receipt as well, **I rule for A-1 and record the coverage loss
explicitly.** Clause 1a still judges every repo correctly and per-repo, so no memory becomes
unretirable.

**Ruling 2 — the payload parser's home: maintainability beats layer-tidiness. (A-2)**
Putting the reader in the adapter (K-1) *feels* more hexagonal — "the domain shouldn't parse
JSON". But the writer is already in the domain, so K-1 buys a tidier `retirement.py` at the
price of keeping the grammar split across a layer boundary. Co-locating reader and writer in
`change_evidence.py` gives the grammar exactly one home, keeps the adapter's dependency
pointing inward (legal), and leaves `retirement.py` just as free of JSON as K-1 does. **I rule
for A-2**; it dominates K-1 on every axis and costs the same number of edits.

**Ruling 3 — the dead ledger-rider scope: production-safety beats minimality. (A-3)**
Pure minimality says delete `canonical_ref` everywhere — the parameter, the port kwarg, the
filter, the marker, the five tests, and the THEORY §5 sentence. That is the smaller change and
nothing breaks. But the behaviour it would ratify is a serve-path advisory that labels a
memory "stale" on a line it never declared — blocking defect 1's advisory twin, contradicting
`branch_route_verdict`'s own posture and THEORY §5's stated contract. Since A-1 and A-2 build
both ingredients anyway (the memory's own line; a typed `ref`), wiring it costs little.
**I rule for wiring it (A-3a), with deletion (A-3b) as the named fallback if the human wants
strictly minimum surface** — but *one of the two must ship*: leaving an unreachable parameter
whose behaviour THEORY asserts as live is the worst of the three options and is the exact
defect class BUG-059/064 exist to prevent.

**Ruling 4 — `repo_remove`'s sweep breadth: safety beats strict minimality. (unchanged)**
Only `ref_tips` is *strictly* required (without a tip, no `anchor_drift` row is reachable).
Sweeping all three is two extra `DELETE`s that **define the error out of existence** (#11) —
no future reader has to reason about whether an orphaned row is reachable. I uphold the plan's
choice, with the justification corrected: it is not defence against a post-deregistration
read (impossible, §V3), it is defence against a **re-registration** read.

**Ruling 5 — `mcp_server.py` decomposition: deliberately NOT in scope.**
1860 lines and 30 methods is the largest agent-navigability cost in the change's blast radius.
Splitting it is the "right" answer in isolation and the wrong one here: THEORY §8.1 names the
god-adapter/god-boundary as a *conscious* tension, and a split driven by a two-defect fix is
exactly the tactical-drift pattern (#2) this review is supposed to catch. Named, deferred, not
smuggled in.

---

## 5. Corrections the plan must absorb (the amendment set)

| ID | Correction | Replaces |
|---|---|---|
| **A-1** | `_gate_own_lines` collapses to: anchor-less ⇒ `None`; ≠1 anchored repo ⇒ `frozenset()`; exactly one anchored repo ⇒ `frozenset({declared_ref})` if it declared one, else `None`. Deletes the `tracked_ref` raw meta read, the `tracked_ref_key` import, the `or set(declared)` fallback, and the collapse-set logic. | K-2 |
| **A-2** | The verify-payload `ref` reader is `hive/domain/change_evidence.py:verify_payload_ref` — beside `render_verify_payload`. `store_sqlite` imports it for both `evidence_rows_for` and `last_verification`; `retirement._row_line` and its `json` import are deleted. The `evidence_rows_for` docstring stops citing the meta-envelope law. | K-1 |
| **A-3** | Decide the dead ledger-rider scope. **A-3a (ruled):** wire it to the memory's own line. **A-3b:** delete the parameter, the port kwarg, the filter, its Law-7 marker, the five tests, and the THEORY §5 sentence. Either way BUG-070 is logged. | (absent) |
| **A-4** | One module-level SQL-fragment constant in `store_sqlite.py` for `e.status='approved' AND e.trust != 'deprecated'`, referenced by `anchors_lacking_fp`, `anchor_carriers`, `declared_refs`. | (absent) |
| **A-5** | `contract.py:72`'s "anchor drift at the canonical tip" → the memory's own declared line. | (absent) |
| **A-6** | BUG-068's symptom rewritten to the **re-registration** trigger; the "serves `fresh` forever after `repo remove`" claim deleted as unreachable. | §4.2 |
| **A-7** | J1's second scenario re-specified: a non-canonical `ref` stamp is reachable **only** through the manual `hive ingest` door, because the daemon censuses the canonical branch only. Test drives a real `census build --ref feature` receipt through the real `ChangeEvidenceService.ingest`. | §7 J1 |
| **A-8** | §5.1's case N2 rewritten from "receipt-only repo" to "deregistered repo"; the decisive argument restated as **baseline provenance** (`combdrift/change.py:88` vs the stored `combdrift/fp`), and the daemon's canonical-only census recorded as a stated limit of clause 1b. | §5.1 |
| **A-9** | New BUG-071 (OPEN): an un-minted anchor's signature break is permanently baselined away by the next backfill — clause 1a is blind to it forever, and clause 1b is the compensating control. | (absent) |
| **A-10** | Two follow-ups added: a `store.meta_get` verb collapsing the five raw meta reads; and the depth-unification precondition on the issue-5 work-list follow-up. | §6 |
| **A-11** | `drift.STALE_TIER` is deleted; `retirement._QUALIFYING_DRIFT` becomes the public `QUALIFYING_DRIFT` and `drift.py` imports it, with a comment naming the shared reasoning so a future divergence is a conscious act. | (absent) |
| **A-12** | `_solo_hint`'s `len(misses) < demand_m` floor is deleted (`if not misses:` instead), removing the second owner of the demand bar and the docstring's false gate claim. | (absent) |
| **A-13** | `_REPOS_PROPERTY`'s description states the `'name'` / `'name@branch'` form so the `repos` schema stops contradicting the tagging directive composed into the same string. No frozen assertion moves. | (absent) |

---

## 6. Things I checked that HELD (no finding)

- **C-1 (the tree is RED).** Reproduced: `uv run --extra dev pytest -m "not embed"
  tests/store/test_change_evidence_store.py` fails exactly on
  `test_evidence_rows_for_filters_to_the_named_kinds_in_order`, asserting the 3-tuple against
  a 4-tuple. Format/lint/mypy pass.
- **C-2 (mcp_server is fully wired, not mid-edit).** `_gate_own_lines` at `:612`, imported,
  typed, passed as `own_lines=` at `:668`. Confirmed.
- **C-3 (the docstring was already fixed).** `retirement.py:183-184` reads "at each repo's OWN
  line's tip". Confirmed; the three *other* stale statements are as the plan says, plus two
  more (§3 S7, red flag 2).
- **[C6] is a lying contract.** Verified directly at `change_evidence.py:809-820` — the
  gate is `stamp is not None`, any phase, with the marker quoted verbatim in the plan.
  Load-bearing for the whole clause-1b verdict, and the plan's reading is correct.
- **The frozen-suite boundary.** `tests/contract/**` per
  `docs/PLANS/BRANCH-SCOPE-AND-DEMAND-PLAN.md:257` (`frozen_paths`). The plan's split of which
  tests fall inside is correct.
- **Byte-identity of the enum projection.** Confirmed by string comparison.
- **`evidence_rows_for` has one caller and no port.** Confirmed by grep.
- **The existing e2e twin only drives the permissive direction.**
  `test_a_branch_scoped_memory_is_judged_at_its_own_line` (`test_retirement_gate_e2e.py:172`)
  asserts `drift:anchor_missing` — clause 1a — and passes with or without the clause-1b bug.
  Confirmed by reading it.
- **`ref_tips_put` runs before the verify batch.** `sync.py:730-736`, under its own Law-7
  marker. The BUG-063 fail-safe is real.
- **`_materialize_drift` already holds the exact `resolved_refs` set** the `ref_tips_prune`
  call needs — no new computation. Confirmed at `sync.py:719-736`.

---

## 7. What I refuted

1. **§4.2's symptom** — "a recall with `repos=["alpha@feature"]` after `hive repo remove
   alpha` serves `fresh`, forever." Unreachable: `normalize_repos` refuses the unregistered
   name and `_handle_recall` returns `refused` before `attach_drift` runs. The defect is real
   under **re-registration**.
2. **§5.1 case N2** — "the receipt-only repo." A never-registered repo has no anchored
   episodes at all (`anchors.py:110-113` refuses unknown repos on both write paths), so clause
   1b is equally dead. The case only holds for a **deregistered** repo.
3. **§2.2 K-1's owner** — the adapter is *an* owner, not *the* owner; the payload's writer is
   `domain/change_evidence.py`. K-1 relocates the fork instead of removing it.
4. **§3 D2's scoring premise** — Option B's "information leakage 2/10 (each design decision
   lives in exactly one module)" is not achieved by Option B. It is achieved by A-2.
5. **§1's "four facts, two owners"** — there are at least nine, two of them inside the two
   methods the plan rewrites.
6. **§7 J1's second scenario** — not satisfiable by "a real sync tick"; the daemon censuses
   the canonical branch only.
7. **The `episode_refs` twin-leak hypothesis** (from the review brief, not the plan) — refuted:
   `episode_refs` is memory data whose retention is deliberate and documented in two places.
8. **The "identical population ⇒ weaker case for two clauses" inference** (from the review
   brief) — refuted: the populations are not quite identical (trust filter, tip requirement),
   and more importantly population identity is not the axis that decides it. Baseline
   provenance is.
9. **My own sweep's proposal to delete the duplicated tagging directive from
   `WRITE_VS_CAPTURE`** — refuted by the frozen suite:
   `tests/contract/test_served_contract.py:171` and `:186` assert `@branch` in
   `WRITE_VS_CAPTURE` *and* in both tool descriptions, explicitly rejecting the
   "buried only in the schema's field description" alternative. Deleting it would have meant
   changing a test to pass a refactor — forbidden. The surviving fix (A-13) is additive on the
   uncapped side and moves no assertion.
10. **My own sweep's proposal to delete `stage`'s unreachable first-wins tie-break** —
    refuted: `episode_refs` PK is `(episode_id, repo)`, so the "dead" guard is what keeps an
    unreachable case from becoming an `IntegrityError` mid-transaction. Recorded, not actioned.

---

## 8. Definition-of-done deltas I require

Beyond the plan's own list:

1. **Every intent maps to an end-to-end behavioural contract test driving real production
   writers.** No test in this change may seed `drift_put` / `ref_tips_put` / `meta_set` /
   `insert_audit` to manufacture the condition under test — that is precisely how blocking
   defect 1 survived. Where a real sync tick structurally cannot produce the condition (J1's
   own-line retirement), the test drives the **real** `census build --ref <line>` CLI and the
   **real** `ChangeEvidenceService.ingest` door instead. Seeded helpers stay legal only for
   *arranging* an unrelated precondition, never for the asserted fact.
2. **Law 7 discharged on four markers, not two:** `_gate_own_lines`' multi-repo arm (A-1), the
   preserved multi-repo widening marker in `retirement.py`, `render_verify_payload`'s
   conditional-`ref` marker (A-2 must not disturb it), and — if A-3a ships — the relocated
   `last_verification` absence-rule marker.
3. **Every deleted symbol verified unreferenced** (`grep` + `graphify query`) and the suite
   green after removal: `retirement._row_line`, the `json` import, `tracked_ref_key` in
   `mcp_server.py`, the `or set(declared)` fallback, `tool_defs`' enum literal, the
   `mcp_server.py:601` and `:632` raw meta reads, and (A-3b) the `canonical_ref` chain.
4. **`/audit-docs --changed` must be clean over `THEORY.md` §5 (both the meta-envelope reach
   sentence and the ledger-rider sentence), `INTERACTIONS.md` [C1]/[C6]/[M2], `contract.py`,
   and the four module docstrings.**
5. **Net production line count down**, counted and recorded — the plan's own criterion,
   strengthened: A-1 through A-4 are all net deletions.
