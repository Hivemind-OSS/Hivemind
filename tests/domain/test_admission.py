"""P1.4 — M05 AdmissionService: the scan → stage → embed → approve gauntlet on a real
SQLite store + authoritative index. CLIENT-GATED capture (HOOK-RELOCATION-PLAN v3): the
server-side pending→approve queue is gone — a clean write lands APPROVED + indexed in one
call, attributed to the caller's ``approved_by``. Owns §6.1 #5a (secret refused/redacted
pre-write); proves the secret never reaches a row, a blob, or a log line, and that the
secret scan is the SOLE always-on gate now that the queue is removed.
"""
from __future__ import annotations

import logging

import pytest

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.scanner_regex import DefaultSecretScanner
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.admission import AdmissionService, WriteResult
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


def _write(svc, text, *, approved_by: str = "human", proposed_by: str = "a", **kw):
    return svc.write(text, approved_by=approved_by, proposed_by=proposed_by, **kw)


def _count(store, table: str) -> int:
    return store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# ── #5a: secret refused before anything is written — 0 rows, 0 blobs ──────────
def test_write_refuses_on_secret():
    svc, store = _svc()
    with pytest.raises(SecretRefused):
        _write(svc, f"my key {SECRET}")
    assert _count(store, "episodes") == 0          # nothing written
    assert _count(store, "blobs") == 0             # no blob written
    assert store.index.size() == 0
    assert store.fetch(content_hash(f"my key {SECRET}")) is None


def test_refuse_calls_store_stage_zero_times(monkeypatch):
    svc, store = _svc()
    calls = {"n": 0}
    orig = store.stage
    monkeypatch.setattr(store, "stage", lambda **kw: (calls.__setitem__("n", calls["n"] + 1), orig(**kw))[1])
    with pytest.raises(SecretRefused):
        _write(svc, f"AWS {SECRET}")
    assert calls["n"] == 0                          # scan fires BEFORE any stage


# ── clean write lands APPROVED + indexed + recallable in one call ─────────────
def test_write_lands_approved_with_value_and_indexes():
    svc, store = _svc()
    r = _write(svc, "vector index rebuild on boot")
    assert isinstance(r, WriteResult) and r.status == "approved"
    ep = store.get_episode(r.episode_id)
    assert ep.status == "approved" and ep.approved_by == "human" and ep.value is not None
    assert store.index.size() == 1                  # write is the indexer now (no approve step)
    assert r.content_hash == content_hash("vector index rebuild on boot")
    assert store.counts() == (1, 0)                 # 1 approved, 0 pending


def test_approved_is_recallable_without_a_separate_step():
    svc, store = _svc()
    text = "use BEGIN IMMEDIATE for the writer lane"
    r = _write(svc, text)
    q = FakeProvider(d=DIM).encode(text)            # deterministic: same text → same vec
    hits = store.index.search(q, k=1)
    assert hits and hits[0][0] == r.episode_id


# ── dedup: same text → same id, one row, one index entry ──────────────────────
def test_write_dedup_same_text():
    svc, store = _svc()
    a = _write(svc, "same insight text")
    b = _write(svc, "same insight text")
    assert a.episode_id == b.episode_id and b.deduped is True
    assert store.counts() == (1, 0)
    assert store.index.size() == 1


def test_dedup_does_not_overwrite_original_approver():
    svc, store = _svc()
    a = _write(svc, "durable shared insight", approved_by="alice")
    b = _write(svc, "durable shared insight", approved_by="bob")
    assert a.episode_id == b.episode_id and b.deduped is True
    assert store.get_episode(a.episode_id).approved_by == "alice"   # first approver wins


# ── #5a redact branch: only masked text is stored, and it is approved ─────────
def test_redact_stores_masked_text_no_raw_secret():
    svc, store = _svc(mode="redact")
    text = f"connect with {SECRET} please"
    r = _write(svc, text)
    assert r.status == "redacted" and r.scan.action == "redact"
    stored = store.get_episode(r.episode_id).text
    assert SECRET not in stored and "[REDACTED]" in stored
    assert r.content_hash == content_hash(stored)               # hash over redacted text
    assert r.content_hash != content_hash(text)
    # the raw secret reached neither the episodes row nor the blob
    assert store.fetch(r.content_hash) == stored
    assert SECRET not in (store.fetch(r.content_hash) or "")
    # a redacted write is ALSO approved + recallable (no pending state survives)
    assert store.get_episode(r.episode_id).status == "approved"
    assert store.index.size() == 1


# ── no-leak on a downstream failure: nothing is left half-written ─────────────
def test_embed_failure_raises_and_leaves_no_row(monkeypatch):
    svc, store = _svc()

    def _boom(_t):
        raise RuntimeError("embedder dead")
    monkeypatch.setattr(svc._embedder, "encode", _boom)
    with pytest.raises(RuntimeError, match="embedder dead"):
        _write(svc, "clean text that fails to embed")
    assert store.counts() == (0, 0)                 # the staged row was dropped
    assert store.index.size() == 0


def test_approve_failure_raises_and_leaves_no_row(monkeypatch):
    svc, store = _svc()
    monkeypatch.setattr(store, "approve", lambda *a, **k: False)   # lost-update CAS race
    with pytest.raises(RuntimeError, match="approve failed"):
        _write(svc, "clean text whose approve loses the CAS")
    assert store.counts() == (0, 0)                 # the dangling staged row was dropped
    assert store.index.size() == 0


# ── the secret-never-logged invariant (REFUSE + REDACT paths) ─────────────────
def test_refuse_log_contains_no_secret_substring(caplog):
    svc, store = _svc()
    with caplog.at_level(logging.DEBUG, logger="hive.admission"):
        with pytest.raises(SecretRefused):
            _write(svc, f"key {SECRET}")
    assert caplog.records                                  # something WAS logged
    for rec in caplog.records:
        assert SECRET not in rec.getMessage()
        assert SECRET not in str(rec.__dict__)             # not in any extra field either


def test_redact_log_contains_no_secret_substring(caplog):
    svc, store = _svc(mode="redact")
    with caplog.at_level(logging.DEBUG, logger="hive.admission"):
        _write(svc, f"key {SECRET} here")
    assert caplog.records
    for rec in caplog.records:
        assert SECRET not in rec.getMessage()
        assert SECRET not in str(rec.__dict__)


def test_secret_refused_exception_carries_no_secret():
    svc, store = _svc()
    try:
        _write(svc, f"key {SECRET}")
        assert False, "expected SecretRefused"
    except SecretRefused as e:
        assert SECRET not in str(e)                        # only rule names + counts


# ── the credit boundary: admission writes zero (dormant) loop tables ──────────
def test_admission_touches_no_loop_tables():
    svc, store = _svc()
    _write(svc, "boundary check")
    _write(svc, "second boundary check")
    assert _count(store, "exposure") == 0
    assert _count(store, "task_outcomes") == 0


# ═══ autonomous capture (quarantine path) + human supersession ═════════════════

class _RecordingLifecycle:
    """The trigger seam capture drives: records the synchronous promotion check
    and the decay-sweep piggyback (the real service is fail-open inside)."""
    def __init__(self) -> None:
        self.captures: list[int] = []
        self.sweeps: int = 0

    def on_capture(self, episode_id: int):
        self.captures.append(int(episode_id))
        return None

    def sweep(self):
        self.sweeps += 1
        return {}


def _svc_v2(mode: str = "refuse", *, lifecycle=None, autonomy_enabled: bool = True):
    conn = connect(":memory:")
    store = SqliteEpisodeStore(conn, index=ExhaustiveCosineIndex(DIM))
    svc = AdmissionService(store, DefaultSecretScanner(redact_mode=mode),
                           FakeProvider(d=DIM), now=lambda: 1_000,
                           lifecycle=lifecycle, autonomy_enabled=autonomy_enabled)
    return svc, store


def test_capture_lands_quarantined_unserved():
    svc, store = _svc_v2()
    r = svc.capture("an unvouched but maybe useful insight", proposed_by="agent-1")
    assert r.status == "quarantined" and r.deduped is False
    ep = store.get_episode(r.episode_id)
    assert ep.status == "approved" and ep.trust == "quarantined"   # materialized…
    assert ep.approved_by is None and ep.value is not None          # …no approver, embedded
    assert store.index.size() == 0                                  # not in the index
    assert store.scan_servable(now=2_000, provisional_ttl_s=10**6) == []  # not scannable
    assert store.scan_approved() == []                              # nor via the alias


def test_capture_secret_floor_unmoved():
    svc, store = _svc_v2()
    with pytest.raises(SecretRefused):
        svc.capture(f"my key {SECRET}", proposed_by="agent-1")
    assert _count(store, "episodes") == 0 and _count(store, "blobs") == 0
    assert store.index.size() == 0


def test_capture_triggers_promotion_check_and_sweep():
    life = _RecordingLifecycle()
    svc, store = _svc_v2(lifecycle=life)
    r = svc.capture("durable insight needing demand", proposed_by="agent-1")
    assert life.captures == [r.episode_id]            # the candidate was evaluated…
    assert life.sweeps == 1                           # …and the sweep piggybacked once
    svc.capture("a second distinct insight", proposed_by="agent-1")
    assert life.sweeps == 2                           # once per capture


def test_capture_dedup_never_touches_existing_trust():
    life = _RecordingLifecycle()
    svc, store = _svc_v2(lifecycle=life)
    est = _write(svc, "an established team fact")     # human-vouched, established
    r = svc.capture("an established team fact", proposed_by="agent-1")
    assert r.deduped is True and r.episode_id == est.episode_id
    ep = store.get_episode(est.episode_id)
    assert ep.trust == "established"                  # capture has NO power over it
    assert ep.approved_by == "human"
    assert life.captures == [] and life.sweeps == 0   # dedup is not a new candidate


def test_capture_disabled_refuses_cleanly():
    life = _RecordingLifecycle()
    svc, store = _svc_v2(lifecycle=life, autonomy_enabled=False)
    r = svc.capture("anything at all", proposed_by="agent-1")
    assert r.status == "disabled" and r.episode_id is None and r.scan is None
    assert _count(store, "episodes") == 0 and _count(store, "blobs") == 0
    assert life.captures == [] and life.sweeps == 0   # triggers never fire


def test_write_unchanged_lands_established():
    svc, store = _svc_v2()
    r = _write(svc, "human approved insight")
    assert r.status == "approved" and r.superseded is None
    ep = store.get_episode(r.episode_id)
    assert ep.trust == "established" and ep.approved_by == "human"
    assert store.index.size() == 1                    # served immediately


def test_write_replaces_supersedes_atomically():
    svc, store = _svc_v2()
    old = _write(svc, "the port is 5432")
    new = _write(svc, "the port is 6543 since the migration", replaces=old.episode_id)
    assert new.superseded == old.episode_id
    dead = store.get_episode(old.episode_id)
    assert dead.trust == "deprecated" and dead.superseded_by == new.episode_id
    assert store.get_episode(new.episode_id).trust == "established"
    # the retired row is out of every serving layer
    assert old.episode_id not in {eid for eid, _ in store.scan_approved()}
    # idempotent retry: same correction again dedups to the same replacement row
    again = _write(svc, "the port is 6543 since the migration", replaces=old.episode_id)
    assert again.deduped is True and again.episode_id == new.episode_id
    n_audits = store.conn.execute(
        "SELECT COUNT(*) AS c FROM evidence_events WHERE episode_id=? AND kind='supersede'",
        (old.episode_id,)).fetchone()["c"]
    assert n_audits == 1                              # no duplicate audit row


def test_write_replaces_unknown_target_fails_whole_call():
    svc, store = _svc_v2()
    with pytest.raises(ValueError, match="does not exist"):
        _write(svc, "a correction aimed at nothing", replaces=424242)
    # the WHOLE call failed BEFORE staging: nothing stored, nothing retired
    assert _count(store, "episodes") == 0 and _count(store, "blobs") == 0


def test_write_replaces_self_dedup_is_benign_noop():
    svc, store = _svc_v2()
    old = _write(svc, "exactly this text")
    # the "correction" dedups back to the target itself → self-supersede refused,
    # the row stays live (a memory can never retire itself)
    r = _write(svc, "exactly this text", replaces=old.episode_id)
    assert r.deduped is True and r.episode_id == old.episode_id
    assert r.superseded is None
    ep = store.get_episode(old.episode_id)
    assert ep.trust == "established" and ep.superseded_by is None
