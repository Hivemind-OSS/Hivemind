"""P1.4 — M05 AdmissionService: the scan → stage → approve gauntlet on a real
SQLite store + authoritative index. Owns §6.1 #5a (secret refused/redacted
pre-stage) and #5b (pending never recallable); proves the secret never reaches a
row, a blob, or a log line.
"""
from __future__ import annotations

import logging

import numpy as np
import pytest

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.scanner_regex import DefaultSecretScanner
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.admission import AdmissionService, StagedEpisode, WriteResult
from hive.domain.errors import SecretRefused
from hive.domain.models import content_hash
from tests.fakes import FakeProvider

DIM = 8
SECRET = "AKIAIOSFODNN7EXAMPLE"


def _svc(mode: str = "refuse"):
    conn = connect(":memory:")
    store = SqliteEpisodeStore(conn, index=ExhaustiveCosineIndex(DIM))
    svc = AdmissionService(store, DefaultSecretScanner(redact_mode=mode),
                           FakeProvider(d=DIM), now=lambda: 0)
    return svc, store


def _count(store, table: str) -> int:
    return store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# ── #5a: secret refused before stage — 0 rows, 0 blobs ────────────────────────
def test_stage_refuses_on_secret():
    svc, store = _svc()
    with pytest.raises(SecretRefused):
        svc.write(f"my key {SECRET}", weight=1.0, source="mark", proposed_by="a")
    assert _count(store, "episodes") == 0          # nothing staged
    assert _count(store, "blobs") == 0             # no blob written
    assert store.index.size() == 0
    assert store.fetch(content_hash(f"my key {SECRET}")) is None


def test_stage_refuses_calls_store_stage_zero_times(monkeypatch):
    svc, store = _svc()
    calls = {"n": 0}
    orig = store.stage
    monkeypatch.setattr(store, "stage", lambda **kw: (calls.__setitem__("n", calls["n"] + 1), orig(**kw))[1])
    with pytest.raises(SecretRefused):
        svc.write(f"AWS {SECRET}", proposed_by="a")
    assert calls["n"] == 0                          # scan fires BEFORE any stage


# ── stage happy path + dedup ──────────────────────────────────────────────────
def test_stage_creates_pending_not_approved():
    svc, store = _svc()
    r = svc.write("db pool exhausted under load", weight=2.0, source="mark", proposed_by="a")
    assert isinstance(r, WriteResult) and r.status == "pending"
    ep = store.get_episode(r.pending_id)
    assert ep.status == "pending" and ep.approved_by is None
    assert store.index.size() == 0                  # NOT indexed at stage
    assert r.content_hash == content_hash("db pool exhausted under load")


def test_pending_value_is_null():
    svc, store = _svc()
    r = svc.write("retry with exponential backoff", proposed_by="a")
    assert store.get_episode(r.pending_id).value is None   # value computed at approve


def test_stage_dedup_same_proposer():
    svc, store = _svc()
    a = svc.write("same insight text", proposed_by="a")
    b = svc.write("same insight text", proposed_by="a")
    assert a.pending_id == b.pending_id and b.deduped is True
    assert len(svc.list_pending()) == 1


# ── #5a redact branch: only masked text is staged ─────────────────────────────
def test_redact_stages_masked_text_no_raw_secret():
    svc, store = _svc(mode="redact")
    text = f"connect with {SECRET} please"
    r = svc.write(text, proposed_by="a")
    assert r.status == "redacted" and r.scan.action == "redact"
    staged = store.get_episode(r.pending_id).text
    assert SECRET not in staged and "[REDACTED]" in staged
    assert r.content_hash == content_hash(staged)               # hash over redacted text
    assert r.content_hash != content_hash(text)
    # the raw secret reached neither the episodes row nor the blob
    assert store.fetch(r.content_hash) == staged
    assert SECRET not in (store.fetch(r.content_hash) or "")


# ── #5b: approve is the only path that indexes; approved is recallable ─────────
def test_approve_flips_status_and_indexes():
    svc, store = _svc()
    r = svc.write("vector index rebuild on boot", proposed_by="a")
    assert store.index.size() == 0
    approved, skipped = svc.approve([r.pending_id], "human")
    assert approved == [r.pending_id] and skipped == []
    ep = store.get_episode(r.pending_id)
    assert ep.status == "approved" and ep.approved_by == "human" and ep.value is not None
    assert store.index.size() == 1                  # approve is the ONLY indexer


def test_approved_is_recallable():
    svc, store = _svc()
    text = "use BEGIN IMMEDIATE for the writer lane"
    r = svc.write(text, proposed_by="a")
    svc.approve([r.pending_id], "human")
    q = FakeProvider(d=DIM).encode(text)            # deterministic: same text → same vec
    hits = store.index.search(q, k=1)
    assert hits and hits[0][0] == r.pending_id


def test_approve_idempotent_and_unknown_skipped():
    svc, store = _svc()
    r = svc.write("idempotent approve", proposed_by="a")
    svc.approve([r.pending_id], "human")
    approved, skipped = svc.approve([r.pending_id, 9999], "human2")  # replay + unknown
    assert approved == [] and set(skipped) == {r.pending_id, 9999}
    assert store.get_episode(r.pending_id).approved_by == "human"   # unchanged
    assert store.index.size() == 1                                   # exactly one entry


# ── reject ────────────────────────────────────────────────────────────────────
def test_reject_drops_and_never_indexes():
    svc, store = _svc()
    r = svc.write("reject me", proposed_by="a")
    rejected, skipped = svc.reject([r.pending_id])
    assert rejected == [r.pending_id]
    assert store.get_episode(r.pending_id) is None and store.index.size() == 0


def test_reject_keep_flag():
    svc, store = _svc()
    r = svc.write("keep but never recall", proposed_by="a")
    svc.reject([r.pending_id], keep_rejected=True)
    ep = store.get_episode(r.pending_id)
    assert ep is not None and ep.status == "pending"   # retained, still non-recallable
    assert store.index.size() == 0


def test_reject_cannot_unpublish_approved():
    svc, store = _svc()
    r = svc.write("approved then reject attempt", proposed_by="a")
    svc.approve([r.pending_id], "human")
    rejected, skipped = svc.reject([r.pending_id])
    assert rejected == [] and skipped == [r.pending_id]
    assert store.get_episode(r.pending_id).status == "approved"


# ── the secret-never-logged invariant (REFUSE + REDACT paths) ─────────────────
def test_refuse_log_contains_no_secret_substring(caplog):
    svc, store = _svc()
    with caplog.at_level(logging.DEBUG, logger="hive.admission"):
        with pytest.raises(SecretRefused):
            svc.write(f"key {SECRET}", proposed_by="a")
    assert caplog.records                                  # something WAS logged
    for rec in caplog.records:
        assert SECRET not in rec.getMessage()
        assert SECRET not in str(rec.__dict__)             # not in any extra field either


def test_redact_log_contains_no_secret_substring(caplog):
    svc, store = _svc(mode="redact")
    with caplog.at_level(logging.DEBUG, logger="hive.admission"):
        svc.write(f"key {SECRET} here", proposed_by="a")
    assert caplog.records
    for rec in caplog.records:
        assert SECRET not in rec.getMessage()
        assert SECRET not in str(rec.__dict__)


def test_secret_refused_exception_carries_no_secret():
    svc, store = _svc()
    try:
        svc.write(f"key {SECRET}", proposed_by="a")
        assert False, "expected SecretRefused"
    except SecretRefused as e:
        assert SECRET not in str(e)                        # only rule names + counts


# ── the credit boundary: admission writes zero loop tables ────────────────────
def test_admission_touches_no_loop_tables():
    svc, store = _svc()
    r = svc.write("boundary check", proposed_by="a")
    svc.approve([r.pending_id], "human")
    r2 = svc.write("second", proposed_by="a")
    svc.reject([r2.pending_id])
    assert _count(store, "exposure") == 0
    assert _count(store, "task_outcomes") == 0


# ── list_pending surfaces frozen StagedEpisode carriers ───────────────────────
def test_list_pending_returns_staged_episodes():
    svc, store = _svc()
    svc.write("pending one", proposed_by="a")
    r2 = svc.write("pending two", proposed_by="b")
    svc.approve([r2.pending_id], "human")              # approved drops out of pending
    pend = svc.list_pending()
    assert len(pend) == 1 and isinstance(pend[0], StagedEpisode)
    assert pend[0].text == "pending one" and pend[0].status == "pending"
