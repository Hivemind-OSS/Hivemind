"""The per-repo mirror + poll contract (v3 §6 step 5, D2).

Every registered repo gets its own mirror at ``<mirror_dir>/<name>/``, fetched
ALL-branches (``+refs/heads/*:refs/remotes/origin/*``) under ``clean_git_env``
(a hook-planted GIT_DIR in the parent env must never leak into the mirror's git
children — the BUG-034 shape). An unreachable remote or an absent row-named
token var is a LOGGED PER-REPO SKIP: the fault lands under
``sync:<name>:last_error``, the OTHER repos and the serve path never feel it,
and the next tick retries. An EMPTY registry is an INERT tick — no git spawn,
no engine, and the mirrors dir is never CREATED (pruning a leftover mirror is
the one allowed filesystem act). A DEREGISTERED repo loses its mirror on the
next tick (BUG-050): the reconciliation prune deletes only direct, slug-named,
non-symlink children of the configured mirrors dir, fails open per name, and
runs BEFORE the empty-registry early-return. The credential never escapes the
mirror's remote config — logs and the error meta are redacted.
"""

from __future__ import annotations

import logging
import shutil

from hive.app.sync import META_LAST_SYNC_TS

from tests.sync.conftest import (
    Origin,
    RecordingRun,
    git,
    make_service,
    meta,
    register_repo,
)


def test_tick_fetches_all_branches_per_repo_mirror(
    origin, store, tmp_path, monkeypatch
):
    # a hook-style GIT_DIR leak: every git call under test must strip it
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "bogus-git-dir"))
    git(origin.work, "checkout", "-q", "-b", "feature")
    origin.commit("app.py", "def other():\n    return 1\n", "feature work")
    origin.push("feature")
    git(origin.work, "checkout", "-q", "main")
    register_repo(store, "alpha", origin.url)
    svc = make_service(store, tmp_path)
    svc.tick()

    mirror = tmp_path / "mirrors" / "alpha"  # the slug path: <mirror_dir>/<name>
    tip = origin.origin_sha("refs/heads/main")
    feature_tip = origin.origin_sha("refs/heads/feature")
    assert git(mirror, "rev-parse", "refs/remotes/origin/main").stdout.strip() == tip
    # ALL branches ride the one fetch — drift demand needs non-canonical tips
    assert (
        git(mirror, "rev-parse", "refs/remotes/origin/feature").stdout.strip()
        == feature_tip
    )
    assert meta(store, "sync:alpha:last_error") is None

    # the remote moves; the SAME mirror (repair-free) fetches the new tip next tick
    origin.commit("app.py", 'def greet(name):\n    return "yo " + name\n', "move")
    origin.push()
    new_tip = origin.origin_sha("refs/heads/main")
    svc.tick()
    assert (
        git(mirror, "rev-parse", "refs/remotes/origin/main").stdout.strip() == new_tip
    )


def test_unreachable_repo_fails_open_others_sync(store, tmp_path, caplog):
    """One repo's remote does not exist: ITS tick leg is a logged skip under ITS
    error key; the healthy repo still syncs; nothing raises. Once the broken
    remote comes up the NEXT tick syncs it — and only a fully-clean tick stamps
    ``sync:last_sync_ts``."""
    healthy = Origin(tmp_path / "remote-b")
    late_root = tmp_path / "remote-late"
    register_repo(store, "alpha", str(late_root / "origin.git"))  # nothing there yet
    register_repo(store, "beta", healthy.url)
    svc = make_service(store, tmp_path, now=lambda: 31_337)
    with caplog.at_level(logging.WARNING, logger="hive.sync"):
        svc.tick()  # ← re-raising here breaks fail-open
    assert meta(store, "sync:alpha:last_error")  # the fault under ALPHA's key
    assert meta(store, "sync:beta:last_error") is None
    assert meta(store, "sync:beta:last_tip") == healthy.origin_sha("refs/heads/main")
    assert meta(store, META_LAST_SYNC_TS) is None  # a faulted tick never stamps
    assert any("sync" in r.name for r in caplog.records)
    assert not (tmp_path / "mirrors" / "alpha").exists()

    late = Origin(late_root)  # the remote comes up in place
    svc.tick()  # the next tick retries alpha
    assert meta(store, "sync:alpha:last_tip") == late.origin_sha("refs/heads/main")
    assert meta(store, META_LAST_SYNC_TS) == "31337"  # now fully clean ⇒ stamped


def test_absent_token_env_var_fails_that_repo_open_named(store, tmp_path, monkeypatch):
    """D2 tick-time indirection: a registry row naming a token env var that is
    ABSENT at tick time fails THAT repo open with the var NAMED in its error —
    the sibling repo is untouched."""
    monkeypatch.delenv("HIVE_SYNC_TEST_MISSING_TOKEN", raising=False)
    healthy = Origin(tmp_path / "remote-b")
    register_repo(
        store,
        "alpha",
        str(tmp_path / "whatever.git"),
        token_env="HIVE_SYNC_TEST_MISSING_TOKEN",
    )
    register_repo(store, "beta", healthy.url)
    run = RecordingRun()
    svc = make_service(store, tmp_path, run=run)
    svc.tick()

    err = meta(store, "sync:alpha:last_error") or ""
    assert "HIVE_SYNC_TEST_MISSING_TOKEN" in err  # the operator sees WHICH var
    assert meta(store, "sync:beta:last_tip") == healthy.origin_sha("refs/heads/main")
    # the broken repo never even reached git (token resolution precedes the clone)
    assert not any(
        "clone" in c and str(tmp_path / "whatever.git") in " ".join(c)
        for c in run.calls
    )


def test_token_resolved_from_row_named_var_and_never_logged(
    store, tmp_path, caplog, monkeypatch
):
    """The row-named var is resolved at tick time; an https clone failure whose
    git stderr echoes the rewritten URL must reach logs and the per-repo error
    REDACTED — the credential lives only in the mirror's remote config, never in
    any observable surface."""
    monkeypatch.setenv("HIVE_SYNC_TEST_TOKEN", "sekrit-token-123")
    register_repo(
        store,
        "alpha",
        "https://127.0.0.1:9/owner/repo.git",
        token_env="HIVE_SYNC_TEST_TOKEN",
    )
    svc = make_service(store, tmp_path)
    with caplog.at_level(logging.DEBUG, logger="hive.sync"):
        svc.tick()  # connection refused, fast + offline
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "sekrit-token-123" not in joined
    assert "sekrit-token-123" not in (meta(store, "sync:alpha:last_error") or "")


def test_authenticated_url_shapes():
    """The token rides ONLY an https remote URL (the x-access-token form); non-https
    URLs and an empty token pass through byte-identical."""
    from hive.app.sync import authenticated_url

    assert (
        authenticated_url("https://github.com/o/r.git", "tok")
        == "https://x-access-token:tok@github.com/o/r.git"
    )
    assert (
        authenticated_url("https://github.com/o/r.git", "")
        == "https://github.com/o/r.git"
    )
    assert (
        authenticated_url("/local/path/origin.git", "tok") == "/local/path/origin.git"
    )


def test_registry_canonical_ref_overrides_tracked_branch(store, tmp_path):
    """The registry row's canonical_ref names the tracked line; the origin default
    (HEAD) is only the fallback. With canonical_ref=trunk, a trunk push is what
    the ledger follows and what the per-repo watermark records."""
    origin = Origin(tmp_path / "remote")  # default branch: main
    git(origin.work, "checkout", "-q", "-b", "trunk")
    origin.commit("app.py", 'def greet(name):\n    return "trunk " + name\n', "trunk")
    origin.push("trunk")
    register_repo(store, "alpha", origin.url, canonical_ref="trunk")
    svc = make_service(store, tmp_path)
    svc.tick()
    trunk_tip = origin.origin_sha("refs/heads/trunk")
    assert (
        git(
            tmp_path / "mirrors" / "alpha", "rev-parse", "refs/remotes/origin/trunk"
        ).stdout.strip()
        == trunk_tip
    )
    assert meta(store, "sync:alpha:last_tip") == trunk_tip


def test_registry_reread_each_tick_no_restart(origin, store, tmp_path):
    """D2: a repo registered AFTER the service started is picked up by the very
    next tick — no restart, no rebuild."""
    svc = make_service(store, tmp_path)
    svc.tick()  # empty registry: inert
    assert not (tmp_path / "mirrors").exists()

    register_repo(store, "alpha", origin.url)  # registered mid-flight
    svc.tick()
    assert meta(store, "sync:alpha:last_tip") == origin.origin_sha("refs/heads/main")


def test_empty_registry_tick_is_inert(store, tmp_path):
    """The unit twin of CT-9's inert test: an EMPTY registry tick spawns nothing
    (no git, no clone, no engine), never CREATES the mirror dir (the leftover
    prune is delete-only), and stamps no last_sync_ts (nothing synced — the
    stamp must not lie)."""
    run = RecordingRun()
    svc = make_service(store, tmp_path, run=run, now=lambda: 999)
    svc.tick()
    assert run.calls == []  # ← cloning here is the mutation
    assert not (tmp_path / "mirrors").exists()
    assert meta(store, META_LAST_SYNC_TS) is None


def test_deregistered_repo_mirror_pruned_next_tick(store, tmp_path):
    """The ``repo remove`` promise (BUG-050): deregistration stops the feed AND
    the next tick prunes ``mirrors/<name>/``; the sibling repo's mirror and its
    sync are untouched, and a clean prune is silent (no error key)."""
    a = Origin(tmp_path / "remote-a")
    b = Origin(tmp_path / "remote-b")
    register_repo(store, "alpha", a.url)
    register_repo(store, "beta", b.url)
    svc = make_service(store, tmp_path)
    svc.tick()
    assert (tmp_path / "mirrors" / "alpha").is_dir()
    assert (tmp_path / "mirrors" / "beta").is_dir()

    assert store.repo_remove("alpha")
    svc.tick()
    assert not (tmp_path / "mirrors" / "alpha").exists()  # ← the prune
    assert (tmp_path / "mirrors" / "beta").is_dir()  # sibling mirror kept
    assert meta(store, "sync:beta:last_tip") == b.origin_sha("refs/heads/main")
    assert meta(store, "sync:alpha:last_error") is None  # a clean prune is silent


def test_emptied_registry_tick_prunes_leftover_spawn_free(origin, store, tmp_path):
    """Deregistering the LAST repo still prunes its mirror: the reconciliation
    runs BEFORE the empty-registry early-return — and the emptied-registry tick
    stays spawn-inert (no git, no engine) while it deletes."""
    run = RecordingRun()
    register_repo(store, "alpha", origin.url)
    svc = make_service(store, tmp_path, run=run)
    svc.tick()
    assert (tmp_path / "mirrors" / "alpha").is_dir()

    assert store.repo_remove("alpha")
    run.calls.clear()
    svc.tick()
    assert run.calls == []  # ← still no spawn on empty registry
    assert not (tmp_path / "mirrors" / "alpha").exists()  # ← but the leftover is gone


def test_prune_skips_slug_invalid_and_symlink_children(store, tmp_path):
    """The prune's scope guard: a child the registry grammar could never have
    named is NEVER rmtree'd, and a slug-named symlink is never followed (nothing
    outside the mirrors dir can be deleted) — both silent skips, not faults,
    and the empty-registry tick stays spawn-free throughout."""
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir(parents=True)
    stray = mirrors / "Not A Slug"  # uppercase + spaces: not ours
    stray.mkdir()
    (stray / "x.txt").write_text("x")
    outside = tmp_path / "outside"  # a slug-named link escaping the base
    outside.mkdir()
    (outside / "data.txt").write_text("precious")
    link = mirrors / "linked"
    link.symlink_to(outside)
    run = RecordingRun()
    svc = make_service(store, tmp_path, run=run)
    svc.tick()
    assert run.calls == []
    assert stray.is_dir() and (stray / "x.txt").exists()  # slug-invalid: untouched
    assert link.is_symlink()  # link skipped, not followed
    assert (outside / "data.txt").exists()  # the target survives whole
    assert (
        meta(store, "sync:linked:last_error") is None
    )  # a skip is silent, not a fault


def test_prune_fault_fails_open_tick_continues(store, tmp_path, monkeypatch, caplog):
    """A prune fault is a logged per-name skip under ``sync:<name>:last_error``:
    the tick lives on, the registered repo's legs still run, and — like every
    fault — the clean ``sync:last_sync_ts`` stamp is withheld."""
    healthy = Origin(tmp_path / "remote-b")
    register_repo(store, "beta", healthy.url)
    leftover = tmp_path / "mirrors" / "gone"
    leftover.mkdir(parents=True)

    def boom(path, *args, **kwargs):
        raise OSError(f"device busy: {path}")

    monkeypatch.setattr(shutil, "rmtree", boom)
    svc = make_service(store, tmp_path, now=lambda: 31_337)
    with caplog.at_level(logging.WARNING, logger="hive.sync"):
        svc.tick()  # ← raising here breaks the tick fail-open
    assert leftover.is_dir()  # the fault left it in place (next tick retries)
    assert "prune" in (meta(store, "sync:gone:last_error") or "")
    assert meta(store, "sync:beta:last_tip") == healthy.origin_sha("refs/heads/main")
    assert meta(store, "sync:beta:last_error") is None
    assert meta(store, META_LAST_SYNC_TS) is None  # a faulted tick never stamps
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "repo=gone" in joined and "leg=prune" in joined


def test_clean_tick_stamps_last_sync_ts_via_now_seam(origin, store, tmp_path):
    """A fully-clean tick stamps ``sync:last_sync_ts`` through the injected
    ``now`` seam — never wall clock; a second clean tick advances it."""
    ticks = iter([111_000, 222_000])
    register_repo(store, "alpha", origin.url)
    svc = make_service(store, tmp_path, now=lambda: next(ticks))
    svc.tick()
    assert meta(store, META_LAST_SYNC_TS) == "111000"  # the seam clock, not time.time()
    svc.tick()  # nothing moved — still a clean sync
    assert meta(store, META_LAST_SYNC_TS) == "222000"
