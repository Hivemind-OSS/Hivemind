"""SqliteEpisodeStore change-evidence seams: ``anchored_episodes`` (AnchoredEpisodeReader —
the change→episode join's candidate set) and ``append_evidence`` (ChangeEvidenceAppender —
the atomic, idempotent batch append censusctl drives). One tx() per batch is REQUIRED, not
stylistic: tx() is non-reentrant, so a partial receipt must be impossible from outside."""
from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME
from hive.domain.ports import AnchoredEpisodeReader, ChangeEvidenceAppender

DIM = 4


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"))          # ledger-only: no index needed


def _approved(s: SqliteEpisodeStore, text: str, *, anchor: str = "",
              trust: str = "established", polarity: str = "neutral") -> int:
    eid, _ = s.stage(text=text, weight=1.0, tags="", proposed_by="w", ts=10,
                     anchor=anchor, polarity=polarity)
    assert s.complete(eid, np.eye(DIM, dtype=np.float32)[0], expected_version=0,
                      trust=trust, approver="h", approved_ts=10, last_active_ts=10)
    return eid


def _evidence_count(s: SqliteEpisodeStore) -> int:
    return s.conn.execute("SELECT COUNT(*) AS c FROM evidence_events").fetchone()["c"]


def _row(eid: int, payload: str = '{"schema":"change_outcome/v1"}',
         *, kind: str = EK_CHANGE_OUTCOME, actor: str = "census",
         ts: int = 100) -> tuple:
    return (eid, kind, actor, ts, payload)


# ── port conformance (the Law-7 isinstance idiom) ─────────────────────────────


def test_real_store_satisfies_anchored_episode_reader():
    assert isinstance(_store(), AnchoredEpisodeReader)


def test_real_store_satisfies_change_evidence_appender():
    assert isinstance(_store(), ChangeEvidenceAppender)


# ── anchored_episodes: approved + non-empty anchor + polarity, ALL trust states ─


def test_anchored_episodes_returns_approved_anchored_rows_with_polarity():
    s = _store()
    a = _approved(s, "anchored prohibition", anchor="hive/app/mcp_server.py::handle",
                  polarity="dont")
    _approved(s, "anchorless fact", anchor="")               # empty anchor ⇒ excluded
    pend, _ = s.stage(text="still pending", weight=1.0, tags="", proposed_by="w",
                      ts=10, anchor="hive/x.py::f")          # pending ⇒ excluded
    rows = s.anchored_episodes()
    assert rows == [(a, "hive/app/mcp_server.py::handle", "dont")]
    assert pend not in {i for i, _a, _p in rows}


def test_anchored_episodes_includes_all_trust_states():
    # mirrors hive_outcome's known-id rule: evidence on a deprecated row is honest
    # ledger history — the join candidate set never filters by trust.
    s = _store()
    est = _approved(s, "established", anchor="a.py::f", trust="established")
    quar = _approved(s, "quarantined", anchor="b.py::g", trust="quarantined")
    dep = _approved(s, "deprecated", anchor="c.py::h", trust="established")
    s.deprecate(dep, actor="human", ts=20)
    ids = {i for i, _a, _p in s.anchored_episodes()}
    assert ids == {est, quar, dep}


# ── append_evidence: atomic batch, content-keyed idempotency ──────────────────


def test_append_evidence_inserts_batch_and_returns_row_ids():
    s = _store()
    a = _approved(s, "one", anchor="a.py::f")
    b = _approved(s, "two", anchor="b.py::g")
    inserted, skipped = s.append_evidence([_row(a), _row(b, '{"other":"payload"}')])
    assert len(inserted) == 2 and skipped == 0
    got = s.conn.execute(
        "SELECT id, episode_id, kind, actor, ts, payload FROM evidence_events "
        "ORDER BY id").fetchall()
    assert [r["id"] for r in got] == inserted
    assert {r["episode_id"] for r in got} == {a, b}
    assert all(r["kind"] == EK_CHANGE_OUTCOME and r["actor"] == "census" for r in got)


def test_append_evidence_same_batch_twice_is_idempotent():
    s = _store()
    a = _approved(s, "one", anchor="a.py::f")
    b = _approved(s, "two", anchor="b.py::g")
    batch = [_row(a), _row(b)]
    first = s.append_evidence(batch)
    assert len(first[0]) == 2 and first[1] == 0
    again = s.append_evidence(batch)
    assert again == ([], 2)                                  # 0 new rows, all skipped
    assert _evidence_count(s) == 2                           # never duplicated


def test_append_evidence_dedup_keyed_on_content_never_on_ts_or_actor():
    # the BUG-001 axis lesson: idempotency keys on (episode_id, kind, payload) — the
    # content — so a retry with a fresh wall clock / different actor still dedups.
    s = _store()
    a = _approved(s, "one", anchor="a.py::f")
    s.append_evidence([_row(a, ts=100)])
    inserted, skipped = s.append_evidence([(a, EK_CHANGE_OUTCOME, "census-retry", 999,
                                            '{"schema":"change_outcome/v1"}')])
    assert (inserted, skipped) == ([], 1)
    assert _evidence_count(s) == 1


def test_append_evidence_distinct_payload_is_a_new_row_not_a_skip():
    s = _store()
    a = _approved(s, "one", anchor="a.py::f")
    s.append_evidence([_row(a, '{"head_sha":"aaa"}')])
    inserted, skipped = s.append_evidence([_row(a, '{"head_sha":"bbb"}')])
    assert len(inserted) == 1 and skipped == 0               # a different change's outcome


def test_append_evidence_poisoned_row_mid_batch_leaves_zero_rows():
    # atomic receipt: a fault on ANY row rolls back ALL (never a silent partial row).
    s = _store()
    a = _approved(s, "one", anchor="a.py::f")
    b = _approved(s, "two", anchor="b.py::g")
    poisoned = [_row(a), (b, EK_CHANGE_OUTCOME, "census", None, "{}")]  # ts=None ⇒ NOT NULL raise
    with pytest.raises(Exception):
        s.append_evidence(poisoned)
    assert _evidence_count(s) == 0                           # the good row rolled back too


def test_append_evidence_empty_batch_is_a_noop():
    s = _store()
    assert s.append_evidence([]) == ([], 0)
    assert _evidence_count(s) == 0
