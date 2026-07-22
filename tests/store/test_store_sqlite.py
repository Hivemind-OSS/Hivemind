"""M03 episode store v3: admission CAS state machine, approved-only recall [B3], and
the ``episode_anchors`` same-tx write (scope + code bindings land with their episode
or not at all). There is no approver anywhere — ``complete(trust=...)`` is the only
flip, and no store path accepts an approver identity or vouch timestamp."""
from __future__ import annotations

import inspect

import numpy as np

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore, _RECALL_PREDICATE
from hive.domain.lifecycle import ESTABLISHED, PROVISIONAL
from hive.domain.models import AnchorRef, content_hash
from hive.domain.ports import MetaStore
from tests.fakes import FakeStore

DIM = 4


def _unit(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def _store():
    conn = connect(":memory:")
    return SqliteEpisodeStore(conn, index=ExhaustiveCosineIndex(DIM))


def _anchor_rows(s: SqliteEpisodeStore, eid: int) -> list[tuple[str, str, str]]:
    return [(r["repo"], r["anchor"], r["fp_meta"]) for r in s.conn.execute(
        "SELECT repo, anchor, fp_meta FROM episode_anchors WHERE episode_id=? "
        "ORDER BY repo, anchor", (eid,))]


# ── stage / complete / reject ─────────────────────────────────────────────────
def test_stage_never_indexes():
    s = _store()
    eid, deduped = s.stage(text="db pool exhausted", weight=1.0, proposed_by="a")
    assert not deduped
    ep = s.get_episode(eid)
    assert ep.status == "pending" and ep.version == 0 and ep.value is None
    assert s.index.size() == 0                  # NOT indexed while pending
    blob = s.conn.execute("SELECT text FROM blobs WHERE content_hash=?",
                          (content_hash("db pool exhausted"),)).fetchone()
    assert blob is not None and blob["text"] == "db pool exhausted"   # staged blob present


def test_stage_dedup_same_text():
    s = _store()
    a, d1 = s.stage(text="same", weight=1.0, proposed_by="a")
    b, d2 = s.stage(text="same", weight=2.0, proposed_by="a")
    assert a == b and d2 is True and s.counts()[1] == 1   # one pending row ⇒ dedup, no 2nd insert


def test_complete_flips_and_indexes_servable():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, proposed_by="a")
    assert s.complete(eid, _unit(1, 0, 0, 0), expected_version=0,
                      trust=PROVISIONAL, last_active_ts=7) is True
    ep = s.get_episode(eid)
    assert ep.status == "approved" and ep.trust == PROVISIONAL and ep.value is not None
    assert ep.last_active_ts == 7
    assert s.index.search(_unit(1, 0, 0, 0), k=1)[0][0] == eid   # recallable


def test_complete_is_idempotent():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, proposed_by="a")
    s.complete(eid, _unit(1, 0, 0, 0), expected_version=0, trust=ESTABLISHED)
    assert s.complete(eid, _unit(0, 1, 0, 0), expected_version=0,
                      trust=PROVISIONAL) is True                 # no-op True
    assert s.get_episode(eid).trust == ESTABLISHED               # unchanged


def test_cas_blocks_stale_complete():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, proposed_by="a")
    assert s.complete(eid, _unit(1, 0, 0, 0), expected_version=99,
                      trust=PROVISIONAL) is False                # stale
    assert s.get_episode(eid).status == "pending"                # no flip


def test_reject_drops_row_and_anchor_rows():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, proposed_by="a",
                     anchors=[("alpha", "a.py::f")], repos=["beta"])
    assert _anchor_rows(s, eid)                                  # staged bindings present
    s.reject(eid)
    assert s.get_episode(eid) is None and s.index.size() == 0
    assert _anchor_rows(s, eid) == []                            # no orphaned bindings


# ── the v3 approver deletion: no store path accepts a vouch ───────────────────
def test_approve_is_gone_and_no_path_accepts_approver():
    s = _store()
    assert not hasattr(s, "approve")                             # the verb is deleted
    for method in (s.stage, s.complete, s.set_trust):
        params = inspect.signature(method).parameters
        assert "approver" not in params and "approved_ts" not in params, (
            f"{method.__name__} still accepts a vouch identity")
    # and the dropped stage params are really gone (CT conftest asserts the same)
    stage_params = inspect.signature(s.stage).parameters
    assert "tags" not in stage_params and "provenance" not in stage_params
    assert "anchors" in stage_params and "repos" in stage_params


# ── episode_anchors: written in stage's tx, projected by get_episode ──────────
def test_stage_writes_anchor_and_scope_rows_same_tx():
    s = _store()
    eid, _ = s.stage(text="anchored fact", weight=1.0, proposed_by="a",
                     anchors=[("alpha", "a.py::f"), ("alpha", "b.py::g")],
                     repos=["beta", "alpha"])
    # anchor pairs land verbatim; the already-covered repo gains NO extra scope row
    assert _anchor_rows(s, eid) == [
        ("alpha", "a.py::f", ""), ("alpha", "b.py::g", ""), ("beta", "", "")]


def test_stage_general_memory_has_zero_anchor_rows():
    s = _store()
    eid, _ = s.stage(text="a general fact", weight=1.0, proposed_by="a")
    assert _anchor_rows(s, eid) == []


def test_stage_deduplicates_anchor_pairs():
    s = _store()
    eid, _ = s.stage(text="doubled pair", weight=1.0, proposed_by="a",
                     anchors=[("alpha", "a.py::f"), ("alpha", "a.py::f")],
                     repos=["alpha", "alpha"])
    assert _anchor_rows(s, eid) == [("alpha", "a.py::f", "")]


def test_stage_dedup_preserves_existing_anchor_rows_unmerged():
    # identity is the text hash alone: a re-stage of identical text returns the
    # existing row UNCHANGED — its anchor/scope rows are never merged or extended.
    s = _store()
    a, _ = s.stage(text="same text", weight=1.0, proposed_by="a",
                   anchors=[("alpha", "a.py::f")])
    b, deduped = s.stage(text="same text", weight=1.0, proposed_by="b",
                         anchors=[("beta", "b.py::g")], repos=["gamma"])
    assert (b, deduped) == (a, True)
    assert _anchor_rows(s, a) == [("alpha", "a.py::f", "")]


def test_get_episode_projects_repos_and_anchors():
    s = _store()
    eid, _ = s.stage(text="scoped", weight=1.0, proposed_by="a",
                     anchors=[("beta", "b.py::g"), ("alpha", "a.py::f")],
                     repos=["gamma"])
    ep = s.get_episode(eid)
    assert ep.repos == ("alpha", "beta", "gamma")     # sorted union of anchors + scope
    assert ep.anchors == (AnchorRef(repo="alpha", anchor="a.py::f"),
                          AnchorRef(repo="beta", anchor="b.py::g"))
    # scope-only membership folds into repos, never a degenerate anchor
    assert all(a.anchor for a in ep.anchors)


def test_get_episode_general_reads_empty_scope():
    s = _store()
    eid, _ = s.stage(text="general", weight=1.0, proposed_by="a")
    ep = s.get_episode(eid)
    assert ep.repos == () and ep.anchors == ()


# ── approved-only recall (two fail-closed defenses) ───────────────────────────
def test_pending_never_in_candidates_single_predicate():
    s = _store()
    a, _ = s.stage(text="approved one", weight=1.0, proposed_by="a")
    p, _ = s.stage(text="pending one", weight=1.0, proposed_by="a")
    s.complete(a, _unit(1, 0, 0, 0), expected_version=0, trust=ESTABLISHED)
    ids = {eid for eid, _ in s.scan_approved()}
    assert ids == {a} and p not in ids
    # single source of "recallable": SQL prefilters materialized rows; the full
    # decision is lifecycle.is_servable inside scan_servable (scan_approved = its
    # no-clock fail-closed alias)
    assert _RECALL_PREDICATE == "status='approved' AND value IS NOT NULL"


def test_pending_value_is_null():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, proposed_by="a")
    assert s.get_episode(eid).value is None           # value computed at complete, not stage


# ── boot-rebuild crash recovery [B3] ──────────────────────────────────────────
def test_rebuild_recovers_after_crash_between_commit_and_add():
    s = _store()
    e1, _ = s.stage(text="one", weight=1.0, proposed_by="a")
    e2, _ = s.stage(text="two", weight=1.0, proposed_by="a")
    s.complete(e1, _unit(1, 0, 0, 0), expected_version=0, trust=ESTABLISHED)
    # simulate a crash: the index add for e2 raises AFTER the row is durably approved
    orig = s.index.sync_approved
    def boom(eid, val):
        if eid == e2:
            raise RuntimeError("crash between commit and index add")
        return orig(eid, val)
    s.index.sync_approved = boom
    s.complete(e2, _unit(0, 1, 0, 0), expected_version=0,
               trust=ESTABLISHED)                       # returns True (durable)
    s.index.sync_approved = orig
    assert s.get_episode(e2).status == "approved"      # durable truth
    assert s.index.size() == 1                          # warm cache diverged (missing e2)
    s.rebuild_index_from_store()                        # the recovery guarantee
    assert s.index.size() == 2
    assert s.index.search(_unit(0, 1, 0, 0), k=2)[0][0] == e2


# ── polarity / kind (carried labels): stage → get_episode round-trip ──────────
def test_stage_roundtrips_polarity_default_neutral():
    s = _store()
    eid, _ = s.stage(text="default polarity", weight=1.0, proposed_by="a")
    assert s.get_episode(eid).polarity == "neutral"     # fail-safe default


def test_stage_roundtrips_explicit_dont():
    s = _store()
    eid, _ = s.stage(text="never run rm -rf on the volume", weight=1.0,
                     proposed_by="a", polarity="dont")
    assert s.get_episode(eid).polarity == "dont"        # the prohibition survives the round-trip


def test_stage_roundtrips_kind_default_note():
    s = _store()
    known, _ = s.stage(text="a known bug", weight=1.0, proposed_by="w", kind="bug")
    bare, _ = s.stage(text="unlabelled", weight=1.0, proposed_by="w")
    assert s.get_episode(known).kind == "bug"
    assert s.get_episode(bare).kind == "note"           # under-claiming default


# ── port conformance [C1] ─────────────────────────────────────────────────────
def test_fake_and_sqlite_store_satisfy_protocol():
    assert isinstance(FakeStore(), MetaStore)
    assert isinstance(SqliteEpisodeStore(connect(":memory:")), MetaStore)
