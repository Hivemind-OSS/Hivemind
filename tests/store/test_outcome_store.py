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


# ── settle_loss: the one-way win→loss flip (revert settlement, monotone) ────────


def test_settle_loss_flips_only_win_rows_for_that_sha(store):
    store.record_outcome(**_row())                                    # abc123 / 7  win
    store.record_outcome(**_row(episode_id=8))                        # abc123 / 8  win
    store.record_outcome(**_row(commit_sha="def9"))                   # def9   / 7  win
    assert store.settle_loss("abc123", ts=999) == 2                   # rowcount returned
    flipped = store.conn.execute(
        "SELECT outcome, ingested_ts FROM task_outcomes WHERE commit_sha='abc123'"
    ).fetchall()
    assert all(r["outcome"] == "loss" and r["ingested_ts"] == 999 for r in flipped)
    other = store.conn.execute(
        "SELECT outcome FROM task_outcomes WHERE commit_sha='def9'").fetchone()
    assert other["outcome"] == "win"                                  # other shas untouched


def test_settle_loss_is_one_way_idempotent(store):
    store.record_outcome(**_row())
    assert store.settle_loss("abc123", ts=300) == 1
    assert store.settle_loss("abc123", ts=400) == 0                   # already loss → no flip
    row = store.conn.execute(
        "SELECT outcome, ingested_ts FROM task_outcomes").fetchone()
    assert row["outcome"] == "loss"
    assert row["ingested_ts"] == 300                                  # first flip's stamp kept


def test_settle_loss_unknown_sha_flips_nothing(store):
    store.record_outcome(**_row())
    assert store.settle_loss("nothere", ts=1) == 0
    row = store.conn.execute("SELECT outcome FROM task_outcomes").fetchone()
    assert row["outcome"] == "win"


# ── outcome_stats_for_episodes: (wins, losses) keyed eid, zero-default ──────────


def test_outcome_stats_counts_wins_and_losses_per_episode(store):
    store.record_outcome(**_row())                                    # 7 win
    store.record_outcome(**_row(commit_sha="def9"))                   # 7 win
    store.record_outcome(**_row(commit_sha="eee3", outcome="loss"))   # 7 loss
    store.record_outcome(**_row(commit_sha="def9", episode_id=8, outcome="loss"))  # 8 loss
    stats = store.outcome_stats_for_episodes([7, 8, 99])
    assert stats == {7: (2, 1), 8: (0, 1), 99: (0, 0)}                # 99: zero-default


def test_outcome_stats_empty_input_returns_empty(store):
    assert store.outcome_stats_for_episodes([]) == {}
