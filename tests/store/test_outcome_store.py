"""CV6 — task_outcomes v2: the settled-credit ledger (CONVERGENCE §8.2 shape).

`record_outcome` upserts keyed (commit_sha, episode_id) — INSERT OR IGNORE is the
idempotency contract that makes full-history re-scans free (double credit is
unconstructable); `exposures_by_trace` returns the credit set behind one recall
envelope; a legacy Phase-0 clawback-shaped task_outcomes table is REFUSED at store
construction (the episodes-guard precedent — clean-store regime, no migration).
Conns go through the prod connect() factory.
"""
from __future__ import annotations

import sqlite3

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore


@pytest.fixture()
def store():
    return SqliteEpisodeStore(connect(":memory:"))


def _row(**kw):
    base = dict(commit_sha="abc123", episode_id=7, trace_id="t1", repo="team/repo",
                outcome="win", recall_margin=0.4, commit_ts=100, ingested_ts=200)
    base.update(kw)
    return base


def test_record_outcome_idempotent_keyed_sha_eid(store):
    assert store.record_outcome(**_row()) is True
    assert store.record_outcome(**_row()) is False           # same (sha,eid) → ignored
    n = store.conn.execute("SELECT COUNT(*) AS c FROM task_outcomes").fetchone()["c"]
    assert n == 1
    assert store.record_outcome(**_row(episode_id=8)) is True   # new eid → new row
    assert store.record_outcome(**_row(commit_sha="def9")) is True  # new sha → new row


def test_outcome_check_rejects_unknown_label(store):
    with pytest.raises(sqlite3.IntegrityError):               # CHECK(outcome IN win|loss)
        store.record_outcome(**_row(outcome="meh"))


def test_exposures_by_trace_returns_credit_set(store):
    store.record_exposure("tr-1", [(3, 0.9), (4, 0.2)], agent_id="a", ts=50)
    store.record_exposure("tr-2", [(5, 0.7)], agent_id="b", ts=60)
    assert store.exposures_by_trace("tr-1") == [(3, 0.9), (4, 0.2)]
    assert store.exposures_by_trace("ghost") == []            # unknown trace → empty, not error


def test_outcome_totals_counts_rows_and_distinct_episodes(store):
    store.record_outcome(**_row())
    store.record_outcome(**_row(commit_sha="def9", outcome="loss"))
    store.record_outcome(**_row(commit_sha="def9", episode_id=8))
    assert store.outcome_totals() == {"settled_rows": 3, "distinct_episodes": 2}


def test_legacy_task_outcomes_shape_refused():
    conn = connect(":memory:")
    conn.execute(
        "CREATE TABLE task_outcomes("
        "task_ref TEXT NOT NULL, trace_id TEXT NOT NULL, family_scope TEXT NOT NULL, "
        "state TEXT NOT NULL, reward REAL NOT NULL, merge_ts INTEGER NOT NULL, "
        "settle_at INTEGER NOT NULL, PRIMARY KEY(task_ref, trace_id))")
    with pytest.raises(RuntimeError):
        SqliteEpisodeStore(conn)
