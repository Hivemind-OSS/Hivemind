# ONBOARDING-HARDENING — remove PyPI, git-install the CLI, reconcile hooks, gate the launch

**Status:** DRAFT — awaiting human approval (no code until approved).
**Supersedes:** `REONBOARD-HOOK-RECONCILE-PLAN.md` (folded in as Part B).
**Contract impact:** edits `hive/app/onboard_ref.py` → the pre-commit guard bumps `CONTRACT_VERSION`
v.13 → v.14 and regenerates the keystone golden, once, for the whole change.
**Closes:** BUG-024 (dead PyPI install channel) + BUG-043 (merge-only hook install).
**Spans two repos:** hivemind (owned here) + hive-edge (coupled; the CLI), because PyPI must leave
*the system*, and the actual PyPI install/upgrade machinery lives in hive-edge.

---

## 1. Goal & scope

**Operator goal:** a new production user clones hivemind from GitHub, stands up the server, their
team's agents connect, install the CLI + the rules/role block (+ hooks on Claude Code), and
everything works with the intended functionality — on their OS/environment, assuming nothing that is
only true on the developer's machine.

**New standing constraint (this change):** **our three distributions — `hive-edge`, `matrix`,
`comb-drift` — are never published to, nor installed from, PyPI. Ever.** They ship as vendored wheels
inside the server image and are installed on agents from the public git repo via uv. PyPI as an
install/publish channel for *our* code is removed from the whole system.

Four parts:
- **Part A — remove PyPI + git-install the CLI (hivemind).** Closes BUG-024's hivemind surface.
- **Part B — deterministic hook reconcile (hivemind).** Closes BUG-043.
- **Part C — remove the PyPI install/upgrade/publish machinery (hive-edge, coupled).** The core of the
  purge; planned & executed in the hive-edge repo under its own conventions.
- **Part D — launch-readiness gate.** Surfaces only hard blockers, else `clear for launch`.

Parts A + B ride **one** contract bump (v.13 → v.14).

## 2. The keep-list — legitimate PyPI usage that must NOT be touched

"Remove PyPI" means *our dists*, not the concept. Removing any of these would break the build or
delete a security feature — leave them exactly as-is:

- **`hive/domain/secret_scan.py`** — the `pypi-` token detection pattern (`pypi_token` rule). It scans
  for *leaked PyPI API tokens* of any package; a security feature, unrelated to publishing ours. KEEP.
- **`Dockerfile:34` + `uv.lock` `registry = "https://pypi.org/simple"` sources** — third-party leaf
  deps (`tree-sitter*`, `networkx`, `sqlglot`, …) resolve from PyPI normally. Only our three dists are
  vendored/git. KEEP every one of the 28 lockfile sources and the Dockerfile comment.
- **`pyproject.toml:74`** (hivemind) — the comment noting `matrix` on PyPI is a squatted project, which
  is *why* we vendor. It reinforces the no-index stance. KEEP.
- **History** — `CHANGELOG.md`, `CONTEXT/BUGS.md`, `docs/PLANS/IMPLEMENTED/**`, and the **frozen
  `SERVER_INSTRUCTIONS_V06`/snapshots in `tests/app/fixtures/contract_corpus_v06.py`**. Never rewrite a
  historical record. Add a *new* CHANGELOG entry; update only the *live-probe* tuple (unit 40), not the
  frozen v.06 baseline.

## 3. Verified evidence base (checked this session against the real repos)

| Fact | Evidence |
|------|----------|
| uv installs hive-edge directly from git, no manual clone; resolves `comb-drift`/`matrix` from `#subdirectory=packages/*` — **no PyPI** | `uv pip compile git+…Hive-edge` → rewrote both engines to git subdirs, exit 0 |
| **uv is mandatory** — pip/pipx read `[project.dependencies]` only → would resolve engines from PyPI (where `matrix` is squatted) | `[tool.uv.sources]` is uv-only |
| `@master`/HEAD = `fcf79d3` = **0.8.0** (reduced CLI: comb-drift+matrix) = the server's vendored wheels | `git ls-remote`; `vendor/wheels/*-0.8.0`; `__version__="0.8.0"` |
| `@release` tag = `1db4a468` = **stale 0.7.0**, still carries `hive-census`+`hive-verifier` (deleted in 0.8.0) | `git ls-remote`; `uv pip compile @release` |
| The PyPI machinery lives in hive-edge: `_install_spec` (PyPI argv), `upgrade` (PyPI self-upgrade), version-keyed pins, `hive-edge upgrade` CLI verb | `hive_edge/launch.py:1,65,160`; `cli.py:764,858`; tests `test_launch.py`/`test_packaging.py`/`test_cli_reduction.py` |
| hivemind PyPI-install references to rewrite | `onboard_ref.EDGE_CLI`, `README.md`, `HIVE-ADMIN.md`, `llms.txt`, `skills/hive-connect-team/SKILL.md`, `TODOS.md:265`, `contract_corpus_v06` unit 40, `test_onboard_ref` |

## 4. Design review (design-it-twice; `/software-design-review` framework, references loaded)

**Reasoned scores (anchored, not measured):** design complexity **3/10**; cognitive load **3/10**;
information leakage **2/10** (repo URL + ref single-owned); extensibility/fit **8/10** (git-install
cleanly reverses the dead PyPI decision; hook reconcile matches the rules-block idiom);
agent-navigability **8/10** (tests + a live gate enforce it, not prose).

**Decisions (options → winner):**
- **Install:** `uv tool install git+<repo>@<ref>` **[winner]** vs. PyPI (removed) vs. manual clone
  (unnecessary — uv fetches internally).
- **Update:** `uv tool upgrade hive-edge` **[winner]** vs. `hive-edge upgrade` (rejected — PyPI-bound;
  being removed in Part C). Directive warns off it.
- **Pin:** `@release` + **move the stale tag to the shipped lockstep** **[winner]** vs. `@master` (no
  release gate) vs. fixed SHA (a per-release edit inside the served contract → a bump each release).
  `@release` is one stable gate matching the project's `release`-ref convention and makes agent
  version == server version by construction. Fallback `@master` if the operator declines tag discipline.
- **uv mandatory, stated with the reason;** **PATH via `uv tool update-shell`** (never hardcode
  `~/.local/bin`); **degraded mode preserved** (Law 6 — no uv/GitHub → no-op; core capture/recall
  unaffected; server backfill still mints); **repo URL/ref single-owned** (`EDGE_REPO_URL`/`EDGE_REPO_REF`).
- **hive-edge machinery:** *remove*, don't repoint-to-git. Under uv-owns-install, `_install_spec` +
  the version-keyed self-upgrade are dead; deleting them is smaller-surface (Law: remove dead code).
  The one live non-PyPI job `upgrade` also does — unwiring retired census git-hooks — is re-homed or
  dropped (fresh users have none); decided in the hive-edge planning pass.

## 5. Intents → contract → validation (traceability)

| # | Intent | Contract (given/when/then) | Validation |
|---|--------|----------------------------|------------|
| A1 | Git-install directive | fresh agent reads the served install → `uv tool install git+<repo>@<ref>`, never PyPI | `test_edge_cli_installs_from_git_repo_not_pypi` + doc-mirror asserts |
| A2 | uv-native update | updating → `uv tool upgrade hive-edge`, warns off `hive-edge upgrade` | `test_edge_cli_update_is_uv_native_and_avoids_dead_verb`; `test_reonboard_names_the_edge_upgrade_step` (updated) |
| A3 | uv mandatory + why | directive states uv required (workspace engines) | `test_edge_cli_states_uv_is_required` |
| A4 | Degraded mode | no hive-edge → supported no-op (Law 6) | `test_edge_cli_preserves_degraded_mode` |
| A5 | Pin coherence | `@<ref>` resolves on the public repo to ≥ MIN_EDGE_VERSION **and == the server lockstep** | **Part D** live install-spike |
| A6 | No PyPI path survives, system-wide | no served/doc/test/CLI path installs-or-publishes our dists via PyPI (keep-list §2 excepted) | `test_no_dead_pypi_or_upgrade_reference` + repo-wide grep in Part D |
| B1–B6 | Hook reconcile (orphan-removal, no-dup/idempotent, operator-preserve, restart, predicate-covers-served, allowlist exact-set) | as Part B | the three Part-B tests |
| D | Journey has no hard blocker | clone→server→connect→install→role→hooks→function has no dev-only assumption / OS break / dead target / silent failure / version+PyPI incoherence | Part D emits blockers or `clear for launch` |

## 6. Implementation — Part A (PyPI removal + git-install, hivemind)

1. **`hive/app/onboard_ref.py`** — add `EDGE_REPO_URL = "https://github.com/Hivemind-OSS/Hive-edge"`
   and `EDGE_REPO_REF = "release"` (single owners); **rewrite `EDGE_CLI`** to: uv-mandatory (+ the
   workspace-engine reason), first-connect `uv tool install git+{EDGE_REPO_URL}@{EDGE_REPO_REF}` +
   `uv tool update-shell` for PATH, update `uv tool upgrade hive-edge` with an explicit "do NOT run
   `hive-edge upgrade`" note, the rollback pin reframed in uv terms, degraded-mode clause preserved.
   Rides the uncapped payload — length is fine.
2. **Doc mirrors → git-install** (drop every "install/upgrade from PyPI" for our CLI):
   `README.md`, `HIVE-ADMIN.md` §8, `llms.txt` (line 16), `skills/hive-connect-team/SKILL.md`.
   `hive connect`'s breadcrumb points at HIVE-ADMIN §8 (BUG-042) → covered, no `cli.py` change.
3. **`TODOS.md:265`** — drop the cancelled "PyPI publish" from the release-tail sequencing note (the
   tail is now: move `release` tag → pin → dogfood cutover → re-onboard).
4. **Tests/fixtures:** update `test_onboard_ref` EDGE_CLI tests (flip `assert "git+" not in e` →
   `in e`, swap the PyPI-install assertion — comment that this reverses the 0.7.0-era "ships from PyPI"
   decision); update `contract_corpus_v06.py` **unit 40's live-probe tuple** (`uv tool install
   hive-edge` → `uv tool install git+`), exactly as BUG-041 did — leave the frozen v.06 snapshot.

## 7. Implementation — Part B (hook reconcile, hivemind) — folded from the prior plan

5. Add `HIVEMIND_HOOK_COMMAND_MARKERS = ("hive-edge hook ", "[hivemind]")` +
   `is_hivemind_owned_hook_command()` (single owner; all 6 served commands match).
6. Rewrite `ONBOARDING_PROCEDURE` step 2 → remove-then-insert keyed on the predicate (prune emptied
   groups/events, insert the served set, leave operator hooks), allowlist exact-set, restart-to-activate;
   step 4 re-onboard clause names the reconcile + restart and uses `uv tool upgrade hive-edge` (A2).
7. Add the three Part-B tests (`test_every_served_hook_command_is_hivemind_owned`,
   `test_reonboard_hook_reconcile_removes_orphans_dedups_and_preserves_operator_hooks` with the in-test
   reconcile oracle keyed on the shipped predicate, `test_procedure_directs_remove_then_insert_reconcile_restart_and_allowlist_exact_set`).
8. Run `pytest tests/app/test_onboard_ref.py -q`, then the full suite. **RULE-2 (Law 7):** break each
   new guard (git-install assertion, reconcile remove step, restart clause, uv-required clause,
   predicate coverage), watch the named test red, restore.

## 8. Implementation — hivemind docs & version

9. `CONTEXT/INTERACTIONS.md` v.13→**v.14** + edge-install line (git, uv-required) + [S6]/[S7]
   hook-reconcile note. `CONTEXT/THEORY.md` §5 — the hook reconcile sentence + "agent CLI installs
   git-from-public-repo via uv; server ships hermetic vendored wheels; PyPI is not an install/publish
   channel for our dists." `CONTEXT/BUGS.md` — **BUG-024 → SOLVED** (git-install + system-wide PyPI
   removal) and **BUG-043 → SOLVED**. `CHANGELOG.md` — one new entry (PyPI removed, git-install, hook
   reconcile, contract v.14). Leave all historical entries.
10. **Commit** via `/commit --audit`. The guard bumps v.13→v.14 + regenerates the keystone golden
    in-commit (commit where `hive` imports; clear `__pycache__` on a stale-`.pyc` false-RED).

## 9. Implementation — Part C (hive-edge PyPI machinery removal, coupled repo)

Executed in `../hive-edge` under **its** CLAUDE.md/THEORY/BUGS + its own planning pass (do not
hand-edit blindly from here). Specified surface & target:

11. **Remove the PyPI install/upgrade machinery:** `hive_edge/launch.py` `_install_spec` + the
    version-keyed `upgrade`/pin protocol; the `upgrade` verb in `hive_edge/cli.py:764,858`. Under
    uv-owns-install these are dead. **Decide** the retired-census-hook unwiring (`launch.py:160`'s
    second job): re-home into a small dedicated verb, or drop it (fresh installs have no old hooks;
    the migration is largely complete) — the hive-edge planning pass calls it.
12. **Tests:** delete/reframe `tests/edge/test_launch.py` (PyPI install-spec), `test_cli_reduction.py`
    (upgrade PyPI protocol); **reframe** `test_packaging.py` intent 10 from "PyPI-ready" to "builds a
    clean wheel/sdist for **vendoring**" (the wheel is still built — `scripts/vendor_edge.py` consumes
    it — just never published).
13. **Docs/metadata:** `hive-edge/README.md:14` (drop "self-upgrades from PyPI"); PyPI-taxonomy
    classifiers in `pyproject.toml` are optional low-priority cleanup (harmless in an unpublished
    wheel). **Audit for any publish path** — `.github/workflows/*`, release runbook, `twine`/`hatch
    publish` — and remove it; there must be no CI or doc that publishes our dists.
14. **Move the `release` tag** to the shipped lockstep (`fcf79d3`, 0.8.0) and push, so
    `uv tool install …@release` gives agents 0.8.0 == the server's vendored version (today it gives
    stale 0.7.0). **Load-bearing; do before agents re-onboard.**

## 10. Part D — launch-readiness gate (final step; the done bar)

Dispatched adversarial subagent (read-only, `/prompt`-briefed with all context), run last. Walks the
production journey: `git clone` hivemind → `hive up` (vendored wheels, Docker + `hive` console script
the only documented prereqs) → agents connect (loopback/tunnel) → install role/rules + hooks
(OS-agnostic, reconciled) → install CLI (`uv tool install git+…@release`, uv present, PATH set,
degrades safely) → core capture/recall/write works with **no** CLI, mint/verify/hooks work where
uv+GitHub present.

**Surface a finding ONLY if it absolutely breaks/prevents the journey. Blocker rubric:**
- a **dev-machine-only assumption** in shipped/served content (`/home/null`, the repo `.venv` hive-edge,
  the `python -m` workaround as the *only* path, loopback-only, an undocumented local tool);
- an **OS break** on a supported production OS with no guard (cf. BUG-031/032/033);
- a **dead/incoherent target** (a served install/ref/command resolving to nonexistent/squatted, or a
  version incoherent with the server — e.g. `@release` still stale);
- **any surviving PyPI install-or-publish path for our dists** (keep-list §2 excepted);
- a **silent failure** (required step fails with no error and no degraded path);
- a **contract/version incoherence** leaving an onboarded agent nonfunctional.

**NOT blockers (do not surface):** style/naming, optional-enhancement gaps that degrade safely
(no GitHub/uv → documented degraded mode is fine for the core journey), the retired `hive-edge upgrade`
rough edge once Part C removes it.

**Output contract:** a numbered blocker list — each with `file:line`, the journey step it breaks, and
the OS/env — **or** the single line `clear for launch` and nothing else.

## 11. Definition of done

Contract suite green (A1–A6, B1–B6); full suite green (both repos); every new guard shown red under its
mutation and restored; docs reconciled (INTERACTIONS/THEORY/BUGS-024+043/CHANGELOG + the doc mirrors +
TODOS); landed at contract **v.14** with the keystone golden regenerated; **no PyPI install/publish path
for our dists anywhere** (keep-list §2 preserved); the `release` tag moved to the shipped lockstep;
**and Part D returns `clear for launch`**.

## 12. Rollout & dispatch

`/update-dogfood-server` ships v.14; the next `hive_*` beacon (v.14 ≠ installed marker) triggers each
agent's re-onboard (git-install of the CLI + reconciled hooks). **Move the `release` tag (step 14)
BEFORE agents re-onboard.** Dogfood caveat: this box runs richer local hook substitutes the predicate
treats as operator hooks — reconcile by hand (keep local OR adopt served, not both); the fleet is clean.

**Dispatch:** hivemind Parts A+B+docs = one focused implementer (< 250k ctx). hive-edge Part C = its own
planning pass in that repo (two-repo, dead-code removal + test reframe). Part D = a final adversarial
subagent. Given two repos + a contract bump, if executed together this is `/daisy-chain-build` scale;
if hive-edge is sequenced separately, single dispatched agents per repo suffice.
