"""P1.3 — M03 episode store: admission CAS state machine + approved-only recall [B3]."""
from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore, _RECALL_PREDICATE
from hive.domain.models import Episode, content_hash
from hive.domain.ports import EpisodeStore
from tests.fakes import FakeStore

DIM = 4


def _unit(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def _store():
    conn = connect(":memory:")
    return SqliteEpisodeStore(conn, index=ExhaustiveCosineIndex(DIM))


def _ep(**kw):
    base = dict(id=1, tenant_id="t", text="hello", weight=1.0, ts=0, source="s", tags="",
                content_hash=content_hash("hello"), status="pending", proposed_by="a")
    base.update(kw)
    return Episode(**base)


# ── frozen Episode invariants ─────────────────────────────────────────────────
def test_content_hash_binds_text():
    with pytest.raises(ValueError):
        _ep(content_hash="deadbeef")            # hash ≠ sha256(text)


def test_episode_rejects_bad_value():
    with pytest.raises(ValueError):
        _ep(value=np.zeros(DIM, dtype=np.float64))   # float64
    with pytest.raises(ValueError):
        _ep(value=np.zeros((DIM, DIM), dtype=np.float32))  # 2-D


def test_episode_rejects_approver_status_mismatch():
    with pytest.raises(ValueError):
        _ep(status="pending", approved_by="u")           # approver on a pending row
    # trust-v2 relaxation: approved WITHOUT an approver is the autonomous-capture
    # shape (trust defaults to quarantined) — constructable, but it under-claims.
    ep = _ep(status="approved", approved_by=None)
    assert ep.trust == "quarantined"


# ── stage / approve / reject ──────────────────────────────────────────────────
def test_stage_never_indexes():
    s = _store()
    eid, deduped = s.stage(text="db pool exhausted", weight=1.0, source="mark", tags="", proposed_by="a")
    assert not deduped
    ep = s.get_episode(eid)
    assert ep.status == "pending" and ep.version == 0 and ep.value is None
    assert s.index.size() == 0                  # NOT indexed while pending
    blob = s.conn.execute("SELECT text FROM blobs WHERE content_hash=?",
                          (content_hash("db pool exhausted"),)).fetchone()
    assert blob is not None and blob["text"] == "db pool exhausted"   # staged blob present


def test_stage_dedup_same_text():
    s = _store()
    a, d1 = s.stage(text="same", weight=1.0, source="m", tags="", proposed_by="a")
    b, d2 = s.stage(text="same", weight=2.0, source="m", tags="", proposed_by="a")
    assert a == b and d2 is True and len(s.list_pending()) == 1


def test_approve_flips_and_indexes():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, source="m", tags="", proposed_by="a")
    assert s.approve(eid, "human", _unit(1, 0, 0, 0), expected_version=0) is True
    ep = s.get_episode(eid)
    assert ep.status == "approved" and ep.approved_by == "human" and ep.value is not None
    assert s.index.search(_unit(1, 0, 0, 0), k=1)[0][0] == eid   # recallable


def test_approve_is_idempotent():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, source="m", tags="", proposed_by="a")
    s.approve(eid, "human", _unit(1, 0, 0, 0), expected_version=0)
    assert s.approve(eid, "human2", _unit(0, 1, 0, 0), expected_version=0) is True  # no-op
    assert s.get_episode(eid).approved_by == "human"             # unchanged


def test_cas_blocks_stale_approve():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, source="m", tags="", proposed_by="a")
    assert s.approve(eid, "human", _unit(1, 0, 0, 0), expected_version=99) is False  # stale
    assert s.get_episode(eid).status == "pending"               # no flip


def test_reject_drops_and_never_indexes():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, source="m", tags="", proposed_by="a")
    s.reject(eid)
    assert s.get_episode(eid) is None and s.index.size() == 0


# ── approved-only recall (two fail-closed defenses) ───────────────────────────
def test_pending_never_in_candidates_single_predicate():
    s = _store()
    a, _ = s.stage(text="approved one", weight=1.0, source="m", tags="", proposed_by="a")
    p, _ = s.stage(text="pending one", weight=1.0, source="m", tags="", proposed_by="a")
    s.approve(a, "human", _unit(1, 0, 0, 0), expected_version=0)
    ids = {eid for eid, _ in s.scan_approved()}
    assert ids == {a} and p not in ids
    # single source of "recallable": SQL prefilters materialized rows; the full
    # decision is lifecycle.is_servable inside scan_servable (scan_approved = its
    # no-clock fail-closed alias)
    assert _RECALL_PREDICATE == "status='approved' AND value IS NOT NULL"


def test_pending_value_is_null():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, source="m", tags="", proposed_by="a")
    assert s.get_episode(eid).value is None           # value computed at approve, not stage


# ── boot-rebuild crash recovery [B3] ──────────────────────────────────────────
def test_rebuild_recovers_after_crash_between_commit_and_add():
    s = _store()
    e1, _ = s.stage(text="one", weight=1.0, source="m", tags="", proposed_by="a")
    e2, _ = s.stage(text="two", weight=1.0, source="m", tags="", proposed_by="a")
    s.approve(e1, "human", _unit(1, 0, 0, 0), expected_version=0)
    # simulate a crash: the index add for e2 raises AFTER the row is durably approved
    orig = s.index.sync_approved
    def boom(eid, val):
        if eid == e2:
            raise RuntimeError("crash between commit and index add")
        return orig(eid, val)
    s.index.sync_approved = boom
    s.approve(e2, "human", _unit(0, 1, 0, 0), expected_version=0)   # returns True (durable)
    s.index.sync_approved = orig
    assert s.get_episode(e2).status == "approved"      # durable truth
    assert s.index.size() == 1                          # warm cache diverged (missing e2)
    s.rebuild_index_from_store()                        # the recovery guarantee
    assert s.index.size() == 2
    assert {eid for eid, _ in [(e1, 0), (e2, 0)]} == {e1, e2}
    assert s.index.search(_unit(0, 1, 0, 0), k=2)[0][0] == e2


# ── polarity (do|dont|neutral): clean-store schema bump + carried label ───────
def test_fresh_store_has_polarity_column():
    s = _store()
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(episodes)")}
    assert "polarity" in cols                    # the DDL carries it on a fresh store


def test_polarity_check_rejects_out_of_enum_value():
    s = _store()
    # the CHECK constraint is the storage-layer enforcement point: a direct INSERT of a
    # bad polarity is refused regardless of any domain/wire guard above it.
    with pytest.raises(Exception):
        s.conn.execute(
            "INSERT INTO episodes(tenant_id, text, weight, ts, content_hash, status, "
            "version, polarity) VALUES('t','x',1.0,0,'h','pending',0,'maybe')")


def test_stage_roundtrips_polarity_default_neutral():
    s = _store()
    eid, _ = s.stage(text="default polarity", weight=1.0, source="m", tags="", proposed_by="a")
    assert s.get_episode(eid).polarity == "neutral"     # fail-safe default


def test_stage_roundtrips_explicit_dont():
    s = _store()
    eid, _ = s.stage(text="never run rm -rf on the volume", weight=1.0, source="m",
                     tags="", proposed_by="a", polarity="dont")
    assert s.get_episode(eid).polarity == "dont"        # the prohibition survives the round-trip


def test_legacy_store_without_polarity_is_refused_at_open():
    """The no-migration contract: a populated episodes table missing ``polarity`` is
    REFUSED at open with the clean-store RuntimeError — NOT a silent KeyError at the
    first recall (get_episode reading r['polarity'])."""
    conn = connect(":memory:")
    # build a TRUST-aware but pre-polarity episodes table (has trust, lacks polarity),
    # then populate it so the boot guard's `cols and …` predicate fires.
    conn.execute(
        "CREATE TABLE episodes(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, "
        "text TEXT, value BLOB, weight REAL, ts INTEGER, source TEXT, tags TEXT, "
        "content_hash TEXT, status TEXT, proposed_by TEXT, approved_by TEXT, "
        "approved_ts INTEGER, version INTEGER, trust TEXT, superseded_by INTEGER, "
        "last_active_ts INTEGER)")
    conn.execute("INSERT INTO episodes(tenant_id, text) VALUES('t','legacy row')")
    with pytest.raises(RuntimeError, match="clean store/volume"):
        SqliteEpisodeStore(conn)


# ── co-access edges (Phase 1 write accrual + Phase 2 neighbor read) ───────────
def test_co_access_canonicalizes_a_lt_b_into_one_row():
    """Inserting (b,a) then (a,b) must land ONE canonical (min,max) row, weight 2.0
    — the CHECK(a_id<b_id) + python min/max guarantee (a,b) and (b,a) never coexist."""
    s = _store()
    s.record_co_access([5, 2], ts=10)           # unordered → canonicalized to (2,5)
    s.record_co_access([2, 5], ts=20)           # same pair, other order → increments
    rows = s.conn.execute(
        "SELECT a_id, b_id, weight, last_co_ts FROM co_access_edges").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert (r["a_id"], r["b_id"]) == (2, 5)     # min,max canonical
    assert r["weight"] == 2.0                   # 1.0 default + 1.0 increment
    assert r["last_co_ts"] == 20                # latest co-access ts


def test_co_access_weight_increments_on_repeat():
    s = _store()
    for _ in range(3):
        s.record_co_access([1, 2], ts=0)
    w = s.conn.execute(
        "SELECT weight FROM co_access_edges WHERE a_id=1 AND b_id=2").fetchone()["weight"]
    assert w == 3.0


def test_co_access_fewer_than_two_distinct_is_noop():
    s = _store()
    s.record_co_access([7], ts=0)               # single eid → no pair
    s.record_co_access([7, 7], ts=0)            # one DISTINCT eid → no pair
    s.record_co_access([], ts=0)                # empty → no pair
    n = s.conn.execute("SELECT COUNT(*) AS c FROM co_access_edges").fetchone()["c"]
    assert n == 0


def test_co_access_writes_all_pairwise_edges():
    s = _store()
    s.record_co_access([1, 2, 3], ts=0)         # C(3,2)=3 canonical pairs
    pairs = {(r["a_id"], r["b_id"]) for r in s.conn.execute(
        "SELECT a_id, b_id FROM co_access_edges")}
    assert pairs == {(1, 2), (1, 3), (2, 3)}


def test_co_access_neighbors_both_directions_and_min_weight():
    """The Phase-2 reader returns the OTHER endpoint of every edge touching ``eid``
    (a_id OR b_id) at/above ``min_weight``, weight-descending."""
    s = _store()
    s.record_co_access([1, 2], ts=0)            # edge (1,2) w1 — below a min_weight=2 floor
    s.record_co_access([1, 3], ts=0)
    s.record_co_access([1, 3], ts=0)            # edge (1,3) w2 — eid 1 is the LOW endpoint
    s.record_co_access([3, 9], ts=0)
    s.record_co_access([3, 9], ts=0)            # edge (3,9) w2 — eid 9 reaches 3 via b-direction
    # neighbors of 3 at min_weight=2: 1 (via a_id=1,b_id=3) and 9 (via a_id=3,b_id=9); NOT 2 (w1)
    got = s.co_access_neighbors(3, min_weight=2.0)
    assert {eid for eid, _w in got} == {1, 9}
    assert all(w >= 2.0 for _eid, w in got)
    # weight-descending contract
    weights = [w for _eid, w in got]
    assert weights == sorted(weights, reverse=True)
    # the min_weight floor excludes the w1 edge to 2
    assert 2 not in {eid for eid, _w in s.co_access_neighbors(3, min_weight=2.0)}


# ── port conformance [C1] ─────────────────────────────────────────────────────
def test_fake_and_sqlite_store_satisfy_protocol():
    assert isinstance(FakeStore(), EpisodeStore)
    assert isinstance(SqliteEpisodeStore(connect(":memory:")), EpisodeStore)


def test_sqlite_store_satisfies_co_access_port():
    from hive.domain.ports import CoAccess
    assert isinstance(SqliteEpisodeStore(connect(":memory:")), CoAccess)
