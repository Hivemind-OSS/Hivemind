"""Shared substrate for the sync contract tests: REAL tmp git origins (a bare
remote + a work clone pushed into), the REAL sqlite store (prod factory, prod
thread posture), and a SyncService builder wired exactly like the entrypoint
does it (ChangeEvidenceService with ``ranges=store``, one global lock). Receipts
in these tests are built by the REAL ``python -m hive.census.cli build``
subprocess — never a mock of git or of the census.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.change_evidence import ChangeEvidenceService

DIM = 4
ANCHOR = "app.py::greet"                 # the path::Symbol the seeded episodes anchor on

# The test harness's own git calls must not inherit a hook-style GIT_DIR the test
# under way may have planted in os.environ (that leak is exactly what the code
# under test must survive — the harness stays out of the blast zone).
_GIT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
             "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR")


def harness_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_VARS}


def git(cwd: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, env=harness_env())
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


class Origin:
    """A real remote: a bare origin plus a work clone commits are pushed from."""

    def __init__(self, root: Path, branch: str = "main") -> None:
        self.branch = branch
        self.bare = root / "origin.git"
        self.work = root / "work"
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", f"--initial-branch={branch}",
                        str(self.bare)], check=True, capture_output=True,
                       env=harness_env())
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.work)],
                       check=True, capture_output=True, env=harness_env())
        git(self.work, "config", "user.email", "sync-test@example.invalid")
        git(self.work, "config", "user.name", "sync-test")
        git(self.work, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        self.commit("app.py", 'def greet(name):\n    return "hi " + name\n', "seed")
        self.push()

    @property
    def url(self) -> str:
        return str(self.bare)

    def commit(self, rel: str, content: str, msg: str) -> str:
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", msg)
        return self.sha("HEAD")

    def push(self, *refs: str, force: bool = False) -> None:
        argv = ["push", "-q"] + (["--force"] if force else []) + ["origin"]
        git(self.work, *argv, *(refs or (self.branch,)))

    def sha(self, ref: str = "HEAD") -> str:
        return git(self.work, "rev-parse", ref).stdout.strip()

    def origin_sha(self, ref: str) -> str:
        return git(self.bare, "rev-parse", ref).stdout.strip()

    def set_pr_ref(self, number: int, sha: Optional[str] = None) -> None:
        git(self.bare, "update-ref", f"refs/pull/{number}/head",
            sha or self.origin_sha(f"refs/heads/{self.branch}"))


@pytest.fixture
def origin(tmp_path: Path) -> Origin:
    return Origin(tmp_path / "remote")


@pytest.fixture
def store() -> SqliteEpisodeStore:
    # prod factory + prod thread posture (the daemon shares one lock-serialized conn)
    return SqliteEpisodeStore(connect(":memory:", check_same_thread=False))


def seed_episode(store: SqliteEpisodeStore, text: str, anchor: str = ANCHOR,
                 ts: int = 10, meta: str = "") -> int:
    eid, _ = store.stage(text=text, weight=1.0, tags="", proposed_by="w", ts=ts,
                         anchor=anchor, meta=meta)
    assert store.approve(eid, "h", np.eye(DIM, dtype=np.float32)[0],
                         expected_version=0, approved_ts=ts)
    return eid


def evidence_rows(store: SqliteEpisodeStore, kind: str = "change_outcome") -> list[dict]:
    return [dict(r) for r in store.conn.execute(
        "SELECT episode_id, kind, actor, ts, payload FROM evidence_events "
        "WHERE kind=? ORDER BY id", (kind,))]


def payloads(store: SqliteEpisodeStore) -> list[dict]:
    return [json.loads(r["payload"]) for r in evidence_rows(store)]


def meta(store: SqliteEpisodeStore, key: str) -> Optional[str]:
    row = store.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def make_service(url: str | Origin, store: SqliteEpisodeStore, tmp_path: Path,
                 run=None, **cfg_kw):
    """A SyncService wired the entrypoint way: real store as reader/appender/ranges,
    one fresh global lock, mirror under tmp_path. ``run`` (optional) injects the
    spawn seam — an observing/replacing double; None keeps the real default."""
    from hive.app.config import SyncConfig
    from hive.app.sync import SyncService
    canonical_ref = cfg_kw.pop("canonical_ref", "")
    cfg = SyncConfig(repo_url=url.url if isinstance(url, Origin) else url,
                     mirror_dir=str(tmp_path / "mirror"), **cfg_kw)
    evidence = ChangeEvidenceService(reader=store, appender=store,
                                     now=lambda: 424242, ranges=store)
    return SyncService(cfg, store, evidence, threading.Lock(),
                       **({"run": run} if run is not None else {}),
                       canonical_ref=canonical_ref)


def build_receipt(repo: Path, base: str, head: str, out_dir: Path,
                  *, repo_id: Optional[str] = None) -> dict:
    """A REAL receipt envelope over base..head via the census CLI subprocess."""
    out = out_dir / f"receipt-{base[:8]}-{head[:8]}.json"
    argv = [sys.executable, "-m", "hive.census.cli", "build", "--repo", str(repo),
            "--base", base, "--head", head, "--out", str(out), "--propagate"]
    if repo_id:
        argv += ["--repo-id", repo_id]
    proc = subprocess.run(argv, capture_output=True, text=True, env=harness_env())
    assert proc.returncode == 0, f"census build failed: {proc.stderr.strip()}"
    return json.loads(out.read_text(encoding="utf-8"))
