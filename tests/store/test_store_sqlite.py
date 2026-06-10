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
    assert s.fetch(content_hash("db pool exhausted")) == "db pool exhausted"


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


# ── port conformance [C1] ─────────────────────────────────────────────────────
def test_fake_and_sqlite_store_satisfy_protocol():
    assert isinstance(FakeStore(), EpisodeStore)
    assert isinstance(SqliteEpisodeStore(connect(":memory:")), EpisodeStore)
