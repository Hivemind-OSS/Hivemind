"""repoctl admin CLI — the durable synced-repo registry verbs (the authctl twin).

add lands a slug-validated row storing NO secret byte (token_env is an env var NAME);
remove deletes it; list prints the registry. db_path resolves from --db /
$HIVE_STORE__DB_PATH, else defaults to /data/shared.db (the BUG-013 zero-config posture
mirroring entrypoint/healthcheck/backupctl — the `hive` CLI execs repoctl with no --db).
Slug validation is single-sourced in the store's repo_add; repoctl surfaces the
refusal as a non-zero exit with the reason on stderr.
"""

from __future__ import annotations

import io
import json

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.sync_keys import (
    COUNTER_FIELDS,
    FLEET_KEY_BUILDERS,
    FLEET_STR_FIELDS,
    KEY_BUILDERS,
    STR_FIELDS,
)
from hive.tools import repoctl

SECRET = "gh-token-value-XYZ-never-at-rest"


def _rows(db_path):
    store = SqliteEpisodeStore(connect(db_path))
    return {r["name"]: dict(r) for r in store.conn.execute("SELECT * FROM repos")}


def _ctl(*argv, env=None, out=None, now=None, connect_fn=None):
    out = out if out is not None else io.StringIO()
    kwargs = {"env": env if env is not None else {}, "out": out}
    if now is not None:
        kwargs["now"] = now
    if connect_fn is not None:
        kwargs["connect_fn"] = connect_fn
    return repoctl.main(list(argv), **kwargs), out


# ── add: the row lands, slug-validated, secretless ─────────────────────────────


def test_add_lands_row_with_all_fields(tmp_path):
    db = str(tmp_path / "shared.db")
    rc, _ = _ctl(
        "--db",
        db,
        "add",
        "https://example.invalid/alpha.git",
        "--name",
        "alpha",
        "--branch",
        "main",
        "--token-env",
        "MY_TOKEN",
        now=lambda: 424242,
    )
    assert rc == repoctl.EX_OK
    row = _rows(db)["alpha"]
    assert row["url"] == "https://example.invalid/alpha.git"
    assert row["canonical_ref"] == "main"
    assert row["token_env"] == "MY_TOKEN"  # the env var NAME — never a secret
    assert row["added_ts"] == 424242  # the injected clock


def test_add_defaults_name_from_url_basename(tmp_path):
    db = str(tmp_path / "shared.db")
    rc, _ = _ctl("--db", db, "add", "https://example.invalid/team/alpha.git")
    assert rc == repoctl.EX_OK
    row = _rows(db)["alpha"]  # basename minus .git
    assert row["canonical_ref"] == "" and row["token_env"] == ""


def test_add_duplicate_name_is_nonzero_and_row_unchanged(tmp_path, capsys):
    db = str(tmp_path / "shared.db")
    _ctl("--db", db, "add", "https://example.invalid/alpha.git", "--name", "alpha")
    rc, _ = _ctl(
        "--db", db, "add", "https://example.invalid/other.git", "--name", "alpha"
    )
    assert rc != repoctl.EX_OK
    assert "already registered" in capsys.readouterr().err
    assert _rows(db)["alpha"]["url"] == "https://example.invalid/alpha.git"


def test_add_bad_slug_is_nonzero_nothing_written(tmp_path, capsys):
    db = str(tmp_path / "shared.db")
    rc, _ = _ctl(
        "--db", db, "add", "https://example.invalid/x.git", "--name", "Bad Name!"
    )
    assert rc != repoctl.EX_OK  # [a-z0-9._-]+ only — it rides mirror paths
    assert "slug" in capsys.readouterr().err
    assert _rows(db) == {}


def test_add_underived_bad_slug_from_url_is_refused_pointing_at_name(tmp_path):
    # a URL whose basename is not a valid slug (uppercase) is refused, never silently
    # normalized — the operator passes --name instead.
    db = str(tmp_path / "shared.db")
    rc, _ = _ctl("--db", db, "add", "https://example.invalid/MyRepo.git")
    assert rc != repoctl.EX_OK
    assert _rows(db) == {}


def test_no_secret_value_is_read_stored_or_printed(tmp_path, capsys):
    # the env CARRIES the secret; repoctl must neither store nor echo a byte of it —
    # only the var NAME appears anywhere (row, stdout, stderr).
    db = str(tmp_path / "shared.db")
    rc, out = _ctl(
        "--db",
        db,
        "add",
        "https://example.invalid/alpha.git",
        "--name",
        "alpha",
        "--token-env",
        "MY_TOKEN",
        env={"MY_TOKEN": SECRET},
    )
    assert rc == repoctl.EX_OK
    err = capsys.readouterr().err
    assert SECRET not in out.getvalue() and SECRET not in err
    assert "MY_TOKEN" in err  # the NAME is fine (operator confirmation)
    dumped = " ".join(str(v) for row in _rows(db).values() for v in row.values())
    assert SECRET not in dumped


# ── list / remove ──────────────────────────────────────────────────────────────


def test_list_prints_one_line_per_repo_name_ordered(tmp_path):
    db = str(tmp_path / "shared.db")
    _ctl("--db", db, "add", "https://example.invalid/beta.git", "--name", "beta")
    _ctl(
        "--db",
        db,
        "add",
        "https://example.invalid/alpha.git",
        "--name",
        "alpha",
        "--branch",
        "main",
        "--token-env",
        "MY_TOKEN",
    )
    rc, out = _ctl("--db", db, "list")
    assert rc == repoctl.EX_OK
    lines = out.getvalue().splitlines()
    assert [ln.split("\t")[0] for ln in lines] == ["alpha", "beta"]  # name-ordered
    assert lines[0].split("\t") == [
        "alpha",
        "https://example.invalid/alpha.git",
        "main",
        "MY_TOKEN",
    ]
    assert lines[1].split("\t") == [
        "beta",
        "https://example.invalid/beta.git",
        "-",
        "-",
    ]  # '-' = defaults, never invented values


def test_list_empty_registry_prints_nothing(tmp_path):
    rc, out = _ctl("--db", str(tmp_path / "shared.db"), "list")
    assert rc == repoctl.EX_OK and out.getvalue() == ""


def test_remove_deletes_row(tmp_path):
    db = str(tmp_path / "shared.db")
    _ctl("--db", db, "add", "https://example.invalid/alpha.git", "--name", "alpha")
    rc, _ = _ctl("--db", db, "remove", "alpha")
    assert rc == repoctl.EX_OK
    assert _rows(db) == {}


def test_remove_unknown_name_is_nonzero(tmp_path, capsys):
    rc, _ = _ctl("--db", str(tmp_path / "shared.db"), "remove", "ghost")
    assert rc != repoctl.EX_OK
    assert "ghost" in capsys.readouterr().err


# ── report: the registry + the daemon's observed state as ONE json document ────


def _store(db_path):
    return SqliteEpisodeStore(connect(db_path))


def _report(db_path):
    rc, out = _ctl("--db", db_path, "report")
    assert rc == repoctl.EX_OK
    return json.loads(out.getvalue())


def test_report_is_one_json_document_with_registry_and_fleet(tmp_path):
    db = str(tmp_path / "shared.db")
    _ctl(
        "--db",
        db,
        "add",
        "https://example.invalid/alpha.git",
        "--name",
        "alpha",
        "--branch",
        "main",
        "--token-env",
        "MY_TOKEN",
    )
    doc = _report(db)
    (row,) = doc["repos"]
    # the registry HALF: every RepoRow column, verbatim — no TSV '-' sentinel here.
    assert row["name"] == "alpha"
    assert row["url"] == "https://example.invalid/alpha.git"
    assert row["canonical_ref"] == "main" and row["token_env"] == "MY_TOKEN"
    assert isinstance(row["added_ts"], int)
    # the fleet block is ALWAYS present, even with nothing synced (BUG-062).
    assert set(doc["fleet"]) == set(FLEET_STR_FIELDS)
    assert all(v is None for v in doc["fleet"].values())


def test_report_empty_registry_is_ok_not_an_error(tmp_path):
    doc = _report(str(tmp_path / "shared.db"))
    assert doc["repos"] == []
    assert set(doc["fleet"]) == set(FLEET_STR_FIELDS)


def test_report_merges_the_daemon_observed_state_per_repo(tmp_path):
    db = str(tmp_path / "shared.db")
    _ctl("--db", db, "add", "https://example.invalid/alpha.git", "--name", "alpha")
    _ctl("--db", db, "add", "https://example.invalid/beta.git", "--name", "beta")
    store = _store(db)
    for field, build in KEY_BUILDERS.items():
        store.meta_set(build("alpha"), "7" if field in COUNTER_FIELDS else f"<{field}>")
    store.meta_set(FLEET_KEY_BUILDERS["last_error"](), "tick failed: registry read")

    rows = {r["name"]: r for r in _report(db)["repos"]}
    for field in STR_FIELDS:
        assert rows["alpha"]["sync"][field] == f"<{field}>"
    for field in COUNTER_FIELDS:
        assert rows["alpha"]["sync"][field] == 7
    # a repo the daemon has left no meta for reads ABSENT, never a confident zero.
    assert "sync" not in rows["beta"]
    assert rows["beta"]["days_since_last_change_outcome"] is None
    assert _report(db)["fleet"]["last_error"] == "tick failed: registry read"


def test_report_restates_no_field_name_of_its_own(tmp_path):
    # H2: the served live-state vocabulary has ONE owner. repoctl composes the report
    # from RepoRow + census_health_report, so every sync field the daemon has a writer
    # for appears without repoctl naming it — and nothing else does.
    db = str(tmp_path / "shared.db")
    _ctl("--db", db, "add", "https://example.invalid/alpha.git", "--name", "alpha")
    store = _store(db)
    for field, build in KEY_BUILDERS.items():
        store.meta_set(build("alpha"), "1" if field in COUNTER_FIELDS else "x")
    (row,) = _report(db)["repos"]
    assert set(KEY_BUILDERS) <= set(row["sync"])
    source = (repoctl.__file__ or "").replace(".pyc", ".py")
    with open(source, encoding="utf-8") as fh:
        text = fh.read()
    for field in KEY_BUILDERS:
        assert field not in text, (
            f"repoctl must not name the sync field {field!r} — it comes from "
            "hive.app.sync_keys through census_health_report"
        )


# ── the 65/70 split: an operator-fixable refusal vs an internal fault ──────────


def test_operator_fixable_refusals_are_dataerr_not_software(tmp_path, capsys):
    # 65 says "you can fix this" and its reason is safe to surface; 70 says "the server
    # is broken" and its text must not be forwarded. An add refusal is always the former.
    db = str(tmp_path / "shared.db")
    _ctl("--db", db, "add", "https://example.invalid/alpha.git", "--name", "alpha")
    capsys.readouterr()

    bad_slug, _ = _ctl(
        "--db", db, "add", "https://example.invalid/x.git", "--name", "Bad Name!"
    )
    assert bad_slug == repoctl.EX_DATAERR
    assert "slug" in capsys.readouterr().err

    duplicate, _ = _ctl(
        "--db", db, "add", "https://example.invalid/other.git", "--name", "alpha"
    )
    assert duplicate == repoctl.EX_DATAERR
    assert "already registered" in capsys.readouterr().err

    unknown, _ = _ctl("--db", db, "remove", "ghost")
    assert unknown == repoctl.EX_DATAERR
    assert "ghost" in capsys.readouterr().err


# ── db_path resolution (the exact BUG-013 zero-config shape) ───────────────────


def test_zero_config_db_path_defaults_to_data_shared_db():
    seen = {}

    def fake_connect(db_path):
        seen["db"] = db_path
        return connect(":memory:")

    rc, _ = _ctl("list", connect_fn=fake_connect)
    assert rc == repoctl.EX_OK
    assert seen["db"] == "/data/shared.db"  # the in-container default, no fail-fast


def test_env_var_overrides_default_and_db_flag_wins(tmp_path):
    seen = {}

    def fake_connect(db_path):
        seen.setdefault("paths", []).append(db_path)
        return connect(":memory:")

    _ctl("list", env={"HIVE_STORE__DB_PATH": "/tmp/env.db"}, connect_fn=fake_connect)
    _ctl(
        "--db",
        "/tmp/flag.db",
        "list",
        env={"HIVE_STORE__DB_PATH": "/tmp/env.db"},
        connect_fn=fake_connect,
    )
    assert seen["paths"] == ["/tmp/env.db", "/tmp/flag.db"]


# ── a pre-v3 store is surfaced as a clean sysexits failure, not a traceback ────


def test_v2_store_refusal_maps_to_software(tmp_path, capsys):
    import sqlite3

    db = str(tmp_path / "v2.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE episodes(id INTEGER PRIMARY KEY, anchor TEXT, provenance TEXT)"
    )  # legacy columns ⇒ the no-migration refusal
    conn.commit()
    conn.close()
    rc, _ = _ctl("--db", db, "list")
    assert rc == repoctl.EX_SOFTWARE
    assert "migration" in capsys.readouterr().err
