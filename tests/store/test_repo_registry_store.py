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


def _sync_keys(s, repo: str) -> set[str]:
    return {
        r["key"]
        for r in s.conn.execute(
            "SELECT key FROM meta WHERE key LIKE ?", (f"sync:{repo}:%",)
        )
    }


def _cache_counts(s, repo: str) -> dict[str, int]:
    return {
        table: s.conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE repo=?", (repo,)
        ).fetchone()["c"]
        for table in ("ref_tips", "anchor_baselines", "anchor_drift", "ref_requests")
    }


def test_repo_remove_forgets_the_feed_state():
    # BUG-060: deregistering means "forget the FEED". Leaving sync:<name>:* behind
    # made a re-registration resume a watermark whose mirror was pruned, and serve a
    # health block for a feed that had never run.
    # BUG-068 widened it to EVERYTHING DERIVED FROM the feed: ref_tips is the branch
    # twin of sync:<name>:last_tip but lives in a table the meta sweep cannot reach,
    # so a re-registered name resolved a dead incarnation's tip and read its
    # surviving anchor_drift rows as `fresh`. `anchor_baselines` joins them for the
    # same reason: a baseline is the commit THIS incarnation observed, so a
    # re-registration must re-baseline rather than measure from a dead feed's tip.
    # All of them are rebuildable caches
    # (Law 5) and re-materialize on the re-registered repo's first tick.
    s = _store()
    s.repo_add(name="alpha", url="https://example.invalid/alpha.git", added_ts=1)
    s.repo_add(name="beta", url="https://example.invalid/beta.git", added_ts=2)
    for repo in ("alpha", "beta"):
        for field in ("tracked_ref", "last_tip", "last_sync_ts", "backfilled_total"):
            s.meta_set(f"sync:{repo}:{field}", "x")
        s.ref_tips_put([(repo, "feature", "a" * 40, 5)])
        s.drift_put([(repo, "a" * 40, "b" * 40, "app.py::greet", "fresh", "{}", 5)])
        s.anchor_baseline_put([(1, repo, "app.py::greet", "b" * 40, 5)])
        s.touch_ref_request(repo, "feature", 5)

    s.repo_remove("alpha")
    assert _sync_keys(s, "alpha") == set()
    assert _cache_counts(s, "alpha") == {
        "ref_tips": 0,
        "anchor_baselines": 0,
        "anchor_drift": 0,
        "ref_requests": 0,
    }, "deregistration forgets the feed AND everything derived from it"
    assert len(_sync_keys(s, "beta")) == 4  # a sibling repo's feed is untouched
    assert _cache_counts(s, "beta") == {
        "ref_tips": 1,
        "anchor_baselines": 1,
        "anchor_drift": 1,
        "ref_requests": 1,
    }


def test_repo_remove_keeps_the_memory_side_scope():
    # the other half of the same invariant: episode_anchors / episode_refs are
    # MEMORY (the scope a writer declared), not feed observations. Deleting them
    # would destroy user-declared scope, so a re-registered repo picks them back up.
    s = _store()
    s.repo_add(name="alpha", url="https://example.invalid/alpha.git", added_ts=1)
    eid, _ = s.stage(
        text="greet stays single-arg",
        weight=1.0,
        proposed_by="w",
        ts=10,
        polarity="neutral",
        anchors=[("alpha", "app.py::greet")],
        repos=[("alpha", "feature")],
    )

    s.repo_remove("alpha")
    assert s.episode_refs(eid) == {"alpha": "feature"}
    assert [
        r["repo"]
        for r in s.conn.execute(
            "SELECT repo FROM episode_anchors WHERE episode_id=?", (eid,)
        )
    ] == ["alpha"]


def test_repo_remove_leaves_the_tick_shell_globals_alone():
    # the 2-part globals belong to the daemon, not to any repo — deregistering one
    # repo must not erase the fleet-wide surface.
    s = _store()
    s.repo_add(name="alpha", url="https://example.invalid/alpha.git", added_ts=1)
    s.meta_set("sync:last_sync_ts", "1000")
    s.meta_set("sync:last_error", "registry: OperationalError: disk I/O error")
    s.repo_remove("alpha")
    survivors = {
        r["key"] for r in s.conn.execute("SELECT key FROM meta WHERE key LIKE 'sync:%'")
    }
    assert survivors == {"sync:last_sync_ts", "sync:last_error"}
