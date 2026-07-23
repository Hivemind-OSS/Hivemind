"""The durable repo registry (D2 — operational data, the authctl-seat pattern):
``repo_add`` / ``repo_remove`` / ``repo_registry`` on the ``repos`` table. Names are
slug-checked ([a-z0-9._-]+ — they ride mirror paths and scope labels); a duplicate or
bad name RAISES with nothing written (repoctl maps it to a non-zero exit); ``token_env``
stores the NAME of an env var — indirection, never a secret byte. These are the
store-level halves of CT-10's scenarios."""

from __future__ import annotations

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import RepoRow, SqliteEpisodeStore

SECRET_VALUE = "s3cr3t-gh-token-value-XYZ"


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"))


def _rows(s: SqliteEpisodeStore) -> dict[str, dict]:
    return {r["name"]: dict(r) for r in s.conn.execute("SELECT * FROM repos")}


# ── add ────────────────────────────────────────────────────────────────────────
def test_add_lands_a_row_with_the_pinned_keyword_surface():
    s = _store()
    s.repo_add(
        name="alpha",
        url="https://example.invalid/alpha.git",
        canonical_ref="main",
        token_env="MY_GH_TOKEN",
        added_ts=42,
    )
    row = _rows(s)["alpha"]
    assert row["url"] == "https://example.invalid/alpha.git"
    assert row["canonical_ref"] == "main"
    assert row["token_env"] == "MY_GH_TOKEN"  # the env var NAME, never the value
    assert row["added_ts"] == 42


def test_add_defaults_canonical_ref_and_token_env_empty():
    s = _store()
    s.repo_add(name="alpha", url="https://example.invalid/alpha.git", added_ts=1)
    row = _rows(s)["alpha"]
    assert row["canonical_ref"] == "" and row["token_env"] == ""


def test_duplicate_name_refused_row_unchanged():
    s = _store()
    s.repo_add(name="alpha", url="https://example.invalid/alpha.git", added_ts=1)
    with pytest.raises(ValueError, match="already registered"):
        s.repo_add(name="alpha", url="https://example.invalid/other.git", added_ts=2)
    assert _rows(s)["alpha"]["url"] == "https://example.invalid/alpha.git"


def test_bad_slug_refused_nothing_written():
    s = _store()
    for bad in ("Bad Name!", "UPPER", "with/slash", "", "sp ace"):
        with pytest.raises(ValueError, match="slug|name"):
            s.repo_add(name=bad, url="https://example.invalid/x.git", added_ts=1)
    assert _rows(s) == {}


def test_path_special_all_dot_names_refused_nothing_written():
    # '.' and '..' satisfy the [a-z0-9._-]+ charset alone but are path-special:
    # a mirror path joined from one escapes (or IS) the mirrors dir, so the gate
    # must refuse them at the source — no downstream path join may ever see one.
    s = _store()
    for bad in ("..", ".", "", "...", "...."):
        with pytest.raises(ValueError, match="slug|name"):
            s.repo_add(name=bad, url="https://example.invalid/x.git", added_ts=1)
    assert _rows(s) == {}  # nothing written on any refusal
    # dots INSIDE a name stay legal — ordinary slugs still register
    for ok in ("repo-a", "svc.thing_1"):
        s.repo_add(name=ok, url="https://example.invalid/x.git", added_ts=1)
    assert set(_rows(s)) == {"repo-a", "svc.thing_1"}


def test_slug_vocabulary_accepts_dots_dashes_underscores():
    s = _store()
    for ok in ("alpha", "a.b-c_d", "repo.name", "0numeric"):
        s.repo_add(name=ok, url="https://example.invalid/x.git", added_ts=1)
    assert set(_rows(s)) == {"alpha", "a.b-c_d", "repo.name", "0numeric"}


def test_empty_url_refused():
    s = _store()
    with pytest.raises(ValueError, match="url"):
        s.repo_add(name="alpha", url="", added_ts=1)
    assert _rows(s) == {}


def test_no_secret_byte_ever_stored():
    # the registry stores the env var NAME — even a caller confusing name and value
    # would land only what it passed; the row never gains the token from the env.
    s = _store()
    s.repo_add(
        name="alpha",
        url="https://example.invalid/alpha.git",
        token_env="MY_GH_TOKEN",
        added_ts=1,
    )
    dumped = "\n".join(
        str(v) for r in s.conn.execute("SELECT * FROM repos") for v in tuple(r)
    )
    assert SECRET_VALUE not in dumped
    assert "MY_GH_TOKEN" in dumped  # the NAME is the stored indirection


# ── list / remove ──────────────────────────────────────────────────────────────
def test_registry_returns_name_ordered_repo_rows():
    s = _store()
    s.repo_add(name="beta", url="https://example.invalid/beta.git", added_ts=2)
    s.repo_add(
        name="alpha",
        url="https://example.invalid/alpha.git",
        canonical_ref="main",
        token_env="T",
        added_ts=1,
    )
    rows = s.repo_registry()
    assert [r.name for r in rows] == ["alpha", "beta"]  # name-ordered
    assert rows[0] == RepoRow(
        name="alpha",
        url="https://example.invalid/alpha.git",
        canonical_ref="main",
        token_env="T",
        added_ts=1,
    )


def test_remove_deletes_the_row_idempotent_bool():
    s = _store()
    s.repo_add(name="alpha", url="https://example.invalid/alpha.git", added_ts=1)
    assert s.repo_remove("alpha") is True
    assert _rows(s) == {}
    assert s.repo_remove("alpha") is False  # no such name ⇒ no-op False


def test_remove_leaves_episode_scope_rows_untouched():
    # memories keep their partition when a repo is deregistered — a re-registered
    # repo picks them straight back up (removal stops the FEED, not the memory).
    s = _store()
    s.repo_add(name="alpha", url="https://example.invalid/alpha.git", added_ts=1)
    eid, _ = s.stage(
        text="alpha-scoped memory",
        weight=1.0,
        proposed_by="w",
        anchors=[("alpha", "a.py::f")],
    )
    s.repo_remove("alpha")
    n = s.conn.execute(
        "SELECT COUNT(*) AS c FROM episode_anchors WHERE episode_id=?", (eid,)
    ).fetchone()["c"]
    assert n == 1
