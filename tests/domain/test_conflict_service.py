"""C7 — the advisory ConflictFlagService (pure): disabled-inert, self-pair + unknown-id
+ winner-rule validation BEFORE any write, scan-resolution-before-persist, canonicalization,
idempotency, and the LOAD-BEARING no-retire guarantee (a flag NEVER changes an episode's
trust/superseded_by — resolution stays human via hive_supersede)."""
from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.conflict import ConflictFlagService, FlagResult
from hive.domain.errors import SecretRefused
from hive.domain.secret_scan import REDACT, REFUSE
from tests.fakes._fakes import (
    FakeClock, FakeConflictFlagStore, FakeEpisodeReader, FakeScanner,
)

_AKIA = "AKIAIOSFODNN7EXAMPLE"   # AKIA + 16 chars → the aws_akia rule fires


def _svc(*, enabled=True, scanner=None, reader=None, flags=None, t=1000):
    reader = reader if reader is not None else _reader_with(10, 20)
    return ConflictFlagService(
        flag_store=flags if flags is not None else FakeConflictFlagStore(),
        scanner=scanner or FakeScanner(), reader=reader,
        now=FakeClock(t).now, enabled=enabled), reader


def _reader_with(*ids) -> FakeEpisodeReader:
    r = FakeEpisodeReader()
    for i in ids:
        r.add(i, f"memory {i}")
    return r


# ── disabled ⇒ inert (nothing written) ─────────────────────────────────────────
def test_disabled_returns_disabled_and_writes_nothing():
    flags = FakeConflictFlagStore()
    svc, _ = _svc(enabled=False, flags=flags)
    res = svc.flag(kind="conflict", a_id=10, b_id=20, winner_id=None,
                   resolution="", proposed_by="a")
    assert isinstance(res, FlagResult) and res.status == "disabled"
    assert flags.rows == []


# ── validation BEFORE persistence ──────────────────────────────────────────────
def test_self_pair_rejected_nothing_written():
    flags = FakeConflictFlagStore()
    svc, _ = _svc(flags=flags)
    with pytest.raises(ValueError):
        svc.flag(kind="conflict", a_id=10, b_id=10, winner_id=None,
                 resolution="", proposed_by="a")
    assert flags.rows == []


def test_unknown_episode_rejected_nothing_written():
    flags = FakeConflictFlagStore()
    svc, _ = _svc(reader=_reader_with(10), flags=flags)   # 20 does not exist
    with pytest.raises(ValueError):
        svc.flag(kind="conflict", a_id=10, b_id=20, winner_id=None,
                 resolution="", proposed_by="a")
    assert flags.rows == []


def test_supersedes_requires_winner_in_pair():
    flags = FakeConflictFlagStore()
    svc, _ = _svc(flags=flags)
    with pytest.raises(ValueError):                       # no winner
        svc.flag(kind="supersedes", a_id=10, b_id=20, winner_id=None,
                 resolution="", proposed_by="a")
    with pytest.raises(ValueError):                       # winner not in {a, b}
        svc.flag(kind="supersedes", a_id=10, b_id=20, winner_id=99,
                 resolution="", proposed_by="a")
    assert flags.rows == []
    # a winner that IS in the pair succeeds
    assert svc.flag(kind="supersedes", a_id=10, b_id=20, winner_id=20,
                    resolution="", proposed_by="a").status == "flagged"
    assert flags.rows[0]["winner_id"] == 20


def test_conflict_is_symmetric_winner_dropped():
    # kind='conflict' is symmetric: any passed winner is normalized to None
    flags = FakeConflictFlagStore()
    svc, _ = _svc(flags=flags)
    svc.flag(kind="conflict", a_id=10, b_id=20, winner_id=20,
             resolution="", proposed_by="a")
    assert flags.rows[0]["winner_id"] is None


# ── scan resolution BEFORE persist ──────────────────────────────────────────────
def test_secret_in_resolution_refused_nothing_written():
    flags = FakeConflictFlagStore()
    svc, _ = _svc(scanner=FakeScanner(REFUSE), flags=flags)
    with pytest.raises(SecretRefused):
        svc.flag(kind="conflict", a_id=10, b_id=20, winner_id=None,
                 resolution=f"the key is {_AKIA}", proposed_by="a")
    assert flags.rows == []                               # 0 rows on refuse


def test_redacted_resolution_stored_masked():
    flags = FakeConflictFlagStore()
    svc, _ = _svc(scanner=FakeScanner(REDACT), flags=flags)
    svc.flag(kind="conflict", a_id=10, b_id=20, winner_id=None,
             resolution=f"the key is {_AKIA}", proposed_by="a")
    stored = flags.rows[0]["resolution"]
    assert _AKIA not in stored and "[REDACTED]" in stored


# ── canonicalization + idempotency ──────────────────────────────────────────────
def test_canonicalizes_a_lt_b():
    flags = FakeConflictFlagStore()
    svc, _ = _svc(reader=_reader_with(12, 40), flags=flags)
    svc.flag(kind="conflict", a_id=40, b_id=12, winner_id=None,
             resolution="", proposed_by="a")
    assert (flags.rows[0]["a_id"], flags.rows[0]["b_id"]) == (12, 40)


def test_idempotent_reflag_recorded_false():
    flags = FakeConflictFlagStore()
    svc, _ = _svc(reader=_reader_with(12, 40), flags=flags)
    first = svc.flag(kind="conflict", a_id=12, b_id=40, winner_id=None,
                     resolution="", proposed_by="a")
    # a re-flag (even with swapped ids) hits the same canonical pair → recorded False
    second = svc.flag(kind="conflict", a_id=40, b_id=12, winner_id=None,
                      resolution="", proposed_by="b")
    assert first.recorded is True and second.recorded is False
    assert len(flags.rows) == 1


# ── ★ the load-bearing no-retire guarantee ─────────────────────────────────────
def test_flag_never_retires_episode_state_unchanged():
    # the real store is BOTH flag_store and reader; flagging must leave both episodes'
    # trust + superseded_by exactly as they were (advisory ≠ resolution, THEORY §10 O7).
    conn = connect(":memory:")
    store = SqliteEpisodeStore(conn, index=ExhaustiveCosineIndex(4))
    vec = np.eye(4, dtype=np.float32)[0]
    a, _ = store.stage(text="A: deploy with make", weight=1.0, tags="",
                       proposed_by="w")
    store.approve(a, "human", vec, expected_version=0, approved_ts=10)
    b, _ = store.stage(text="B: deploy with ship.sh", weight=1.0, tags="",
                       proposed_by="w")
    store.approve(b, "human", vec, expected_version=0, approved_ts=10)

    # tripwire: if the service ever reaches for retirement, this raises
    def _boom(*a, **k):
        raise AssertionError("ConflictFlagService must NEVER call supersede")
    store.supersede = _boom   # type: ignore[assignment]

    svc = ConflictFlagService(flag_store=store, scanner=FakeScanner(), reader=store,
                              now=FakeClock(1000).now, enabled=True)
    res = svc.flag(kind="supersedes", a_id=a, b_id=b, winner_id=b,
                   resolution="b is the corrected deploy", proposed_by="agent-A")
    assert res.status == "flagged"
    # both episodes remain exactly servable — NOTHING retired
    for eid in (a, b):
        ep = store.get_episode(eid)
        assert ep.trust == "established" and ep.superseded_by is None
