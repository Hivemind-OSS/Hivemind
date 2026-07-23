"""SqliteEpisodeStore.deprecate — the bare retirement (no replacement) behind hive_prune.
Flips trust→deprecated, writes ONE 'prune' audit, leaves superseded_by NULL (distinct from
supersede's retire-with-replacement), de-indexes a servable row, and is idempotent. NOT a hard
delete: the row + its evidence_events stay in the append-only ledger."""

from __future__ import annotations

import numpy as np

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.evidence_kinds import EVIDENCE_KINDS

DIM = 4


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"), index=ExhaustiveCosineIndex(DIM))


def _established(s: SqliteEpisodeStore, text: str, *, ts: int = 10) -> int:
    eid, _ = s.stage(text=text, weight=1.0, proposed_by="writer", ts=ts)
    assert s.complete(
        eid,
        np.eye(DIM, dtype=np.float32)[0],
        expected_version=0,
        trust="established",
        last_active_ts=ts,
    )
    return eid


def test_deprecate_flips_trust_and_writes_prune_audit():
    s = _store()
    eid = _established(s, "an incorrect memory")
    assert s.deprecate(eid, actor="human", ts=20) is True
    ep = s.get_episode(eid)
    assert ep.trust == "deprecated"
    assert ep.superseded_by is None  # bare retirement: NO replacement
    rows = s.conn.execute(
        "SELECT kind, actor FROM evidence_events WHERE episode_id=?", (eid,)
    ).fetchall()
    assert [(r["kind"], r["actor"]) for r in rows] == [("prune", "human")]
    # the write site emits a registry member (evidence_events has no DDL CHECK)
    assert {r["kind"] for r in rows} <= EVIDENCE_KINDS


def test_deprecate_de_indexes_so_not_recalled():
    s = _store()
    eid = _established(s, "served then pruned")
    assert s.index.size() == 1
    s.deprecate(eid, actor="human", ts=20)
    assert s.index.size() == 0  # dropped from the warm cache
    # and it is no longer a servable candidate
    assert eid not in {i for i, _v in s.scan_approved()}


def test_deprecate_unknown_id_is_false_noop():
    s = _store()
    assert s.deprecate(9999, actor="human", ts=20) is False


def test_deprecate_is_idempotent_no_duplicate_audit():
    s = _store()
    eid = _established(s, "prune me twice")
    assert s.deprecate(eid, actor="human", ts=20) is True
    assert (
        s.deprecate(eid, actor="human", ts=21) is False
    )  # already retired ⇒ no change
    rows = s.conn.execute(
        "SELECT COUNT(*) AS c FROM evidence_events WHERE episode_id=? AND kind='prune'",
        (eid,),
    ).fetchone()
    assert rows["c"] == 1  # exactly one audit, never doubled


def test_deprecate_a_quarantined_row_kills_it_before_promotion():
    # a malicious capture can be pruned while still quarantined (status='approved', never indexed).
    s = _store()
    eid, _ = s.stage(text="poison candidate", weight=1.0, proposed_by="attacker", ts=10)
    assert s.complete(
        eid,
        np.eye(DIM, dtype=np.float32)[0],
        expected_version=0,
        trust="quarantined",
        last_active_ts=10,
    )
    assert s.deprecate(eid, actor="human", ts=20) is True
    assert s.get_episode(eid).trust == "deprecated"
    # it can never become a promotion candidate now
    assert eid not in {
        row[0] for row in s.quarantined_candidates(now=20, quarantine_ttl_s=10**9)
    }
