"""CT-4 — the candidate-evaluation contract (intent 4).

A PR head ref appearing or moving is evaluated on the next tick: ONE receipt over
``merge-base..pr-head`` built with ``--ref refs/pull/N/head``, the verifier running
inside the (optionally uv-provisioned) child PATH, ingested pre_merge with the
verdict DERIVED by the service. First connect baselines every existing head without
evaluating (the PR-storm direction); a revision re-evaluates; a closed PR is
silent; a hang is killed at the timeout with no receipt and the ledger untouched;
``verify_candidates=False`` leaves the leg without a handle; and a candidate
verify row is NEVER visible to a canonical-scoped ``last_verified`` read.

Every test drives a REAL tmp origin (bare + pushes + real ``refs/pull/N/head``)
through ``SyncService.tick()`` with the real sqlite store and REAL subprocess
census builds — the verifier actually runs the candidate tree's tests. The
default candidate tree is a GENUINE single-source-root package layout (ALL
parsed source under ``pkg/``, the in-corpus test importing via the package
path), so the flagship promotion test doubles as the BUG-039 end-to-end
(package-dialect receipts must join); the flagship's second arm keeps the flat
root-level shape as the offset-"" regression dual.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

import hive.app.sync as sync_mod
from hive.adapters.index_exhaustive import build_index
from hive.app.sync import default_run, normalized_repo_id
from hive.domain.lifecycle import (
    PROVISIONAL, QUARANTINED, DemandRule, LifecycleService,
)

from tests.sync.conftest import (
    DIM, evidence_rows, git, harness_env, make_service, meta, seed_episode,
)

META_PR_HEADS = "sync:pr_heads"
META_EVALUATED = "sync:candidates_evaluated"
META_REFUSED = "sync:candidates_refused"
META_LAST_ERROR = "sync:last_error"

CANDIDATE_ANCHOR = "pkg/mod.py::risky"

# The default candidate dialect is the BUG-039 package shape: ALL parsed source
# under pkg/, imports via the package path, only config at the repo root. The
# [tool.pytest.ini_options] table anchors the spawned run's rootdir at the
# candidate root (a bare [project] table is not a config anchor under pytest 9);
# mod.py carries a second symbol so the file's source_file index is never a
# single-node degenerate case.
PYPROJECT = ('[project]\nname = "candidate"\nversion = "0.1.0"\n\n'
             '[tool.pytest.ini_options]\ntestpaths = ["pkg"]\n')
PYPROJECT_UV = PYPROJECT + '\n[tool.uv]\npackage = false\n'
MOD_V1 = ('def risky(name):\n    return "hi " + name\n\n\n'
          'def steady(name):\n    return risky(name)\n')
MOD_BREAKING = ('def risky(name, mode):\n    return "hi " + name + mode\n\n\n'
                'def steady(name):\n    return risky(name, "!")\n')
MOD_ADDITIVE = ('def risky(name, mode=""):\n    return "hi " + name + mode\n\n\n'
                'def steady(name):\n    return risky(name)\n')
TEST_V1 = ('from pkg.mod import risky\n\n'
           'def test_risky():\n    assert risky("x") == "hi x"\n')
TEST_FIXED = ('from pkg.mod import risky\n\n'
              'def test_risky():\n    assert risky("x", "!") == "hi x!"\n')
TEST_SLEEPY = ('import time\n\nfrom pkg.mod import risky\n\n'
               'def test_risky():\n    time.sleep(60)\n'
               '    assert risky("x") == "hi x"\n')

# The flat root-level dual (offset "" — the pre-existing working dialect), kept
# as the flagship's explicit regression arm after the symbol-seeding change.
FLAT_PYPROJECT = '[project]\nname = "candidate"\nversion = "0.1.0"\n'
FLAT_LIB_V1 = 'def risky(name):\n    return "hi " + name\n'
FLAT_LIB_BREAKING = 'def risky(name, mode):\n    return "hi " + name + mode\n'
FLAT_TEST_V1 = ('from lib import risky\n\n'
                'def test_risky():\n    assert risky("x") == "hi x"\n')

LAYOUTS: dict[str, dict] = {
    "package": {"pyproject": PYPROJECT,
                "seed": {"pkg/__init__.py": "", "pkg/mod.py": MOD_V1,
                         "pkg/test_mod.py": TEST_V1},
                "anchor": CANDIDATE_ANCHOR,
                "breaking": {"pkg/mod.py": MOD_BREAKING}},
    "flat": {"pyproject": FLAT_PYPROJECT,
             "seed": {"lib.py": FLAT_LIB_V1, "test_lib.py": FLAT_TEST_V1},
             "anchor": "lib.py::risky",
             "breaking": {"lib.py": FLAT_LIB_BREAKING}},
}


@pytest.fixture(autouse=True)
def _verifier_tools_on_path(monkeypatch):
    """The deploy image bakes the verifier's tools (pytest) onto PATH; give the
    spawned children the same guarantee here regardless of how the dev suite was
    invoked (running .venv/bin/pytest does not put .venv/bin on PATH)."""
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep
                       + os.environ.get("PATH", ""))


# ── candidate-repo scaffolding (real trees, real PR refs) ──────────────────────

def seed_project(origin, pyproject: str | None = None,
                 layout: str = "package") -> str:
    """The candidate project on the tracked branch. The default is the GENUINE
    single-source-root package shape (BUG-039's precondition): every parsed
    source file lives under pkg/ — Origin's root-level app.py is removed — with
    the in-corpus test importing via the package path and only config at the
    repo root, so matrix roots the graph at pkg/ and the receipt must bridge
    the dialect. ``layout="flat"`` keeps the offset-"" dual (root-level source)
    as the regression arm."""
    spec = LAYOUTS[layout]
    (origin.work / "pyproject.toml").write_text(pyproject or spec["pyproject"])
    for rel, content in spec["seed"].items():
        path = origin.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if layout == "package":
        (origin.work / "app.py").unlink()  # root source would flip the dialect
    git(origin.work, "add", "-A")
    git(origin.work, "commit", "-qm", "candidate project")
    origin.push()
    sha = origin.sha("HEAD")
    if layout == "package":
        # Pin the premise the docstring claims — the e2e verifier's finding was
        # exactly a "single-root" fixture that wasn't: any parsed source outside
        # pkg/ silently reconverges this tree to the flat/offset-"" dialect.
        tree = git(origin.work, "ls-tree", "-r", "--name-only", sha).stdout
        stray = [p for p in tree.splitlines()
                 if p.endswith(".py") and not p.startswith("pkg/")]
        assert not stray, f"single-root premise broken by root-level source: {stray}"
    return sha


def open_pr(origin, number: int, files: dict[str, str]) -> str:
    """Commit ``files`` on the PR's own branch (created off main on first use,
    extended on revision) and publish it at ``refs/pull/<number>/head``."""
    branch = f"pr{number}"
    if git(origin.work, "checkout", "-q", branch, check=False).returncode != 0:
        git(origin.work, "checkout", "-q", "-b", branch, "main")
    for rel, content in files.items():
        path = origin.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(origin.work, "add", "-A")
    git(origin.work, "commit", "-qm", f"pr{number} revision")
    sha = origin.sha("HEAD")
    origin.push(f"{branch}:refs/pull/{number}/head")
    git(origin.work, "checkout", "-q", "main")
    return sha


def close_pr(origin, number: int) -> None:
    git(origin.bare, "update-ref", "-d", f"refs/pull/{number}/head")


def merge_base(origin, a: str, b: str) -> str:
    return git(origin.work, "merge-base", a, b).stdout.strip()


def quarantine_dont(store, text: str, anchor: str, ts: int = 10) -> int:
    """A captured prohibition: approved-status, quarantined-trust, dont-polarity —
    exactly what hive_capture lands and what the L4 rung may later promote."""
    eid, _ = store.stage(text=text, weight=1.0, tags="", proposed_by="w", ts=ts,
                         anchor=anchor, polarity="dont")
    assert store.complete(eid, np.eye(DIM, dtype=np.float32)[0],
                          expected_version=0, trust=QUARANTINED, last_active_ts=ts)
    return eid


def pr_heads_meta(store):
    raw = meta(store, META_PR_HEADS)
    return None if raw is None else json.loads(raw)


def pre_merge_payloads(store) -> list[dict]:
    bodies = [json.loads(r["payload"]) for r in evidence_rows(store)]
    return [b for b in bodies if b["phase"] == "pre_merge"]


def pre_merge_range_count(store) -> int:
    return store.conn.execute(
        "SELECT COUNT(*) FROM ingested_ranges WHERE phase='pre_merge'").fetchone()[0]


class RunSpy:
    """The injectable Run seam, observing: every spawn is recorded (argv, env,
    timeout) and delegated to the real ``default_run`` — git and the verifier stay
    real. ``intercept_build=True`` swallows census builds with rc=1 instead (the
    seam's replace direction), so env/argv hygiene is assertable without paying
    for a build."""

    def __init__(self, intercept_build: bool = False) -> None:
        self.calls: list[tuple[list[str], dict[str, str], object]] = []
        self.intercept_build = intercept_build

    def __call__(self, argv, env=None, timeout=None):
        self.calls.append((list(argv), dict(env or {}), timeout))
        if self.intercept_build and "hive.census.cli" in argv:
            return subprocess.CompletedProcess(list(argv), 1, stdout="",
                                               stderr="intercepted by RunSpy")
        return default_run(argv, env=env, timeout=timeout)

    def candidate_builds(self):
        return [c for c in self.calls if "hive.census.cli" in c[0] and "--ref" in c[0]]

    def uv_syncs(self):
        return [c for c in self.calls if c[0][:2] == ["uv", "sync"]]


# ── CT-4: the seven named contract scenarios ───────────────────────────────────

def test_pr_head_evaluated(origin, store, tmp_path):
    """PR opened: the next tick builds ONE receipt over merge-base..pr-head with
    --ref refs/pull/1/head and ingests it pre_merge — verdict DERIVED from the
    decided failing run, range recorded, baseline advanced, no error."""
    seed_project(origin)
    eid = seed_episode(store, "risky trips on widened signatures", CANDIDATE_ANCHOR)
    svc = make_service(origin, store, tmp_path)
    svc.tick()                                    # first connect
    assert pr_heads_meta(store) == {}             # baselined: zero PRs yet
    main_tip = origin.sha("main")
    head = open_pr(origin, 1, {"pkg/mod.py": MOD_BREAKING})
    svc.tick()

    bodies = pre_merge_payloads(store)
    assert len(bodies) == 1
    body = bodies[0]
    assert body["phase"] == "pre_merge"
    assert body["verdict"] == "fail"              # derived, never caller-asserted
    assert body["tag"] == "bounded-estimate"      # scoped (non-authoritative) run
    assert body["ref"] == "refs/pull/1/head"
    repo_key = body["repo"]
    assert repo_key == normalized_repo_id(origin.url)
    mb = merge_base(origin, main_tip, head)
    assert store.already_ingested_range(repo_key, mb, head, "pre_merge")
    assert pr_heads_meta(store) == {"1": head}
    assert meta(store, META_EVALUATED) == "1"
    assert meta(store, META_LAST_ERROR) is None


@pytest.mark.parametrize("layout", ["package", "flat"])
def test_helped_fuel_promotes(origin, store, tmp_path, layout):
    """The flagship, end to end with ZERO human/agent action: a dont-memory whose
    warned anchor drifts breaking under a PR whose reached test fails ⇒ the HELPED
    row lands mechanically, and the L4 verified-win rung promotes the quarantined
    memory at the next demand tick with the competitor veto held. The "package"
    arm runs on a GENUINE single-source-root tree (all parsed source under pkg/,
    package-path imports) — the BUG-039 end-to-end: the package-dialect receipt
    must join. The "flat" arm is the offset-"" regression dual: root-level
    source must keep deciding after the symbol-seeding change."""
    seed_project(origin, layout=layout)
    eid = quarantine_dont(store, "dont widen risky's signature — callers pass "
                                 "one arg", LAYOUTS[layout]["anchor"])
    svc = make_service(origin, store, tmp_path)
    svc.tick()
    head = open_pr(origin, 1, LAYOUTS[layout]["breaking"])
    svc.tick()

    helped = evidence_rows(store, "outcome_verified_helped")
    assert [r["episode_id"] for r in helped] == [eid]
    body = json.loads(helped[0]["payload"])
    assert body["verdict"] == "fail" and body["ref"] == "refs/pull/1/head"
    assert body["stamp"]["head_sha"] == head      # SHA-bound corroboration
    # The join happened in the REPO dialect (the BUG-039 seam): an unbridged
    # package receipt speaks "mod.py::risky", which cannot match this anchor.
    anchor_path, _, anchor_symbol = LAYOUTS[layout]["anchor"].partition("::")
    assert body["matched"] == {"path": anchor_path, "symbol": anchor_symbol,
                               "level": "symbol"}

    # The next demand tick: demand alone cannot promote (m astronomically high) —
    # the SHA-bound verified win does, competitor veto consulted against the index.
    life = LifecycleService(
        store=store, index=build_index("exhaustive", DIM),
        rule=DemandRule(demand_m=10**9, demand_tau=0.55, competitor_tau=0.95),
        now=lambda: 5000, demand_window_s=10**6, quarantine_ttl_s=10**7,
        provisional_ttl_s=10**7, enabled=True, verified_reader=store)
    decisions = dict(life.on_miss(np.eye(DIM, dtype=np.float32)[0], "reader-agent"))
    assert decisions[eid].promote and decisions[eid].reason == "verified_win"
    trust = store.conn.execute(
        "SELECT trust FROM episodes WHERE id=?", (eid,)).fetchone()["trust"]
    assert trust == PROVISIONAL
    promote_audits = [json.loads(r["payload"]) for r in evidence_rows(store, "promote")]
    assert promote_audits and promote_audits[-1]["rule"] == "verified_win"


def test_revision_reevaluated(origin, store, tmp_path):
    """A moved PR head re-evaluates: two ranges land (merge-base..head1 and
    merge-base..head2), and the revision's verdict is its OWN derivation (the
    broken head fails, the fixed head passes)."""
    seed_project(origin)
    seed_episode(store, "risky trips on widened signatures", CANDIDATE_ANCHOR)
    svc = make_service(origin, store, tmp_path)
    svc.tick()
    main_tip = origin.sha("main")
    head1 = open_pr(origin, 1, {"pkg/mod.py": MOD_BREAKING})
    svc.tick()
    head2 = open_pr(origin, 1, {"pkg/test_mod.py": TEST_FIXED})
    svc.tick()

    bodies = pre_merge_payloads(store)
    assert sorted(b["verdict"] for b in bodies) == ["fail", "pass"]
    repo_key = normalized_repo_id(origin.url)
    mb = merge_base(origin, main_tip, head1)
    assert store.already_ingested_range(repo_key, mb, head1, "pre_merge")
    assert store.already_ingested_range(repo_key, mb, head2, "pre_merge")
    assert pr_heads_meta(store) == {"1": head2}
    assert meta(store, META_EVALUATED) == "2"


def test_closed_silent(origin, store, tmp_path):
    """A closed (deleted-ref) PR is silence: the pruned head drops out of the
    baseline, no receipt, no error — nothing to measure, nothing measured."""
    seed_project(origin)
    seed_episode(store, "risky trips on widened signatures", CANDIDATE_ANCHOR)
    head = open_pr(origin, 1, {"pkg/mod.py": MOD_BREAKING})   # exists before first tick
    spy = RunSpy()
    svc = make_service(origin, store, tmp_path, run=spy)
    svc.tick()                                    # first connect: baselined only
    assert pr_heads_meta(store) == {"1": head}
    close_pr(origin, 1)
    svc.tick()
    assert pr_heads_meta(store) == {}             # the entry left with the ref
    assert spy.candidate_builds() == []
    assert pre_merge_payloads(store) == []
    assert meta(store, META_LAST_ERROR) is None


def test_hang_fail_open(origin, store, tmp_path, monkeypatch):
    """A hanging verifier is killed at the constant timeout: no receipt, the range
    ledger untouched, the hung head NOT advanced (the next tick retries), and the
    loop continues to the NEXT candidate in the same tick."""
    seed_project(origin)
    seed_episode(store, "risky trips on widened signatures", CANDIDATE_ANCHOR)
    svc = make_service(origin, store, tmp_path)
    svc.tick()
    head1 = open_pr(origin, 1, {"pkg/mod.py": MOD_BREAKING,
                                "pkg/test_mod.py": TEST_SLEEPY})
    head2 = open_pr(origin, 2, {"README.md": "notes\n"})
    # marker: an unbounded candidate build turns this tick into the 60s sleep —
    # the elapsed bound below reds without the timeout kill.
    monkeypatch.setattr(sync_mod, "_CANDIDATE_TIMEOUT_S", 12)
    started = time.monotonic()
    svc.tick()
    assert time.monotonic() - started < 50
    assert pre_merge_payloads(store) == []        # killed build: no receipt at all
    assert pre_merge_range_count(store) == 0      # ledger unaffected
    heads = pr_heads_meta(store)
    assert "1" not in heads                       # not advanced ⇒ retried next tick
    assert heads.get("2") == head2                # the loop survived past the hang
    assert "candidate" in (meta(store, META_LAST_ERROR) or "")


def test_opt_out(origin, store, tmp_path):
    """verify_candidates=False ⇒ the leg is ABSENT (no handle), not merely idle:
    no discovery, no baseline meta, no counters, no builds — byte-inert."""
    seed_project(origin)
    open_pr(origin, 1, {"pkg/mod.py": MOD_BREAKING})
    spy = RunSpy()
    svc = make_service(origin, store, tmp_path, run=spy, verify_candidates=False)
    assert svc._candidate_leg is None             # the §9.9 shape: OFF ⇒ no handle
    svc.tick()
    svc.tick()
    assert pr_heads_meta(store) is None           # never even baselined
    assert spy.candidate_builds() == []
    assert pre_merge_payloads(store) == []
    assert meta(store, META_EVALUATED) is None


def test_candidate_ref_never_canonical(origin, store, tmp_path):
    """Ref-scoping at the READ side: the candidate evaluation writes its verify row
    under refs/pull/N/head, and a last_verification read scoped to the canonical
    line CANNOT see it — only the unscoped read can."""
    seed_project(origin)
    eid = seed_episode(store, "risky trips on widened signatures", CANDIDATE_ANCHOR)
    svc = make_service(origin, store, tmp_path)
    svc.tick()
    head = open_pr(origin, 1, {"pkg/mod.py": MOD_BREAKING})
    svc.tick()

    stale = evidence_rows(store, "verify_stale")
    assert [r["episode_id"] for r in stale] == [eid]
    body = json.loads(stale[0]["payload"])
    assert body["ref"] == "refs/pull/1/head"      # the measured line rides the row
    # marker: serving candidate rows to a canonical-scoped read is the mutation —
    # a PR-head verification must never stand in for the tracked line's state.
    assert store.last_verification([eid], canonical_ref="main") == {}
    unscoped = store.last_verification([eid])
    assert unscoped[eid][1] == head and unscoped[eid][2] == "stale"


# ── the named guards around the leg (cap, dedupe, first connect, refusal, env) ──

def test_first_connect_baselines_without_evaluating(origin, store, tmp_path):
    """A head existing at FIRST CONNECT is baselined, never evaluated (the
    PR-storm direction); only movement AFTER the baseline is measured."""
    seed_project(origin)
    seed_episode(store, "risky trips on widened signatures", CANDIDATE_ANCHOR)
    head1 = open_pr(origin, 1, {"pkg/mod.py": MOD_BREAKING})
    spy = RunSpy()
    svc = make_service(origin, store, tmp_path, run=spy)
    svc.tick()
    # marker: evaluating pre-existing heads here is the first-connect PR storm.
    assert spy.candidate_builds() == []
    assert pr_heads_meta(store) == {"1": head1}
    assert pre_merge_payloads(store) == []
    assert meta(store, META_EVALUATED) is None

    head2 = open_pr(origin, 1, {"pkg/mod.py": MOD_ADDITIVE})  # movement after baseline
    svc.tick()
    assert len(spy.candidate_builds()) == 1
    assert pr_heads_meta(store) == {"1": head2}
    assert meta(store, META_EVALUATED) == "1"


def test_cap_carries_over(origin, store, tmp_path):
    """At most 5 changed heads are processed per tick, ascending PR number; the
    remainder carries over untouched and drains on the next tick. (Every head here
    is range-deduped, so the cap itself is the only thing under test.)"""
    seed_project(origin)
    spy = RunSpy()
    svc = make_service(origin, store, tmp_path, run=spy)
    svc.tick()
    main_tip = origin.sha("main")
    repo_key = normalized_repo_id(origin.url)
    heads = {}
    for n in range(1, 8):
        heads[str(n)] = open_pr(origin, n, {"README.md": f"doc {n}\n"})
        store.record_ingested_range(
            repo_key, merge_base(origin, main_tip, heads[str(n)]),
            heads[str(n)], "pre_merge", 1)
    svc.tick()
    # marker: without the cap all 7 advance in one tick; without carry-over the
    # second tick leaves 6 and 7 stranded.
    assert set(pr_heads_meta(store)) == {"1", "2", "3", "4", "5"}
    svc.tick()
    assert pr_heads_meta(store) == heads
    assert spy.candidate_builds() == []           # every slot was a dedupe skip
    assert pre_merge_payloads(store) == []


def test_already_ingested_range_skips_build(origin, store, tmp_path):
    """The durable dedupe guard: an exact (repo, merge-base, head, pre_merge) range
    already in the ledger absorbs the candidate BEFORE any build is spawned; the
    baseline still advances (the work is done, not pending)."""
    seed_project(origin)
    spy = RunSpy()
    svc = make_service(origin, store, tmp_path, run=spy)
    svc.tick()
    main_tip = origin.sha("main")
    head = open_pr(origin, 1, {"README.md": "seen already\n"})
    store.record_ingested_range(normalized_repo_id(origin.url),
                                merge_base(origin, main_tip, head), head,
                                "pre_merge", 1)
    svc.tick()
    # marker: dropping the already_ingested_range guard re-runs this build → red.
    assert spy.candidate_builds() == []
    assert pr_heads_meta(store) == {"1": head}
    assert meta(store, META_EVALUATED) is None    # a skip is not an evaluation
    assert pre_merge_payloads(store) == []


def test_refused_receipt_counted(origin, store, tmp_path, caplog):
    """A nothing-decided receipt (no decided execution evidence) is REFUSED by the
    derivation: logged, counted, baseline advanced (never rebuilt forever), and the
    loop continues to the next candidate in the same tick — a refusal is an
    outcome, not a fault."""
    seed_project(origin)
    seed_episode(store, "risky trips on widened signatures", CANDIDATE_ANCHOR)
    svc = make_service(origin, store, tmp_path)
    svc.tick()
    head1 = open_pr(origin, 1, {"README.md": "prose only\n"})
    head2 = open_pr(origin, 2, {"NOTES.md": "more prose\n"})
    with caplog.at_level(logging.INFO, logger="hive.sync"):
        svc.tick()
    assert pre_merge_payloads(store) == []
    assert pre_merge_range_count(store) == 0
    assert pr_heads_meta(store) == {"1": head1, "2": head2}   # advanced, both
    assert meta(store, META_REFUSED) == "2"
    assert meta(store, META_EVALUATED) == "2"
    assert any("refused" in r.getMessage() for r in caplog.records)
    assert meta(store, META_LAST_ERROR) is None


def test_subprocess_env_stripped(origin, store, tmp_path, monkeypatch):
    """The candidate build child env is STRIPPED: clean git env, no HIVE_* keys,
    the token nowhere in env or argv — and the spawn is bounded by the constant
    timeout. (The build is intercepted at the seam; git stays real.)"""
    seed_project(origin)
    monkeypatch.setenv("HIVE_SYNC__TOKEN", "sekrit-token")
    monkeypatch.setenv("HIVE_STORE__DB_PATH", "/data/nope.db")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "hook-leak"))
    spy = RunSpy(intercept_build=True)
    svc = make_service(origin, store, tmp_path, run=spy, token="sekrit-token")
    svc.tick()
    open_pr(origin, 1, {"README.md": "x\n"})
    svc.tick()

    builds = spy.candidate_builds()
    assert len(builds) == 1
    argv, env, timeout = builds[0]
    # marker: an unbounded candidate spawn (timeout None) is the hang mutation.
    assert timeout == sync_mod._CANDIDATE_TIMEOUT_S
    assert not any(k.startswith("HIVE_") for k in env)
    assert "GIT_DIR" not in env
    assert all("sekrit-token" not in v for v in env.values())
    assert all("sekrit-token" not in a for a in argv)
    assert argv[argv.index("--ref") + 1] == "refs/pull/1/head"
    assert argv[argv.index("--repo-id") + 1] == normalized_repo_id(origin.url)
    # the intercepted (failed) build fails OPEN: no advance, fault surfaced
    assert "1" not in (pr_heads_meta(store) or {})
    assert "candidate" in (meta(store, META_LAST_ERROR) or "")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_uv_lock_provisions_env(origin, store, tmp_path):
    """D8: a candidate root carrying uv.lock gets `uv sync --frozen` into a
    per-lock-hash venv whose bin rides the build child's PATH; the SAME lock on a
    later revision reuses the cached venv (no second sync)."""
    (origin.work / "pyproject.toml").write_text(PYPROJECT_UV)
    lock = subprocess.run(["uv", "lock", "--offline", "-q"], cwd=origin.work,
                          capture_output=True, text=True, timeout=120,
                          env=harness_env())
    # dep-less lock is offline-resolvable; an exotic uv/python setup that cannot
    # even do that is an environment gap, not a contract failure
    if lock.returncode != 0:
        pytest.skip(f"uv lock unavailable here: {lock.stderr.strip()[:200]}")
    seed_project(origin, pyproject=PYPROJECT_UV)   # commits pyproject + uv.lock

    spy = RunSpy()
    svc = make_service(origin, store, tmp_path, run=spy)
    svc.tick()
    open_pr(origin, 1, {"README.md": "one\n"})
    svc.tick()

    syncs = spy.uv_syncs()
    assert len(syncs) == 1
    uv_argv, uv_env, uv_timeout = syncs[0]
    assert "--frozen" in uv_argv and "--project" in uv_argv
    assert uv_timeout == sync_mod._CANDIDATE_TIMEOUT_S
    venv = Path(uv_env["UV_PROJECT_ENVIRONMENT"])
    assert venv.is_relative_to(tmp_path)           # per-service cache, volume-local
    assert (venv / "bin" / "python").exists()
    assert not any(k.startswith("HIVE_") for k in uv_env)
    b_argv, b_env, _ = spy.candidate_builds()[-1]
    assert b_env["PATH"].startswith(str(venv / "bin") + os.pathsep)

    open_pr(origin, 1, {"README.md": "two\n"})     # same uv.lock ⇒ same lock hash
    svc.tick()
    # marker: re-syncing here breaks the per-lock-hash cache (one sync per lock).
    assert len(spy.uv_syncs()) == 1
    assert len(spy.candidate_builds()) == 2
