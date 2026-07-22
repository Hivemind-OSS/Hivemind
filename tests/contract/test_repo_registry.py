"""CT-10 — repo registry verbs (plan §4 intent 10, D2, §6 step 6).

``hive repo add <url> [--name --branch --token-env]`` lands a slug-validated row
in the durable ``repos`` registry storing NO secret byte (``token_env`` is the
NAME of an env var); ``remove`` stops feeding and prunes the mirror; ``repos``
lists. The in-container tool is ``hive.tools.repoctl`` (the authctl twin); the
operator CLI shells to it. A registered-but-ABSENT token env var is EX_CONFIG at
boot (fail-fast, no silent default) and a per-repo named fail-open error at tick.
"""

from __future__ import annotations

import io

from tests.contract.conftest import (
    Origin,
    connect,
    load_module,
    make_syncer,
    meta_value,
    mirror_is_git,
    register_repo,
    registry_rows,
)

EX_OK = 0
EX_CONFIG = 78
SECRET_VALUE = "s3cr3t-gh-token-value-XYZ"


def _repoctl():
    mod = load_module("hive.tools.repoctl")
    assert mod is not None, (
        "hive.tools.repoctl does not exist yet (the authctl twin — plan §6 step 6)"
    )
    return mod


def _ctl(db_path: str, *argv: str, env: dict | None = None):
    mod = _repoctl()
    out = io.StringIO()
    rc = mod.main(["--db", db_path, *argv], env=env or {}, out=out)
    return rc, out.getvalue()


def _store_at(db_path: str):
    from hive.adapters.store_sqlite import SqliteEpisodeStore

    return SqliteEpisodeStore(connect(db_path, check_same_thread=False))


# ── add / list / remove ───────────────────────────────────────────────────────


def test_add_lands_a_slug_validated_row(tmp_path):
    db = str(tmp_path / "hive.db")
    store = _store_at(db)
    rc, _out = _ctl(
        db,
        "add",
        "https://example.invalid/alpha.git",
        "--name",
        "alpha",
        "--branch",
        "main",
        "--token-env",
        "MY_GH_TOKEN",
    )
    assert rc == EX_OK
    rows = registry_rows(store)
    assert "alpha" in rows, f"the add verb lands the registry row: {rows}"
    row = rows["alpha"]
    assert row["url"] == "https://example.invalid/alpha.git"
    assert row["canonical_ref"] == "main"
    assert row["token_env"] == "MY_GH_TOKEN", (
        "the row stores the env var NAME — indirection, never the secret"
    )


def test_list_prints_registered_names(tmp_path):
    db = str(tmp_path / "hive.db")
    _store_at(db)
    _ctl(db, "add", "https://example.invalid/alpha.git", "--name", "alpha")
    _ctl(db, "add", "https://example.invalid/beta.git", "--name", "beta")
    rc, out = _ctl(db, "list")
    assert rc == EX_OK
    assert "alpha" in out and "beta" in out


def test_remove_deletes_the_row(tmp_path):
    db = str(tmp_path / "hive.db")
    store = _store_at(db)
    _ctl(db, "add", "https://example.invalid/alpha.git", "--name", "alpha")
    rc, _ = _ctl(db, "remove", "alpha")
    assert rc == EX_OK
    assert "alpha" not in registry_rows(store)


def test_duplicate_name_refused(tmp_path):
    db = str(tmp_path / "hive.db")
    store = _store_at(db)
    _ctl(db, "add", "https://example.invalid/alpha.git", "--name", "alpha")
    rc, _ = _ctl(db, "add", "https://example.invalid/other.git", "--name", "alpha")
    assert rc != EX_OK, "a duplicate registry name is refused"
    assert registry_rows(store)["alpha"]["url"] == "https://example.invalid/alpha.git"


def test_bad_slug_refused(tmp_path):
    db = str(tmp_path / "hive.db")
    store = _store_at(db)
    rc, _ = _ctl(db, "add", "https://example.invalid/x.git", "--name", "Bad Name!")
    assert rc != EX_OK, "a non-slug name is refused ([a-z0-9._-]+ — it rides paths)"
    assert "Bad Name!" not in registry_rows(store)


def test_no_secret_byte_ever_stored(tmp_path, monkeypatch):
    db = str(tmp_path / "hive.db")
    store = _store_at(db)
    monkeypatch.setenv("MY_GH_TOKEN", SECRET_VALUE)
    rc, out = _ctl(
        db,
        "add",
        "https://example.invalid/alpha.git",
        "--name",
        "alpha",
        "--token-env",
        "MY_GH_TOKEN",
        env={"MY_GH_TOKEN": SECRET_VALUE},
    )
    assert rc == EX_OK
    assert SECRET_VALUE not in out, "the ctl never echoes a credential value"
    dumped = "\n".join(
        json_row
        for r in store.conn.execute("SELECT * FROM repos")
        for json_row in map(str, tuple(r))
    )
    assert SECRET_VALUE not in dumped, "no secret byte may land in the registry"


# ── the sync side: removal prunes; absent token env fails open, NAMED ─────────


def test_remove_stops_feeding_and_prunes_the_mirror(sync_store, tmp_path):
    origin = Origin(tmp_path / "remote")
    register_repo(sync_store, "alpha", origin.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()
    assert mirror_is_git(syncer.mirror_base, "alpha"), "precondition: mirrored"

    remove = getattr(sync_store, "repo_remove", None)
    assert remove is not None, "v3 store surface repo_remove missing"
    remove("alpha")
    syncer.service.tick()
    assert not mirror_is_git(syncer.mirror_base, "alpha"), (
        "a removed repo's mirror is pruned — it stops feeding"
    )


def test_absent_token_env_at_tick_fails_open_named(sync_store, tmp_path, monkeypatch):
    origin = Origin(tmp_path / "remote")
    monkeypatch.delenv("CT_ABSENT_TOKEN", raising=False)
    register_repo(
        sync_store,
        "alpha",
        origin.url,
        canonical_ref="main",
        token_env="CT_ABSENT_TOKEN",
    )
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()  # must NOT raise — the leg fails open

    err = meta_value(sync_store, "sync:alpha:last_error") or ""
    assert "CT_ABSENT_TOKEN" in err, (
        f"the fail-open error must NAME the missing env var: {err!r}"
    )


# ── boot fail-fast: a registered token_env var must exist (EX_CONFIG) ─────────


def test_boot_fails_fast_ex_config_when_token_env_missing(tmp_path):
    from hive.app.container import build_container
    from hive.tools import entrypoint
    from tests.fakes._fakes import FakeWarmProvider

    db = str(tmp_path / "hive.db")
    _ctl(
        db,
        "add",
        "https://example.invalid/alpha.git",
        "--name",
        "alpha",
        "--token-env",
        "CT_ABSENT_TOKEN",
    )

    def build_boot(cfg, *, tenant_id, agent_id):
        return build_container(
            cfg,
            tenant_id=tenant_id,
            agent_id=agent_id,
            embedder=FakeWarmProvider(d=8),
        )

    env = {"HIVE_STORE__DB_PATH": db}  # CT_ABSENT_TOKEN deliberately unset
    rc = entrypoint.main([], env=env, build_boot=build_boot, serve=lambda s: None)
    assert rc == EX_CONFIG, (
        f"a registered token_env var ABSENT at boot is EX_CONFIG (fail fast, "
        f"never a silent default) — got {rc}"
    )


def test_boot_proceeds_when_token_env_present(tmp_path):
    from hive.app.container import build_container
    from hive.tools import entrypoint
    from tests.fakes._fakes import FakeWarmProvider

    db = str(tmp_path / "hive.db")
    _ctl(
        db,
        "add",
        "https://example.invalid/alpha.git",
        "--name",
        "alpha",
        "--token-env",
        "CT_PRESENT_TOKEN",
    )

    def build_boot(cfg, *, tenant_id, agent_id):
        return build_container(
            cfg,
            tenant_id=tenant_id,
            agent_id=agent_id,
            embedder=FakeWarmProvider(d=8),
        )

    env = {"HIVE_STORE__DB_PATH": db, "CT_PRESENT_TOKEN": SECRET_VALUE}
    rc = entrypoint.main([], env=env, build_boot=build_boot, serve=lambda s: None)
    assert rc == EX_OK


# ── the operator CLI shells to the in-container ctl (argv contract) ───────────


def _cli_call(argv):
    from hive.tools import cli

    class FakeRun:
        def __init__(self):
            self.calls = []

        def __call__(self, cmd, env=None, **kw):
            import subprocess

            cmd = [str(c) for c in cmd]
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    import argparse

    fake = FakeRun()
    try:
        rc = cli.main(argv, run=fake, out=io.StringIO(), env={})
    except (SystemExit, argparse.ArgumentError) as exc:
        raise AssertionError(
            f"the `hive {' '.join(argv)}` verb is not registered in the CLI "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    return rc, fake.calls


def _flat(calls) -> str:
    return " || ".join(" ".join(c) for c in calls)


def test_cli_repo_add_shells_to_repoctl(tmp_path):
    rc, calls = _cli_call(
        ["repo", "add", "https://example.invalid/alpha.git", "--name", "alpha"]
    )
    assert rc == EX_OK
    flat = _flat(calls)
    assert "hive.tools.repoctl" in flat, (
        f"`hive repo add` shells to the in-container repoctl (the authctl "
        f"pattern): {flat}"
    )
    assert "add" in flat and "https://example.invalid/alpha.git" in flat


def test_cli_repos_lists_via_repoctl(tmp_path):
    rc, calls = _cli_call(["repos"])
    assert rc == EX_OK
    flat = _flat(calls)
    assert "hive.tools.repoctl" in flat and "list" in flat
