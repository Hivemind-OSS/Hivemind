"""CV6 — creditctl: trailer scan (host half) + settled-credit ingest (container half).

Fixture repos are REAL git repos built by each test (init → commit → branch → merge /
revert / squash), so classification is pinned against git's actual ancestry and revert
format — never simulated strings. The ★ contracts: merged ⇒ win / reverted ⇒ loss /
branch-only ⇒ unsettled, with NO diff text ever parsed (no --patch in any git argv);
ingest is idempotent on (commit_sha, episode_id) so full-history re-scans are free.
"""
from __future__ import annotations

import io
import json
import subprocess

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.tools import creditctl

_GIT_ID = ("-c", "user.email=t@test", "-c", "user.name=T", "-c", "commit.gpgsign=false")


def _git(repo, *args, date=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo)}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    r = subprocess.run(["git", "-C", str(repo), *_GIT_ID, *args],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _commit(repo, fname, msg, *, date=None) -> str:
    (repo / fname).write_text(fname)
    _git(repo, "add", fname)
    _git(repo, "commit", "-m", msg, date=date)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    _commit(d, "seed.txt", "seed")
    return d


class SpyRun:
    """Records every argv and delegates to the real subprocess — the no-diff-text
    assertion needs the actual commands the scan issued."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append([str(a) for a in argv])
        return subprocess.run([str(a) for a in argv], capture_output=True, text=True)


def _by_sha(rows):
    return {r["commit_sha"]: r for r in rows}


# ── scan classification (★) ─────────────────────────────────────────────────────


def test_scan_classifies_merge_revert_unsettled(repo):
    on_main = _commit(repo, "a.txt", "feat A\n\nHive-Trace: tA")
    _git(repo, "checkout", "-q", "-b", "feat")
    merged = _commit(repo, "b.txt", "feat B\n\nHive-Trace: tB tB2")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "feat")
    reverted = _commit(repo, "c.txt", "feat C\n\nHive-Trace: tC")
    _git(repo, "revert", "--no-edit", reverted)
    _git(repo, "checkout", "-q", "-b", "dangling")
    unsettled = _commit(repo, "d.txt", "feat D\n\nHive-Trace: tD")
    _git(repo, "checkout", "-q", "main")

    spy = SpyRun()
    rows = creditctl.scan_repo(str(repo), run=spy)
    by = _by_sha(rows)
    assert by[on_main]["settled"] and by[on_main]["outcome"] == "win"
    assert by[merged]["settled"] and by[merged]["outcome"] == "win"
    assert by[merged]["trace_ids"] == ["tB", "tB2"]
    assert by[reverted]["settled"] and by[reverted]["outcome"] == "loss"
    assert not by[unsettled]["settled"] and by[unsettled]["outcome"] is None
    # the no-diff-text pin: classification rides ancestry + format fields only
    for argv in spy.calls:
        assert "--patch" not in argv and "-p" not in argv


def test_mirror_bare_clone_resolves_main(repo, tmp_path):
    win = _commit(repo, "a.txt", "feat A\n\nHive-Trace: tA")
    mirror = tmp_path / "scan.git"
    subprocess.run(["git", "clone", "-q", "--mirror", str(repo), str(mirror)],
                   capture_output=True, check=True)
    rows = creditctl.scan_repo(str(mirror))       # no origin/* in a bare mirror
    by = _by_sha(rows)
    assert by[win]["settled"] and by[win]["outcome"] == "win"


def test_squash_carried_trailer_settles_main_sha_only(repo):
    _git(repo, "checkout", "-q", "-b", "feat2")
    e = _commit(repo, "e.txt", "part 1\n\nHive-Trace: tE")
    f = _commit(repo, "f.txt", "part 2\n\nHive-Trace: tF")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--squash", "feat2")
    _git(repo, "commit", "-m", "squashed feature\n\nHive-Trace: tE tF")
    squash_sha = _git(repo, "rev-parse", "HEAD")

    rows = creditctl.scan_repo(str(repo))
    by = _by_sha(rows)
    assert by[squash_sha]["settled"] and by[squash_sha]["outcome"] == "win"
    assert by[squash_sha]["trace_ids"] == ["tE", "tF"]
    assert not by[e]["settled"] and not by[f]["settled"]   # originals stay unsettled


def test_report_flags_aged_unsettled(repo):
    _git(repo, "checkout", "-q", "-b", "stale")
    _commit(repo, "old.txt", "old work\n\nHive-Trace: tOld",
            date="2020-01-01T00:00:00 +0000")
    _git(repo, "checkout", "-q", "main")
    rows = creditctl.scan_repo(str(repo))
    now = 1_600_000_000                                    # well past 2020 + threshold
    summary = creditctl.scan_summary(rows, now=now)
    assert summary["unsettled"] >= 1
    assert summary["aged_unsettled"] >= 1                  # the squash-leak alarm
    fresh = creditctl.scan_summary(rows, now=1_577_836_800 + 3600)   # 1h after commit
    assert fresh["aged_unsettled"] == 0


def test_unknown_main_ref_fails_loud(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    _git(d, "init", "-q", "-b", "trunk")                   # no main/master at all
    with pytest.raises(ValueError):
        creditctl.scan_repo(str(d))


# ── ingest (★ idempotent) ───────────────────────────────────────────────────────


@pytest.fixture()
def store():
    return SqliteEpisodeStore(connect(":memory:"))


def _settled_row(sha="abc1", traces=("tr1",), outcome="win", ts=100):
    return {"commit_sha": sha, "commit_ts": ts, "repo": "team/repo",
            "trace_ids": list(traces), "settled": True, "outcome": outcome}


def test_ingest_idempotent_no_double_credit(store):
    store.record_exposure("tr1", [(3, 0.9), (4, 0.2)], agent_id="a", ts=50)
    r1 = creditctl.ingest(store, [_settled_row()], now=200)
    assert r1["ingested"] == 2 and r1["duplicates"] == 0
    r2 = creditctl.ingest(store, [_settled_row()], now=300)   # full re-scan, same rows
    assert r2["ingested"] == 0 and r2["duplicates"] == 2
    n = store.conn.execute("SELECT COUNT(*) AS c FROM task_outcomes").fetchone()["c"]
    assert n == 2                                             # one row per (sha, eid)


def test_unknown_trace_ids_reported_not_written(store):
    report = creditctl.ingest(store, [_settled_row(traces=("ghost",))], now=200)
    assert report["unknown_traces"] == 1 and report["ingested"] == 0
    n = store.conn.execute("SELECT COUNT(*) AS c FROM task_outcomes").fetchone()["c"]
    assert n == 0


def test_unsettled_then_settled_credits_once(store, repo):
    store.record_exposure("tG", [(7, 0.5)], agent_id="a", ts=50)
    _git(repo, "checkout", "-q", "-b", "wip")
    _commit(repo, "g.txt", "feat G\n\nHive-Trace: tG")
    _git(repo, "checkout", "-q", "main")

    rows = creditctl.scan_repo(str(repo))                     # branch-only: unsettled
    r1 = creditctl.ingest(store, rows, now=100)
    assert r1["ingested"] == 0 and r1["unsettled_skipped"] >= 1

    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "wip")  # now it lands
    r2 = creditctl.ingest(store, creditctl.scan_repo(str(repo)), now=200)
    assert r2["ingested"] == 1
    wins = store.conn.execute(
        "SELECT COUNT(*) AS c FROM task_outcomes WHERE outcome='win' AND episode_id=7"
    ).fetchone()["c"]
    assert wins == 1                                          # exactly one win row


def test_ingest_loss_rows_carry_sign(store):
    store.record_exposure("tr1", [(3, 0.9)], agent_id="a", ts=50)
    creditctl.ingest(store, [_settled_row(outcome="loss")], now=200)
    row = store.conn.execute("SELECT outcome, repo FROM task_outcomes").fetchone()
    assert row["outcome"] == "loss" and row["repo"] == "team/repo"


# ── the module entry (`python -m hive.tools.creditctl ingest --ndjson -`) ───────


def test_main_ingest_reads_ndjson_stdin_reports_json(tmp_path):
    db = str(tmp_path / "shared.db")
    SqliteEpisodeStore(connect(db)).record_exposure("tr1", [(3, 0.9)], agent_id="a", ts=50)
    ndjson = json.dumps(_settled_row()) + "\n"
    out = io.StringIO()
    rc = creditctl.main(["ingest", "--ndjson", "-"],
                        env={"HIVE_STORE__DB_PATH": db},
                        stdin=io.StringIO(ndjson), out=out)
    assert rc == creditctl.EX_OK
    report = json.loads(out.getvalue())
    assert report["ingested"] == 1
    assert report["totals"]["settled_rows"] == 1


def test_main_missing_db_is_config_error():
    rc = creditctl.main(["ingest", "--ndjson", "-"], env={},
                        stdin=io.StringIO(""), out=io.StringIO())
    assert rc == creditctl.EX_CONFIG
