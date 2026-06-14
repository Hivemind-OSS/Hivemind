"""hive trends — the read-only KPI surface (`hive.tools.trendsctl`).

Driven against the REAL sqlite store (the aggregates are SQL; a fake would test the fake),
seeded through the prod connect() factory and CLOSED before probing so the read-only
mode=ro connection sees committed rows. Assertions are τ-INDEPENDENT (recalls_confident /
confident_rate / n_promotions) — only demand_entropy depends on the clusterer τ, so the
test never couples to how demand_tau resolves. Plus the never-raise main() contract.
"""
from __future__ import annotations

import json

import numpy as np

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.tools import trendsctl

D = 8
DAY = 86_400
NOW = 100 * DAY


def _unit(*xs) -> np.ndarray:
    v = np.array(list(xs) + [0.0] * (D - len(xs)), dtype=np.float32)
    return v / np.linalg.norm(v)


U = _unit(1.0)


def _seed(db_path) -> None:
    """One confident recall + one miss + one promotion, through the prod factory; CLOSE the
    writer so its WAL checkpoints and a fresh mode=ro reader sees the rows."""
    conn = connect(str(db_path))
    s = SqliteEpisodeStore(conn, index=ExhaustiveCosineIndex(D))
    eid, _ = s.stage(text="x" * 40, weight=1.0, source="t", tags="",
                     proposed_by="writer", ts=NOW - 5 * DAY)
    s.complete(eid, U, expected_version=0, trust="established", approver="h",
               approved_ts=NOW - 5 * DAY, last_active_ts=NOW - 5 * DAY)
    s.record_exposure("t-1", [(eid, 0.5)], agent_id="a", ts=NOW - 4 * DAY)
    s.record_miss("m1", U.tobytes(), "a", "no_match", ts=NOW - 2 * DAY)
    s.insert_audit(eid, "promote", "server", NOW - 2 * DAY, '{"rule":"demand"}')
    conn.close()


def test_default_probe_against_seeded_db(tmp_path):
    db = tmp_path / "shared.db"
    _seed(db)
    snap = trendsctl._default_probe({"HIVE_STORE__DB_PATH": str(db)}, now=NOW)
    cur = snap["current"]
    assert cur["recalls_confident"] == 1
    assert cur["confident_rate"] == 0.5          # 1 confident / (1 confident + 1 miss)
    assert cur["n_promotions"] == 1
    assert set(snap) == {"current", "previous", "deltas"}   # the envelope shape


def test_empty_store_is_zero_safe(tmp_path):
    db = tmp_path / "shared.db"
    conn = connect(str(db))
    SqliteEpisodeStore(conn, index=ExhaustiveCosineIndex(D))   # creates an empty schema
    conn.close()
    snap = trendsctl._default_probe({"HIVE_STORE__DB_PATH": str(db)}, now=NOW)
    assert snap["current"]["recalls_confident"] == 0
    assert snap["current"]["confident_rate"] == 0.0


def test_missing_db_is_failure_not_crash(tmp_path, capsys):
    # a never-booted store: _default_probe raises (mode=ro on a missing file); main maps
    # that to EX_FAIL with NO stdout (an empty read must not look like a valid report).
    rc = trendsctl.main(env={"HIVE_STORE__DB_PATH": str(tmp_path / "nope.db")})
    assert rc == trendsctl.EX_FAIL
    assert capsys.readouterr().out == ""


def test_probe_raise_returns_nonzero_no_stdout(capsys):
    def boom(env):
        raise RuntimeError("db gone")
    rc = trendsctl.main(env={}, probe=boom)
    assert rc == trendsctl.EX_FAIL
    assert capsys.readouterr().out == ""


def test_main_prints_probe_json(capsys):
    fake = {"current": {"confident_rate": 0.7}, "previous": {}, "deltas": {}}
    rc = trendsctl.main(env={}, probe=lambda env: fake)
    assert rc == trendsctl.EX_OK
    assert json.loads(capsys.readouterr().out) == fake
