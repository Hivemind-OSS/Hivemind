# U5-EDGE-ABSORB — one repo, first-party engines, zero cross-repo machinery

**Target state, one line:** the hivemind repo self-contains every engine (`hive.matrix`,
`hive.combdrift`, `hive.edge`) as first-party subpackages of the one `hive` dist — the
hive-edge GitHub repo, the vendored-wheel channel, `scripts/vendor_edge.py`, and all
version-lockstep machinery stop existing; server behavior is byte-identical.

Status: **DRAFT — awaiting human confirmation. No code until confirmed (hard gate).**

Lineage: U4 §2 D3 kept the vendored wheels + `hive-edge mint`/`verify` subprocess seam and
named "absorb the mint/verify core into hivemind" as the right final shape, deferred. This
plan is that follow-on, extended to FULL self-containment (all three packages + tests), so
the `Hivemind-OSS/Hive-edge` GitHub repo can be archived/deleted and consumers need only
the Hivemind repo.

---

## 0. Scope summary

| # | Subsystem | Change |
|---|-----------|--------|
| 1 | Engine code | `packages/matrix` → `hive/matrix/`; `packages/comb-drift/combdrift` → `hive/combdrift/`; `hive_edge` → `hive/edge/` — byte-preserving move + mechanical aliased-import rewrite |
| 2 | Meta registry | `hive_edge/meta_registry.py` → `hive/domain/meta_registry.py` (pure; `kinds.py` one-source pattern); `hivemind:` owner prefixes dropped |
| 3 | CLI seam | `hive-edge` console script KEPT, re-pointed to `hive.edge.cli:main`; `sync.py` argv/discovery byte-untouched; frozen CT suite untouched |
| 4 | Agent-side vestiges | `hooks.py`, `hook` verb, `worktree-delta` verb NOT ported (dead post-U4); join the exit-2 pinned set |
| 5 | Vendoring | `vendor/wheels/`, `scripts/vendor_edge.py`, `[tool.uv.sources]` engine pins, Dockerfile wheel steps — all DELETED |
| 6 | Dependencies | engine third-party deps land in the `sync` extra (tree-sitter<0.26 + 7 grammars, networkx, sqlglot); `tree-sitter-sql` dev-only (image behavior parity) |
| 7 | Typing | absorbed code brought to `mypy --strict`; `combdrift.*`/`matrix.*` overrides DELETED (measured debt: ~271 mostly-mechanical errors) |
| 8 | Versions | lockstep guard + `KNOWN_HIVEMIND_MIN_EDGE_VERSION` machinery dies; `COMBDRIFT_VERSION`/`matrix.__version__` stay as frozen provenance literals; `ENGINE_VERSION` importlib-metadata read → source literal |
| 9 | Tests | 789 engine tests absorbed under `tests/{matrix,combdrift,edge}/`; 8 matrix language goldens + regen tool ported; purity fence extended; 3 new CT modules |
| 10 | Docs | README / HIVE-ADMIN / OPERATIONS / CONTRIBUTING / llms.txt / TODOS + THEORY §2/§5/§9.15 + INTERACTIONS [E1][E2][E4] |

**Behavioral invariants (the whole point):** fingerprint tokens byte-identical; verify
verdict JSON identical; receipts identical except `matrix` provenance `engine_version` no
longer able to silently read `0+unknown`; stored `fp_meta` tokens from existing volumes
verify identically; frozen `tests/contract/` (CT-1…14) passes UNTOUCHED.

---

## 1. Grounding (what was read / measured)

Full reads: U4 plan §2 D3 + §5–§8, `CONTEXT/THEORY.md`, `CONTEXT/INTERACTIONS.md`
([E1][E2][E4] are the moving entries), `CONTEXT/BUGS.md` (BUG-034…055 in full; BUG-022/045
by section), `pyproject.toml`, `Makefile`, `Dockerfile`, both repos' layouts. Two full
surface maps (hive-edge inventory; hivemind consumption points) — key facts the plan
rides on:

- Only TWO verbs cross the subprocess seam (`hive-edge mint|verify`), owned by
  `hive/app/sync.py:_mint`/`_verify_anchor` (argv, 600 s timeout, fail-open `{}`/
  `unverifiable` contracts). There is **zero** `import hive_edge` anywhere in hivemind.
- `matrix`/`combdrift` are ALREADY library-imported (lazily, call-time) by
  `hive/census/{engines,join,receipt,diff}.py`, `hive/verifier/verify.py`, and
  `hive/app/sync.py` (`gitenv.clean_git_env`, `FINGERPRINT_VERSION`). Laziness is
  load-bearing: `matrix` reads `MATRIX_OUT` once at import; the empty-registry tick must
  stay byte-inert (CT-9), and `engines.py:51` guards on `sys.modules["matrix"]`.
- hive-edge @ `f94e1a7` (clean, pushed): `hive_edge` 1,230 LOC (cli 869, hooks 257,
  meta_registry 92), `matrix` 7,469 LOC / 25 modules, `combdrift` 3,981 LOC / 20 modules;
  tests 10,702 LOC / 789 fns; 8 committed per-language extraction goldens.
- mypy `--strict` probe over the engine sources: 27 (combdrift) + 202 (matrix) + 42
  (hive_edge) error lines; top codes `[type-arg]` 109, `[no-untyped-def]` 55 — mechanical.
  `tree_sitter` 0.25.2 + `sqlglot` ship `py.typed`; `types-networkx` already a dev dep.
- Dockerfile already uses deps-early/source-late (`COPY pyproject.toml` at line 8;
  `pip install ".[embed]"` resolves deps before any source COPY) — the same pattern
  absorbs the engine deps once the bare engine names leave the `sync` extra.
- setuptools `packages.find include=["hive*","tests*"]` — subpackages auto-discovered
  recursively (no BUG-040 explicit-list trap).

Bugs designed against: BUG-034 (clean_git_env stays single-owned at
`hive/matrix/gitenv.py`), BUG-035/036/038/039 (dialect family — code moved byte-preserving,
their regression suites ported), BUG-037 (version gates preserved verbatim; parity CT),
BUG-040 (packaging pin test), BUG-041/051 (vendoring rot class — machinery deleted
outright), BUG-045 (PyPI never a channel; absorbing kills the bare-name-resolution trap),
BUG-053 (gate env stays `--extra dev`-clean; no heavy dep enters the gate).

Law compliance (THEORY): §5 engine-subprocess idiom UNCHANGED (mint/verify still spawn,
never under the lock); meta envelope law §5.1–7 UNTOUCHED (same keys, same token versions,
mint current-version-only); Law 5 (mirrors/drift cache rebuildable) untouched; Law 7 —
every deleted guard's mutation marker dies WITH its subsystem or is re-homed (§7 checklist).
THEORY revisions this plan owns: §10 below.

---

## 2. Design decisions (design-it-twice results)

**D1 — Landing shape.** *Winner:* subpackages of the ONE `hive` dist — `hive/matrix/`,
`hive/combdrift/`, `hive/edge/` — completing the U1 precedent (`hive-census`→`hive/census`,
`hive-verifier`→`hive/verifier`). *Loser A (keep top-level `matrix`/`combdrift` in-repo):*
zero rewrites, but pollutes the top-level namespace with a PyPI-squatted name (BUG-045),
keeps a multi-dist story alive, breaks the house convention. *Loser B (deep-merge into
census/etc.):* re-shapes the most regression-prone code (the BUG-035/038/039 family) —
exactly the fork risk U4 D3 deferred; the engines keep independent identity because token
prefixes (`combdrift-fp/`, `matrix-subgraph-fp/`) and receipt provenance blocks name them.

**D2 — Import rewrite law (mechanical, alias-preserving; bodies byte-identical).**
- `import matrix` → `import hive.matrix as matrix` · `import combdrift` →
  `import hive.combdrift as combdrift` (all attribute chains in bodies keep working)
- `import matrix.paths` → `import hive.matrix.paths` (paired with the alias import at the
  same site: `engines.py:107-108` becomes `import hive.matrix as matrix` +
  `import hive.matrix.paths`)
- `from matrix import X` → `from hive.matrix import X`; `from matrix.sub import X` →
  `from hive.matrix.sub import X`; same for `combdrift`
- engine-INTERNAL absolute imports get the same prefix rewrite (`from matrix.model import`
  → `from hive.matrix.model import`), NOT converted to relative — pure prefix sed, lowest
  regression surface
- `sys.modules["matrix"]` (`hive/census/engines.py:51`) → `sys.modules["hive.matrix"]`
  (named edge case; its guard test moves with it)
- `hive_edge/cli.py:33 import combdrift` → `import hive.combdrift as combdrift`; its lazy
  `import matrix` sites → `import hive.matrix as matrix`
- TYPE_CHECKING imports: same prefix rule.

**D3 — CLI seam.** *Winner:* keep the `hive-edge` console script name — pyproject
`[project.scripts]` gains `hive-edge = "hive.edge.cli:main"`. `sync.py` discovery
(`Path(sys.executable).parent/"hive-edge"`) and argv are byte-untouched; the FROZEN CT
conftest (`is_mint_argv` checks `"hive-edge" in argv[0]`) never needs an edit. *Loser
(`python -m hive.edge.cli` for census-idiom parity):* forces edits inside the frozen suite
— inverting the freeze for zero behavioral gain. *Loser (in-process import):* violates the
§5 engine-subprocess law and the lock discipline; eager tree-sitter loads enter the serve
process. The `comb-drift` console script is NOT re-registered (nothing shells it; the
module stays importable / `python -m hive.combdrift.cli`).

**D4 — Agent-side vestiges die.** `hive_edge/hooks.py`, the `hook` verb group, the
`worktree-delta` verb, `_wd_state`, and the `wd-*.hash` session-state writes are NOT
ported: U4 deleted their only callers (client-side hooks); in-container nothing invokes
them; the dead-code hygiene rule requires removal in the same change. `hook` and
`worktree-delta` join `census`/`audit`/`upgrade` in the argparse-rejected exit-2 pinned
set. KEPT: `mint` (incl. `--branch-scope` — it mints the registered `git/branches` key),
`verify` (incl. `--branches`/`branch_scoped` reader), the operator `graph
update|radius|fp` group, `--version` (now printing source literals), `HIVE_EDGE_HOME`
state dir (the matrix graph cache under `state/matrix/<digest>/` still powers the
subgraph-fp pipeline), and `last-error.log` fail-open logging.

**D5 — Meta registry re-homes to `hive/domain/meta_registry.py`.** Pure frozen rows —
purity-gate compatible; sits beside `domain/meta.py` (the token-envelope reader), the
`kinds.py` one-source precedent. Owner strings drop the `hivemind:` prefix (all owners are
in-repo now); the registry ratchet suite moves to `tests/domain/test_meta_registry.py` and
its `hivemind:`-special-casing is deleted. PRAMANA's pending `sheaf/relation` row becomes a
one-file domain edit instead of a cross-repo release. *Loser (keep beside the CLI in
`hive/edge/`):* preserves adjacency nobody needs; the registry is contract vocabulary, not
engine code. THEORY §5 clause 7 + §9.15 updated to the new path (§10).

**D6 — Full `mypy --strict` on absorbed code.** Measured: ~271 error lines, dominated by
bare generics + missing annotations; third-party stubs exist (`py.typed` in tree_sitter +
sqlglot; `types-networkx` installed). The `combdrift.*`/`matrix.*`
`ignore_missing_imports` overrides are DELETED and the "overrides are
stub-availability-only, never first-party" rule stays literally true. *Loser (first-party
relaxation overrides):* violates that documented rule and plants Any-seams (no feedback
signal) inside first-party code. Escape hatch: if a specific module resists strict typing
beyond mechanical effort, escalate to the human — do NOT silently add an override.

**D7 — Version identity.** The four-site lockstep + `KNOWN_HIVEMIND_MIN_EDGE_VERSION` +
their guard tests DIE (single repo, single dist). What remains are ENGINE PROVENANCE
literals riding durable artifacts: `hive/combdrift/version.py:COMBDRIFT_VERSION = "0.9.0"`
and `hive/matrix/__init__.py:__version__ = "0.9.0"` — frozen at the absorption value,
re-documented as provenance constants (they stamp receipts via
`provenance["combdrift"]`/`["matrix"]`, consumed by
`hive/domain/change_evidence.py:version_stamp` — the fail-closed rider gate). Token
versions (`FINGERPRINT_VERSION="1"`, `SUBGRAPH_FP_VERSION="1"`, `BRANCHES_VERSION="1"`)
are untouched (meta envelope law). `matrix/version.py:ENGINE_VERSION` stops reading
`importlib.metadata.version("matrix")` (which would silently degrade to `"0+unknown"`
post-move) and becomes `ENGINE_VERSION = __version__` from the package literal — a
strict improvement (kills a silent-degradation trap). `hive-edge --version` output keeps
its 3-line shape, now printing the literals.

---

## 3. The move map (normative)

### 3.1 Code moves (source @ hive-edge `f94e1a7` → destination)

| Source | Destination | Notes |
|---|---|---|
| `packages/matrix/matrix/**` (25 modules) | `hive/matrix/**` | byte-preserving + D2 rewrite; `version.py` ENGINE_VERSION → literal (D7) |
| `packages/comb-drift/combdrift/**` (20 modules) | `hive/combdrift/**` | byte-preserving + D2 rewrite |
| `hive_edge/cli.py` | `hive/edge/cli.py` | D2 rewrite; `hook`/`worktree-delta` dispatch + `_wd_state` + wd-state writes REMOVED (D4); `_print_version` reads literals; meta_registry import (if any) → `hive.domain.meta_registry` |
| `hive_edge/__init__.py` | `hive/edge/__init__.py` | docstring rewritten (in-image engine CLI front); `__version__` DELETED (no separate dist) |
| `hive_edge/meta_registry.py` | `hive/domain/meta_registry.py` | owner strings lose `hivemind:` prefix (D5) |
| `hive_edge/hooks.py` | — NOT PORTED | D4 |
| `packages/matrix/tools/regen_golden.py` | `scripts/regen_matrix_goldens.py` | paths updated to `tests/matrix/golden/`; documented in `scripts/CLAUDE.md` (local file) |

### 3.2 Test moves

| Source | Destination | Notes |
|---|---|---|
| `packages/matrix/tests/**` (incl. `golden/*.json`, `fixtures/**`, `normalize.py`, `fixtures_manifest.py`) | `tests/matrix/**` | add `__init__.py`; sibling-helper imports become package imports (`from tests.matrix.normalize import …`); D2 prefix rewrite |
| `packages/comb-drift/tests/**` (25 files) | `tests/combdrift/**` | add `__init__.py`; D2 rewrite |
| `tests/edge/**` (9 files) | `tests/edge/**` | D2 rewrite; DROPPED: `test_hooks.py`, hook/wd cases in `test_cli.py`, `test_no_census_imports.py` (superseded by the purity fence), `test_packaging.py` (superseded by CT-16), the lockstep + floor guard tests (`test_workspace_members_share_one_lockstep_version`, `test_edge_version_matches_the_hivemind_floor_file`); `test_cli_reduction.py` EXTENDED: `hook` + `worktree-delta` now also exit 2 |

Excluded from the move: `packages/comb-drift/build/**` (stale build artifact),
`graphify-out/`, hive-edge's `pyproject.toml`/`uv.lock`/CI/`CHANGELOG.md`/`README.md`/
`CONTRIBUTING.md`/`CLAUDE.md`/`KNOWN_HIVEMIND_MIN_EDGE_VERSION` (two-repo machinery; the
absorption commit message + CHANGELOG entry record the source SHA `f94e1a7`).

### 3.3 hivemind-side consumer edits (the complete list; D2 rewrite only)

`hive/census/engines.py` (:26-28, :51 `sys.modules` key, :59-64, :107-113, :128, :334,
:342-350) · `hive/census/join.py` (:24-25, :98, :108-110) · `hive/census/receipt.py`
(:36, :70) · `hive/census/diff.py` (:80) · `hive/verifier/verify.py` (:50, :359) ·
`hive/app/sync.py` (:153-155, :186-189) · `tests/census/**`, `tests/verifier/**`,
`tests/domain/test_change_evidence.py`, `tests/domain/test_meta.py` (import sites only).
`tests/sync/**` and FROZEN `tests/contract/**` import no engine module — **zero edits**;
any red in the frozen suite is a stop-and-escalate defect, never an edit.

### 3.4 Packaging / build / gate edits

- `pyproject.toml`: `sync` extra drops the three bare engine names, gains
  `networkx>=3.4`, `tree-sitter>=0.25,<0.26` (comment: `<0.26` load-bearing — matrix AST
  goldens built on 0.25.x), `tree-sitter-python>=0.23`, `tree-sitter-javascript`,
  `tree-sitter-typescript`, `tree-sitter-go`, `tree-sitter-rust`, `tree-sitter-c`,
  `tree-sitter-cpp` (grammar pins copied verbatim from the two engine pyprojects, capped
  `<0.26`), `sqlglot>=30`; `dev` extra gains `tree-sitter-sql>=0.3` (goldens; image
  deliberately excludes it — behavior parity). `[project.scripts]` gains
  `hive-edge = "hive.edge.cli:main"`. `[tool.uv.sources]` engine pins DELETED (whole
  table if then empty). mypy overrides for `combdrift.*`/`matrix.*` DELETED. `uv lock`
  regenerated.
- `Dockerfile`: line 15 becomes `RUN pip install --no-cache-dir ".[embed,sync]"` (deps
  resolve from the pyproject-only layer — engine third-party deps cache BEFORE the model
  bake; safe now the bare names are gone); lines 29-36 (wheelhouse comment + `COPY
  vendor/wheels/` + `RUN pip install /wheels/*.whl`) DELETED; line 42 comment updated
  (engines are first-party; the extra is already satisfied). Layer economics preserved:
  a `hive/` source edit re-runs only the final source install.
- `Makefile`: unchanged (the gate's `--extra dev` env now resolves engine deps from PyPI
  via the lock, no index trickery).
- `.dockerignore` / `tests/container/test_dockerfile.py` /
  `tests/container/test_dockerignore_no_secret.py`: vendor-wheels assertions replaced with
  their absence + the new line-15 shape.
- `vendor/wheels/*` + `scripts/vendor_edge.py` DELETED (Step 6, after parity is proven).

---

## 4. Intents → contracts → tests (traceability; new modules join `tests/contract/`)

New CT modules are authored FIRST (Step 0), observed RED, then frozen with the existing
suite (`frozen_paths: tests/contract/**`). Existing CT-1…14 are never edited.

| # | Intent | Contract (given / when / then) | Scenarios covered | Contract test |
|---|--------|-------------------------------|-------------------|---------------|
| 1 | Self-containment: the repo alone builds, checks, and ships | Given a checkout of hivemind ONLY (no sibling hive-edge, no wheelhouse), when the dev env syncs and `make check` runs and the wheel is built, then everything is green and the wheel contains `hive/matrix`, `hive/combdrift`, `hive/edge` + the 8 language goldens' engine can load | wheel subpackage presence (BUG-040 pin); `vendor/wheels/` absent; no `[tool.uv.sources]` engine pin; no bare `import matrix`/`import combdrift`/`import hive_edge` anywhere in `hive/` or `tests/` (AST-level grep gate); no committed reference to `Hive-edge` GitHub URL / `vendor_edge` outside CHANGELOG/BUGS/docs-PLANS history (allowlist) | `CT-15 tests/contract/test_selfcontained.py` |
| 2 | Behavioral invariance of the engine seam | Given the golden fixture repo minted with the 0.9.0 wheels BEFORE the move (goldens committed as fixtures), when the in-repo `hive-edge mint`/`verify` run against the same fixture tree, then tokens are byte-equal and verdict JSON is field-equal | `combdrift/fp` byte-equality; `matrix/subgraph_fp` byte-equality; verify verdicts for: fresh anchor, signature-changed (`stale/signature_changed` + delta), symbol-missing, prose anchor (`unverifiable/not_a_code_anchor` — BUG-022), incomparable future-version fp (`unverifiable`, BUG-037), off-branch (`branch_scoped`); single-source-root fixture layout (BUG-035/038 dialect) | `CT-16 tests/contract/test_engine_parity.py` |
| 3 | Agent-side deletion is total | Given the installed CLI, when `hive-edge hook …` or `hive-edge worktree-delta …` is invoked, then argparse rejects (exit 2, like `census`/`upgrade`); `hive.edge.hooks` is not importable; no served string names them | hook exit 2; worktree-delta exit 2; import error pinned; served-contract DEAD_TOKENS sweep still green (existing `tests/app/test_contract.py` untouched) | `CT-17 tests/contract/test_agentside_deletion.py` |
| 4 | The live seam still works end-to-end in the image posture | (existing intents — unchanged contracts) sync tick mints absent fps via the real CLI at the canonical tip; drift materializes per (repo, tip); frozen suite proves it | all CT-5 / CT-6 / CT-9 scenarios, UNTOUCHED and green post-move (the decisive regression harness) | existing frozen `CT-5/6/9` |
| 5 | Meta envelope law survives the registry move | Given the relocated registry, when the ratchet suite runs, then coverage/agreement/retention ratchets hold with in-repo owner paths; `hive_health(include_meta_versions)` histogram unchanged | 4 keys present; owner paths resolve in-repo; prefix-only carve-out untouched; registry validation rejects malformed rows | `tests/domain/test_meta_registry.py` (ported ratchets) + existing health suite |
| 6 | Engine test corpus preserved | Given the absorbed suites, when `make check` runs, then all ported matrix/combdrift/edge tests pass in ONE pytest run (789 minus the D4-dropped set) | 8 language extraction goldens; BUG-025 `update()≡build_graph()` contract; BUG-035/036/038/039 regression suites; BUG-037 omission tests; subgraph-fp spec suite; multilang e2e | `tests/{matrix,combdrift,edge}/**` (unit layer) |
| 7 | Strict-typing floor extends to the engines | Given the gate env, when `mypy hive/ --strict` runs, then absorbed packages pass with NO first-party override | the `combdrift.*`/`matrix.*` override entries are gone from pyproject (asserted in CT-15's config sweep) | `make check` typecheck leg + CT-15 |

Property-based coverage: the ported suites' existing generative tests move as-is; no new
property surface is introduced (the input spaces — token envelopes, extraction — are
already covered by goldens + the registry ratchets).

**Edge-case bar:** CT-16 must cover the FULL verify verdict matrix above (not just
fresh/stale), including the BUG-022/035/037/038 branches — those bugs are exactly where a
re-port would silently fork.

---

## 5. External-interaction inventory (boundary → declared failure behavior → failure test)

| Boundary | Failure behavior | Test |
|---|---|---|
| `hive-edge mint` subprocess (argv unchanged) | `{}`/nonzero/unparseable ⇒ silent skip, carry over — UNCHANGED (`sync.py:_mint`) | frozen CT-5; CT-16 parity |
| `hive-edge verify` subprocess (argv unchanged) | nonzero/unparseable ⇒ `unverifiable`, never false-stale/fresh — UNCHANGED | frozen CT-6; CT-16 |
| `python -m hive.census.cli build` subprocess | fail-OPEN leg skip — UNCHANGED | frozen CT-9 |
| git subprocess (mirror/worktree) | fail-OPEN per repo per tick; `clean_git_env` on every spawn — now from `hive.matrix.gitenv` (BUG-034 owner moves WITH the code, single-owned) | existing sync suites + `tests/matrix/test_gitenv.py` (ported identity pin) |
| PyPI at image-build/dev-sync time (tree-sitter grammars, networkx, sqlglot) | build-time only; runtime stays hermetically offline (`HF_HUB_OFFLINE=1` untouched); a resolution failure fails the BUILD loudly, never the runtime | Dockerfile line-15 shape pinned in `tests/container/test_dockerfile.py`; cold-start check |
| env `MATRIX_OUT` (read once at `hive.matrix` import) | unchanged name + semantics; late pin ⇒ `EngineError` — the `sys.modules["hive.matrix"]` guard preserves the pinned-before-first-import contract | existing `tests/census` guard test (rewritten key) |
| env `HIVE_EDGE_HOME` (CLI state dir) | unchanged name + semantics (volume-local `edge-home`); absent ⇒ `~/.hive-edge` default | ported `tests/edge` state-dir tests |
| Required env vars | NONE added, NONE removed — no new secrets; the existing `token_env` boot probe (EX_CONFIG) untouched | existing CT-10 secrets fail-fast probe |
| Deleted machinery (`vendor_edge.py`, wheels, floor file) | ceases to exist; any reference is a CT-15 grep-gate failure | CT-15 |

---

## 6. Implementation plan (numbered; order is dependency-safe)

**Step 0 — parity goldens + contract suite (RED first).**
(a) With the CURRENT build (0.9.0 wheels still installed), mint the CT-16 golden fixtures:
a committed fixture tree (`tests/contract/fixtures/engine_parity/` — a small repo with a
Python pkg exercising the single-source-root layout + one multi-root variant + a
signature-change pair) and the expected token/verdict JSON captured via the REAL
`hive-edge mint`/`verify` into `tests/contract/fixtures/engine_parity/goldens.json`.
(b) Author CT-15/16/17 asserting the POST-move world (imports `hive.matrix`…, wheel
contents, exit-2 pins). Observe RED (module-not-found / vendor still present / hook verb
still exits 0). Freeze `tests/contract/**`.

**Step 1 — move the engines** (pure moves + D2 rewrite; no hivemind consumer edits yet).
`git mv`-equivalent copies from `../hive-edge` @ `f94e1a7` per §3.1; apply the D2 prefix
rewrite across the moved files; D4 trims in `hive/edge/cli.py`; D7 literal in
`hive/matrix/version.py`; registry to `hive/domain/meta_registry.py` with owner-prefix
drop. Add `__init__.py` docstrings stating each package's role + fail direction (house
style).

**Step 2 — move the engine tests** per §3.2 (package `__init__.py`, helper imports,
`test_cli_reduction.py` extension). Wire `scripts/regen_matrix_goldens.py`.

**Step 3 — rewrite hivemind consumers** per §3.3 (the ~15 sites; `sys.modules` key named
explicitly). Extend `tests/test_purity.py`: the census/verifier fence clause now names
`hive.matrix`/`hive.combdrift` as the permitted engine imports; add the same
must-not-import-server-stack fence for `hive/matrix/**`, `hive/combdrift/**`,
`hive/edge/**`; the dead `hive_census`/`hive_verifier` name-ban gains
`hive_edge`/bare-`matrix`/bare-`combdrift`.

**Step 4 — packaging + gate** per §3.4 (pyproject extras/scripts/sources/overrides,
`uv lock`, Dockerfile, dockerignore + container tests). Then the strict-typing pass over
`hive/{matrix,combdrift,edge}/` (~271 mechanical errors; no behavior edits — annotations
and generics only; any semantic fix a type error exposes is escalated, not silently
changed).

**Step 5 — green the world.** Full `make check`; frozen suite must pass UNTOUCHED
(any frozen red = stop + escalate); CT-15/16/17 flip GREEN.

**Step 6 — deletions sweep** (only now): `vendor/wheels/`, `scripts/vendor_edge.py`,
stale comments naming them. Grep-verify via CT-15's gate. Law-7 audit: mutation markers
inside moved engine code are preserved verbatim (they moved with their tests); the
lockstep/floor guard tests died WITH their subsystem (two-repo coupling) — no marker
re-home needed; `_incomparable_fp_version`'s wheel-absent `except` branch is KEPT
byte-identical (still reachable: a base install without the `sync` extra).

**Step 7 — docs + theory** (same change): README (§architecture, repo-layout line, tree),
HIVE-ADMIN §"Staying current" (vendored-wheels paragraph → first-party engines),
OPERATIONS release runbook step 1 (vendor refresh step DELETED), CONTRIBUTING (wheelhouse
sentence → plain `uv sync --extra dev`), llms.txt blurb, TODOS.md cross-repo `../hive-edge`
paths → in-repo paths, CHANGELOG entry (records source SHA `f94e1a7`), THEORY + INTERACTIONS
per §10, `docs/engines/{matrix,comb-drift}.md` (the two engine READMEs, trimmed of
install/workspace/PyPI sections — reference docs for the moved engines). `graphify update .`

**Step 8 — verification.** `/verify` (runtime surface): compose build from the repo alone
→ boot → register a scratch repo → observe the real tick mint fps (`hive-sync/minted`
provenance present) → break an anchor on the canonical line → observe `anchor_changed`
drift + retirement-gate accept — the U4 §9 flow, re-driven on the wheel-less image.
Cold-start check (fresh clone → README bootstrap → `make check`) — REQUIRED: deps +
bootstrap changed. Confirm image size delta is noise (same third-party set, minus the
wheelhouse layer).

Order rationale: goldens must be minted while the OLD engines are still installed (Step 0
before Step 1); consumers rewritten only after the new packages exist (3 after 1); vendor
deletion only after parity + green (6 after 5), so a red anywhere leaves a working tree.

---

## 7. Deletion inventory (what stops existing)

hivemind-side: `vendor/wheels/{hive_edge,matrix,comb_drift}-0.9.0-*.whl`,
`scripts/vendor_edge.py`, `[tool.uv.sources]` engine pins, the `sync` extra's three bare
names, the `combdrift.*`/`matrix.*` mypy overrides, Dockerfile wheelhouse lines, the
vendored-wheels doc story (README/HIVE-ADMIN/OPERATIONS/CONTRIBUTING/llms.txt).
hive-edge-side (dies with the repo): the workspace + lockstep machinery
(4-site version, guard test), `KNOWN_HIVEMIND_MIN_EDGE_VERSION` + floor test,
`guard-master-source.yml`, `vendor`-facing packaging tests, `hooks.py` + `hook` +
`worktree-delta` (+ tests), the `comb-drift` console-script registration, the
`uv tool install git+…@release` story, the `release` tag process.
GitHub: `Hivemind-OSS/Hive-edge` archived or deleted (user action, §9).

## 8. What deliberately does NOT change

The mint/verify subprocess argv, timeout, and fail-open contracts; the `hive-edge`
console-script name and discovery; sync tick structure, caps, and lock discipline; ALL
token formats, versions, and the four registered meta keys; receipt schema + provenance
block shapes; `MATRIX_OUT`/`HIVE_EDGE_HOME` env semantics; the frozen CT-1…14 suite
(byte-identical); the hermetic-offline runtime posture; `tau_serve`/gate/lifecycle —
everything U4 shipped; PyPI remains a non-channel for OUR dist (BUG-045 posture — the
`hive` dist is still distributed as this repo, not published).

## 9. Operational consequences (state plainly at rollout)

- **No store/volume migration:** stored `fp_meta` tokens verify identically (same engines,
  same versions); mirrors + drift cache are rebuildable caches either way. `hive upgrade`
  / `/update-dogfood-server` from a pre-U5 image is a plain rebuild.
- **Sequencing for GitHub deletion:** land U5 on `development` → merge to `master` →
  update the dogfood server → confirm a scratch-repo tick mints/verifies → THEN
  archive/delete `Hivemind-OSS/Hive-edge`. Archiving (read-only) preserves history and
  inbound links at zero cost; deletion is the stated intent — either satisfies this plan.
  The local `~/Desktop/work/hive-edge` checkout is the cold archive until the user removes it.
- **PRAMANA:** its specs' one residual hive-edge touch (a registry-row-only 0.10.0
  release + re-vendor) collapses to "add one row to `hive/domain/meta_registry.py`" —
  strictly simpler; the pramana spec should be realigned when that work starts (out of
  scope here).
- Image build now resolves engine third-party deps from PyPI in the pyproject-only layer
  (they were already PyPI-resolved transitively — no new supply-chain surface, one fewer
  local artifact channel).

## 10. THEORY.md / INTERACTIONS.md revisions this plan owns (docs step)

THEORY: §2 map — `census/` line's "there is no agent-side edge CLI; mint, verify, and
drift all live in these server engines" stays true and gains the engine rows
(`matrix/ · combdrift/ · edge/` as server engine subpackages); §5 meta envelope law
clause 7 registry path → `hive/domain/meta_registry.py`; §5 distribution posture — "ships
hermetic vendored engine wheels (`vendor/wheels/`, refreshed by `scripts/vendor_edge.py`)"
→ "the engines are first-party subpackages of the one `hive` dist; no wheelhouse, no
external engine repo" (BUG-045 PyPI note kept); §9 checklist item 15 registry path.
INTERACTIONS: [E1] anchor text unchanged in behavior ("in-image `hive-edge mint` CLI" —
still literally true) with anchors re-pointed (`hive/edge/cli.py`); [E2] same; [E4]
"the engines are the vendored `combdrift` (call-shape) + `matrix` (dependency-neighborhood
digest)" → "the first-party `hive.combdrift` + `hive.matrix` engines", anchor
`vendor/wheels/` → `hive/combdrift/`, `hive/matrix/`. Load-bearing-defaults block:
unchanged (no default moves).

## 11. Risks (stated, mitigated, accepted)

1. **Silent engine-behavior fork during the move** (the D3-deferral reason): mitigated
   three ways — byte-preserving move + alias-only import rewrite (D2), the ported 789-test
   corpus incl. every BUG-03x regression suite, and CT-16 byte-parity goldens minted from
   the pre-move wheels. Residual: accepted.
2. **Strict-typing pass introduces a semantic edit under the guise of an annotation**:
   rule in Step 4 — annotations/generics only; any type error requiring a behavior change
   escalates to the human. Enforced by review discipline + the parity/regression suites.
3. **Frozen-suite pressure**: if any CT-1…14 test reds during the build, the freeze holds
   — stop and escalate (a red there means the move broke real behavior; the suite is never
   edited to pass).
4. **One `make check` run gets heavier** (~789 more tests, tree-sitter parse-heavy):
   accepted — they ran per-member before; the gate stays one verb. If wall-time becomes a
   problem, test-tier markers are a follow-on, not this change.
5. **`matrix`/`combdrift` names remain PyPI-squatted / unrelated upstream**: irrelevant
   post-absorb (nothing resolves those names from an index anymore); the early-layer
   `pip install ".[embed,sync]"` is safe exactly because the bare names leave the extra.

## 12. Handoff

Size: ~16.7k LOC moved + ~15 consumer sites + packaging/docs — well over a single-context
build: **/daisy-chain-build**, with `frozen_paths: ["tests/contract/**"]` (CT-1…17 after
Step 0). Definition of done: full `make check` green (incl. absorbed suites + strict mypy
with zero first-party overrides) + CT-15/16/17 green with CT-1…14 byte-untouched +
`/verify` runtime pass (Step 8 flow) + cold-start check pass. After implementation, move
this file to `docs/PLANS/IMPLEMENTED/`.
