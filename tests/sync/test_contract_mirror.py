"""The per-repo mirror + poll contract (v3 §6 step 5, D2).

Every registered repo gets its own mirror at ``<mirror_dir>/<name>-<url-digest>/``
— the directory is bound to the repository the mirror is a mirror OF, so a name
re-registered against a different remote can never reach the previous
incarnation's checkout — fetched
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
mirror's remote config — and that config is RECONCILED against the registry
every tick, so a rotated secret takes effect without a re-clone. Logs and the
error meta are redacted. A fault in the
tick SHELL itself belongs to no repo, so it rides the 2-part fleet keys and is
SERVED in the health report's ``fleet`` block — the only in-band statement that
the daemon is down (BUG-062).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

from hive.app.census_health import census_health_report
from hive.app.sync import default_run, mirror_dirname
from hive.app.sync_keys import fleet_last_error_key, fleet_last_sync_ts_key

from tests.sync.conftest import (
    Origin,
    RecordingRun,
    git,
    harness_env,
    make_service,
    meta,
    register_repo,
    seed_episode,
)


def _mirror(tmp_path: Path, name: str, url: str) -> Path:
    """A repo's mirror dir, always through the ONE owner of that name. A test that
    re-derived the layout by hand would stop testing the production rule."""
    return tmp_path / "mirrors" / mirror_dirname(name, url)


def _remote_url(mirror: Path) -> str:
    return git(mirror, "config", "--get", "remote.origin.url").stdout.strip()


def _mint_repo_args(calls) -> list[str]:
    """The ``--repo`` argument of every ``hive-edge mint`` spawn — the checkout the
    engine's path-keyed graph cache is derived from."""
    return [c[c.index("--repo") + 1] for c in calls if "mint" in c and "--repo" in c]


# A credential is observable in a mirror's remote config only under an https
# registry URL (``authenticated_url`` rewrites nothing else), so the token tests
# below register one and let git's own ``url.<base>.insteadOf`` redirect the
# transport to a REAL local origin. Only the transport is redirected: the URL git
# RECORDS — the thing under test — is exactly what production writes, so every
# clone, fetch, config read and set-url stays real git on a real repository.
HTTPS_URL = "https://example.invalid/o/r.git"
TOKEN_VAR = "HIVE_SYNC_TEST_ROTATING_TOKEN"
TOKEN_ONE = "rotating-token-one-aaaa"
TOKEN_TWO = "rotating-token-two-bbbb"


def _authed(token: str) -> str:
    return f"https://x-access-token:{token}@example.invalid/o/r.git"


def _https_registry_url(monkeypatch, origin: Origin, *tokens: str) -> str:
    """Back ``HTTPS_URL`` with ``origin``: one rewrite rule per credential the test
    will present, plus the credential-free form."""
    urls = [HTTPS_URL, *(_authed(t) for t in tokens)]
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(urls)))
    for i, url in enumerate(urls):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", f"url.{origin.url}.insteadOf")
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", url)
    return HTTPS_URL


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

    mirror = _mirror(tmp_path, "alpha", origin.url)  # <mirror_dir>/<name>-<digest>
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
    assert meta(store, fleet_last_sync_ts_key()) is None  # a faulted tick never stamps
    assert any("sync" in r.name for r in caplog.records)
    assert not _mirror(tmp_path, "alpha", str(late_root / "origin.git")).exists()

    late = Origin(late_root)  # the remote comes up in place
    svc.tick()  # the next tick retries alpha
    assert meta(store, "sync:alpha:last_tip") == late.origin_sha("refs/heads/main")
    assert meta(store, fleet_last_sync_ts_key()) == "31337"  # now fully clean ⇒ stamped


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
    mirror = _mirror(tmp_path, "alpha", origin.url)
    assert git(mirror, "rev-parse", "refs/remotes/origin/trunk").stdout.strip() == (
        trunk_tip
    )
    assert meta(store, "sync:alpha:last_tip") == trunk_tip
    assert meta(store, "sync:alpha:tracked_ref") == "trunk"


def test_tracked_ref_records_the_resolved_default_branch(store, tmp_path):
    """BUG-059: ``sync:<name>:tracked_ref`` is the branch the daemon RESOLVED, which
    is why the registry row cannot answer it — the common "track whatever the default
    is" registration carries no canonical_ref at all, and the resolved name is only
    knowable from the clone's origin/HEAD."""
    origin = Origin(tmp_path / "remote")  # default branch: main
    origin.commit("app.py", "def greet(name):\n    return name\n", "seed")
    origin.push()
    register_repo(store, "alpha", origin.url, canonical_ref="")
    make_service(store, tmp_path).tick()
    assert meta(store, "sync:alpha:tracked_ref") == "main"


def test_tracked_ref_is_stamped_even_when_a_later_leg_faults(origin, store, tmp_path):
    """The stamp rides branch RESOLUTION, not a clean tick: "which line do you believe
    you track" is exactly the question a faulted feed needs answered, so it must not be
    withheld alongside last_error the way the freshness stamp is."""
    broken = [False]

    def breaking_run(argv, env=None, timeout=None):
        if broken[0] and "hive.census.cli" in list(argv):
            return subprocess.CompletedProcess(
                list(argv), 1, stdout="", stderr="census build broken"
            )
        return default_run(argv, env=env, timeout=timeout)

    register_repo(store, "alpha", origin.url)
    svc = make_service(store, tmp_path, run=breaking_run, now=lambda: 1_000)
    svc.tick()  # first connect: baseline only — clean
    store.meta_set("sync:alpha:tracked_ref", "")  # cleared, so the re-stamp is visible

    origin.commit("app.py", "def greet(name):\n    return name\n", "move the tip")
    origin.push()
    broken[0] = True
    svc.tick()  # the ledger leg faults on the moved tip
    assert meta(store, "sync:alpha:last_error").startswith("ledger:")
    assert meta(store, "sync:alpha:tracked_ref") == "main"  # ← re-stamped anyway
    assert meta(store, "sync:alpha:last_sync_ts") == "1000"  # ← freshness held back


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
    assert meta(store, fleet_last_sync_ts_key()) is None


def test_deregistered_repo_mirror_pruned_next_tick(store, tmp_path):
    """The ``repo remove`` promise (BUG-050): deregistration stops the feed AND
    the next tick prunes that name's mirror; the sibling repo's mirror and its
    sync are untouched, and a clean prune is silent (no error key)."""
    a = Origin(tmp_path / "remote-a")
    b = Origin(tmp_path / "remote-b")
    register_repo(store, "alpha", a.url)
    register_repo(store, "beta", b.url)
    svc = make_service(store, tmp_path)
    svc.tick()
    alpha_dir = _mirror(tmp_path, "alpha", a.url)
    beta_dir = _mirror(tmp_path, "beta", b.url)
    assert alpha_dir.is_dir()
    assert beta_dir.is_dir()

    assert store.repo_remove("alpha")
    svc.tick()
    assert not alpha_dir.exists()  # ← the prune
    assert beta_dir.is_dir()  # sibling mirror kept
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
    mirror = _mirror(tmp_path, "alpha", origin.url)
    assert mirror.is_dir()

    assert store.repo_remove("alpha")
    run.calls.clear()
    svc.tick()
    assert run.calls == []  # ← still no spawn on empty registry
    assert not mirror.exists()  # ← but the leftover is gone


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
    """A prune fault is a logged skip on the tick-SHELL key: the tick lives on, the
    registered repo's legs still run, and — like every fault — the clean
    ``sync:last_sync_ts`` stamp is withheld.

    It rides the SHELL key, never ``sync:<name>:last_error`` (BUG-061): the name being
    pruned is DEREGISTERED, so a per-repo key written under it has no health block and
    is readable by nobody — the stuck mirror would leak disk in total silence."""
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
    shell_error = meta(store, fleet_last_error_key()) or ""
    assert "prune" in shell_error and "gone" in shell_error  # the name is still named
    assert meta(store, "sync:gone:last_error") is None  # never a blockless per-repo key
    assert meta(store, "sync:beta:last_tip") == healthy.origin_sha("refs/heads/main")
    assert meta(store, "sync:beta:last_error") is None
    assert meta(store, fleet_last_sync_ts_key()) is None  # a faulted tick never stamps
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "prune[gone]" in joined


def test_clean_tick_stamps_last_sync_ts_via_now_seam(origin, store, tmp_path):
    """A fully-clean tick stamps BOTH freshness surfaces through the injected ``now``
    seam — never wall clock; a second clean tick advances both. The fleet-wide global
    means "every repo ran clean", the per-repo key means "THIS repo ran clean" (they
    only agree while one repo is registered)."""
    clock = [111_000]  # settable, not a consuming iterator: one tick reads the seam
    register_repo(store, "alpha", origin.url)  # once per repo AND once for the shell
    svc = make_service(store, tmp_path, now=lambda: clock[0])
    svc.tick()
    assert (
        meta(store, fleet_last_sync_ts_key()) == "111000"
    )  # the seam clock, not time.time()
    assert meta(store, "sync:alpha:last_sync_ts") == "111000"
    clock[0] = 222_000
    svc.tick()  # nothing moved — still a clean sync
    assert meta(store, fleet_last_sync_ts_key()) == "222000"
    assert meta(store, "sync:alpha:last_sync_ts") == "222000"


def test_dead_daemon_is_named_in_the_fleet_block_not_in_any_repo_block(
    origin, store, tmp_path
):
    """BUG-062, the operator-visible symptom: when the tick SHELL's registry read
    faults, ``tick()`` returns before the prune and before any repo is reached, so no
    per-repo key is written OR cleared — every block keeps its last-healthy values and
    reads character-for-character as the connect runbook's PASS row. Only the fleet
    block can say the daemon is down, and it must, or a week of darkness reports N
    healthy repos.

    The registry read is broken for the DAEMON only, not for the report: that is the
    real shape (a wedged tick against a serve path that still reads fine), and it is
    also the only way the symptom is visible at all — a report that cannot read the
    registry serves no repo blocks to be fooled by."""
    register_repo(store, "alpha", origin.url)
    svc = make_service(store, tmp_path, now=lambda: 111_000)
    svc.tick()  # one fully-clean tick: alpha is cloned, resolved, baselined

    real_registry = store.repo_registry
    down = [True]

    def flaky_registry():
        if down[0]:
            raise sqlite3.OperationalError("database is locked")
        return real_registry()

    store.repo_registry = flaky_registry
    svc.tick()  # ← the whole tick dies in the shell; alpha is never touched
    down[0] = False  # the serve path's own registry read is healthy

    report = census_health_report(store)
    block = report["repos"]["alpha"]["sync"]
    # (a) the repo still looks entirely passing — the frozen snapshot of the last
    # healthy tick, with no fault of its own to show
    assert block["tracked_ref"] == "main"
    assert block["last_tip"] == origin.origin_sha("refs/heads/main")
    assert block["last_error"] is None
    assert block["last_sync_ts"] == "111000"
    # (b) …and the fleet block is where the diagnosis lives
    fleet = report["fleet"]
    assert fleet["last_error"].startswith("registry:")
    assert "database is locked" in fleet["last_error"]
    # the clean-tick stamp is withheld by the faulted tick, so it stays frozen next to
    # the fault — "the daemon last completed a full sync at 111000, then broke"
    assert fleet["last_sync_ts"] == "111000"


# ── mirror identity: a mirror never outlives the repository it mirrors ────────


def test_reregistration_with_a_new_url_never_feeds_the_old_remote(store, tmp_path):
    """remove + add is the ONLY way to change a registered URL and nothing forces a
    tick between the two, so the mirror's IDENTITY — not the prune's timing — has to
    carry the guarantee. Everything the new registration earns must be measured on
    the new remote: its remote config, its watermark, and the objects it holds."""
    old = Origin(tmp_path / "remote-old")
    old.commit("app.py", "def only_on_the_old_remote():\n    return 1\n", "old only")
    old.push()
    register_repo(store, "alpha", old.url)
    svc = make_service(store, tmp_path)
    svc.tick()
    old_head = old.origin_sha("refs/heads/main")
    assert meta(store, "sync:alpha:last_tip") == old_head  # precondition: fed by OLD

    new = Origin(tmp_path / "remote-new")
    assert store.repo_remove("alpha")
    register_repo(store, "alpha", new.url)  # ← no tick between the remove and the add
    svc.tick()

    mirror = _mirror(tmp_path, "alpha", new.url)
    assert mirror.is_dir(), "the re-registered repo must get a mirror of its own"
    assert _remote_url(mirror) == new.url
    assert meta(store, "sync:alpha:last_tip") == new.origin_sha("refs/heads/main")
    # the previous incarnation's history is not merely unread — it is not present
    assert git(mirror, "cat-file", "-e", old_head, check=False).returncode != 0


def test_reregistration_with_the_same_url_reuses_the_mirror(origin, store, tmp_path):
    """The identity is (name, URL), not (name, registration): re-adding a name
    against the SAME remote keeps its checkout, so an operator correcting a
    canonical_ref or a token_env never pays a re-clone."""
    register_repo(store, "alpha", origin.url)
    make_service(store, tmp_path).tick()
    mirror = _mirror(tmp_path, "alpha", origin.url)
    assert mirror.is_dir(), "precondition: mirrored"
    sentinel = mirror / ".git" / "hive-reclone-probe"
    sentinel.write_text("kept")  # a fetch keeps it; a re-clone structurally cannot

    assert store.repo_remove("alpha")
    register_repo(store, "alpha", origin.url, canonical_ref="main")
    run = RecordingRun()
    make_service(store, tmp_path, run=run).tick()

    assert sentinel.exists(), "an unchanged remote is the same mirror"
    assert not any("clone" in c for c in run.calls)
    assert meta(store, "sync:alpha:last_tip") == origin.origin_sha("refs/heads/main")


def test_the_previous_incarnations_mirror_is_reaped(store, tmp_path):
    """The superseded checkout is not merely unused — it is gone by the end of the
    SAME tick that clones its replacement (the reconciliation prune runs before the
    repo legs), so a URL change costs one re-clone and never a growing pile."""
    old = Origin(tmp_path / "remote-old")
    old.commit("app.py", "def only_on_the_old_remote():\n    return 1\n", "old only")
    old.push()
    new = Origin(tmp_path / "remote-new")
    register_repo(store, "alpha", old.url)
    svc = make_service(store, tmp_path)
    svc.tick()
    old_dir = _mirror(tmp_path, "alpha", old.url)
    assert old_dir.is_dir(), "precondition: mirrored"

    assert store.repo_remove("alpha")
    register_repo(store, "alpha", new.url)
    svc.tick()

    assert not old_dir.exists()
    assert sorted(p.name for p in (tmp_path / "mirrors").iterdir()) == [
        _mirror(tmp_path, "alpha", new.url).name
    ]


def test_a_new_url_gives_the_mint_leg_a_fresh_checkout_path(store, tmp_path):
    """The engine keys its graph cache on a digest of the checkout PATH and
    refreshes it INCREMENTALLY against the working tree, so a new repository at an
    old path would be minted through the previous repository's cache. The
    identity-bound directory makes that unconstructable: a different remote is a
    different ``--repo`` argument, hence a different cache."""
    old = Origin(tmp_path / "remote-old")
    old.commit("app.py", "def only_on_the_old_remote():\n    return 1\n", "old only")
    old.push()
    new = Origin(tmp_path / "remote-new")
    # an anchor no tree resolves: the mint yields nothing, so the carrier stays
    # EMPTY and the backfill sweep really runs against BOTH incarnations
    seed_episode(store, "greet stays single-arg", anchor="app.py::nosuchsymbol")
    register_repo(store, "alpha", old.url)
    run = RecordingRun()
    svc = make_service(store, tmp_path, run=run)
    svc.tick()
    before = _mint_repo_args(run.calls)
    assert before, "precondition: the mint leg ran against the first incarnation"

    assert store.repo_remove("alpha")
    register_repo(store, "alpha", new.url)
    run.calls.clear()
    svc.tick()

    after = _mint_repo_args(run.calls)
    assert after, "the mint leg must run against the new incarnation too"
    assert set(after).isdisjoint(before)


def test_a_legacy_name_only_mirror_is_reaped_and_recloned(origin, store, tmp_path):
    """The upgrade path: a fleet mirrored under the older name-only layout repairs
    itself on the first tick with no operator action. The legacy directory is absent
    from the live set, so the prune reaps it and the repo legs clone the
    identity-bound one — the mirror is a rebuildable cache, and this is what that
    licenses."""
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir(parents=True)
    legacy = mirrors / "alpha"
    subprocess.run(
        ["git", "clone", "--quiet", origin.url, str(legacy)],
        check=True,
        capture_output=True,
        env=harness_env(),
    )
    register_repo(store, "alpha", origin.url)
    make_service(store, tmp_path).tick()

    assert not legacy.exists()
    bound = _mirror(tmp_path, "alpha", origin.url)
    assert _remote_url(bound) == origin.url
    assert meta(store, "sync:alpha:last_tip") == origin.origin_sha("refs/heads/main")
    assert meta(store, "sync:alpha:last_error") is None


# ── the mirror's remote is reconciled against the registry, not trusted ───────


def test_a_rotated_token_is_reconciled_without_a_reclone(
    origin, store, tmp_path, monkeypatch, caplog
):
    """The credential enters the mirror once, at clone time, and every later fetch
    reads it back out of the mirror's own config — so a rotated secret has to be
    written INTO that config or it never takes effect and the repo eventually fails
    open forever. The registry URL is unchanged and the path already pins the
    repository, so the difference can only be the credential: reconcile in place
    rather than pay a re-clone. The value itself must never be logged."""
    url = _https_registry_url(monkeypatch, origin, TOKEN_ONE, TOKEN_TWO)
    monkeypatch.setenv(TOKEN_VAR, TOKEN_ONE)
    register_repo(store, "alpha", url, token_env=TOKEN_VAR)
    make_service(store, tmp_path).tick()
    mirror = _mirror(tmp_path, "alpha", url)
    assert _remote_url(mirror) == _authed(TOKEN_ONE)  # precondition: cloned with t1
    sentinel = mirror / ".git" / "hive-reclone-probe"
    sentinel.write_text("kept")

    monkeypatch.setenv(TOKEN_VAR, TOKEN_TWO)  # the secret rotates behind the NAME
    origin.commit("app.py", "def greet(name):\n    return name\n", "after rotation")
    origin.push()
    run = RecordingRun()
    with caplog.at_level(logging.DEBUG, logger="hive.sync"):
        make_service(store, tmp_path, run=run).tick()

    assert _remote_url(mirror) == _authed(TOKEN_TWO)
    assert sentinel.exists(), "reconciling a credential must not cost a re-clone"
    assert not any("clone" in c for c in run.calls)
    # the rotated credential is what actually fetched
    assert meta(store, "sync:alpha:last_tip") == origin.origin_sha("refs/heads/main")
    assert meta(store, "sync:alpha:last_error") is None
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN_ONE not in joined and TOKEN_TWO not in joined


def test_token_removal_strips_the_credential_from_the_remote(
    origin, store, tmp_path, monkeypatch
):
    """Rotation's other direction: a repo that stops being private must stop
    presenting a credential. The mirror's remote is reconciled TOWARD the registry's
    own URL — never the reverse — and still with no re-clone."""
    url = _https_registry_url(monkeypatch, origin, TOKEN_ONE)
    monkeypatch.setenv(TOKEN_VAR, TOKEN_ONE)
    register_repo(store, "alpha", url, token_env=TOKEN_VAR)
    make_service(store, tmp_path).tick()
    mirror = _mirror(tmp_path, "alpha", url)
    assert _remote_url(mirror) == _authed(TOKEN_ONE)  # precondition

    monkeypatch.setenv(TOKEN_VAR, "")  # the secret is emptied; the registry row stands
    origin.commit("app.py", "def greet(name):\n    return name\n", "after removal")
    origin.push()
    run = RecordingRun()
    make_service(store, tmp_path, run=run).tick()

    assert _remote_url(mirror) == url
    assert "x-access-token" not in _remote_url(mirror)
    assert not any("clone" in c for c in run.calls)
    assert meta(store, "sync:alpha:last_tip") == origin.origin_sha("refs/heads/main")


def test_unreadable_remote_config_forces_a_reclone(origin, store, tmp_path):
    """A checkout that cannot say what it is a mirror OF has lost its identity, so
    it is a BROKEN cache and is rebuilt whole. Direction (Law 6): fail toward
    REBUILD, never toward reuse — fetching from an unprovable remote is the one
    outcome that could feed the wrong repository."""
    register_repo(store, "alpha", origin.url)
    svc = make_service(store, tmp_path)
    svc.tick()
    mirror = _mirror(tmp_path, "alpha", origin.url)
    assert mirror.is_dir(), "precondition: mirrored"
    sentinel = mirror / ".git" / "hive-reclone-probe"

    for label, argv in (
        ("the config read exits non-zero", ["config", "--unset", "remote.origin.url"]),
        ("the config read returns empty", ["config", "remote.origin.url", ""]),
    ):
        sentinel.write_text("stale")
        git(mirror, *argv)
        svc.tick()
        assert not sentinel.exists(), f"{label}: the mirror must be rebuilt, not reused"
        assert _remote_url(mirror) == origin.url
        assert meta(store, "sync:alpha:last_error") is None


def test_set_url_fault_fails_that_repo_open(origin, store, tmp_path, monkeypatch):
    """The reconcile is a git WRITE, so it can fail. Direction: this repo skips the
    tick under its own error key rather than fetching with a credential the registry
    no longer names; the sibling repo syncs, nothing raises past the tick, the
    faulted tick withholds the clean stamp, and the token never reaches the surfaced
    message."""
    url = _https_registry_url(monkeypatch, origin, TOKEN_ONE, TOKEN_TWO)
    healthy = Origin(tmp_path / "remote-healthy")
    monkeypatch.setenv(TOKEN_VAR, TOKEN_ONE)
    register_repo(store, "alpha", url, token_env=TOKEN_VAR)
    register_repo(store, "beta", healthy.url)
    clock = [31_337]
    svc = make_service(store, tmp_path, now=lambda: clock[0])
    svc.tick()
    mirror = _mirror(tmp_path, "alpha", url)
    assert mirror.is_dir(), "precondition: mirrored"
    git_dir = mirror / ".git"
    mode = stat.S_IMODE(git_dir.stat().st_mode)

    monkeypatch.setenv(TOKEN_VAR, TOKEN_TWO)
    clock[0] = 42_000
    git_dir.chmod(0o555)  # git cannot take the config lock
    try:
        svc.tick()  # ← raising here breaks the per-repo fail-open
    finally:
        git_dir.chmod(mode)

    err = meta(store, "sync:alpha:last_error") or ""
    assert err.startswith("mirror:")
    assert TOKEN_ONE not in err and TOKEN_TWO not in err
    assert _remote_url(mirror) == _authed(TOKEN_ONE)  # never half-applied
    assert meta(store, "sync:beta:last_error") is None
    assert meta(store, "sync:beta:last_tip") == healthy.origin_sha("refs/heads/main")
    assert meta(store, fleet_last_sync_ts_key()) == "31337"  # the fault withheld it
