"""SqliteEpisodeStore change-evidence seams: ``anchored_episodes`` (AnchoredEpisodeReader —
the change→episode join's candidate set, v3 ``(id, repo, anchor, polarity)`` rows),
``evidence_rows_for`` (the retirement gate's kind-filtered ledger feed), and
``append_evidence`` (ChangeEvidenceAppender — the atomic, idempotent batch append the
census ingest drives). One tx() per batch is REQUIRED, not stylistic: tx() is
non-reentrant, so a partial receipt must be impossible from outside."""

from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.evidence_kinds import (
    EK_CHANGE_OUTCOME,
    EK_OUTCOME_HURT,
    EK_OUTCOME_VERIFIED_HURT,
    EK_VERIFY_STALE,
)
from hive.domain.ports import AnchoredEpisodeReader, ChangeEvidenceAppender

DIM = 4


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"))  # ledger-only: no index needed


def _approved(
    s: SqliteEpisodeStore,
    text: str,
    *,
    anchors=(),
    trust: str = "established",
    polarity: str = "neutral",
) -> int:
    eid, _ = s.stage(
        text=text,
        weight=1.0,
        proposed_by="w",
        ts=10,
        anchors=list(anchors),
        polarity=polarity,
    )
    assert s.complete(
        eid,
        np.eye(DIM, dtype=np.float32)[0],
        expected_version=0,
        trust=trust,
        last_active_ts=10,
    )
    return eid


def _evidence_count(s: SqliteEpisodeStore) -> int:
    return s.conn.execute("SELECT COUNT(*) AS c FROM evidence_events").fetchone()["c"]


def _row(
    eid: int,
    payload: str = '{"schema":"change_outcome/v1"}',
    *,
    kind: str = EK_CHANGE_OUTCOME,
    actor: str = "census",
    ts: int = 100,
) -> tuple:
    return (eid, kind, actor, ts, payload)


# ── port conformance (the Law-7 isinstance idiom) ─────────────────────────────


def test_real_store_satisfies_anchored_episode_reader():
    assert isinstance(_store(), AnchoredEpisodeReader)


def test_real_store_satisfies_change_evidence_appender():
    assert isinstance(_store(), ChangeEvidenceAppender)


# ── anchored_episodes: (id, repo, anchor, polarity), ALL trust states ─────────


def test_anchored_episodes_returns_repo_keyed_binding_rows():
    s = _store()
    a = _approved(
        s,
        "anchored prohibition",
        anchors=[("hive", "hive/app/mcp_server.py::handle")],
        polarity="dont",
    )
    _approved(s, "scope-only fact", anchors=[])  # no binding ⇒ excluded
    pend, _ = s.stage(
        text="still pending",
        weight=1.0,
        proposed_by="w",
        ts=10,
        anchors=[("hive", "hive/x.py::f")],
    )  # pending ⇒ excluded
    rows = s.anchored_episodes()
    assert rows == [(a, "hive", "hive/app/mcp_server.py::handle", "dont")]
    assert pend not in {i for i, _r, _a, _p in rows}


def test_anchored_episodes_one_row_per_binding():
    # an episode carrying several bindings yields one 4-tuple per binding — the
    # repo-filtered join (change_evidence.match_anchors) consumes them directly.
    s = _store()
    eid = _approved(
        s, "bound in two repos", anchors=[("beta", "b.py::g"), ("alpha", "a.py::f")]
    )
    assert s.anchored_episodes() == [
        (eid, "alpha", "a.py::f", "neutral"),
        (eid, "beta", "b.py::g", "neutral"),
    ]


def test_anchored_episodes_excludes_scope_only_rows():
    s = _store()
    eid, _ = s.stage(
        text="repo-scoped, no code binding",
        weight=1.0,
        proposed_by="w",
        ts=10,
        repos=["alpha"],
    )
    assert s.complete(
        eid,
        np.eye(DIM, dtype=np.float32)[0],
        expected_version=0,
        trust="established",
        last_active_ts=10,
    )
    assert s.anchored_episodes() == []  # anchor='' never joins


def test_anchored_episodes_includes_all_trust_states():
    # mirrors hive_outcome's known-id rule: evidence on a deprecated row is honest
    # ledger history — the join candidate set never filters by trust.
    s = _store()
    est = _approved(s, "established", anchors=[("r", "a.py::f")], trust="established")
    quar = _approved(s, "quarantined", anchors=[("r", "b.py::g")], trust="quarantined")
    dep = _approved(s, "deprecated", anchors=[("r", "c.py::h")], trust="established")
    s.deprecate(dep, actor="human", ts=20)
    ids = {i for i, _r, _a, _p in s.anchored_episodes()}
    assert ids == {est, quar, dep}


# ── evidence_rows_for: the retirement gate's kind-filtered feed ───────────────


def test_evidence_rows_for_filters_to_the_named_kinds_in_order():
    s = _store()
    eid = _approved(s, "gated target", anchors=[("r", "a.py::f")])
    s.insert_audit(eid, EK_VERIFY_STALE, "census", 30, "{}")
    s.insert_audit(eid, EK_OUTCOME_HURT, "agent-B", 10, "{}")
    s.insert_audit(eid, EK_CHANGE_OUTCOME, "census", 20, "{}")  # foreign kind
    rows = s.evidence_rows_for(eid, [EK_OUTCOME_HURT, EK_VERIFY_STALE])
    assert rows == [
        (EK_VERIFY_STALE, "census", 30),
        (EK_OUTCOME_HURT, "agent-B", 10),
    ]  # insertion order, kinds only
    # the 3-tuple shape is exactly what retirement_evidence's duck-typed feed takes
    kind, actor, ts = rows[0]
    assert isinstance(kind, str) and isinstance(actor, str) and isinstance(ts, int)


def test_evidence_rows_for_empty_kinds_reads_empty():
    # the caller NAMES what qualifies — an unfiltered read would hand the gate
    # foreign kinds to ignore.
    s = _store()
    eid = _approved(s, "target", anchors=[("r", "a.py::f")])
    s.insert_audit(eid, EK_OUTCOME_VERIFIED_HURT, "census", 10, "{}")
    assert s.evidence_rows_for(eid, []) == []


def test_evidence_rows_for_unknown_id_reads_empty():
    assert _store().evidence_rows_for(999, [EK_OUTCOME_HURT]) == []


def test_evidence_rows_for_is_per_episode():
    s = _store()
    a = _approved(s, "one", anchors=[("r", "a.py::f")])
    b = _approved(s, "two", anchors=[("r", "b.py::g")])
    s.insert_audit(a, EK_OUTCOME_HURT, "agent", 10, "{}")
    assert s.evidence_rows_for(b, [EK_OUTCOME_HURT]) == []


# ── append_evidence: atomic batch, content-keyed idempotency ──────────────────


def test_append_evidence_inserts_batch_and_returns_row_ids():
    s = _store()
    a = _approved(s, "one", anchors=[("r", "a.py::f")])
    b = _approved(s, "two", anchors=[("r", "b.py::g")])
    inserted, skipped = s.append_evidence([_row(a), _row(b, '{"other":"payload"}')])
    assert len(inserted) == 2 and skipped == 0
    got = s.conn.execute(
        "SELECT id, episode_id, kind, actor, ts, payload FROM evidence_events "
        "ORDER BY id"
    ).fetchall()
    assert [r["id"] for r in got] == inserted
    assert {r["episode_id"] for r in got} == {a, b}
    assert all(r["kind"] == EK_CHANGE_OUTCOME and r["actor"] == "census" for r in got)


def test_append_evidence_same_batch_twice_is_idempotent():
    s = _store()
    a = _approved(s, "one", anchors=[("r", "a.py::f")])
    b = _approved(s, "two", anchors=[("r", "b.py::g")])
    batch = [_row(a), _row(b)]
    first = s.append_evidence(batch)
    assert len(first[0]) == 2 and first[1] == 0
    again = s.append_evidence(batch)
    assert again == ([], 2)  # 0 new rows, all skipped
    assert _evidence_count(s) == 2  # never duplicated


def test_append_evidence_dedup_keyed_on_content_never_on_ts_or_actor():
    # the BUG-001 axis lesson: idempotency keys on (episode_id, kind, payload) — the
    # content — so a retry with a fresh wall clock / different actor still dedups.
    s = _store()
    a = _approved(s, "one", anchors=[("r", "a.py::f")])
    s.append_evidence([_row(a, ts=100)])
    inserted, skipped = s.append_evidence(
        [(a, EK_CHANGE_OUTCOME, "census-retry", 999, '{"schema":"change_outcome/v1"}')]
    )
    assert (inserted, skipped) == ([], 1)
    assert _evidence_count(s) == 1


def test_append_evidence_distinct_payload_is_a_new_row_not_a_skip():
    s = _store()
    a = _approved(s, "one", anchors=[("r", "a.py::f")])
    s.append_evidence([_row(a, '{"head_sha":"aaa"}')])
    inserted, skipped = s.append_evidence([_row(a, '{"head_sha":"bbb"}')])
    assert len(inserted) == 1 and skipped == 0  # a different change's outcome


def test_append_evidence_poisoned_row_mid_batch_leaves_zero_rows():
    # atomic receipt: a fault on ANY row rolls back ALL (never a silent partial row).
    s = _store()
    a = _approved(s, "one", anchors=[("r", "a.py::f")])
    b = _approved(s, "two", anchors=[("r", "b.py::g")])
    poisoned = [
        _row(a),
        (b, EK_CHANGE_OUTCOME, "census", None, "{}"),
    ]  # ts=None ⇒ NOT NULL raise
    with pytest.raises(Exception):
        s.append_evidence(poisoned)
    assert _evidence_count(s) == 0  # the good row rolled back too


def test_append_evidence_empty_batch_is_a_noop():
    s = _store()
    assert s.append_evidence([]) == ([], 0)
    assert _evidence_count(s) == 0
