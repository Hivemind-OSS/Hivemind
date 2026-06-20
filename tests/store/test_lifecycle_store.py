"""Store lifecycle methods: complete (trust-aware materialization), set_trust
(promotion/demotion + index sync), supersede (the ONE retirement-with-replacement
owner), the ExposureLedger write side
(exposure bumps liveness in the same tx; misses persist secret-safely), the decay
sweep, the quarantine candidate scan, and the servable scan (the single predicate
source — ``scan_approved`` is its no-clock FAIL-CLOSED alias)."""
from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.lifecycle import (
    DEPRECATED, ESTABLISHED, PROVISIONAL, QUARANTINED, MissRow,
)
from hive.domain.ports import ExposureLedger

DIM = 4
NOW = 1_000
Q_TTL, P_TTL = 100, 200


def _unit(*xs) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


_VECS = [_unit(*row) for row in np.eye(DIM, dtype=np.float32)]


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"), index=ExhaustiveCosineIndex(DIM))


def _materialize(s: SqliteEpisodeStore, text: str, *, trust: str, vec=None,
                 approver=None, ts: int = 10, last_active=None) -> int:
    """stage → complete with an explicit trust (the generalized admission flip)."""
    eid, _ = s.stage(text=text, weight=1.0, source="m", tags="", proposed_by="writer", ts=ts)
    ok = s.complete(eid, vec if vec is not None else _VECS[0], expected_version=0,
                    trust=trust, approver=approver, approved_ts=ts,
                    last_active_ts=ts if last_active is None else last_active)
    assert ok
    return eid


def _index_ids(s: SqliteEpisodeStore) -> set[int]:
    return {eid for v in _VECS for eid, _sim in s.index.search(v, k=16)}


def _audits(s: SqliteEpisodeStore, episode_id: int, kind: str) -> list[dict]:
    return [dict(r) for r in s.conn.execute(
        "SELECT * FROM evidence_events WHERE episode_id=? AND kind=? ORDER BY id",
        (episode_id, kind))]


# ── complete: trust-aware materialization ──────────────────────────────────────
def test_complete_quarantined_is_unindexed():
    s = _store()
    eid = _materialize(s, "auto-captured insight", trust=QUARANTINED)
    ep = s.get_episode(eid)
    assert ep.status == "approved" and ep.trust == QUARANTINED   # materialized, unvouched
    assert ep.approved_by is None and ep.value is not None        # embedded
    assert s.index.size() == 0                                    # NOT in the warm index
    assert s.scan_servable(now=NOW, provisional_ttl_s=P_TTL) == []  # NOT scannable


def test_approve_delegates_to_established():
    # approve() stays byte-compatible AND now stamps trust + liveness.
    s = _store()
    eid, _ = s.stage(text="human approved", weight=1.0, source="m", tags="",
                     proposed_by="a", ts=5)
    assert s.approve(eid, "human", _VECS[0], expected_version=0, approved_ts=7) is True
    ep = s.get_episode(eid)
    assert ep.trust == ESTABLISHED and ep.approved_by == "human"
    assert ep.last_active_ts == 7
    assert s.index.search(_VECS[0], k=1)[0][0] == eid             # served immediately


def test_complete_rejects_unknown_trust():
    s = _store()
    eid, _ = s.stage(text="x", weight=1.0, source="m", tags="", proposed_by="a")
    with pytest.raises(ValueError):
        s.complete(eid, _VECS[0], expected_version=0, trust="bogus")


# ── set_trust: promotion adds, demotion removes ────────────────────────────────
def test_set_trust_promote_adds_demote_removes():
    s = _store()
    eid = _materialize(s, "promotable", trust=QUARANTINED, vec=_VECS[1])
    assert s.index.size() == 0
    assert s.set_trust(eid, PROVISIONAL, now=50) is True
    ep = s.get_episode(eid)
    assert ep.trust == PROVISIONAL and ep.last_active_ts == 50    # promotion stamps liveness
    assert s.index.search(_VECS[1], k=1)[0][0] == eid             # searchable
    assert s.set_trust(eid, DEPRECATED, now=60) is True
    assert s.index.size() == 0                                    # vector gone from search
    assert s.get_episode(eid).last_active_ts == 50                # demotion does NOT re-stamp


# ── set_trust: human-vouched establishment records the approver (B0/BUG-001) ────
def test_set_trust_establish_records_approver():
    s = _store()
    eid = _materialize(s, "captured then vouched", trust=QUARANTINED, vec=_VECS[2])
    assert s.get_episode(eid).approved_by is None                 # capture left it unvouched
    assert s.set_trust(eid, ESTABLISHED, now=70, approver="alice", approved_ts=70) is True
    ep = s.get_episode(eid)
    assert ep.trust == ESTABLISHED and ep.approved_by == "alice"  # the vouch is recorded
    assert ep.last_active_ts == 70                                # liveness stamped
    assert s.index.search(_VECS[2], k=1)[0][0] == eid             # entered the index


def test_set_trust_without_approver_leaves_approved_by_untouched():
    # mechanical promotion (lifecycle) passes no approver → approved_by stays as-is
    s = _store()
    eid = _materialize(s, "mechanically promoted", trust=QUARANTINED, vec=_VECS[3])
    assert s.set_trust(eid, PROVISIONAL, now=50) is True
    assert s.get_episode(eid).approved_by is None                 # untouched by promotion


def test_set_trust_guards():
    s = _store()
    assert s.set_trust(404, PROVISIONAL, now=1) is False          # unknown row
    pend, _ = s.stage(text="pending", weight=1.0, source="m", tags="", proposed_by="a")
    assert s.set_trust(pend, PROVISIONAL, now=1) is False         # not materialized
    eid = _materialize(s, "ok", trust=QUARANTINED)
    with pytest.raises(ValueError):
        s.set_trust(eid, "bogus", now=1)                          # unknown trust label


# ── the servable scan: ONE predicate source ────────────────────────────────────
def _mixed_fixture(s: SqliteEpisodeStore):
    e_est = _materialize(s, "established, stale liveness", trust=ESTABLISHED,
                         approver="h", vec=_VECS[0], last_active=0)
    e_fresh = _materialize(s, "fresh provisional", trust=QUARANTINED, vec=_VECS[1])
    s.set_trust(e_fresh, PROVISIONAL, now=NOW - 1)
    e_lapsed = _materialize(s, "lapsed provisional", trust=QUARANTINED, vec=_VECS[2])
    s.set_trust(e_lapsed, PROVISIONAL, now=NOW - P_TTL - 1)
    e_q = _materialize(s, "quarantined", trust=QUARANTINED, vec=_VECS[3])
    e_dep = _materialize(s, "deprecated", trust=ESTABLISHED, approver="h", vec=_VECS[3])
    s.set_trust(e_dep, DEPRECATED, now=NOW)
    return e_est, e_fresh, e_lapsed, e_q, e_dep


def test_scan_servable_predicate_single_source():
    s = _store()
    e_est, e_fresh, e_lapsed, e_q, e_dep = _mixed_fixture(s)
    ids = {eid for eid, _v in s.scan_servable(now=NOW, provisional_ttl_s=P_TTL)}
    assert ids == {e_est, e_fresh}        # established always; provisional iff fresh
    # the no-clock compat alias is FAIL-CLOSED: established only (a provisional row
    # needs a clock to prove freshness; without one it is not handed to a rebuild)
    assert {eid for eid, _v in s.scan_approved()} == {e_est}


def test_rebuild_indexes_exactly_servable_set():
    s = _store()
    e_est, e_fresh, e_lapsed, e_q, e_dep = _mixed_fixture(s)
    s.rebuild_index_from_store(now=NOW, provisional_ttl_s=P_TTL)
    assert _index_ids(s) == {e_est, e_fresh}
    s.rebuild_index_from_store()                       # no clock ⇒ fail-closed
    assert _index_ids(s) == {e_est}


# ── scan_servable_labeled (the conflict scan's candidate source) ───────────────
def test_scan_servable_labeled_is_exactly_the_servable_set():
    # same is_servable filter as scan_servable: established always, provisional iff
    # fresh; quarantined / lapsed-provisional / deprecated are EXCLUDED.
    s = _store()
    e_est, e_fresh, e_lapsed, e_q, e_dep = _mixed_fixture(s)
    rows = s.scan_servable_labeled(now=NOW, provisional_ttl_s=P_TTL)
    assert {r[0] for r in rows} == {e_est, e_fresh}
    by_id = {r[0]: r for r in rows}
    assert by_id[e_est][5] == ESTABLISHED and by_id[e_fresh][5] == PROVISIONAL
    # the vector rides position 1 (the conflict detector compares it)
    val = by_id[e_est][1]
    assert isinstance(val, np.ndarray) and val.shape == (DIM,)


def test_scan_servable_labeled_carries_polarity_anchor_ts():
    s = _store()
    eid, _ = s.stage(text="dont run as root", weight=1.0, source="m", tags="",
                     proposed_by="w", ts=7, polarity="dont", anchor="Dockerfile")
    s.approve(eid, "human", _VECS[0], expected_version=0, approved_ts=7)
    (rid, _val, pol, anc, ts, trust), = s.scan_servable_labeled(
        now=NOW, provisional_ttl_s=P_TTL)
    assert (rid, pol, anc, ts, trust) == (eid, "dont", "Dockerfile", 7, ESTABLISHED)


# ── ExposureLedger write side ──────────────────────────────────────────────────
def test_store_conforms_to_exposure_ledger():
    assert isinstance(SqliteEpisodeStore(connect(":memory:")), ExposureLedger)


def test_exposure_bumps_last_active_same_tx():
    s = _store()
    eid = _materialize(s, "served row", trust=ESTABLISHED, approver="h", ts=10)
    s.record_exposure("trace-1", [(eid, 0.7)], agent_id="agent-A", ts=99)
    row = s.conn.execute("SELECT * FROM exposure WHERE trace_id='trace-1'").fetchone()
    assert row["episode_id"] == eid and row["agent_id"] == "agent-A"
    assert row["recall_margin"] == pytest.approx(0.7) and row["injected_ts"] == 99
    assert s.get_episode(eid).last_active_ts == 99                # liveness refreshed
    # atomicity: a duplicate (trace_id, episode_id) violates the PK → the WHOLE call
    # rolls back — no partial exposure rows, liveness NOT re-bumped.
    with pytest.raises(Exception):
        s.record_exposure("trace-2", [(eid, 0.5), (eid, 0.5)], agent_id="a", ts=200)
    n2 = s.conn.execute(
        "SELECT COUNT(*) AS c FROM exposure WHERE trace_id='trace-2'").fetchone()["c"]
    assert n2 == 0
    assert s.get_episode(eid).last_active_ts == 99


def test_record_miss_types_and_secret_handling():
    s = _store()
    raw = _VECS[0].tobytes()
    redacted = _VECS[1].tobytes()
    s.record_miss("how to fix the pool", raw, "agent-A", "no_match", ts=5)
    s.record_miss("how to tune the gate", raw, "agent-B", "abstained", ts=6)
    # secret-refused misses persist NO query content: empty text, NULL vector
    s.record_miss("", None, "agent-C", "secret_refused", ts=7)
    # redacted misses persist the MASKED text + the vector re-encoded FROM the masked
    # text — never the raw query's vector
    s.record_miss("token [MASKED] expired", redacted, "agent-D", "no_match", ts=8)
    rows = [dict(r) for r in s.conn.execute("SELECT * FROM recall_misses ORDER BY id")]
    assert [r["miss_type"] for r in rows] == ["no_match", "abstained",
                                              "secret_refused", "no_match"]
    assert rows[2]["query_text"] == "" and rows[2]["query_vector"] is None
    assert rows[3]["query_vector"] == redacted and rows[3]["query_vector"] != raw
    with pytest.raises(Exception):
        s.record_miss("q", None, "a", "bogus_type", ts=9)         # CHECK-constrained


def test_misses_window_vectors_only_and_windowed():
    s = _store()
    s.record_miss("early", _VECS[0].tobytes(), "agent-A", "no_match", ts=5)
    s.record_miss("late", _VECS[1].tobytes(), "agent-B", "abstained", ts=50)
    s.record_miss("", None, "agent-C", "secret_refused", ts=60)   # NULL vector: excluded
    out = s.misses_window(since_ts=5)                             # STRICT: ts > since
    assert [m.agent_id for m in out] == ["agent-B"]
    m = out[0]
    assert isinstance(m, MissRow) and m.ts == 50
    assert np.allclose(m.vector, _VECS[1])


# ── supersession: the one retirement-with-replacement owner ────────────────────
def test_supersede_atomic_idempotent_self_refused():
    s = _store()
    old = _materialize(s, "old fact", trust=ESTABLISHED, approver="h", vec=_VECS[0])
    new = _materialize(s, "corrected fact", trust=ESTABLISHED, approver="h", vec=_VECS[1])
    assert s.supersede(old, new, actor="h", ts=100) is True
    ep = s.get_episode(old)
    assert ep.trust == DEPRECATED and ep.superseded_by == new     # retired + stamped
    assert old not in _index_ids(s) and new in _index_ids(s)      # de-indexed
    assert len(_audits(s, old, "supersede")) == 1                 # ONE audit row
    assert s.supersede(old, new, actor="h", ts=101) is True       # idempotent re-run
    assert len(_audits(s, old, "supersede")) == 1                 # no duplicate audit
    assert s.supersede(old, old, actor="h", ts=102) is False      # self-supersede refused
    assert s.supersede(404, new, actor="h", ts=103) is False      # unknown target
    assert s.supersede(old, 404, actor="h", ts=104) is False      # unknown replacement


def test_re_supersede_last_write_wins_history_in_ledger():
    s = _store()
    old = _materialize(s, "v1", trust=ESTABLISHED, approver="h", vec=_VECS[0])
    b = _materialize(s, "v2", trust=ESTABLISHED, approver="h", vec=_VECS[1])
    c = _materialize(s, "v3", trust=ESTABLISHED, approver="h", vec=_VECS[2])
    s.supersede(old, b, actor="h", ts=10)
    assert s.supersede(old, c, actor="h", ts=20) is True          # last-write-wins
    assert s.get_episode(old).superseded_by == c
    assert len(_audits(s, old, "supersede")) == 2                 # full history kept


# ── decay sweep + quarantine candidate scan ────────────────────────────────────
def _decay_fixture(s: SqliteEpisodeStore):
    dead_q = _materialize(s, "stale quarantined", trust=QUARANTINED, vec=_VECS[0],
                          ts=NOW - Q_TTL - 1, last_active=NOW - Q_TTL - 1)
    live_q = _materialize(s, "fresh quarantined", trust=QUARANTINED, vec=_VECS[1],
                          ts=NOW - 1, last_active=NOW - 1)
    dead_p = _materialize(s, "stale provisional", trust=QUARANTINED, vec=_VECS[2])
    s.set_trust(dead_p, PROVISIONAL, now=NOW - P_TTL - 1)
    live_p = _materialize(s, "fresh provisional", trust=QUARANTINED, vec=_VECS[3])
    s.set_trust(live_p, PROVISIONAL, now=NOW - 1)
    est = _materialize(s, "established forever", trust=ESTABLISHED, approver="h",
                       vec=_VECS[0], ts=0, last_active=0)
    return dead_q, live_q, dead_p, live_p, est


def test_sweep_decays_and_deindexes():
    s = _store()
    dead_q, live_q, dead_p, live_p, est = _decay_fixture(s)
    res = s.sweep_decayed(now=NOW, q_ttl_s=Q_TTL, p_ttl_s=P_TTL)
    assert res == {"quarantined": 1, "provisional": 1}
    assert s.get_episode(dead_q).trust == DEPRECATED
    assert s.get_episode(dead_p).trust == DEPRECATED
    assert len(_audits(s, dead_q, "ttl_expired")) == 1
    assert len(_audits(s, dead_p, "ttl_expired")) == 1
    assert dead_p not in _index_ids(s)                            # lapsed provisional de-indexed
    assert live_p in _index_ids(s)
    assert s.get_episode(live_q).trust == QUARANTINED             # untouched
    assert s.get_episode(est).trust == ESTABLISHED                # never age-decays
    res2 = s.sweep_decayed(now=NOW, q_ttl_s=Q_TTL, p_ttl_s=P_TTL)  # idempotent
    assert res2 == {"quarantined": 0, "provisional": 0}
    assert len(_audits(s, dead_q, "ttl_expired")) == 1            # no duplicate audits


def test_quarantined_candidates_excludes_dead():
    s = _store()
    dead_q, live_q, dead_p, live_p, est = _decay_fixture(s)
    cands = s.quarantined_candidates(now=NOW, quarantine_ttl_s=Q_TTL)
    assert [c[0] for c in cands] == [live_q]                      # TTL-lapsed excluded
    eid, vec, proposed_by, ts, last_active = cands[0]
    assert proposed_by == "writer" and ts == NOW - 1 and last_active == NOW - 1
    assert np.allclose(vec, _VECS[1])


# ── audit + counts ─────────────────────────────────────────────────────────────
def test_insert_audit_roundtrip():
    s = _store()
    eid = _materialize(s, "audited", trust=QUARANTINED)
    rid = s.insert_audit(eid, "promote", "server", 42, '{"rule":"demand"}')
    assert isinstance(rid, int) and rid > 0
    row = _audits(s, eid, "promote")[0]
    assert row["actor"] == "server" and row["ts"] == 42
    assert row["payload"] == '{"rule":"demand"}'


def test_trust_counts_all_states_present():
    s = _store()
    _decay_fixture(s)
    counts = s.trust_counts()
    assert set(counts) == {QUARANTINED, PROVISIONAL, ESTABLISHED, DEPRECATED}
    assert counts[QUARANTINED] == 2 and counts[PROVISIONAL] == 2
    assert counts[ESTABLISHED] == 1 and counts[DEPRECATED] == 0   # zero-states visible


# ── kind / anchor carried labels: stage → get_episode round-trip ───────────────
def test_stage_roundtrips_kind_and_anchor():
    s = _store()
    eid, _ = s.stage(text="a known bug", weight=1.0, source="m", tags="",
                     proposed_by="w", kind="bug", anchor="hive/domain/recall.py")
    ep = s.get_episode(eid)
    assert ep.kind == "bug" and ep.anchor == "hive/domain/recall.py"


def test_stage_defaults_kind_note_and_anchor_empty():
    # a write-site that names no kind/anchor under-claims (generic note, no WHERE)
    s = _store()
    eid, _ = s.stage(text="unlabelled", weight=1.0, source="m", tags="", proposed_by="w")
    ep = s.get_episode(eid)
    assert ep.kind == "note" and ep.anchor == ""
