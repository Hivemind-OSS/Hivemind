"""Schema v3 — the repo-partitioned store shape and its refusal posture.

Episodes DROP the anchor/provenance/approver/tags columns; ``episode_anchors`` is the one
owner of scope + code bindings (+ per-anchor fp carriers); ``repos`` / ``anchor_drift`` /
``ref_requests`` land; ``recall_misses`` gains the ``repos`` scope column.

There is NO migration/backfill path: this build starts from a clean, empty store. Refusal
now triggers on the PRESENCE of the legacy episodes columns (a v2 store must never be
half-served), on the absence of the v3 columns (a pre-lifecycle store), and on a v3
episodes table missing its ``episode_anchors`` twin — each loudly at CONSTRUCTION with
the no-migration message, never a mid-recall "no such column" crash. Connections come
from the prod ``connect()`` factory — a hand-rolled test conn with default (deferred)
isolation diverges from the autocommit+BEGIN IMMEDIATE discipline the store is written
against.
"""

from __future__ import annotations

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.lifecycle import QUARANTINED

# The v2 episodes table VERBATIM as it last shipped (with the legacy
# anchor/provenance/approver/tags columns) — deliberately a frozen snapshot, so the v3
# store class can never accidentally create it. Used ONLY to prove a v2 store refuses.
_V2_EPISODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL,
  provenance TEXT NOT NULL DEFAULT 'agent_reasoned',
  tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0,
  trust TEXT NOT NULL DEFAULT 'quarantined',
  superseded_by INTEGER,
  last_active_ts INTEGER NOT NULL DEFAULT 0,
  polarity TEXT NOT NULL DEFAULT 'neutral' CHECK(polarity IN ('do','dont','neutral')),
  kind TEXT NOT NULL DEFAULT 'note',
  anchor TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '');
"""

# The pre-trust episodes table, verbatim as it shipped — refuses on its legacy
# ``tags`` column (presence-check) before any missing-column reasoning.
_PRE_LIFECYCLE_EPISODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL, source TEXT, tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0);
"""

# A hand-built v3-column episodes table WITHOUT its episode_anchors twin — the
# tampered/partial shape the anchors-absence guard refuses.
_V3_EPISODES_ONLY_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT,
  version INTEGER NOT NULL DEFAULT 0,
  trust TEXT NOT NULL DEFAULT 'quarantined',
  superseded_by INTEGER,
  last_active_ts INTEGER NOT NULL DEFAULT 0,
  polarity TEXT NOT NULL DEFAULT 'neutral',
  kind TEXT NOT NULL DEFAULT 'note',
  meta TEXT NOT NULL DEFAULT '');
"""

_DEAD_EPISODE_COLUMNS = {"anchor", "provenance", "approved_by", "approved_ts", "tags"}


def _cols(conn, table: str = "episodes") -> dict[str, dict]:
    return {r["name"]: dict(r) for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _indexes(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }


# ── DDL: the v3 table set on a fresh store ─────────────────────────────────────
def test_v3_tables_present():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    assert {
        "episodes",
        "episode_anchors",
        "repos",
        "anchor_drift",
        "ref_requests",
        "blobs",
        "exposure",
        "meta",
        "recall_misses",
        "evidence_events",
        "conflict_flags",
        "ingested_ranges",
    } <= _tables(conn)
    assert {"idx_episodes_trust", "idx_misses_ts", "idx_evidence_episode"} <= _indexes(
        conn
    )


def test_dead_episode_columns_gone_from_ddl():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    cols = set(_cols(conn))
    assert not (_DEAD_EPISODE_COLUMNS & cols), (
        f"schema v3 drops the legacy columns — still present: "
        f"{sorted(_DEAD_EPISODE_COLUMNS & cols)}"
    )
    # the CT-12 substring pin: the stored CREATE TABLE text itself is clean
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='episodes'"
    ).fetchone()["sql"]
    assert "provenance" not in ddl and "approved_by" not in ddl


def test_v3_lifecycle_columns_and_defaults():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    ep_cols = _cols(conn)
    assert {
        "trust",
        "superseded_by",
        "last_active_ts",
        "polarity",
        "kind",
        "meta",
    } <= set(ep_cols)
    assert ep_cols["trust"]["dflt_value"] == "'quarantined'"  # fail-safe default
    assert ep_cols["trust"]["notnull"] == 1
    assert ep_cols["kind"]["dflt_value"] == "'note'"  # under-claiming default
    assert ep_cols["meta"]["dflt_value"] == "''"


def test_episode_anchors_shape():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    cols = _cols(conn, "episode_anchors")
    assert set(cols) == {"episode_id", "repo", "anchor", "fp_meta"}
    assert cols["anchor"]["dflt_value"] == "''"  # '' = scope-only membership
    assert cols["fp_meta"]["dflt_value"] == "''"
    # PK(episode_id, repo, anchor): the same binding can never land twice
    conn.execute(
        "INSERT INTO episode_anchors(episode_id, repo, anchor) "
        "VALUES(1, 'alpha', 'a.py::f')"
    )
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO episode_anchors(episode_id, repo, anchor) "
            "VALUES(1, 'alpha', 'a.py::f')"
        )


def test_repos_registry_shape():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    cols = _cols(conn, "repos")
    assert set(cols) == {"name", "url", "canonical_ref", "token_env", "added_ts"}
    assert cols["token_env"]["dflt_value"] == "''"  # the env-var NAME, never a secret


def test_anchor_drift_and_ref_requests_shape():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    drift = _cols(conn, "anchor_drift")
    assert set(drift) == {"repo", "tip_sha", "anchor", "verdict", "detail", "ts"}
    assert drift["detail"]["dflt_value"] == "'{}'"
    reqs = _cols(conn, "ref_requests")
    assert set(reqs) == {"repo", "ref", "last_requested_ts"}
    # PK(repo, tip_sha, anchor) on the drift cache
    conn.execute(
        "INSERT INTO anchor_drift(repo, tip_sha, anchor, verdict, ts) "
        "VALUES('a', 't', 'x', 'fresh', 0)"
    )
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO anchor_drift(repo, tip_sha, anchor, verdict, ts) "
            "VALUES('a', 't', 'x', 'fresh', 1)"
        )


def test_recall_misses_carries_repos_scope():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    cols = _cols(conn, "recall_misses")
    assert "repos" in cols
    assert cols["repos"]["dflt_value"] == "''" and cols["repos"]["notnull"] == 1
    # the DDL default holds on a raw INSERT naming no repos column ('' = global miss)
    conn.execute(
        "INSERT INTO recall_misses(query_text, agent_id, miss_type, ts) "
        "VALUES('q', 'a', 'no_match', 0)"
    )
    r = conn.execute("SELECT repos FROM recall_misses").fetchone()
    assert r["repos"] == ""
    # miss_type stays CHECK-constrained to the three recorded non-answer kinds
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO recall_misses(query_text, agent_id, miss_type, ts) "
            "VALUES('q', 'a', 'bogus', 0)"
        )


def test_kind_and_polarity_checks_still_enforced():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO episodes(tenant_id, text, weight, ts, content_hash, "
            "status, kind) VALUES('t','x',1.0,0,'h','pending','rumor')"
        )
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO episodes(tenant_id, text, weight, ts, content_hash, "
            "status, polarity) VALUES('t','x',1.0,0,'h','pending','maybe')"
        )


def test_reconstruction_is_idempotent():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    before = set(_cols(conn))
    SqliteEpisodeStore(conn)  # second construction on the same conn
    assert set(_cols(conn)) == before  # nothing added twice, no raise


# ── the refusal posture: presence of legacy columns / absence of v3 shape ──────
def test_v2_shaped_store_refused_with_no_migration_message():
    conn = connect(":memory:")
    conn.executescript(_V2_EPISODES_SCHEMA)
    with pytest.raises(RuntimeError, match="migration") as excinfo:
        SqliteEpisodeStore(conn)
    assert "clean store" in str(excinfo.value)


def test_v2_refusal_message_is_deterministic():
    # a restored v2 backup refuses with the SAME message every time (CT-14's
    # store-level half: the operator sees one clear, stable instruction).
    messages = []
    for _ in range(2):
        conn = connect(":memory:")
        conn.executescript(_V2_EPISODES_SCHEMA)
        with pytest.raises(RuntimeError) as excinfo:
            SqliteEpisodeStore(conn)
        messages.append(str(excinfo.value))
    assert messages[0] == messages[1]


def test_pre_lifecycle_store_refused():
    conn = connect(":memory:")
    conn.executescript(_PRE_LIFECYCLE_EPISODES_SCHEMA)
    with pytest.raises(RuntimeError, match="migration"):
        SqliteEpisodeStore(conn)


def test_v3_episodes_without_episode_anchors_refused():
    # a legacy-column-free episodes table WITHOUT its episode_anchors twin is a
    # tampered/partial store — CREATE IF NOT EXISTS would bolt the table on and
    # silently serve every memory as GENERAL (scope lost). Refuse instead.
    conn = connect(":memory:")
    conn.executescript(_V3_EPISODES_ONLY_SCHEMA)
    with pytest.raises(RuntimeError, match="migration") as excinfo:
        SqliteEpisodeStore(conn)
    assert "episode_anchors" in str(excinfo.value)


def test_fresh_empty_store_boots():
    # an ABSENT episodes table is not a refusal — the script creates the v3 shape.
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    assert "episode_anchors" in _tables(conn)


def test_default_insert_lands_quarantined():
    # stage() names no trust ⇒ the column default applies: a forgotten future
    # write-site fails SAFE (lands unserved, never over-served).
    conn = connect(":memory:")
    s = SqliteEpisodeStore(conn)
    eid, _ = s.stage(text="x", weight=1.0, proposed_by="a")
    assert s.get_episode(eid).trust == QUARANTINED
