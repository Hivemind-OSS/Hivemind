"""SqliteEpisodeStore.settled_wins — the subset of requested ids carrying >= 1 settled
win audit: ``outcome_helped`` (self-reported via hive_outcome) OR ``outcome_verified_helped``
(SHA-bound census corroboration) — the union form. Read-only; powers the suspect-consensus
martingale clause. Conforms to the SettledWinReader port. ``verified_wins`` is the
verified-only sibling read the verified-promotion rung consumes."""

from __future__ import annotations

import numpy as np

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.ports import SettledWinReader

DIM = 4


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"), index=ExhaustiveCosineIndex(DIM))


def _seed(s: SqliteEpisodeStore, text: str) -> int:
    eid, _ = s.stage(text=text, weight=1.0, proposed_by="w", ts=10)
    s.complete(
        eid,
        np.eye(DIM, dtype=np.float32)[0],
        expected_version=0,
        trust="provisional",
        last_active_ts=10,
    )
    return eid


def test_real_store_satisfies_settled_win_reader():
    assert isinstance(SqliteEpisodeStore(connect(":memory:")), SettledWinReader)


def test_id_with_a_helped_event_is_a_settled_win():
    s = _store()
    eid = _seed(s, "helped memory")
    s.insert_audit(eid, "outcome_helped", "agent", 11, "{}")
    assert s.settled_wins([eid]) == {eid}


def test_id_without_a_helped_event_is_omitted():
    s = _store()
    eid = _seed(s, "no helped event")
    assert s.settled_wins([eid]) == set()


def test_only_helped_counts_not_hurt():
    # an outcome_hurt audit is NOT a settled win (only outcome_helped corroborates).
    s = _store()
    eid = _seed(s, "hurt only")
    s.insert_audit(eid, "outcome_hurt", "agent", 11, "{}")
    assert s.settled_wins([eid]) == set()


def test_multiple_helped_events_dedup_to_one_id():
    s = _store()
    eid = _seed(s, "helped twice")
    s.insert_audit(eid, "outcome_helped", "agentA", 11, "{}")
    s.insert_audit(eid, "outcome_helped", "agentB", 12, "{}")
    assert s.settled_wins([eid]) == {eid}  # the id appears once


def test_mixed_ids_returns_only_the_helped_subset():
    s = _store()
    a, b = _seed(s, "won"), _seed(s, "not won")
    s.insert_audit(a, "outcome_helped", "agent", 11, "{}")
    assert s.settled_wins([a, b]) == {a}


def test_empty_ids_returns_empty_set():
    assert _store().settled_wins([]) == set()


# ── the union: a verified win settles exactly like a self-reported one ─────────


def test_verified_helped_alone_is_a_settled_win():
    s = _store()
    eid = _seed(s, "verified win only")
    s.insert_audit(eid, "outcome_verified_helped", "census", 11, "{}")
    assert s.settled_wins([eid]) == {eid}


def test_union_pins_both_sources_and_excludes_hurt_kinds():
    s = _store()
    self_only = _seed(s, "self-reported only")
    verified_only = _seed(s, "census-verified only")
    hurt_only = _seed(s, "hurt only, never a win")
    s.insert_audit(self_only, "outcome_helped", "agent", 11, "{}")
    s.insert_audit(verified_only, "outcome_verified_helped", "census", 11, "{}")
    s.insert_audit(hurt_only, "outcome_hurt", "agent", 11, "{}")
    s.insert_audit(hurt_only, "outcome_verified_hurt", "census", 11, "{}")
    assert s.settled_wins([self_only, verified_only, hurt_only]) == {
        self_only,
        verified_only,
    }


# ── verified_wins: the verified-ONLY read the promotion rung consumes ──────────


def test_verified_wins_counts_only_the_verified_helped_kind():
    s = _store()
    self_report = _seed(s, "helped by self-report")
    verified = _seed(s, "helped by census verification")
    hurt = _seed(s, "verified hurt")
    s.insert_audit(self_report, "outcome_helped", "agent", 11, "{}")
    s.insert_audit(verified, "outcome_verified_helped", "census", 11, "{}")
    s.insert_audit(hurt, "outcome_verified_hurt", "census", 11, "{}")
    assert s.verified_wins([self_report, verified, hurt]) == {verified}


def test_verified_wins_empty_ids_returns_empty_set():
    assert _store().verified_wins([]) == set()
