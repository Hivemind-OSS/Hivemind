"""C6 — the conflict_flags durable write seam: DDL (kind/status CHECK + UNIQUE pair),
record_conflict_flag (idempotent on the canonical (a_id,b_id,kind), returns bool),
open_conflict_flags (status='open' read), and the ConflictFlagStore port conformance.
The container-level required-table registration is the container suite's pin — a
store-level test never imports the app layer.

A flag stores IDS, not hashes (hash derivable on read); status keeps only 'open'/'dismissed'
— 'resolved' is implicit (the report hides a flag once either episode is non-servable)."""
from __future__ import annotations

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.ports import ConflictFlagStore


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"))


def _tables(conn) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


# ── DDL ────────────────────────────────────────────────────────────────────────
def test_conflict_flags_table_and_unique_index_present():
    s = _store()
    assert "conflict_flags" in _tables(s.conn)
    assert "idx_conflict_flags_pair" in _indexes(s.conn)


def test_kind_check_rejects_out_of_vocabulary():
    # a raw insert of a bad kind is refused at the DDL (loosening the CHECK REDs this)
    s = _store()
    with pytest.raises(Exception):
        s.conn.execute(
            "INSERT INTO conflict_flags(kind, a_id, b_id, proposed_by, ts) "
            "VALUES('rumor', 1, 2, 'a', 0)")


def test_status_check_rejects_out_of_vocabulary():
    s = _store()
    with pytest.raises(Exception):
        s.conn.execute(
            "INSERT INTO conflict_flags(kind, a_id, b_id, proposed_by, ts, status) "
            "VALUES('conflict', 1, 2, 'a', 0, 'bogus')")


# ── record_conflict_flag (idempotent on canonical pair+kind) ───────────────────
def test_record_writes_then_idempotent_no_duplicate():
    s = _store()
    assert s.record_conflict_flag(kind="conflict", a_id=12, b_id=40, winner_id=None,
                                  resolution="", proposed_by="agent-A", ts=100) is True
    # re-flag of the SAME canonical pair+kind is a no-op (one row only)
    assert s.record_conflict_flag(kind="conflict", a_id=12, b_id=40, winner_id=None,
                                  resolution="changed", proposed_by="agent-B", ts=200) is False
    rows = s.open_conflict_flags()
    assert len(rows) == 1
    assert rows[0]["resolution"] == ""          # the FIRST write stands (no overwrite)


def test_distinct_kind_same_pair_coexist():
    # the UNIQUE key is (a_id, b_id, kind): a 'conflict' and a 'supersedes' on the same
    # pair are two distinct advisory facts.
    s = _store()
    assert s.record_conflict_flag(kind="conflict", a_id=1, b_id=2, winner_id=None,
                                  resolution="", proposed_by="a", ts=0) is True
    assert s.record_conflict_flag(kind="supersedes", a_id=1, b_id=2, winner_id=2,
                                  resolution="", proposed_by="a", ts=0) is True
    assert len(s.open_conflict_flags()) == 2


def test_record_rejects_bad_kind():
    # defense in depth: even the method path raises on a bad kind (the CHECK fires)
    s = _store()
    with pytest.raises(Exception):
        s.record_conflict_flag(kind="rumor", a_id=1, b_id=2, winner_id=None,
                               resolution="", proposed_by="a", ts=0)


def test_open_returns_open_only_dismissed_hidden():
    s = _store()
    s.record_conflict_flag(kind="conflict", a_id=1, b_id=2, winner_id=None,
                           resolution="", proposed_by="a", ts=0)
    # an operator dismisses it → it drops off the worklist
    s.conn.execute("UPDATE conflict_flags SET status='dismissed' WHERE a_id=1 AND b_id=2")
    assert s.open_conflict_flags() == []


def test_winner_id_roundtrips():
    s = _store()
    s.record_conflict_flag(kind="supersedes", a_id=5, b_id=9, winner_id=9,
                           resolution="9 is the corrected version", proposed_by="a", ts=3)
    row = s.open_conflict_flags()[0]
    assert row["winner_id"] == 9 and row["resolution"] == "9 is the corrected version"


# ── the ONE new narrow port ────────────────────────────────────────────────────
def test_real_store_satisfies_conflict_flag_store_port():
    # conformance against the REAL adapter, not just a fake (runtime_checkable checks
    # name-presence only — a port green with fake-only tests can leave the real adapter short).
    assert isinstance(_store(), ConflictFlagStore)
