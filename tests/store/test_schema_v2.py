"""Schema v2 — trust/lifecycle columns, the recall_misses + evidence_events tables.
Episode invariant v2 + RecallHit label-carrier pins live here too.

There is NO migration/backfill path: this build starts from a clean, empty store
(human decision — prior-version memories are not carried over; context loading is a
later concern). A store whose episodes table predates the lifecycle columns is
REFUSED at construction, loudly, instead of crashing mid-recall on a missing column.
Connections come from the prod ``connect()`` factory — a hand-rolled test conn with
default (deferred) isolation diverges from the autocommit+BEGIN IMMEDIATE discipline
the store is written against.
"""
from __future__ import annotations

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.lifecycle import QUARANTINED
from hive.domain.models import Episode, RecallHit, content_hash

# The pre-trust episodes table, verbatim as it shipped — used ONLY to prove the
# old-format store is refused (snapshot, deliberately not imported).
_OLD_EPISODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL, source TEXT, tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0);
"""


# The v2 episodes table BEFORE the kind/anchor label columns — has trust+polarity
# but lacks kind/anchor; used ONLY to prove a half-migrated (kind-less) store is refused.
_V2_NO_KIND_EPISODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL, source TEXT, tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0,
  trust TEXT NOT NULL DEFAULT 'quarantined',
  superseded_by INTEGER,
  last_active_ts INTEGER NOT NULL DEFAULT 0,
  polarity TEXT NOT NULL DEFAULT 'neutral' CHECK(polarity IN ('do','dont','neutral')));
"""


# The schema BEFORE the provenance column — has every lifecycle + kind/anchor column
# but lacks provenance; used ONLY to prove a store predating provenance is refused.
_NO_PROVENANCE_EPISODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL, source TEXT, tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0,
  trust TEXT NOT NULL DEFAULT 'quarantined',
  superseded_by INTEGER,
  last_active_ts INTEGER NOT NULL DEFAULT 0,
  polarity TEXT NOT NULL DEFAULT 'neutral' CHECK(polarity IN ('do','dont','neutral')),
  kind TEXT NOT NULL DEFAULT 'note',
  anchor TEXT NOT NULL DEFAULT '');
"""


def _cols(conn, table: str) -> dict[str, dict]:
    return {r["name"]: dict(r) for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


# ── DDL: columns / tables / indexes on a fresh store ───────────────────────────
def test_new_columns_and_tables_present():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    ep_cols = _cols(conn, "episodes")
    assert {"trust", "superseded_by", "last_active_ts"} <= set(ep_cols)
    assert ep_cols["trust"]["dflt_value"] == "'quarantined'"   # fail-safe default
    assert ep_cols["trust"]["notnull"] == 1
    assert "agent_id" in _cols(conn, "exposure")
    assert {"recall_misses", "evidence_events"} <= _tables(conn)
    assert {"idx_episodes_trust", "idx_misses_ts", "idx_evidence_episode"} <= _indexes(conn)
    # miss_type is CHECK-constrained to the three recorded non-answer kinds
    with pytest.raises(Exception):
        conn.execute("INSERT INTO recall_misses(query_text, agent_id, miss_type, ts) "
                     "VALUES('q','a','bogus',0)")


def test_reconstruction_is_idempotent():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    before = set(_cols(conn, "episodes"))
    SqliteEpisodeStore(conn)                     # second construction on the same conn
    assert set(_cols(conn, "episodes")) == before   # nothing added twice, no raise


def test_old_schema_store_refused():
    # No in-place migration exists: an episodes table without the lifecycle columns
    # must be refused at CONSTRUCTION with a clear message, never limp to a
    # mid-recall "no such column" crash.
    conn = connect(":memory:")
    conn.executescript(_OLD_EPISODES_SCHEMA)
    with pytest.raises(RuntimeError, match="clean"):
        SqliteEpisodeStore(conn)


def test_kind_anchor_columns_present_and_constrained():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    ep_cols = _cols(conn, "episodes")
    assert {"kind", "anchor"} <= set(ep_cols)
    assert ep_cols["kind"]["dflt_value"] == "'note'"     # fail-safe under-claiming default
    assert ep_cols["kind"]["notnull"] == 1
    assert ep_cols["anchor"]["dflt_value"] == "''"
    assert ep_cols["anchor"]["notnull"] == 1
    # kind is CHECK-constrained to the registry vocabulary — a bad kind is refused at the DDL
    with pytest.raises(Exception):
        conn.execute("INSERT INTO episodes(tenant_id, text, weight, ts, content_hash, "
                     "status, kind) VALUES('t','x',1.0,0,'h','pending','rumor')")


def test_store_missing_kind_refused():
    # A store with trust+polarity but no kind/anchor is the half-migrated trap: the
    # CREATE IF NOT EXISTS would no-op and the store would limp to a mid-write "no such
    # column" crash. The predates guard must refuse it, same as the pre-trust schema.
    conn = connect(":memory:")
    conn.executescript(_V2_NO_KIND_EPISODES_SCHEMA)
    with pytest.raises(RuntimeError, match="kind"):
        SqliteEpisodeStore(conn)


def test_provenance_column_present_and_constrained():
    conn = connect(":memory:")
    SqliteEpisodeStore(conn)
    ep_cols = _cols(conn, "episodes")
    assert "provenance" in ep_cols
    assert ep_cols["provenance"]["dflt_value"] == "'agent_reasoned'"  # fail-safe under-claim
    assert ep_cols["provenance"]["notnull"] == 1
    # provenance is CHECK-constrained to the registry vocabulary — a bad value is refused at the DDL
    with pytest.raises(Exception):
        conn.execute("INSERT INTO episodes(tenant_id, text, weight, ts, content_hash, "
                     "status, provenance) VALUES('t','x',1.0,0,'h','pending','rumored')")


def test_store_missing_provenance_refused():
    # A store with every lifecycle + kind/anchor column but no provenance predates the
    # current schema: the CREATE IF NOT EXISTS would no-op and the store would limp to a
    # mid-read "no such column" crash. The predates guard must refuse it loudly.
    conn = connect(":memory:")
    conn.executescript(_NO_PROVENANCE_EPISODES_SCHEMA)
    with pytest.raises(RuntimeError, match="provenance"):
        SqliteEpisodeStore(conn)


def test_default_insert_lands_quarantined():
    # stage() names no trust ⇒ the column default applies: a forgotten future
    # write-site fails SAFE (lands unserved, never over-served).
    conn = connect(":memory:")
    s = SqliteEpisodeStore(conn)
    eid, _ = s.stage(text="x", weight=1.0, tags="", proposed_by="a")
    assert s.get_episode(eid).trust == QUARANTINED


# ── Episode invariants v2 ──────────────────────────────────────────────────────
def _ep(**kw) -> Episode:
    base = dict(id=1, tenant_id="t", text="hello", weight=1.0, ts=0, tags="", content_hash=content_hash("hello"), status="pending",
                proposed_by="a")
    base.update(kw)
    return Episode(**base)


def test_episode_invariants_v2():
    with pytest.raises(ValueError):
        _ep(trust="bogus")                                       # unknown trust label
    with pytest.raises(ValueError):
        _ep(status="pending", approved_by="u")                   # approver ⇒ approved
    with pytest.raises(ValueError):
        _ep(status="pending", trust="established")               # servable trust ⇒ approved
    with pytest.raises(ValueError):
        _ep(status="pending", trust="provisional")
    with pytest.raises(ValueError):
        _ep(superseded_by=9)                                     # a live row cannot point at a successor
    # the autonomous-capture shape: approved, no approver, quarantined — constructs
    auto = _ep(status="approved", approved_by=None, trust="quarantined")
    assert auto.trust == QUARANTINED and auto.approved_by is None
    # a retired row: deprecated + successor stamped — constructs
    dead = _ep(status="approved", approved_by="u", approved_ts=0,
               trust="deprecated", superseded_by=9)
    assert dead.superseded_by == 9


def test_recall_hit_carries_trust_and_ts():
    h = RecallHit(episode_id=3, text="t", sim=0.9, trust="provisional", ts=123)
    assert h.trust == "provisional" and h.ts == 123
    # fail-safe defaults: an unwired construction site under-claims, never over-claims
    bare = RecallHit(episode_id=3, text="t", sim=0.9)
    assert bare.trust == QUARANTINED and bare.ts == 0
