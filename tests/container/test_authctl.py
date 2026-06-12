"""Chunk 5 — authctl admin CLI: mint + revoke per-device tokens (AUTH-PLAN §4/§10).

create LABEL prints the 256-bit plaintext to stdout ONCE (the operator hands it over via a
secret manager); revoke LABEL deletes the token so the device's next request 401s. db_path
resolves from --db / $HIVE_STORE__DB_PATH and FAILS FAST (EX_CONFIG) if absent — mirroring the
entrypoint. The RULE-2 mutation (revoke claims success without deleting) makes the
revoke→verify-None test red.
"""
from __future__ import annotations

import io

import pytest

from hive.adapters.auth_store_sqlite import SqliteTokenStore, token_hash
from hive.adapters.sqlite_db import connect
from hive.tools import authctl


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "shared.db")


def _store(db_path):
    return SqliteTokenStore(connect(db_path))


def test_create_prints_token_whose_hash_lands_in_table(db):
    out = io.StringIO()
    rc = authctl.main(["create", "alice-laptop"], env={"HIVE_STORE__DB_PATH": db}, out=out)
    assert rc == authctl.EX_OK
    token = out.getvalue().strip()
    assert token.startswith("hive_")                       # the plaintext, shown once
    store = _store(db)
    assert store.verify(token) == "alice-laptop"           # the printed token actually works
    row = store.conn.execute("SELECT token_hash FROM access_tokens").fetchone()
    assert row["token_hash"] == token_hash(token)          # only the hash is at rest


def test_revoke_makes_a_valid_token_verify_none(db):
    out = io.StringIO()
    authctl.main(["create", "bob-laptop"], env={"HIVE_STORE__DB_PATH": db}, out=out)
    token = out.getvalue().strip()
    assert _store(db).verify(token) == "bob-laptop"
    rc = authctl.main(["revoke", "bob-laptop"], env={"HIVE_STORE__DB_PATH": db})
    assert rc == authctl.EX_OK
    assert _store(db).verify(token) is None                # the device's next request now 401s


def test_missing_db_is_config_error():
    rc = authctl.main(["create", "x"], env={}, out=io.StringIO())     # no --db, no env
    assert rc == authctl.EX_CONFIG


def test_db_flag_overrides_env(db):
    out = io.StringIO()
    rc = authctl.main(["--db", db, "create", "carol"], env={}, out=out)
    assert rc == authctl.EX_OK
    assert _store(db).verify(out.getvalue().strip()) == "carol"


def test_duplicate_label_is_nonzero(db):
    authctl.main(["create", "dup"], env={"HIVE_STORE__DB_PATH": db}, out=io.StringIO())
    rc = authctl.main(["create", "dup"], env={"HIVE_STORE__DB_PATH": db}, out=io.StringIO())
    assert rc != authctl.EX_OK


def test_revoke_unknown_label_is_nonzero(db):
    rc = authctl.main(["revoke", "ghost"], env={"HIVE_STORE__DB_PATH": db})
    assert rc != authctl.EX_OK


def test_connect_fn_injection_uses_provided_conn():
    conn = connect(":memory:")                             # one shared in-memory conn
    out = io.StringIO()
    rc = authctl.main(["create", "dave"], env={"HIVE_STORE__DB_PATH": "/ignored"},
                      connect_fn=lambda _db: conn, out=out)
    assert rc == authctl.EX_OK
    assert SqliteTokenStore(conn).verify(out.getvalue().strip()) == "dave"


def test_list_prints_provisioned_labels(db):
    out = io.StringIO()                                    # empty store → no output, EX_OK
    rc = authctl.main(["list"], env={"HIVE_STORE__DB_PATH": db}, out=out)
    assert rc == authctl.EX_OK
    assert out.getvalue() == ""
    authctl.main(["create", "bob"], env={"HIVE_STORE__DB_PATH": db}, out=io.StringIO())
    authctl.main(["create", "alice"], env={"HIVE_STORE__DB_PATH": db}, out=io.StringIO())
    out = io.StringIO()
    rc = authctl.main(["list"], env={"HIVE_STORE__DB_PATH": db}, out=out)
    assert rc == authctl.EX_OK
    assert out.getvalue().splitlines() == ["alice", "bob"]  # one per line, sorted


def test_create_emits_only_the_token_on_stdout(db):
    out = io.StringIO()
    authctl.main(["create", "erin"], env={"HIVE_STORE__DB_PATH": db}, out=out)
    # stdout carries exactly one line: the token (machine-parseable; metadata goes to stderr)
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("hive_")
