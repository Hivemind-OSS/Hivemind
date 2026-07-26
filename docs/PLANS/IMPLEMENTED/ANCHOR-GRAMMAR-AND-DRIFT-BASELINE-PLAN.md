# ANCHOR-GRAMMAR-AND-DRIFT-BASELINE — implementation plan

Closes three open defects that are one defect class: **two owners of a single fact disagree, and
the disagreement resolves silently to a NON-ACTIONABLE verdict.** BUG-077 is the write-side half
(an anchor spelling the server accepts but the census join can never match); BUG-071 is the
read-side half (a drift verdict that reads `fresh` off a comparison that never happened); BUG-078
is the wire-mapping half (a deleted FILE — the strongest possible evidence of a dead anchor —
falls through a catch-all and lands on the one verdict that never qualifies retirement).

This is the shape BUG-069 named: *"the same fact had acquired two owners."* The plan treats the
class, not the three incidents — so each fix names its single owner, and the last of them makes
the class-level recurrence (a new engine reason silently degrading) unconstructable rather than
remembered.

Status: **plan only.** No production or test code is written until a human confirms it.

> **Revision note (adversarial review pass).** This plan was reviewed against the tree at
> `21484a7` after its first draft. Four of its claims did not survive and are corrected in place;
> each correction is marked **[REVISED]** at the point it applies, with the refuted claim quoted so
> the reasoning stays auditable. BUG-078 was moved from "out of scope" to a first-class peer, and
> the staleness completeness table (§4) is new. Two further defects found during the review are
> logged independently (BUG-079, BUG-080) and are explicitly NOT folded in.

---

## 1 · Scope

### Intents

| # | Intent |
|---|--------|
| **I1** | The write boundary accepts an `anchor` only in a form that can structurally join the census subject feed; every other form is a loud refusal naming the violated clause, with nothing stored. |
| **I2** | The server-side anchor grammar has exactly ONE owner, consumed by the write gate, the census join, and the sync backfill — so acceptance and matching cannot fork again. |
| **I3** | A `fresh` drift verdict at the symbol tier is unconstructable without an actual fingerprint comparison; an anchor whose call shape was never compared reads `unverifiable`. |
| **I4** | The mint-backfill never writes a baseline from a tree in which the server has just watched that anchor change; such an anchor stays un-baselined until a quiet tick. |
| **I5** | A source FILE that is gone from the memory's own line produces the same actionable served verdict as a gone SYMBOL — `anchor_missing` — and qualifies the retirement gate. |
| **I6** | The engine-reason → wire-verdict mapping is EXHAUSTIVE by construction: every reason the engine can classify `stale` carries an explicit wire arrow, an unrecognized reason still fails safe to `unverifiable`, and adding a stale reason to the engine without deciding its wire arrow cannot pass the suite. |

### In scope

- `hive/app/anchors.py` — the boundary grammar gate gains the anchor-form clause.
- A new pure `hive/domain/anchor_grammar.py` — the single server-side owner of the grammar.
- `hive/domain/change_evidence.py` — `_anchor_match_level` consumes the shared splitter;
  `IngestReport` carries the receipt's touched paths (on BOTH return sites).
- `hive/combdrift/{types,resolution,verdict}.py` — the no-fingerprint reason and its tier.
- `hive/app/drift.py` — `wire_verdict`'s stale arm becomes an explicit exhaustive table; the
  `file_missing` arrow lands in it.
- `hive/app/sync.py` — `_ledger_leg` returns what changed; `_backfill` defers a changed anchor.
- `hive/domain/retirement.py` — the module docstring's clause-1b justification is re-stated
  against the narrowed (not closed) window.
- `hive/app/tool_defs.py` — the advertised anchor grammar states the refusal.
- Contract + unit tests, and the doc set in §8.

### Explicitly OUT of scope

- **Repairing anchors already stored in the single-colon form.** Decided in §3.1(b): no
  migration, no repair verb, no rewrite. Justified there.
- **Making `hive/edge/cli.py:_split_anchor` strict.** Evaluated and REJECTED in §3.1(c) —
  it would destroy a correct signal and remove legacy rows from retirement coverage.
- **BUG-079** (an anchor the mint can never resolve is re-swept forever and starves the capped
  backfill queue). Found during this review, logged independently. It shares `_backfill`'s queue
  with this plan's deferral but is not created by it: a deferred anchor mints on the first quiet
  tick, an unmintable one never does.
- **BUG-080** (a served `fresh` riding an UNCOMPARED radius tier). Found during this review while
  building §4, logged independently. It is BUG-071's defect one tier over, but closing it means
  either contradicting the meta-envelope law's clause 6 or splitting the `fresh` wire verdict —
  a vocabulary change with its own contract, schema, and doc obligations. §4 records it as the one
  row of the completeness table still wrong after this plan lands.
- Any change to `QUALIFYING_DRIFT` itself, to the retirement gate's clause set, or to the
  `branch_scoped` routing. (BUG-078 changes which *reason* reaches an existing member of
  `QUALIFYING_DRIFT`; it does not change the set.)
- Deleting retirement clause 1b. §3.2 shows the fix narrows its window without closing it, so
  1b remains load-bearing and its docstring is updated rather than removed.

---

## 2 · Diagnosis (verified against the tree at `21484a7`)

### 2.1 BUG-077 — the anchor-grammar acceptance gap

Three owners disagree about what an anchor *is*:

| Owner | File:line | Grammar it implements |
|---|---|---|
| Acceptance | `hive/app/anchors.py:103-107` (inside `normalize_anchors`, defined at `:64`) | any non-empty `str` |
| Census join | `hive/domain/change_evidence.py:543` (inside `_anchor_match_level`, defined at `:532`) | `anchor.partition("::")` only |
| Drift engine | `hive/edge/cli.py:88` `_split_anchor` | `"::"` first, then the first `":"` |

`normalize_anchors`' own docstring (`hive/app/anchors.py:8`) still calls the anchor "free text
(`path` or `path::Symbol`)" — the module-level statement of the gap.

**The failure, exactly.** For `anchor = "hive/app/sync.py:SyncService"`:

- `_anchor_match_level` partitions on `"::"`, finds none, leaves the WHOLE string in `path`,
  compares it to `TouchedSubject.path` (`"hive/app/sync.py"`), and falls through every tier —
  neither `symbol` nor `file`. No `change_outcome` row is ever written for that memory.
- `_split_anchor` splits it on the single `":"` into `("hive/app/sync.py", "SyncService")`, so
  mint and verify both resolve the real symbol and the drift verdict is computed correctly.

So the memory serves, shows a **correct** drift verdict, and is invisible only to the outcome
leg: no `change_outcome` → no `outcome_verified_helped` → `promote_established`
(`hive/domain/lifecycle.py`) can never fire → it dies `deprecated` at the 45-day provisional TTL
while still being right. This is the *acceptance* half of BUG-058, which fixed only the
*advertised* half.

**Correction to the received framing.** The healthy drift verdict is not a lie — it is a real
verdict computed by a lenient-but-self-consistent tokenizer, and it drives retirement clause 1a
correctly for these rows. `_split_anchor`'s leniency **disguises** the join failure; it does not
manufacture a false one. §3.1(c) turns on this distinction.

**The dead-spelling census, corrected. [REVISED]** The first draft asserted that five spellings
were "shown dead": `a.py:Sym`, `a.py:42`, `a:b/c.py::S`, `a.py::`, `a.py::42`. Traced one by one
against `_anchor_match_level` and `_split_anchor`, only three are:

| Spelling | Census join (`_anchor_match_level`) | Drift engine (`_split_anchor`) | Verdict |
|---|---|---|---|
| `a.py:Sym` | whole string is `path`; matches no subject path → **no tier** | `("a.py", "Sym")` — resolves | **DEAD in the join** — the bug |
| `a.py:42` | whole string is `path` → **no tier** | `("a.py", None)` — file-scoped | **DEAD in the join** |
| `a.py::42` | `sep` and `symbol` truthy → symbol tier, `"42" != subject.symbol` → `continue`, never falls back to file | `("a.py", None)` — file-scoped | **DEAD in the join** |
| `a:b/c.py::S` | `path="a:b/c.py"`, `symbol="S"` → **symbol tier, matches** | `("a:b/c.py", "S")` — resolves | **FULLY FUNCTIONAL on both sides** |
| `a.py::` | `sep` truthy, `symbol` empty → `best = "file"` → **file tier, matches** | `("a.py", None)` — file-scoped | **FUNCTIONAL** (redundant spelling of `a.py`) |

The engine already knows the bare-number shape is not a symbol: `hive/edge/cli.py:55`
`_LINE_NUMBER` exists precisely to demote it to existence-only. A fix that closes only the
single-colon spelling leaves `a.py::42` open.

`a:b/c.py` (a colon-bearing path with NO `::`) is the awkward case: it **joins** at the file tier
(`partition("::")` leaves the whole string in `path`) but the drift engine mis-splits it into
`("a", "b/c.py")`, so mint and verify have never resolved it. It half-works, and it is
lexically indistinguishable from the bug shape. §3.1(a) refuses it and states the cost.

**Corpus check (design question (c), answered).** No existing test, fixture or served-contract
example drives a single-colon anchor through `normalize_anchors`:

- `tests/edge/test_cli.py`, `tests/edge/test_cli_reduction.py`, `tests/edge/test_subgraph_fp.py`,
  `tests/domain/test_meta_registry.py` and the frozen `tests/contract/test_engine_parity.py`
  all invoke the `hive-edge` CLI **directly**; they never touch the boundary gate.
- `tests/clients/test_hive_client.py:97,108,124,132,176` only asserts verbatim payload
  pass-through — no server, no gate. (Its example anchors nonetheless contradict
  `hive/client.py:43`, which already advertises `path/file.py::symbol`.)
- `tests/domain/test_change_evidence.py:1687` asserts
  `_anchor_match_level(["path/file.py:symbol"], subject) is None` — the deliberate anti-fork pin
  from BUG-058. It stays green and stays the pin.
- `tests/app/test_anchors.py:89` `test_anchor_is_free_text` uses
  `"some anchor // with spaces, ünïcode and no path shape"` — no colon, so it still admits; only
  the test's NAME overstates the post-fix rule.
- The served contract (`hive/app/contract.py:96`), the `hive_write`/`hive_capture` schema
  (`hive/app/tool_defs.py:56-67`) and `hive/client.py:43` were all corrected to
  `path/file.py::symbol` by BUG-058, so nothing advertised has to change to make refusal honest.

### 2.2 BUG-071 — the drift baseline minted from the post-break tree

Two facts compose, both confirmed.

**(1) An empty carrier yields `fresh`, not silence.** `hive/app/sync.py:937` `_verify_anchor`
appends `--fp` / `--subgraph-fp` only when the token is non-empty; `hive/edge/cli.py:_verify_core`
builds `combdrift.Anchor(path, symbol, fingerprint=(fp or None))`. In
`hive/combdrift/resolution.py`, the `status == "found"` arm reaches `if anchor.fingerprint is
None:` at **line 206** and returns `found=True, reason=REASON_OK` at lines 207-213 —
*before* `_compare_fingerprint`, which is called at line 214 and defined at line 217 and is the
sole producer of `REASON_SIGNATURE_CHANGED` (line 243). `hive/combdrift/verdict.py:_classify`
then reads `REASON_OK` + `found=True` as `current`, and `hive/app/drift.py:wire_verdict` maps
`current → fresh`.

*(Received-brief line numbers 212/217 refer to the middle of the return block and to
`_compare_fingerprint`'s `def` rather than its call site; corrected above.)*

So an anchor whose call shape was **never compared** is reported as `fresh` — a positive claim
about a comparison that did not happen. `anchor_changed` is unconstructable for it, and
`blast_radius_changed` likewise (an empty `subgraph_fp` omits the radius key entirely).

**(2) The baseline is minted from the tip.** `hive/app/sync.py:376` `_repo_tick` runs three legs
in order — `_ledger_leg` (`:589`, called at `:402`), `_backfill` (`:688`, called at `:410`),
`_materialize_drift` (`:754`, called at `:416`). `_backfill` does `git reset --hard <tip_sha>`
(`:706`) and mints every empty carrier against that tree, stamping
`hive-sync-minted/1:server@<tip> <ref>`. A signature break that landed before that mint is
therefore recorded AS the baseline, and clause 1a of the retirement gate reads `fresh` for that
anchor forever.

**Correction to the received framing — the first fix direction in the bug entry is a no-op.**
`_ledger_leg` runs *before* `_backfill` and, on every successful leg, ends with
`self._store.meta_set(tip_key, tip)` — in the ingest branch at `:629` and in the
no-new-commits early return at `:620`. By the time `_backfill` reads anything, the watermark
**is** the tip. "Backfill against the watermark tree rather than the fetched tip" therefore
changes nothing unless the pre-leg base is captured and threaded, or the legs are reordered.
§3.2 evaluates that repaired form as a real option and rejects it on different grounds.

**The compensating control is real and stays.** `hive/domain/retirement.py`'s module docstring
records why clause 1b is not redundant with 1a: 1b's baseline is re-derived by the census from
the range's own base tree (`hive/combdrift/change.py:fingerprint_anchor`) and no backfill can
corrupt it; 1b is also the only clause reaching a deregistered repo, and it is O(changed) where
1a is O(anchors × live tips). All three reasons survive this plan.

### 2.3 BUG-078 — a deleted FILE falls through the wire mapper's catch-all

**The two owners.** `hive/combdrift/verdict.py:27` owns "which engine reasons are unverifiable"
(`_UNVERIFIABLE_PREFIXES`), and by exclusion which are stale. `hive/app/drift.py:93`
`wire_verdict` owns "which stale reason means what on the wire" — but it names only two
(`signature_changed`, `symbol_missing`) and falls every other stale reason through to
`return DRIFT_UNVERIFIABLE`. `REASON_FILE_MISSING` (`hive/combdrift/types.py:19`) is **not** in
`_UNVERIFIABLE_PREFIXES`, so the engine classifies it `stale` — and the mapper discards it.

`unverifiable ∉ QUALIFYING_DRIFT` (`hive/domain/retirement.py:81`), so:

- a deleted SYMBOL (`pkg/f.py::foo`, file intact) → `symbol_missing` → `anchor_missing` →
  **qualifies retirement**;
- a deleted FILE (`pkg/f.py` gone entirely) → `file_missing` → `unverifiable` → **never
  qualifies**, and the memory keeps serving indefinitely, kept alive by exposure.

The stronger evidence of absence produces the weaker verdict. Both tiers are the same claim
("the thing this memory names is not there"), so the asymmetry has no stated justification —
it is a spec gap implemented exactly as written (`wire_verdict`'s own docstring quotes the §3.4
table, and that table never listed `file_missing`).

**Why it is a CLASS defect, not one arrow.** The catch-all is silent by construction: any reason
the engine classifies `stale` that the mapper does not name degrades to `unverifiable` with no
test, no log, and no way for the two owners to notice they disagree. Enumerated against the
current tree, the engine can classify exactly three reasons `stale` — `file_missing`,
`symbol_missing`, `signature_changed` — and the mapper names two. The next reason added to
`hive/combdrift/types.py` reproduces the bug automatically. §3.3 fixes the mechanism, not the
instance.

**Where `file_missing` actually comes from (the over-claim question, settled on evidence).**
`hive/combdrift/resolution.py:69` emits it when `os.path.isfile(abs_path)` is False, after
`_within_repo` has already proven containment (an escaping path is `path_outside_repo` →
unverifiable, a different arm). On the server path the tree it measures is a worktree produced by
`git worktree add --detach <tip>` (`hive/app/sync.py:_verify_at_tip:901-916`) from a mirror cloned
by `ensure_mirror` with `git clone --quiet [--branch <ref>]` — **no `--depth`, no `--filter`** —
and refreshed by `_fetch` with `+refs/heads/*:refs/remotes/origin/*`. Therefore:

1. **Not a shallow/partial-clone artifact.** The clone is complete and the worktree is a full
   checkout of that exact commit.
2. **Not a fetch-race or partial-checkout artifact.** A non-zero `worktree add` raises
   `_SyncFault`, which abandons the whole `_verify_at_tip` batch and is caught by `_repo_tick`'s
   drift-leg guard — a failed or partial checkout produces NO verdict rather than a spurious
   `file_missing`.
3. **Not a ref/branch mismatch artifact.** Which tip is judged is decided upstream and
   identically for both tiers — `drift.tip_for` at recall, `mcp_server._gate_drift_verdicts`'
   own-declared-line tip at the gate — and an off-line consumer's stale-tier verdict is already
   softened to `branch_scoped` by `branch_route_verdict`. Nothing about `file_missing` is special
   here.
4. **Prose is already carved out before the mapper.** `hive/edge/cli.py:239-245` reclassifies a
   `stale`/`file_missing` on a NON-code-shaped path to `unverifiable`/`not_a_code_anchor` — the
   named marker at that site — so a prose anchor never reaches the wire map as stale.

What remains: a path typo, a sub-directory-relative anchor, an untracked/gitignored file, or a
path that only ever existed on a line the memory never declared. **Each is the exact twin of a
class `symbol_missing` already admits today** (a symbol typo, a symbol that only exists on
another line, a symbol in an untracked file). The fix therefore removes an asymmetry rather than
adding exposure — the system's risk posture is unchanged, and retirement stays agent-INITIATED
and machine-GATED (a qualifying signal is a permission for a conscious `hive_prune` /
`hive_supersede`, never an automatic retirement — THEORY §10 O7).

---

## 3 · Design

Scores below are reasoned judgments against a fixed rubric (interface depth, cognitive load,
information leakage, extensibility/fit, agent-navigability), **not** tool measurements. The
tie-break order used throughout is: fewest moving parts > cannot silently regress > safe under
adversarial input.

### 3.0 The import wall, stated correctly [REVISED]

The first draft asserted, and reasoned from, this constraint:

> *"That knowledge lives in `hive/edge/cli.py:_is_code_shaped_path`, which `hive/domain/**` and
> `hive/app/**` **cannot import** — `tests/test_purity.py::test_absorbed_engines_never_import_the_server_stack`
> walls the engines off from the server stack."*

**That is false for `hive/app/**`, and the plan must not reason from it.** The purity gate is
strictly one-directional: `test_absorbed_engines_never_import_the_server_stack`
(`tests/test_purity.py:153`) scans `hive/{matrix,combdrift,edge}/**` for imports of
`hive.{domain,app,adapters,tools,census,verifier}`. No test forbids the reverse. The live
counter-example is in the very file this plan edits: `hive/app/sync.py:226` imports
`from hive.combdrift.fingerprint import FINGERPRINT_VERSION` — deliberately, with the comment
*"the engine stays the single owner of the token format"* — and `hive/app/sync.py:175` imports
`hive.matrix.gitenv` as "the ONE denylist owner". The idiom is established: **`hive/app/` MAY
read an engine constant precisely to avoid forking a fact, and does so call-time.**

What IS forbidden: `hive/domain/**` may not import `sqlite3|torch|subprocess|os|git|time` or
`hive.census|hive.verifier` (`test_domain_imports_no_io`, `test_domain_never_imports_census_or_verifier`).

Consequences for this plan, both load-bearing:

- Option A in §3.1(a) still loses, but **not** on the import ground — it loses on coverage
  (§3.1(a) grounds 1-2). Its former ground 3 is struck.
- §3.3's exhaustive-mapping design is therefore free to consider importing the engine's reason
  vocabulary into `hive/app/drift.py`; it is evaluated there on its real cost (the runtime import
  graph) rather than a phantom rule.

### 3.1 BUG-077

#### (a) The discriminator

The rule must be checkable at the boundary with no repo access, total over hostile input, and
narrow enough not to refuse anchors that work today.

**Option A — "a single `:` whose left side ends in a source-file extension and whose right side
is identifier-shaped"** (the candidate recorded in the bug entry).
- Misses `path/file.py:42`: the right side is not identifier-shaped, so it admits — and that
  spelling joins nothing either (§2.1). The rule closes one dead spelling and leaves its twin.
- Misses `path/file.py::42` entirely (no single colon at all).
- Needs an "is this a source-file extension" test, whose only owner is
  `hive/edge/cli.py:_is_code_shaped_path`. Importing it is *permitted* (§3.0) but wrong here:
  that predicate answers "is this a code CLAIM at all" for the drift engine's prose carve-out,
  not "is this anchor joinable", and the boundary gate has no repo, no engine, and no business
  loading one to validate a string.
- Scores: leakage 6/10, extensibility 4/10, agent-navigability 4/10 (two similar-but-different
  notions of "code path" in two packages).

**Option B — "no `:` unless a `::` is present, and a `::` symbol is neither empty nor a bare
line number."** Computed by splitting exactly the way the census join splits. **[REVISED — the
first draft's clause 2 was "the path component contains no `:`", which over-refuses; see below.]**
- Refuses `a.py:Sym`, `a.py:42`, `a:b/c.py`, `a.py::`, `a.py::42` — every spelling that is dead
  or unresolvable, by one uniform statement.
- Admits `a.py`, `a.py::Sym`, `a.py::Ns::C.m` (nested `::` inside the symbol, which real C++/Rust
  receipt subjects use), **`a:b/c.py::S`** (a colon-bearing path made unambiguous by an explicit
  `::`), and colon-free prose.
- Needs no extension knowledge, no repo access, no regex over user input beyond a bare-digits
  test — nothing to backtrack on, so adversarial input costs O(len).
- Scores: leakage 2/10, cognitive load 2/10, extensibility 8/10, agent-navigability 8/10.

**Winner: Option B (corrected).** It closes the whole dead class in one clause instead of
enumerating spellings, and it needs no second copy of any fact.

**Why clause 2 narrowed. [REVISED]** The first draft refused *any* `:` in the path component,
which refuses `a:b/c.py::S`. Traced against the code (§2.1's table), that spelling is **fully
functional on both sides**: the census join partitions on `::`, gets `path="a:b/c.py"`,
`symbol="S"`, and matches at the SYMBOL tier; `_split_anchor` partitions on `::` first and hands
the engine the same pair, which resolves. The draft justified refusing it by citing
`_split_anchor("weird:name.py")` — a spelling with NO `::` — which does not generalize. Refusing
a spelling that works end to end is exactly the over-refusal this gate exists to avoid. The
corrected clause keys on **ambiguity, not on colons**: when `::` is present the split is
unambiguous and the path may contain anything.

**What Option B still costs, stated honestly.** `a:b/c.py` (colon-bearing path, no `::`) is
refused. It *does* join at the file tier today, so this withdraws a working file-tier binding —
but the drift engine has never been able to resolve it (`_split_anchor` yields
`("a", "b/c.py")`), and it is lexically indistinguishable from the bug shape, so no boundary rule
can admit one and refuse the other. A memory that genuinely needs to name such a path can bind it
with an explicit `::` symbol, or with `repos=[...]` scope-only. This is the one accepted refusal.

**`a.py::` is refused as canonical form, not as a dead spelling. [REVISED]** The first draft
listed it among "every spelling shown dead in §2.1". It is not dead — it joins at the file tier
and the engine reads it file-scoped, identically to `a.py`. Clause 3 refuses it anyway, and the
plan states why plainly: it is a redundant spelling of `a.py` whose most likely origin is a
truncated symbol, and the refusal message says exactly that (*"drop the separator to bind the
file"*). This is a canonical-form decision, not an evidence-driven one, and it is recorded as such
so a future reviewer does not mistake it for a correctness claim.

**The bare-line-number clause re-homes a fact rather than inventing one.** `_LINE_NUMBER`
(`hive/edge/cli.py:55`) already encodes "a bare number is not a symbol"; the boundary clause
states the same fact as a refusal for new writes while the engine keeps its own copy for reading
legacy rows. Both are pinned by the agreement test (§6, CT-A5) so they cannot drift apart.

**Deliberately NOT constrained: symbol shape beyond the bare-number case.** An identifier regex
would false-refuse exotic-language spellings the census legitimately emits. Only the bare-number
case is evidence-backed as dead, so only it is refused.

#### (b) Anchors already stored in the dead grammar — decision: **do nothing**

Three options were weighed. The deciding evidence is §2.1's correction: for a stored
single-colon anchor the drift verdict is **correct**, not false.

| Option | Verdict |
|---|---|
| **Migrate / repair** (`a.py:Sym` → `a.py::Sym` in `episode_anchors`) | **REJECTED.** Off-theory: an "update" of a memory is a new immutable row, never an in-place edit of meaning; supersession only retires the old one. Rewriting a stored binding is exactly the in-place update the store refuses. It also has no home — `hive/adapters/store_sqlite.py:215-250` states plainly that **no in-place migration path exists** and refuses old-format tables at boot, so a repair would mean introducing a data-migration facility the system has deliberately never had. And the transform is ambiguous for `a.py:42`. |
| **Surface** (a new `hive_health` worklist counting ungrammatical bindings) | **REJECTED.** New interaction, new surface, new tests, for a population that is already serving correctly and drifting correctly. The tie-break's first term (fewest moving parts) decides it. |
| **Do nothing** | **CHOSEN.** |

Justification: a legacy row keeps serving, keeps a correct drift verdict, and keeps qualifying
retirement clause 1a. Its only loss is the top trust rung. The repair path already exists and is
the sanctioned one — an agent that recalls such a memory issues
`hive_write(replaces=<id>, anchors=[{repo, "path/file.py::Symbol"}])`, which the retirement gate
adjudicates like any other supersession. The gate stops the population growing; the fleet drains
it through the normal lifecycle. Nothing new is built to manage a set that is already shrinking.

#### (c) Should `hive/edge/cli.py:_split_anchor` become strict? — **NO**

This was the plan's largest candidate and it is rejected on evidence.

Making it strict would send a legacy anchor down the existing prose gate (`_is_code_shaped_path`
rejects `"a.py:Sym"` — `os.path.splitext` yields `".py:Sym"`, which `_CODE_EXTENSION` will not
fullmatch), producing `unverifiable`/`not_a_code_anchor`. Three costs, any one of them
disqualifying:

1. **It destroys a correct signal.** The verdict those rows carry today is genuinely computed.
   Replacing `fresh`/`anchor_changed` with `unverifiable` trades information for nothing.
2. **It removes legacy rows from retirement coverage.** `unverifiable ∉ QUALIFYING_DRIFT`
   (`hive/domain/retirement.py:81`), so every legacy anchor would stop qualifying clause 1a — a
   real regression in the machine-gated retirement path, in a plan whose whole purpose is to
   stop over-claiming.
3. **It reaches the frozen contract suite.** `tests/contract/test_engine_parity.py` replays
   scenarios spelled with single colons — verified: six of its nine scenarios use
   `src/core.py:compute`, `alpha/a.py:run`, `mod.py:transform`, `src/core.py:does_not_exist` —
   against goldens minted from the pre-move 0.9.0 engine; strictness changes those outputs, and
   the goldens cannot be re-minted without destroying the parity claim they exist to make.
   (~35 further call sites across `tests/edge/**` would also churn.)

Instead, the leniency is **named** rather than removed. It is an instance of a law the system
already runs on — the meta-envelope law's clause 4/6 posture, "mint is always current-version-only;
compatibility lives in READERS; old formats age out by refresh, never by rewrite"
(`CONTEXT/THEORY.md` §5). The boundary gate is the mint side (canonical spelling only, from now
on); `_split_anchor` is the reader side (keeps historical spellings resolvable). The change adds
a comment saying exactly that, so the next minimality pass does not delete it as a fork.

#### (d) Where the one owner lives

**Chosen: a new pure module `hive/domain/anchor_grammar.py`.**

- Consumers are `hive/app/anchors.py` (validate), `hive/domain/change_evidence.py` (split), and
  `hive/app/sync.py` (split, for §3.2). All three are on the server side of the engine wall, so
  one owner is reachable for all of them.
- **Rejected: fold into `hive/domain/change_evidence.py`.** That module is the receipt/evidence
  engine; making the *write boundary* import it to validate an anchor string couples two
  unrelated boundaries and drags a large module into the write path. The anchor grammar is
  general-purpose vocabulary; the join's tier policy is special-purpose — they separate.
- **Rejected: put the invariant in `AnchorRef.__post_init__`.** This looks like the strongest
  Law-2 "make the illegal state unconstructable" move and it is **wrong here**:
  `hive/adapters/store_sqlite.py:512` constructs `AnchorRef` from **stored rows** on the read
  path, so a raise there would blow up every recall touching a legacy anchor — and because the
  answer path fails closed, it would do so *silently*, as an empty result. The correct idiom is
  the one already in force: validate at the WRITE boundary, let readers assume validity.
- The module matches an established local pattern — a small `hive/domain/` module owning one
  vocabulary from which every consumer projects (`hive/domain/kinds.py`,
  `hive/domain/meta_registry.py`).

Surface (deliberately two functions, no class, no config):

```python
# hive/domain/anchor_grammar.py — pure, stdlib only.

def split_anchor(anchor: str) -> tuple[str, str]:
    """(path, symbol) — partition on the FIRST '::'. symbol == '' means file-scoped
    (both 'a.py' and the degenerate 'a.py::' read as file-scoped, matching the join's
    behaviour today). TOTAL over any str; never raises."""

def anchor_grammar_error(anchor: str) -> str | None:
    """None when `anchor` is structurally joinable; else the violated clause, phrased
    for the refusal envelope. TOTAL over any str; never raises."""
```

### 3.2 BUG-071

#### (a) The empty-carrier arm must stop reporting `REASON_OK`

`resolution.py` proves the symbol EXISTS and then reports `ok` — a reason that, through
`verdict._classify` and `wire_verdict`, becomes the positive claim `fresh`. Under Law 6's
direction (under-claim, never over-claim) the honest report is: existence proven, call shape
**not compared**.

The fix is three lines in three files: a new `REASON_NO_FINGERPRINT = "no_fingerprint"` in
`hive/combdrift/types.py`; the `anchor.fingerprint is None` branch at `resolution.py:206` returns
it (keeping `found=True` and a populated `location` — the same idiom `_compare_fingerprint`'s
docstring already states, "`found` stays True and `location` stays populated in every branch");
and the reason joins `_UNVERIFIABLE_PREFIXES` in `hive/combdrift/verdict.py`.

Effect: `unverifiable`, which `wire_verdict` already maps to `DRIFT_UNVERIFIABLE`. Nothing new
is added to the wire vocabulary.

This is the Law-1 move the bug needs: **`fresh` at the symbol tier becomes unconstructable
without a real comparison**, rather than something a future code path must remember not to
claim. It covers every un-baselined case at once — the pre-mint window, an unresolvable mint, a
capped-out backlog, and the deferrals introduced by (b).

**Blast-radius check the implementer must preserve (added by review).** `resolve_anchor` has a
SECOND consumer besides the memory path: `hive/combdrift/change.py:verify_change`, the census
change path that feeds `change_outcome` / `verify_current` / `verify_stale` — i.e. retirement
clause 1b, this plan's own compensating control. It calls `resolve_anchor` twice with a
fingerprint-less `Anchor` (`change.py:87`) and once with `fingerprint=old_token` (`:92-94`).
Traced: `base_res.reason` is never read (only `base_res.found`), and `head_res.reason` is read in
exactly ONE branch of `_classify_symbol` (`:132-135`), which is guarded by
`old_token is not None` — precisely the branch where the fingerprint is NOT None. **The
no-fingerprint reason is therefore unreachable in every `change.py` path that reads a reason**,
and clause 1b is unaffected. This is not luck to be relied on silently: §7 M14 mutates it and §6
pins it.

Untouched by design: a FILE-scoped anchor (`symbol is None`) keeps `REASON_NO_SYMBOL_REQUESTED`
→ `fresh`, because existence *is* its whole claim; and a missing file/symbol still short-circuits
to the stale tier before this branch is reached, so `anchor_missing` is unaffected.

**The frozen parity suite is safe — verified, not assumed.** `tests/contract/test_engine_parity.py`
has five `verify` scenarios. Two carry no `--fp` (`verify_symbol_missing`,
`verify_prose_not_a_code_anchor`), and both short-circuit at the missing-file/missing-symbol arms
**before** `resolution.py:206`. No scenario exercises "resolvable symbol, no fp", so no golden
moves and the suite stays green unmodified (DoD #7).

#### (b) Never baseline from a tree in which the anchor just changed

| Option | Score / verdict |
|---|---|
| **B1 — mint at the tick's censused base sha** (the bug entry's first direction, repaired to capture the pre-leg base) | **REJECTED — unsafe.** A memory written *after* the change but *before* the tick would be baselined at the pre-change shape and immediately read `anchor_changed`, which is in `QUALIFYING_DRIFT` and therefore **qualifies a retirement**. It trades a false `fresh` for a false stale, and false-stale is the direction every other owner in this subsystem bends over backwards to avoid (`_verify_core`'s prose reclassification, `wire_verdict`'s fall-through, the BUG-037 version gate). `tests/sync/test_contract_backfill.py::test_matches_edge_mint_at_the_moved_tip` pins exactly this scenario today. |
| **B2 — refuse to mint until the ledger has censused that anchor's range** (the entry's second direction) | **REJECTED — unbounded coverage loss.** An anchor in a file no receipt ever touches would never acquire a carrier, so clause 1a would be permanently unreachable for it. It also needs new durable per-anchor "has been censused" state. |
| **B3 — mint at the commit that was tip when the episode was written** | **REJECTED — adversarially unsafe.** It keys a retirement-qualifying signal on git commit dates, which are writable and rewritable by whoever pushes; a backdated commit becomes a way to manufacture `anchor_changed` on a healthy memory. It also re-mints history that `_ledger_leg` deliberately refuses to mint at first connect. |
| **B4 — mint synchronously at `hive_write`** | **REJECTED.** Puts an engine subprocess on the write path, under the one global lock the whole daemon is careful never to hold across a mint. |
| **B5 — double-mint at base and tip, accept only on agreement** | Correct, but doubles engine spawns (up to 400/repo/tick at the shipped `backfill_per_tick=200`) to learn something the receipt already knows. |
| **B6 — defer any anchor the tick's own receipt reported as changed** | **CHOSEN.** |

**B6, concretely.** `ChangeEvidenceService.ingest` already computes
`subjects, skipped_lines = touched_subjects(lines)` (`hive/domain/change_evidence.py:792`).
`IngestReport` gains one defaulted field carrying those subjects' paths; `_ledger_leg` returns
them; `_repo_tick` threads them into `_backfill`, which skips any anchor whose
`split_anchor(anchor)[0]` is in the set. The carrier stays empty, so under (a) the anchor reads
`unverifiable` — the honest unknown — and the SAME range's receipt has already written the
`change_outcome` / `verify_*` rows that feed retirement clause 1b.

**The range-skip return site must carry the paths too. [REVISED]** The first draft specified
`touched_paths` as "empty on a range-skipped ingest". That reopens the exact window B6 exists to
close: a range already ingested through the manual `hive ingest` door makes `ingest` return early
with `range_skipped=True`, the daemon would learn nothing changed, and `_backfill` would mint at
the post-break tip for that tick. `subjects` is already computed at `:792`, **before** the
range-ledger check, so populating `touched_paths` on BOTH return sites costs nothing and closes
the hole. The range-skip report's other fields stay zero (nothing was done); `touched_paths`
is not a count of work done, it is a statement of what the range contained.

Why B6 beats B5: identical guarantee for the class that matters, at zero extra subprocesses.
The evidence-line classes `touched_subjects` reads are exactly the ones that produce
signature/existence drift, so the set is not a proxy for "what changed" — it *is* the set of
changes that could invalidate a baseline.

**Fault direction.** A faulted `_ledger_leg` means the server does not know what changed, so the
backfill leg is skipped for that repo that tick (carriers stay empty → `unverifiable` → retried
next tick). This makes the two legs deliberately dependent; the dependency is the correctness
relation, not incidental coupling, and it is expressed as an explicit parameter rather than
shared state so the edge is traceable. **Named residual:** a repo whose ledger leg faults
*persistently* (a census build that always fails on some commit) never backfills at all, so
clause 1a stays unreachable for it. That is the under-claiming direction and it is not silent —
the fault is surfaced on `sync:<name>:last_error` and in `hive_health(include_census_health)`
every tick. No retry-count escape hatch is added (§9 #14: no measured right N).

**Residuals, named rather than papered over.** These are why clause 1b stays load-bearing and
why its docstring is edited rather than deleted:

1. **First connect.** `_ledger_leg` baselines at the remote tip and mints no historical receipt,
   so the touched set is empty and the mint takes the tip. The server has never observed the
   anchor's earlier state and no honest reconstruction exists (B3 is the only candidate and it
   is adversarially unsafe). Unchanged by this plan.
2. **Radius tier.** `blast_radius_changed` moves when the anchor's *neighbourhood* changes, which
   need not appear in the receipt's touched subjects; and an anchor carrying `combdrift/fp` but no
   `matrix/subgraph_fp` has no radius baseline at all. Both read `fresh` on that tier. The second
   is a genuine over-claim of the same species as this bug and is logged as **BUG-080** — see §4
   and the scope exclusion. Not closed here: the naive fix contradicts the meta-envelope law's
   clause 6 (absence is byte-inert) and would strip the legacy population out of retirement
   coverage.
3. **Churn.** An anchor whose file changes every tick defers indefinitely and stays
   `unverifiable`. Honest, and covered by 1b. No "force mint after N deferrals" escape hatch is
   added: there is no measured right N, and a knob with no right answer is refused by design.

### 3.3 BUG-078

#### (a) The destination for `file_missing`: `anchor_missing`

§2.3 settles the over-claim question on evidence. `file_missing` on a code-shaped path, measured
in a full worktree at the judged tip, is a genuine measurement, and every residual spurious class
is the exact twin of one `symbol_missing` already admits. The file being gone is the maximal form
of "the anchor is missing", so the arrow is `file_missing → anchor_missing`.

Two consequences, both intended:
- `anchor_missing ∈ QUALIFYING_DRIFT`, so clause 1a now reaches a deleted file (I5).
- `anchor_missing` is stale-tier, so `branch_route_verdict` now softens it to `branch_scoped` for
  an off-line consumer instead of leaving `unverifiable`. Strictly more informative, still
  advisory, never an upgrade to `fresh`.

Coverage note: this also fixes the FILE-SCOPED case (`pkg/f.py`, no symbol). `resolve_anchor`
checks `os.path.isfile` **before** any symbol logic, so a file-scoped anchor to a deleted file
already emits `stale`/`file_missing` and now reaches `anchor_missing` — no fingerprint required.

#### (b) Where the fix goes — the mechanism, not the instance

| Option | Verdict |
|---|---|
| **D-A — one more `if code.startswith("file_missing")` arm in `wire_verdict`** | **REJECTED.** It closes this instance and leaves the mechanism: the next reason added to `hive/combdrift/types.py` that the engine classifies stale degrades silently, with no test able to notice. That is the defect class this plan exists to treat. |
| **D-B — an EXPLICIT exhaustive stale-reason table in `hive/app/drift.py` + a static coverage ratchet test** | **CHOSEN.** |
| **D-C — import the engine's reason constants into `hive/app/drift.py` so there is literally one owner** | **REJECTED on cost, not on the (phantom) import wall — see §3.0.** `hive/combdrift/__init__.py` eagerly imports `change`, `evidence`, `fingerprint`, `resolution`, `types`, `verdict`, `version` — and `resolution` imports `os` — so a module-level `from hive.combdrift.types import ...` in `drift.py` puts the whole engine on the recall read path's import graph. `hive/app/sync.py` deliberately keeps its one engine import CALL-TIME, with an engine-absent fallback (`:226-229`). A per-call import inside `wire_verdict` (which runs per anchor per tip per tick) is worse. The ratchet in D-B achieves the same anti-fork guarantee with zero runtime coupling. |
| **D-D — gate `file_missing` on a recorded fingerprint** ("the file demonstrably existed when we minted, so its absence is real") | **REJECTED — it replaces one asymmetry with another.** `symbol_missing` qualifies today with or without a fingerprint; gating only the FILE tier on a baseline makes the two tiers disagree about what evidence a missing thing needs. It would also leave file-scoped anchors — which can never carry a `combdrift/fp` (`_mint_core` returns `{}` when `symbol is None`) — permanently unable to reach `anchor_missing`, i.e. BUG-078 half-open. Coherence would require gating BOTH tiers, which is a coverage regression well outside this bug and contradicts §3.2(a)'s explicit "`anchor_missing` is unaffected". |

**D-B, concretely.** `wire_verdict`'s stale arm becomes an explicit ordered arrow table:

```python
# Every engine reason `hive.combdrift.verdict` can classify `stale`, placed
# EXPLICITLY. Prefix-matched in order (a reason carries a ": <detail>" suffix).
_STALE_ARROWS: tuple[tuple[str, str], ...] = (
    ("signature_changed", DRIFT_ANCHOR_CHANGED),
    ("symbol_missing", DRIFT_ANCHOR_MISSING),
    ("file_missing", DRIFT_ANCHOR_MISSING),
)
```

The final `return DRIFT_UNVERIFIABLE` **stays** — it is the fail-safe for a genuinely unknown
reason string (a hostile cache row, an older engine's output, a future reason a rolling upgrade
has not taught this server about). What changes is that "unknown" now means *unknown*, not
*unenumerated*.

**The ratchet is what makes the class unconstructable.** A new static test (§6, CT-D1) derives the
engine's stale-classifiable reason set at test time — for each `REASON_*` constant in
`hive.combdrift.types`, build an `AnchorResult` and ask `hive.combdrift.verdict._classify` — and
asserts every member appears in `hive/app/drift._STALE_ARROWS`. Adding a stale reason to the
engine without deciding its wire arrow reds that test. Tests may import anything, so the ratchet
carries the single-ownership guarantee with none of D-C's runtime cost. This is the idiom the repo
already uses for exactly this problem: `tests/app/test_sync_keys.py` statically pins the served
health fields against the daemon's key builders (BUG-059), and `hive/domain/meta_registry.py`
carries a test-enforced coverage ratchet.

Enumerated against the tree at `21484a7`, the engine can classify exactly three reasons `stale`
(`file_missing`, `symbol_missing`, `signature_changed`); after §3.2(a) adds `no_fingerprint` to
`_UNVERIFIABLE_PREFIXES`, that stays three. The table is small, complete, and now provably so.

---

## 4 · Staleness completeness table (today vs post-remediation)

The audit surface, end to end, one row per engine reason. The chain is:
engine reason (`hive/combdrift/types.py:18-36`) → `_anchor_makes_stale` / `_anchor_makes_unverifiable`
(`hive/combdrift/verdict.py`) → `RecordVerdict` → `hive/edge/cli.py:_verify_core`'s emitted
`(verdict, reason)` → `hive/app/drift.py:wire_verdict` → `QUALIFYING_DRIFT`
(`hive/domain/retirement.py:81`) → the served riders (`hit["drift"]`, `hit["last_verified"]`,
`hit["remediation"]` at `hive/app/mcp_server.py:961`).

Two reasons are read together because the ENGINE reason alone does not determine the outcome:
`file_missing` splits on the code-shaped/prose carve-out (`hive/edge/cli.py:239-245`), and `ok`
splits on whether a fingerprint was actually compared.

### 4.1 Today (`21484a7`)

| # | Engine reason | Engine class | `_verify_core` emits | Wire verdict | Qualifies retirement? | Served rider | Intended? |
|---|---|---|---|---|---|---|---|
| 1 | `ok` — fingerprint compared, matched/additive | current | `current`/`ok` | `fresh` | no | `drift.type: fresh`, no remediation | ✅ the claim is measured |
| 1b | `ok` — **no fingerprint recorded** (empty carrier) | current | `current`/`ok` | `fresh` | no | `drift.type: fresh` | ❌ **BUG-071** — a positive claim about a comparison that never ran; and `_backfill` then mints the baseline from the post-break tip, freezing it |
| 2 | `file_missing`, **code-shaped path** | stale | `stale`/`file_missing` | `unverifiable` | **no** | `drift.type: unverifiable` | ❌ **BUG-078** — the strongest evidence of a dead anchor is the one verdict that never retires |
| 2b | `file_missing`, **prose path** | stale | `unverifiable`/`not_a_code_anchor` (reclassified) | `unverifiable` | no | `drift.type: unverifiable` | ✅ a prose string was never code, so code cannot have moved past it |
| 3 | `path_outside_repo` | unverifiable | `unverifiable`/`path_outside_repo` | `unverifiable` | no | `drift.type: unverifiable` | ✅ containment failure is undecidable, not stale |
| 4 | `parse_error` | unverifiable | `unverifiable`/`parse_error` | `unverifiable` | no | `drift.type: unverifiable` | ✅ an unparseable tree proves nothing about the anchor |
| 5 | `symbol_missing` | stale | `stale`/`symbol_missing` (+ remediation) | `anchor_missing` | **yes** | `drift.type: anchor_missing` | ✅ provable absence at the declaration site |
| 6 | `no_symbol_requested` (file-scoped, file present) | current | `current`/`no_symbol_requested` | `fresh` | no | `drift.type: fresh` | ✅ existence IS the whole claim for a file-scoped anchor |
| 7 | `no_anchors` | unverifiable (`_classify(())`) | **unreachable** — `_verify_core` always builds a 1-anchor `Record` | — | — | — | ✅ dead on this path; a vocabulary member with no wire arrow, which is why §3.3's ratchet keys on *stale-classifiable*, not on *every constant* |
| 8 | `unsupported_language` | unverifiable | `unverifiable`/`unsupported_language` | `unverifiable` | no | `drift.type: unverifiable` | ✅ precision over coverage |
| 9 | `symbol_indirect` | unverifiable | `unverifiable`/`symbol_indirect` | `unverifiable` | no | `drift.type: unverifiable` | ✅ presence combdrift cannot positively resolve is never stale |
| 10 | `signature_changed` | stale | `stale`/`signature_changed` (+ remediation + delta) | `anchor_changed` | **yes** | `drift.type: anchor_changed` | ✅ a proven call-shape break |
| 11 | `fingerprint_version_mismatch` | unverifiable | `unverifiable`/`fingerprint_version_mismatch` | `unverifiable` | no | `drift.type: unverifiable` | ✅ the BUG-037 law: incomparable ⇒ silence |
| 12 | `no_fingerprint` | — | does not exist | — | — | — | ❌ its absence IS row 1b's defect |
| R | radius tier: `current` + `radius: changed` | current | `current` + `radius` key | `blast_radius_changed` (synthesized in `sync.py:_verify_anchor:974`) | **yes** | `drift.type: blast_radius_changed` | ✅ when the radius was compared |
| R2 | radius tier: `current` + **no `matrix/subgraph_fp` carrier** | current | `current`, radius key OMITTED | `fresh` | no | `drift.type: fresh` | ❌ **BUG-080** — `fresh` sits below `blast_radius_changed` on the ladder, so it reads as "nothing detected on any tier"; the neighbourhood was never compared |
| B | `_verify_core` branch-scope route (off-set consumer, would-be-stale) | — | `branch_scoped`/`off_branch` | `branch_scoped` | no | `drift.type: branch_scoped` + notice | ✅ scoped elsewhere, not stale here |
| F | spawn fault / unparseable output / incomparable stored fp version | — | — (belt in `sync.py:945-952`, `961-972`) | `unverifiable` | no | `drift.type: unverifiable` | ✅ every miss is fail-safe |

**Wrong today: 3 rows** — 1b (BUG-071), 2 (BUG-078), R2 (BUG-080).
BUG-077 does not appear as a row: it is upstream of the whole chain (an accepted anchor that the
census join can never match), and its symptom is the *absence* of `change_outcome` rows, not a
wrong verdict.

### 4.2 After the full remediation

| # | Engine reason | Engine class | `_verify_core` emits | Wire verdict | Qualifies retirement? | Served rider | Intended? |
|---|---|---|---|---|---|---|---|
| 1 | `ok` — fingerprint compared | current | `current`/`ok` | `fresh` | no | `drift.type: fresh` | ✅ and now `ok` is **reachable only after a real comparison** |
| 1b | *(gone)* — an empty carrier can no longer produce `ok` | — | — | — | — | — | ✅ collapsed into row 12 |
| 2 | `file_missing`, code-shaped path | stale | `stale`/`file_missing` (+ remediation) | **`anchor_missing`** | **yes** | `drift.type: anchor_missing` | ✅ **I5** — symmetric with row 5; covers symbol-scoped AND file-scoped anchors |
| 2b | `file_missing`, prose path | stale | `unverifiable`/`not_a_code_anchor` | `unverifiable` | no | `drift.type: unverifiable` | ✅ unchanged — the carve-out runs before the wire map |
| 3 | `path_outside_repo` | unverifiable | same | `unverifiable` | no | same | ✅ unchanged; now EXPLICITLY not-stale via the ratchet |
| 4 | `parse_error` | unverifiable | same | `unverifiable` | no | same | ✅ unchanged |
| 5 | `symbol_missing` | stale | same | `anchor_missing` | **yes** | same | ✅ unchanged |
| 6 | `no_symbol_requested` | current | same | `fresh` | no | same | ✅ unchanged. Named limit: a file-scoped anchor whose file was rewritten still reads `fresh` — existence is its declared claim, and it never carries a fingerprint or a radius token to compare |
| 7 | `no_anchors` | unverifiable | unreachable | — | — | — | ✅ unchanged; the ratchet does not require an arrow for a reason the engine never classifies stale |
| 8 | `unsupported_language` | unverifiable | same | `unverifiable` | no | same | ✅ unchanged |
| 9 | `symbol_indirect` | unverifiable | same | `unverifiable` | no | same | ✅ unchanged |
| 10 | `signature_changed` | stale | same | `anchor_changed` | **yes** | same | ✅ unchanged |
| 11 | `fingerprint_version_mismatch` | unverifiable | same | `unverifiable` | no | same | ✅ unchanged |
| 12 | **`no_fingerprint`** (NEW) | **unverifiable** | `unverifiable`/`no_fingerprint` | `unverifiable` | no | `drift.type: unverifiable`, no remediation | ✅ **I3** — the honest unknown; covers the pre-mint window, an unresolvable mint, a capped backlog, and B6's deferrals |
| R | radius compared, changed | current | `current` + radius | `blast_radius_changed` | **yes** | same | ✅ unchanged |
| R2 | radius never compared (no `matrix/subgraph_fp`) | current | `current`, radius omitted | `fresh` | no | `drift.type: fresh` | ❌ **STILL WRONG — BUG-080**, logged, deliberately out of scope (see §1 and §3.2 residual 2) |
| B | branch-scope route | — | `branch_scoped`/`off_branch` | `branch_scoped` | no | same | ✅ unchanged, and now also reachable from a deleted FILE (row 2 became stale-tier) |
| F | fault belts | — | — | `unverifiable` | no | same | ✅ unchanged |
| U | an UNRECOGNIZED stale reason (future engine, hostile cache row) | stale | `stale`/`<unknown>` | `unverifiable` | no | `drift.type: unverifiable` | ✅ **I6** — fail-safe retained; and the ratchet (CT-D1) makes "unenumerated" impossible for reasons this engine can actually emit |

**Wrong after remediation: 1 row** — R2 (BUG-080), logged with its own entry and its own design
question (splitting the `fresh` wire verdict vs. contradicting the meta-envelope law's clause 6).
Every other row is correct and actionable end to end, and the two structural guarantees behind
that are: `fresh` is unconstructable without a comparison (I3), and the stale→wire mapping is
exhaustive by construction (I6).

---

## 5 · Implementation plan (ordered; each step safe on its own)

Steps 1-4 close BUG-077; steps 5-7 close BUG-071; step 8 closes BUG-078. Test authoring order is
inverted per §6.

**Step 0 — author the contract suite first, red.** Add `tests/contract/test_anchor_grammar_e2e.py`,
`tests/contract/test_drift_baseline_e2e.py`, and `tests/contract/test_stale_wire_mapping_e2e.py`.
Run them against the unmodified tree and record each as failing **on an assertion**, never a
collection error. `frozen_paths: tests/contract/**`. No existing file under `tests/contract/` is
edited by this plan.

**Step 1 — `hive/domain/anchor_grammar.py` (new).** Pure, stdlib-only, no imports from
`hive.app`/`hive.adapters`/any engine package. The two functions of §3.1(d). Clauses, in order,
each with the message the refusal envelope carries:

1. path component empty (after splitting on the first `"::"`) →
   `"anchors[i].anchor has an empty path component"`.
2. `"::"` ABSENT and `":"` present →
   `"anchors[i].anchor 'a.py:Sym' — the symbol separator is '::', not ':' (a single-colon anchor matches no census subject); write 'a.py::Sym'"`.
3. `"::"` present and symbol empty → `"…names no symbol after '::' — drop the separator to bind the file"`.
4. `"::"` present and symbol is all digits →
   `"…'42' is a line number, not a symbol — bind the file as 'a.py'"`.

Note clause 2's guard: `"::" not in anchor and ":" in anchor`. `a:b/c.py::S` therefore ADMITS
(§3.1(a)). Safe alone: nothing imports it yet.

**Step 2 — wire the gate.** In `hive/app/anchors.py:normalize_anchors`, after the existing
non-empty check (`:103`) and before the registry check, call `anchor_grammar_error(anchor)` and
`raise BadAnchors(f"anchors[{i}].anchor {msg}")` when it returns a message. Update the module
docstring (`:8`) — the anchor is no longer "free text". Safe alone: the only behaviour change is
that a previously-accepted dead spelling now produces the existing `status="refused"` envelope.

**Step 3 — collapse the join onto the shared splitter.** In
`hive/domain/change_evidence.py:_anchor_match_level`, replace the inline
`anchor.partition("::")` with `split_anchor(anchor)`, treating `symbol == ""` as the file tier.
Behaviour-identical (`"a.py::"` already fell to the file tier via `if sep and symbol`), so every
existing test — including the BUG-058 anti-fork pin at `tests/domain/test_change_evidence.py:1687`
— stays green. Safe alone: pure refactor to one owner.

**Step 4 — advertise the enforced rule and name the reader-side leniency.**
`hive/app/tool_defs.py:_ANCHORS_PROPERTY["description"]` states the separator is `::` and that a
single-colon anchor is refused. This rides inside `inputSchema`, not the capped tool
`description`, so the `METADATA_FIELD_LIMIT` headroom is untouched — assert the existing cap test
stays green. Add the comment on `hive/edge/cli.py:_split_anchor` recording §3.1(c): it is the
reader side of a mint-current/read-historical split, not a fork to be tidied away.

**Step 5 — the no-fingerprint reason.** `hive/combdrift/types.py`: add
`REASON_NO_FINGERPRINT = "no_fingerprint"`. `hive/combdrift/resolution.py:206`: return it from
the `anchor.fingerprint is None` branch, `found=True`, `location` populated, with a comment
naming the claim it refuses to make. `hive/combdrift/verdict.py`: add it to
`_UNVERIFIABLE_PREFIXES`. Do NOT touch `hive/combdrift/change.py` — §3.2(a)'s blast-radius check
shows the new reason is unreachable in every branch that reads a reason there, and M14 pins it.
Safe alone: strictly moves verdicts from `current` to `unverifiable`, never the reverse, so
nothing can newly qualify a retirement.

**Step 6 — carry the touched paths.** `hive/domain/change_evidence.py`: add
`touched_paths: frozenset[str] = frozenset()` as the last field of `IngestReport` (defaulted, so
every existing construction stays valid; the dataclass is `frozen=True, slots=True`, so the field
must be last) and populate it in `ingest` from the `subjects` computed at `:792` — on **both**
return sites, including the `range_skipped` early return (§3.2(b)). Safe alone: an additive,
unread field.

**Step 7 — defer the changed anchors.** `hive/app/sync.py`:
- `_ledger_leg(...) -> frozenset[str]` — return `report.touched_paths` after a successful
  ingest, and `frozenset()` on the no-new-commits early return (nothing changed ⇒ nothing to
  defer).
- `_repo_tick` — capture the return; on a ledger fault leave it `None` and skip the backfill leg
  entirely (the drift leg is unaffected and still runs).
- `_backfill(self, row, mirror, tip_sha, ref, *, changed_paths: frozenset[str])` — inside the
  `todo` loop, `continue` when `split_anchor(anchor)[0] in changed_paths`, with a named mutation
  marker (§7).
Safe alone: with steps 5-6 already in, a deferred anchor reads `unverifiable` rather than a
premature `fresh`.

**Step 8 — the exhaustive stale→wire mapping.** `hive/app/drift.py`:
- Add the `_STALE_ARROWS` table of §3.3(b), with `file_missing → DRIFT_ANCHOR_MISSING` in it.
- Rewrite `wire_verdict`'s `head == "stale"` arm to walk the table by prefix, keeping the final
  `return DRIFT_UNVERIFIABLE` as the fail-safe for an unrecognized reason, with a marker naming
  it as the fail-safe (deleting the table and returning `DRIFT_UNVERIFIABLE` unconditionally is
  mutation M18).
- Correct the docstring: it currently quotes the §3.4 table verbatim and that table is the source
  of the spec gap. It must now state the three arrows and that the set is ratchet-enforced.
Safe alone: it strictly moves `file_missing` from `unverifiable` to `anchor_missing`; no other
input changes. It is placed LAST because it is the only step that makes something newly qualify a
retirement, so it lands on a tree where §3.2's under-claiming fixes are already in.

**Step 9 — docs (§8) in the same change.**

**Dead code.** This plan deletes no code; it replaces one inline partition with a call to its new
owner (step 3) and one two-arm `if` chain with an explicit table (step 8). `_LINE_NUMBER` and
`_split_anchor`'s single-colon branch are deliberately retained per §3.1(c), with the comment
that records why — so a later minimality pass does not read them as leftovers.

**New dependencies.** None. `hive/domain/anchor_grammar.py` is stdlib-only and adds no import
that `tests/test_purity.py` forbids, and step 8 adds no runtime import to `hive/app/drift.py`
(§3.3(b), option D-C rejected).

---

## 6 · Intent → contract → test traceability

Contract tests live in `tests/contract/` (the frozen set) and drive real entry points over the
real temp store that `tests/contract/conftest.py` already provides — `HiveMCPServer.handle`
assembled by `build_container`, and `SyncService.tick` against real tmp git origins (the reused
`Origin` / `git` factory from `tests/sync/conftest.py`) with the scripted `Run` seam. No mocked
component stands in for a boundary any of the three bugs crosses.

| Intent | Contract (given / when / then) | Scenarios covered | Contract test(s) |
|---|---|---|---|
| **I1** | **Given** a registered repo and a live server, **when** `hive_write` / `hive_capture` carries an anchor with a `:` but no `::`, or whose `::` symbol is empty or a bare line number, **then** the envelope is `status="refused"` naming the clause, nothing is stored, and the episode count is unchanged. | `a.py:Sym`; `a.py:42`; `a:b/c.py`; `a.py::`; `a.py::42`; `::S` (empty path); both verbs; a refusal alongside a valid second anchor refuses the WHOLE call; the refusal is an envelope, not a JSON-RPC error. | `test_anchor_grammar_e2e.py::test_dead_anchor_spellings_are_refused_and_store_nothing` |
| **I1** | **Given** the same server, **when** an anchor is `path`, `path::Symbol`, `path::Ns::C.m`, **`a:b/c.py::S`**, or colon-free prose, **then** the write lands and the anchor is stored verbatim. | file-scoped; symbol-scoped; nested-`::` symbol; **colon-bearing path made unambiguous by `::`** (the [REVISED] admission of §3.1(a)); unicode/space-bearing prose; a symbol containing digits but not only digits (`f2`); max-entries boundary unchanged. | `test_anchor_grammar_e2e.py::test_canonical_and_prose_anchors_still_admit` |
| **I1** | **Given** a hostile payload, **when** the anchor is a non-str, empty, or the array is malformed, **then** the pre-existing refusal clause fires unchanged and the loop never crashes. | non-str anchor; `""`; non-dict entry; unknown key; over-cap array. | `test_anchor_grammar_e2e.py::test_pre_existing_refusals_are_unchanged` |
| **I2** | **Given** an anchor written in the canonical grammar, **when** it is run through the server-side splitter and through `hive.edge.cli._split_anchor`, **then** both yield the same `(path, symbol)` (with `""` and `None` read as the same file-scoped meaning); and **when** it is joined against a matching receipt subject, **then** it matches at the `symbol` tier. | `a.py::f`; `a.py::C.m`; `a.py::Ns::C.m`; `a:b/c.py::S`; `a.py`; and the negative pin — a single-colon anchor still joins nothing while the engine still resolves it (the reader-side leniency of §3.1(c)). | `test_anchor_grammar_e2e.py::test_the_canonical_grammar_tokenizes_identically_on_both_sides` (CT-A5) |
| **I2** | **Given** a memory bound with the canonical grammar, **when** a real tick censuses a commit touching that symbol, **then** a `change_outcome` row lands for that episode — the end-to-end proof the accepted grammar is the joinable one. | symbol tier; file tier. | `test_anchor_grammar_e2e.py::test_an_accepted_anchor_reaches_the_change_outcome_feed` |
| **I3** | **Given** a registered repo and a memory anchored at a symbol whose fp carrier is empty, **when** the drift materializer runs and recall serves the hit, **then** `drift.type == "unverifiable"`, never `"fresh"`. | empty carrier, symbol present; empty carrier, symbol MISSING (still `anchor_missing`, unchanged); file-scoped anchor with empty carrier (still `fresh` — existence is the whole claim); populated carrier + unchanged code (`fresh`); populated carrier + changed signature (`anchor_changed`). | `test_drift_baseline_e2e.py::test_an_uncompared_symbol_never_reads_fresh` |
| **I3** | **Given** an anchor reading `unverifiable` for want of a baseline, **when** an agent calls `hive_prune` on it, **then** the call is the benign no-op envelope — unknown never retires. | prune; supersede. | `test_drift_baseline_e2e.py::test_an_unbaselined_anchor_never_qualifies_retirement` |
| **I3** | **Given** a real receipt over a range whose touched symbol is overloaded at base (so `fingerprint_anchor` yields `None`), **when** the census ingests it, **then** the derived `SymbolChange.reason` and the `verify_*` rows are byte-identical to today — the new engine reason never leaks into the change path. | overloaded base symbol; indirect base symbol; symbol added in the range; symbol deleted in the range. | `test_drift_baseline_e2e.py::test_the_no_fingerprint_reason_never_reaches_the_census_change_path` |
| **I4** | **Given** a repo whose canonical line advances with a commit that changes an anchored symbol, and a memory on that anchor whose carrier is still empty, **when** one tick runs, **then** no carrier is minted for that anchor, its drift reads `unverifiable`, and the tick reports no error. | changed symbol; changed file with a second, UNTOUCHED anchor that DOES mint in the same tick; two repos where only one changed. | `test_drift_baseline_e2e.py::test_a_just_changed_anchor_is_not_baselined_this_tick` |
| **I4** | **Given** the deferral above, **when** a subsequent tick brings no change to that path, **then** the carrier mints and drift becomes comparable (`fresh`, then `anchor_changed` after a later break). | defer → mint → break → `anchor_changed`, in one test across three ticks. | `test_drift_baseline_e2e.py::test_a_deferred_anchor_baselines_on_the_next_quiet_tick` |
| **I4** | **Given** a repo whose ledger leg faults (unreachable remote mid-tick), **when** the tick runs, **then** no carrier is minted, `sync:<name>:last_error` is set, the drift leg still runs, and the next clean tick mints. | ledger fault; fault then recovery. | `test_drift_baseline_e2e.py::test_a_faulted_ledger_leg_mints_no_baseline` |
| **I4** | **Given** a range the manual `hive ingest` door already absorbed, **when** the daemon's ledger leg re-ingests it and the report comes back `range_skipped`, **then** the touched paths are still known and the changed anchor is still deferred. | range-skipped ingest with a changed anchor; range-skipped ingest with an untouched anchor (which still mints). | `test_drift_baseline_e2e.py::test_a_range_skipped_ingest_still_defers_the_changed_anchor` |
| **I5** | **Given** a memory anchored at `pkg/f.py::foo` on its own declared line, **when** the whole FILE is deleted and a real tick materializes drift, **then** the served `drift.type` is `anchor_missing` and a conscious `hive_prune` on that memory RETIRES it with a `drift:anchor_missing` signal in the audit. | symbol-scoped anchor, file deleted; FILE-SCOPED anchor (`pkg/f.py`), file deleted; the deleted-SYMBOL twin (unchanged, `anchor_missing`); a prose anchor whose "path" is missing (still `unverifiable`, still a no-op prune); an off-line consumer reading the deleted file (now `branch_scoped`, not `unverifiable`). | `test_stale_wire_mapping_e2e.py::test_a_deleted_file_reads_anchor_missing_and_qualifies_retirement` |
| **I5** | **Given** a memory whose anchor path was never in the repo at all (a typo), **when** drift materializes, **then** it reads `anchor_missing` exactly like a typo'd SYMBOL does — the two tiers agree, and the agent-initiated gate is the only thing that can act on it. | typo'd path; typo'd symbol; both under a `hive_prune` that IS gated and one that is NOT (no anchors ⇒ no-op). | `test_stale_wire_mapping_e2e.py::test_the_file_and_symbol_tiers_agree_on_an_absent_anchor` |
| **I6** | **Given** the engine's reason vocabulary, **when** every `REASON_*` constant in `hive.combdrift.types` is classified by `hive.combdrift.verdict`, **then** every reason it classifies `stale` has an explicit arrow in `hive.app.drift._STALE_ARROWS`, and every arrow's target is a member of `WIRE_VERDICTS`. | all twelve reasons; the ratchet reds when a stale reason is added without an arrow (asserted by constructing the hypothetical, not by editing the engine). | `test_stale_wire_mapping_e2e.py::test_every_stale_reason_the_engine_can_emit_has_an_explicit_wire_arrow` (CT-D1) |
| **I6** | **Given** an unrecognized stale reason on the wire (a hostile cache row, a future engine's output), **when** it reaches `wire_verdict` and the drift cache, **then** it degrades to `unverifiable` and never qualifies a retirement. | `stale/<unknown>`; `stale` with no reason; a non-string state; a non-string reason; an out-of-vocabulary cached verdict served through `attach_drift`. | `test_stale_wire_mapping_e2e.py::test_an_unknown_stale_reason_still_fails_safe` |

### Unit tests (component level)

- `tests/domain/test_anchor_grammar.py` (new) — the clause table for `anchor_grammar_error`
  (every accept/refuse row of §5 step 1, including the `a:b/c.py::S` ADMIT row) and
  `split_anchor` totality, including a **property test** over generated strings asserting
  `split_anchor` never raises and that
  `anchor_grammar_error(a) is None ⇒ (":" not in a or "::" in a)`. The input space is generative,
  so it earns a generative test.
- `tests/app/test_anchors.py` — add the refusal rows to the existing
  `test_malformed_anchors_refuse_with_the_clause` table; rename `test_anchor_is_free_text` to
  state the narrowed rule (its subject string has no colon and still admits).
- `tests/domain/test_change_evidence.py` — assert `_anchor_match_level` is unchanged across the
  splitter swap, including `"a.py::"` at the file tier; the BUG-058 pin at `:1687` stays as-is.
- `tests/combdrift/test_resolution.py` / `test_verdict.py` — the `no_fingerprint` reason, its
  `found=True` + populated `location`, and its unverifiable tier.
- `tests/combdrift/test_change.py` — the §3.2(a) blast-radius pin: `verify_change` over a base
  tree where `fingerprint_anchor` yields `None` produces the SAME `SymbolChange.reason` and the
  SAME rolled-up `verdict` as before the change.
- `tests/edge/test_cli.py` — `verify` with no `--fp` on a resolvable symbol now prints
  `unverifiable`; the single-colon tokenizer tests stay green and gain the comment that they pin
  reader-side historical support.
- `tests/app/test_drift.py` — `test_the_five_named_arrows` gains the `("stale", "file_missing",
  DRIFT_ANCHOR_MISSING)` row; `test_everything_else_maps_unverifiable` loses its `file_missing`
  membership claim (it currently pins "a bare or unrecognized stale reason" and must be narrowed
  to reasons that are genuinely unrecognized) and gains an explicit unknown-reason row.
- `tests/sync/test_contract_backfill.py` — `test_matches_edge_mint_at_the_moved_tip` is
  **rewritten**: its scenario (episode seeded after a commit that changes the anchor, minted on
  the next tick) is now the deferral case. Split it into (i) byte-equality with a direct edge
  mint on a tick whose range did NOT touch the anchor, and (ii) the deferral. Byte-equality with
  the real `hive-edge` subprocess is preserved — it is the property that test exists for.
- `tests/domain/test_change_evidence.py` — `IngestReport.touched_paths` populated from the
  receipt on the normal path AND on the range-skipped path.

### External-interaction inventory

| Boundary | Where | Declared failure behaviour | Failure-path test |
|---|---|---|---|
| `hive-edge mint` subprocess | `sync.py:_mint` | Unchanged: nonzero exit / unparseable stdout ⇒ `{}` ⇒ silent skip, loop alive, carrier stays empty (now reads `unverifiable`). | `test_drift_baseline_e2e.py::test_an_uncompared_symbol_never_reads_fresh` + existing `test_skips_unresolvable_silently_loop_alive` |
| `hive-edge verify` subprocess | `sync.py:_verify_anchor` | Unchanged: fault ⇒ `unverifiable`. | existing `tests/sync/test_contract_drift.py` |
| `git worktree add --detach <tip>` | `sync.py:_verify_at_tip` | Unchanged: nonzero ⇒ `_SyncFault` ⇒ the WHOLE batch is abandoned and no verdict is written. This is what makes `file_missing` a genuine measurement rather than a partial-checkout artifact (§2.3), so it now has a named test. | `test_stale_wire_mapping_e2e.py::test_a_failed_worktree_writes_no_verdict_rather_than_a_false_anchor_missing` |
| `python -m hive.census.cli build` subprocess | `sync.py:_build_receipt` | Unchanged: nonzero ⇒ `_SyncFault` ⇒ the ledger leg fails open per repo. **New:** the backfill leg is then skipped for that repo this tick. | `test_drift_baseline_e2e.py::test_a_faulted_ledger_leg_mints_no_baseline` |
| git fetch / clone against a real tmp origin | `sync.py:_fetch`, `ensure_mirror` | Unchanged: per-repo fail-open, `sync:<name>:last_error`, next tick retries. | existing `test_unreachable_fail_open` |
| MCP write boundary | `mcp_server` → `normalize_anchors` | **New refusal path:** `BadAnchors` ⇒ `status="refused"` envelope inside a 200, nothing stored, never a JSON-RPC error and never a crash. | `test_anchor_grammar_e2e.py::test_dead_anchor_spellings_are_refused_and_store_nothing` |
| Environment | — | This plan introduces **no** new environment variable and no new secret. The existing `HIVE_EDGE_HOME` / `HIVE_SYNC__*` handling is untouched; no fail-fast probe is added because no required variable is added. | n/a (stated so the absence is deliberate, not an omission) |

---

## 7 · Mutation discipline

Each mutation must be applied, watched to red the named test, then restored.

**BUG-077**

| # | Mutation | Test that must go red |
|---|---|---|
| M1 | Delete the `anchor_grammar_error` call from `normalize_anchors`. | `test_anchor_grammar_e2e.py::test_dead_anchor_spellings_are_refused_and_store_nothing` |
| M2 | Drop clause 2's `"::" not in anchor` guard (refuse ANY `:`, over-refusing `a:b/c.py::S`). | `test_anchor_grammar_e2e.py::test_canonical_and_prose_anchors_still_admit` |
| M3 | Drop clause 4 (allow a bare-line-number symbol). | `tests/domain/test_anchor_grammar.py` line-number row + the e2e refusal test |
| M4 | Make `anchor_grammar_error` refuse anything with a `":"` anywhere (over-refusal — `a.py::Ns::C.m` dies). | `test_anchor_grammar_e2e.py::test_canonical_and_prose_anchors_still_admit` |
| M5 | Make `split_anchor` partition on the LAST `::` instead of the first. | `test_anchor_grammar_e2e.py::test_the_canonical_grammar_tokenizes_identically_on_both_sides` |
| M6 | Make `normalize_anchors` silently normalize `a.py:S` → `a.py::S` instead of refusing. | `test_anchor_grammar_e2e.py::test_dead_anchor_spellings_are_refused_and_store_nothing` (the stored-nothing assertion) |

**BUG-071**

| # | Mutation | Test that must go red |
|---|---|---|
| M7 | Restore `REASON_OK` in the `anchor.fingerprint is None` branch. | `test_drift_baseline_e2e.py::test_an_uncompared_symbol_never_reads_fresh` |
| M8 | Remove `REASON_NO_FINGERPRINT` from `_UNVERIFIABLE_PREFIXES` (so it falls through to `current`). | same, plus `tests/combdrift/test_verdict.py` |
| M9 | Extend the new reason to the `symbol is None` arm as well (over-reach: file anchors stop reading `fresh`). | `test_drift_baseline_e2e.py::test_an_uncompared_symbol_never_reads_fresh` (the file-scoped scenario) |
| M10 | Delete the `changed_paths` skip in `_backfill`. | `test_drift_baseline_e2e.py::test_a_just_changed_anchor_is_not_baselined_this_tick` |
| M11 | Make `_ledger_leg` return `frozenset()` unconditionally. | same as M10 |
| M12 | Run `_backfill` even when the ledger leg faulted. | `test_drift_baseline_e2e.py::test_a_faulted_ledger_leg_mints_no_baseline` |
| M13 | Make the skip permanent (never mint an anchor once deferred). | `test_drift_baseline_e2e.py::test_a_deferred_anchor_baselines_on_the_next_quiet_tick` |
| M14 | Make `change.py:_classify_symbol` read `head_res.reason` in the `old_token is None` branch (so `no_fingerprint` DOES leak into the census change path). | `test_drift_baseline_e2e.py::test_the_no_fingerprint_reason_never_reaches_the_census_change_path` + `tests/combdrift/test_change.py` |
| M15 | Return `frozenset()` for `touched_paths` on the `range_skipped` early return. | `test_drift_baseline_e2e.py::test_a_range_skipped_ingest_still_defers_the_changed_anchor` |

**BUG-078 / the completeness guarantee**

| # | Mutation | Test that must go red |
|---|---|---|
| M16 | Remove the `file_missing` arrow from `_STALE_ARROWS` (restoring the catch-all fall-through). | `test_stale_wire_mapping_e2e.py::test_a_deleted_file_reads_anchor_missing_and_qualifies_retirement` |
| M17 | Point `file_missing` at `DRIFT_ANCHOR_CHANGED` instead of `DRIFT_ANCHOR_MISSING` (wrong member of the same tier — it would still qualify retirement, so only a verdict-exact assertion catches it). | same test's `drift.type` assertion + `tests/app/test_drift.py`'s arrow row |
| M18 | Delete `_STALE_ARROWS` entirely and return `DRIFT_UNVERIFIABLE` for every stale reason (the pre-fix behaviour, generalized). | `test_stale_wire_mapping_e2e.py::test_a_deleted_file_reads_anchor_missing_and_qualifies_retirement` AND the deleted-SYMBOL scenario — one mutation, two tiers red |
| M19 | Weaken the ratchet to check only the arrows that exist (assert-nothing), i.e. iterate `_STALE_ARROWS` instead of the engine's reason set. | `test_stale_wire_mapping_e2e.py::test_every_stale_reason_the_engine_can_emit_has_an_explicit_wire_arrow` — verified by adding a throwaway stale reason to the engine in the mutation run and watching the ratchet stay green under the mutation and red under the original |
| M20 | Drop the prose carve-out in `_verify_core` (a missing PROSE path becomes `stale`/`file_missing`, which now maps to `anchor_missing` — the false-retirement path BUG-022 closed). | `test_stale_wire_mapping_e2e.py::test_a_deleted_file_reads_anchor_missing_and_qualifies_retirement` (the prose scenario) + the existing `verify_prose_not_a_code_anchor` parity golden |
| M21 | Make `_verify_at_tip` swallow a failed `worktree add` and verify against an empty directory (every anchor would read `file_missing` ⇒ `anchor_missing` ⇒ mass false retirement eligibility). | `test_stale_wire_mapping_e2e.py::test_a_failed_worktree_writes_no_verdict_rather_than_a_false_anchor_missing` |

**Markers preserved / re-homed (Law 7).** No existing named marker is deleted. `_backfill`'s
`backfill_per_tick` cap marker, `_ledger_leg`'s first-connect baseline marker, `_verify_core`'s
prose-reclassification marker, and `_verify_anchor`'s BUG-037 version-gate marker all stay
verbatim. Three markers are added: the deferral skip in `_backfill` (M10), the fail-safe
fall-through in `wire_verdict` (M18), and the no-fingerprint branch in `resolution.py` (M7).
`_LINE_NUMBER`'s "a bare number is not a symbol" fact is **re-homed** (not moved) — the engine
keeps its copy for reading legacy anchors, the boundary states it as a refusal for new ones, and
CT-A5 pins the pair.

---

## 8 · Doc obligations (same change)

1. **`CONTEXT/BUGS.md`** — flip BUG-077, BUG-071 and BUG-078 to `SOLVED`, add `Date solved`, and
   record the fix as applied, including the five corrections this diagnosis made: that the
   received "backfill against the watermark tree" direction is a no-op given the leg order; that
   a stored single-colon anchor's drift verdict is correct rather than false; that
   `a:b/c.py::S` and `a.py::` are functional spellings the first draft wrongly classed as dead;
   that `hive/app/**` is NOT walled off from the engines (only the reverse is); and that
   `file_missing`'s over-claim risk is bounded because every residual spurious class is the exact
   twin of one `symbol_missing` already admits. BUG-079 and BUG-080 stay `UNSOLVED` with their
   out-of-scope rationale.
2. **`CONTEXT/INTERACTIONS.md`** — `[P1]` (§2) must stop calling the anchor "free
   `path`/`path::Symbol` text" and state the enforced clauses; `[E1]` (§5) gains the deferral
   ("an anchor the tick's own receipt reported changed is not minted this tick" — and the ledger
   leg is now a precondition of the backfill leg, which also changes `[C2]`'s three-leg
   description); `[E2]`/`[E3]` note that a symbol anchor with no baseline reads `unverifiable`,
   never `fresh`; `[E4]` ("How an edit moves a verdict") must now say that **deleting the FILE**
   — not only the symbol — yields `anchor_missing`; `[X12]` (§8)'s "un-materialized drift anchor
   reads `unverifiable`" now also covers the un-baselined case.
3. **`CONTEXT/THEORY.md`** — §5's meta-envelope discussion gains the anchor grammar as a second
   instance of mint-current/read-historical (§3.1(c)). §2's dependency-rule paragraph should note
   explicitly that the engine wall is one-directional (engines never import the server stack; the
   server MAY read an engine constant call-time to avoid forking a fact, as `sync.py` does) —
   §3.0 found the plan's own author had read it as bidirectional, which is a sign the document
   under-specifies it. §9's checklist is unchanged. No law is contradicted by this change; if
   implementation and THEORY diverge during the build, the code wins and THEORY is corrected.
4. **`CHANGELOG.md`** — one entry covering the three fixes.
5. **Served contract / tool schemas** — `hive/app/contract.py` and the `hive_write` /
   `hive_capture` descriptions already advertise `path/file.py::symbol` (BUG-058), so the
   advertised grammar does not change; only `_ANCHORS_PROPERTY`'s description gains the refusal
   fact (step 4). `REMEDIATION_NOTICE` (`hive/app/contract.py:66`) needs no change — it already
   describes "anchor drift on the memory's own declared line" without enumerating which drift, so
   the new `file_missing → anchor_missing` arrow rides it unchanged. Re-run the description cap
   test.
6. **`docs/engines/comb-drift.md`** — the reason-code list at `:411` gains `no_fingerprint` and
   its tier; the §"how a verdict is reached" prose must state that a found symbol with NO
   recorded fingerprint is `unverifiable`, not `current`.
7. **`docs/PLANS/AGENT-LOOP-HARNESS-PLAN.md` §13** — its "deferred, with the seam named" entry
   for the anchor-acceptance gap is now built; leave the plan file as the historical record and
   do not rewrite it (it carries unrelated in-flight edits), but the BUGS.md entry records that
   §13's seam is closed.
8. **`hive/app/anchors.py` module docstring**, **`hive/app/drift.py` module docstring** (its
   §3.4 wire-mapping table is the source of BUG-078's spec gap and must now state the three
   arrows plus the ratchet), and **`hive/domain/retirement.py` module docstring** — the last
   one's clause-1b justification is re-stated: 1a's baseline window is narrowed by the deferral
   but NOT closed (first connect, the radius tier, and pre-registration history remain), so 1b is
   still not redundant and must not be deleted by a minimality pass.
9. **`skills/hive-connect-repo/SKILL.md:155`** already says "The separator is `::`, not `:`" and
   explains the consequence; it must now also state that the wrong form is **refused at write**,
   since that paragraph is the operator-facing statement of this rule.
10. **This plan** moves to `docs/PLANS/IMPLEMENTED/` when done.

---

## 9 · Definition of done

1. **`make check` green** — `ruff format --check .`, `ruff check .`, `mypy hive/ --strict`,
   `pytest -m "not embed"`.
2. All three new contract-test files were observed **red on assertions** against the unmodified
   tree before any implementation, and that red-first evidence is recorded.
3. The frozen set (`frozen_paths: tests/contract/**`) was not edited by any implementation step;
   the three new files are the only additions to it, authored in step 0.
4. Every intent in §6 maps to ≥1 green contract test, and every contract test maps back to an
   intent — checked in both directions.
5. All twenty-one mutations in §7 were applied, each broke its named test, and each was restored.
6. `tests/test_purity.py` stays green — `hive/domain/anchor_grammar.py` imports no I/O module,
   nothing in `hive/{matrix,combdrift,edge}/` gained an import of the server stack, and
   `hive/app/drift.py` gained no engine import at all (§3.3(b) D-C).
7. `tests/contract/test_engine_parity.py` stays green **unmodified** — the standing proof that
   the engines were not forked by this change. (§3.2(a) verified no scenario exercises the
   changed branch; §3.3 changes only the server-side mapper, which parity does not cover.)
8. **The completeness table (§4.2) is re-walked against the built tree** and every row's actual
   outcome matches the table, with the single expected exception R2 (BUG-080). A row that
   disagrees is a defect in the build or an error in the table — either way it blocks done.
9. **`/verify`** — the real runtime surface driven, not just green tests: against a live server
   with a registered repo, (a) a `hive_write` carrying `path/file.py:Symbol` returns a `refused`
   envelope naming the clause and stores nothing; (b) the same write with `::` lands, and
   `a:b/c.py::S` also lands; (c) a freshly anchored memory serves `drift: unverifiable` before
   its first mint and `fresh` after; (d) a tick whose range changes the anchored symbol leaves
   the carrier empty and the verdict `unverifiable`; (e) the following quiet tick mints it;
   (f) deleting the anchored FILE and running one tick serves `drift: anchor_missing`, and a
   conscious `hive_prune` on that memory retires it with a `drift:anchor_missing` audit signal.
10. The doc obligations of §8 landed in the same change.
11. No code was written before a human confirmed this plan.
