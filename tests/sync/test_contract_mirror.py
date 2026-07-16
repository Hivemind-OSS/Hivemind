"""CT-2 — the mirror + poll contract (intent 2).

Given an armed sync, a tick fetches the tracked branch AND ``refs/pull/*/head``
into the mirror under ``clean_git_env`` (a hook-planted GIT_DIR in the parent env
must never leak into the mirror's git children — the BUG-034 shape). An
unreachable remote is a LOGGED SKIP: the tick returns, ``sync:last_error`` is
noted, and the next tick retries; nothing raises toward the serve path. The
credential never escapes the mirror's remote config — logs and the error meta
are redacted.
"""
from __future__ import annotations

import logging
from pathlib import Path

from hive.app.sync import META_LAST_SYNC_TS, META_TRACKED_REF

from tests.sync.conftest import Origin, git, make_service, meta

META_LAST_ERROR = "sync:last_error"


def test_tick_fetches(origin, store, tmp_path, monkeypatch):
    # a hook-style GIT_DIR leak: every git call under test must strip it
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "bogus-git-dir"))
    origin.set_pr_ref(7)                             # a PR head the fetch must carry
    svc = make_service(origin, store, tmp_path)
    svc.tick()

    mirror = tmp_path / "mirror"
    tip = origin.origin_sha("refs/heads/main")
    assert git(mirror, "rev-parse", "refs/remotes/origin/main").stdout.strip() == tip
    assert git(mirror, "rev-parse", "refs/sync/pr/7").stdout.strip() == tip
    assert meta(store, META_LAST_ERROR) is None      # a clean tick notes no fault

    # the remote moves; the SAME mirror (repair-free) fetches the new tip next tick
    origin.commit("app.py", 'def greet(name):\n    return "yo " + name\n', "move")
    origin.push()
    new_tip = origin.origin_sha("refs/heads/main")
    svc.tick()
    assert git(mirror, "rev-parse", "refs/remotes/origin/main").stdout.strip() == new_tip


def test_unreachable_fail_open(store, tmp_path, caplog):
    """The remote does not exist yet: the tick is a logged skip (no raise, error
    noted), and once the remote comes up the NEXT tick syncs it — serve unaffected.
    The FAILED tick leaves ``sync:last_sync_ts`` unset (nothing synced — the
    timestamp must not lie) and ``sync:tracked_ref`` unset (nothing resolved);
    the recovering clean tick stamps both."""
    remote_root = tmp_path / "remote"
    url = str(remote_root / "origin.git")            # nothing there yet
    svc = make_service(url, store, tmp_path, now=lambda: 31_337)
    with caplog.at_level(logging.WARNING, logger="hive.sync"):
        svc.tick()                                   # ← re-raising here breaks fail-open
    assert meta(store, META_LAST_ERROR)              # the fault is surfaced, not fatal
    assert meta(store, META_LAST_SYNC_TS) is None    # a failed tick never advances it
    assert meta(store, META_TRACKED_REF) is None     # nothing resolved, nothing invented
    assert any("sync" in r.name for r in caplog.records)
    assert not (tmp_path / "mirror" / ".git").exists()

    origin = Origin(remote_root)                     # the remote comes up
    svc.tick()                                       # the next tick retries and succeeds
    tip = origin.origin_sha("refs/heads/main")
    assert git(tmp_path / "mirror", "rev-parse",
               "refs/remotes/origin/main").stdout.strip() == tip
    assert meta(store, META_LAST_SYNC_TS) == "31337"  # the clean tick stamps the seam clock
    assert meta(store, META_TRACKED_REF) == "main"


def test_token_never_logged(store, tmp_path, caplog):
    """An https clone failure whose git stderr echoes the rewritten URL must reach
    logs and ``sync:last_error`` REDACTED — the credential lives only in the mirror's
    remote config, never in any observable surface."""
    svc = make_service("https://127.0.0.1:9/owner/repo.git", store, tmp_path,
                       token="sekrit-token-123")
    with caplog.at_level(logging.DEBUG, logger="hive.sync"):
        svc.tick()                                   # connection refused, fast + offline
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "sekrit-token-123" not in joined
    assert "sekrit-token-123" not in (meta(store, META_LAST_ERROR) or "")


def test_authenticated_url_shapes():
    """The token rides ONLY an https remote URL (the x-access-token form); non-https
    URLs and an empty token pass through byte-identical."""
    from hive.app.sync import authenticated_url
    assert (authenticated_url("https://github.com/o/r.git", "tok")
            == "https://x-access-token:tok@github.com/o/r.git")
    assert authenticated_url("https://github.com/o/r.git", "") == "https://github.com/o/r.git"
    assert authenticated_url("/local/path/origin.git", "tok") == "/local/path/origin.git"


def test_normalized_repo_id_is_credential_free_and_stable():
    """The repo identity riding payload rows and the range ledger: userinfo (a
    credential!) is stripped, and cosmetic variants (.git, trailing slash) collapse
    to one stable key."""
    from hive.app.sync import normalized_repo_id
    assert (normalized_repo_id("https://x-access-token:tok@github.com/o/r.git")
            == "https://github.com/o/r")
    assert normalized_repo_id("https://github.com/o/r.git/") == "https://github.com/o/r"
    assert normalized_repo_id("https://github.com/o/r") == "https://github.com/o/r"
    assert normalized_repo_id("/data/repos/origin.git") == "/data/repos/origin"


def test_canonical_ref_overrides_tracked_branch(store, tmp_path):
    """census.canonical_ref names the tracked line; the origin default (HEAD) is only
    the fallback. With canonical_ref=trunk, a trunk push is what the mirror follows —
    and trunk is what ``sync:tracked_ref`` persists for the operator surface."""
    origin = Origin(tmp_path / "remote")             # default branch: main
    git(origin.work, "checkout", "-q", "-b", "trunk")
    origin.commit("app.py", 'def greet(name):\n    return "trunk " + name\n', "trunk")
    origin.push("trunk")
    svc = make_service(origin, store, tmp_path, canonical_ref="trunk")
    svc.tick()
    trunk_tip = origin.origin_sha("refs/heads/trunk")
    assert git(tmp_path / "mirror", "rev-parse",
               "refs/remotes/origin/trunk").stdout.strip() == trunk_tip
    assert meta(store, "sync:last_tip") == trunk_tip
    assert meta(store, META_TRACKED_REF) == "trunk"


def test_tick_writes_tracked_ref_and_last_sync_ts(origin, store, tmp_path):
    """Intent 12's write side: a clean tick persists the RESOLVED tracked line
    (``sync:tracked_ref``, origin-default fallback here) and stamps
    ``sync:last_sync_ts`` through the injected ``now`` seam — never wall clock —
    so the census-health sync block can serve both from store meta alone. A second
    clean tick advances the stamp."""
    ticks = iter([111_000, 222_000])
    svc = make_service(origin, store, tmp_path, now=lambda: next(ticks))
    svc.tick()
    assert meta(store, META_TRACKED_REF) == "main"   # resolved at mirror time
    assert meta(store, META_LAST_SYNC_TS) == "111000"  # the seam clock, not time.time()
    svc.tick()                                       # nothing moved — still a clean sync
    assert meta(store, META_LAST_SYNC_TS) == "222000"
