"""SqliteEpisodeStore.stale_suspect_rows — the newest ``stale_suspect`` ledger row per
episode, as ``[(episode_id, ts, payload)]``. Read-only, no DDL; powers the
``hive_health(include_stale_suspects)`` worklist, whose app-side report owns the
servable filter and the defensive payload parse (the payload rides OPAQUE here)."""
from __future__ import annotations

import json

import numpy as np

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.evidence_kinds import EK_STALE_SUSPECT

DIM = 4


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"))


def _seed(s: SqliteEpisodeStore, text: str) -> int:
    eid, _ = s.stage(text=text, weight=1.0, tags="", proposed_by="w", ts=10,
                     anchor="a.py::F")
    s.complete(eid, np.eye(DIM, dtype=np.float32)[0], expected_version=0,
               trust="established", approver="h", approved_ts=10, last_active_ts=10)
    return eid


def _suspect_payload(seed: str = "m/x.py::Seed") -> str:
    return json.dumps({"schema": "stale_suspect/v1",
                       "matched": {"path": "a.py", "symbol": "F"},
                       "seed": seed, "drift": "removed",
                       "stamp": {"base_sha": "a" * 40, "head_sha": "b" * 40}},
                      sort_keys=True, separators=(",", ":"))


def test_newest_suspect_row_wins_per_episode():
    s = _store()
    eid = _seed(s, "suspected memory")
    s.insert_audit(eid, EK_STALE_SUSPECT, "census", 100, _suspect_payload("old.py::S"))
    s.insert_audit(eid, EK_STALE_SUSPECT, "census", 200, _suspect_payload("new.py::S"))
    rows = s.stale_suspect_rows()
    assert len(rows) == 1
    got_eid, got_ts, payload = rows[0]
    assert (got_eid, got_ts) == (eid, 200)
    assert json.loads(payload)["seed"] == "new.py::S"


def test_same_ts_newest_row_id_wins():
    s = _store()
    eid = _seed(s, "same-ts memory")
    s.insert_audit(eid, EK_STALE_SUSPECT, "census", 100, _suspect_payload("first.py::S"))
    s.insert_audit(eid, EK_STALE_SUSPECT, "census", 100, _suspect_payload("second.py::S"))
    (_, _, payload), = s.stale_suspect_rows()
    assert json.loads(payload)["seed"] == "second.py::S"


def test_one_row_per_episode_across_episodes():
    s = _store()
    a, b, plain = _seed(s, "one"), _seed(s, "two"), _seed(s, "never suspected")
    s.insert_audit(a, EK_STALE_SUSPECT, "census", 100, _suspect_payload())
    s.insert_audit(b, EK_STALE_SUSPECT, "census", 100, _suspect_payload())
    rows = s.stale_suspect_rows()
    assert {r[0] for r in rows} == {a, b}
    assert plain not in {r[0] for r in rows}


def test_other_evidence_kinds_never_count():
    s = _store()
    eid = _seed(s, "only other kinds")
    s.insert_audit(eid, "verify_stale", "census", 100, _suspect_payload())
    s.insert_audit(eid, "change_outcome", "census", 100, _suspect_payload())
    assert s.stale_suspect_rows() == []


def test_payload_rides_opaque_even_when_malformed():
    # the store read is shape-only (newest per id); the app report owns the parse.
    s = _store()
    eid = _seed(s, "broken payload")
    s.insert_audit(eid, EK_STALE_SUSPECT, "census", 100, "not-json")
    (got_eid, _, payload), = s.stale_suspect_rows()
    assert got_eid == eid and payload == "not-json"


def test_empty_ledger_returns_empty_list():
    assert _store().stale_suspect_rows() == []
