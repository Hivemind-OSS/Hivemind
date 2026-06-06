"""P0.5 — SqliteEpisodeStore ledger group: exposure + task_outcomes + meta, one DB [A7]."""
from __future__ import annotations

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.models import OutcomeRow

FAM = "r|python|general"


@pytest.fixture()
def store():
    return SqliteEpisodeStore(connect(":memory:"))


def _row(task_ref="c1", trace_id="t1", state="provisional", settle_at=1000,
         introduced=frozenset({10, 11})):
    return OutcomeRow(task_ref=task_ref, trace_id=trace_id, family_scope=FAM, state=state,
                      reward=0.2, merge_ts=0, settle_at=settle_at, introduced_lines=introduced,
                      files_touched=("a.py",), repo="r")


def test_record_exposure_and_read(store):
    store.record_exposure("t1", [(1, 0.6), (2, 0.4)], ts=100)
    assert store.exposed_for("t1") == [(1, 0.6), (2, 0.4)]
    assert store.task_ref_of("t1") is None


def test_set_task_ref_joins_window(store):
    store.record_exposure("t1", [(1, 0.6)], ts=100)
    store.set_task_ref("t1", "c1")
    assert store.task_ref_of("t1") == "c1"


def test_settle_due_only_ripe_provisional(store):
    store.link_task(_row(trace_id="ripe", settle_at=500))
    store.link_task(_row(trace_id="unripe", settle_at=5000))
    assert store.due_settlements(now=1000) == ["c1"]   # both share task_ref c1, ripe one present
    # distinct trace granularity:
    store2 = SqliteEpisodeStore(connect(":memory:"))
    store2.link_task(_row(task_ref="a", trace_id="x", settle_at=500))
    store2.link_task(_row(task_ref="b", trace_id="y", settle_at=5000))
    assert store2.due_settlements(now=1000) == ["a"]


def test_task_outcomes_pk_upsert(store):
    store.link_task(_row(state="provisional"))
    store.link_task(_row(state="provisional"))   # same (task_ref, trace_id)
    assert len(store.outcomes_for_task("c1")) == 1   # PK upsert, no duplicate


def test_link_get_roundtrip(store):
    store.link_task(_row(introduced=frozenset({10, 11, 12})))
    got = store.get_outcome("c1", "t1")
    assert got.introduced_lines == frozenset({10, 11, 12})
    assert got.files_touched == ("a.py",) and got.family_scope == FAM


def test_settle_and_clawback_transitions(store):
    store.link_task(_row())
    store.settle("c1", "t1", 0.2)
    assert store.get_outcome("c1", "t1").state == "settled_pos"
    store.clawback("c1", "t1", -1.0)
    assert store.get_outcome("c1", "t1").state == "clawed_back"


def test_meta_watermark(store):
    assert store.meta_get("_last_drain_ts") is None
    store.meta_set("_last_drain_ts", "42")
    assert store.meta_get("_last_drain_ts") == "42"


def test_recall_window_excludes_linked(store):
    store.record_exposure("t1", [(1, 0.6)], ts=100)
    store.record_exposure("t2", [(2, 0.4)], ts=120)
    store.set_task_ref("t2", "c9")   # linked ⇒ excluded
    win = store.recall_window(min_ts=0)
    assert {w.trace_id for w in win.items} == {"t1"}
