"""P1.13 / M12 — the healthcheck: exit 0 IFF ok AND embedder_loaded.

The ★ mutation target lives here: dropping the `embedder_loaded` conjunct lets a cold
server report healthy. Also pins the cross-process design — a SEPARATE healthcheck
process reads a readiness marker (+ verifies the serve PID is alive) from the shared DB,
so a stale marker from a crashed server does NOT read green. No network.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from hive.tools import entrypoint as E
from hive.tools import healthcheck as H


# ── the conjunction contract (injected probe) ────────────────────────────────
def test_healthcheck_green_when_loaded():
    rc = H.main(probe=lambda env: {"ok": True, "embedder_loaded": True})
    assert rc == H.HEALTHY == 0


def test_healthcheck_red_before_embedder_resident():
    # ★ ok but the model is not resident ⇒ UNHEALTHY (the one fault that would let an
    # orchestrator route a recall at a cold server). Dropping the conjunct = the mutation.
    rc = H.main(probe=lambda env: {"ok": True, "embedder_loaded": False})
    assert rc == H.UNHEALTHY == 1


def test_healthcheck_red_when_db_unreachable():
    rc = H.main(probe=lambda env: {"ok": False, "embedder_loaded": False})
    assert rc == H.UNHEALTHY


def test_healthcheck_red_when_keys_absent():
    rc = H.main(probe=lambda env: {})
    assert rc == H.UNHEALTHY


def test_healthcheck_marker_keys_match_entrypoint():
    # the two modules agree on the marker keys WITHOUT importing each other (drift guard)
    assert H.MARK_EMBEDDER_LOADED == E.MARK_EMBEDDER_LOADED
    assert H.MARK_SERVE_PID == E.MARK_SERVE_PID
    assert H.MARK_SERVE_STARTTIME == E.MARK_SERVE_STARTTIME


# ── _pid_alive + _proc_starttime liveness probes ──────────────────────────────
def test_pid_alive_self_true_and_invalid_false():
    assert H._pid_alive(os.getpid()) is True
    assert H._pid_alive(0) is False
    assert H._pid_alive(-1) is False
    # a positive-but-dead pid (no such process) must read as not-alive
    assert H._pid_alive(2147483646) is False


def test_proc_starttime_self_and_dead_and_cross_module_agree():
    st = H._proc_starttime(os.getpid())
    assert isinstance(st, int) and st > 0
    assert H._proc_starttime(2147483646) is None       # dead/absent ⇒ None
    assert H._proc_starttime(-1) is None
    # entrypoint and healthcheck must read /proc/<pid>/stat IDENTICALLY (the value the
    # entrypoint records must be the value the healthcheck compares).
    assert E._proc_starttime(os.getpid()) == st


# ── default probe over a REAL tmp DB (no network) ─────────────────────────────
def _make_db(tmp_path, *, marker=None, pid=None, starttime="__live__"):
    """starttime defaults to the live start-time of `pid` (so the marker is internally
    consistent — a green-eligible row); pass an explicit value to simulate a stale one."""
    db = tmp_path / "shared.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    if marker is not None:
        conn.execute("INSERT INTO meta VALUES (?,?)", (H.MARK_EMBEDDER_LOADED, marker))
    if pid is not None:
        conn.execute("INSERT INTO meta VALUES (?,?)", (H.MARK_SERVE_PID, str(pid)))
        st = H._proc_starttime(pid) if starttime == "__live__" else starttime
        if st is not None:
            conn.execute("INSERT INTO meta VALUES (?,?)", (H.MARK_SERVE_STARTTIME, str(st)))
    conn.commit()
    conn.close()
    return db


def test_default_probe_green_with_marker_and_live_pid(tmp_path):
    db = _make_db(tmp_path, marker="1", pid=os.getpid())   # live starttime by default
    snap = H._default_probe({"HIVE_STORE__DB_PATH": str(db)})
    assert snap["ok"] is True and snap["embedder_loaded"] is True
    assert H.main(env={"HIVE_STORE__DB_PATH": str(db)}) == H.HEALTHY


def test_default_probe_red_without_marker(tmp_path):
    db = _make_db(tmp_path, marker=None, pid=os.getpid())
    snap = H._default_probe({"HIVE_STORE__DB_PATH": str(db)})
    assert snap["ok"] is True and snap["embedder_loaded"] is False


def test_default_probe_red_with_dead_pid(tmp_path):
    # marker set but the recorded server process is gone ⇒ NOT healthy (no stale-green)
    db = _make_db(tmp_path, marker="1", pid=2147483646, starttime="123")
    snap = H._default_probe({"HIVE_STORE__DB_PATH": str(db)})
    assert snap["ok"] is True and snap["embedder_loaded"] is False
    assert H.main(env={"HIVE_STORE__DB_PATH": str(db)}) == H.UNHEALTHY


def test_default_probe_red_on_pid_reuse_stale_incarnation(tmp_path):
    # THE restart hazard: the recorded PID is ALIVE (PID 1 reused across restart) but its
    # recorded start-time belongs to a PRIOR incarnation ⇒ the marker is stale ⇒ red.
    db = _make_db(tmp_path, marker="1", pid=os.getpid(), starttime="999999999")
    snap = H._default_probe({"HIVE_STORE__DB_PATH": str(db)})
    assert H._pid_alive(os.getpid()) is True            # the pid IS alive...
    assert snap["embedder_loaded"] is False             # ...but the incarnation does not match
    assert H.main(env={"HIVE_STORE__DB_PATH": str(db)}) == H.UNHEALTHY


def test_default_probe_red_when_starttime_absent(tmp_path):
    # marker + live pid but NO recorded start-time ⇒ cannot prove identity ⇒ red
    db = _make_db(tmp_path, marker="1", pid=os.getpid(), starttime=None)
    snap = H._default_probe({"HIVE_STORE__DB_PATH": str(db)})
    assert snap["embedder_loaded"] is False


def test_default_probe_red_when_db_missing(tmp_path):
    snap = H._default_probe({"HIVE_STORE__DB_PATH": str(tmp_path / "nope.db")})
    assert snap["ok"] is False and snap["embedder_loaded"] is False


def test_healthcheck_no_network(tmp_path, monkeypatch):
    # prove the health path opens NO socket: make socket.socket fatal and confirm a real
    # default-probe run still returns green from the local DB alone.
    import socket

    def _boom(*a, **k):
        raise RuntimeError("network access attempted in healthcheck")

    monkeypatch.setattr(socket, "socket", _boom)
    db = _make_db(tmp_path, marker="1", pid=os.getpid())
    assert H.main(env={"HIVE_STORE__DB_PATH": str(db)}) == H.HEALTHY


def test_default_probe_no_create(tmp_path):
    # the healthcheck must never create/migrate the store as a side effect
    missing = tmp_path / "absent.db"
    H._default_probe({"HIVE_STORE__DB_PATH": str(missing)})
    assert not missing.exists()


def test_default_probe_opens_read_only(tmp_path, monkeypatch):
    # stronger than no-create: the connection URI must request mode=ro, so a regression to
    # mode=rw (a write-lock on the live WAL store from a separate process) is caught.
    db = _make_db(tmp_path, marker="1", pid=os.getpid())
    seen = {}
    real_connect = sqlite3.connect

    def _spy(database, *a, **k):                        # NB: param not named 'uri' — connect
        seen["uri"] = database                          # is called with a uri=True kwarg
        return real_connect(database, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", _spy)
    H._default_probe({"HIVE_STORE__DB_PATH": str(db)})
    assert "mode=ro" in seen.get("uri", ""), f"probe did not open read-only: {seen.get('uri')!r}"


# ── report mode (--trends): conn-only KPI snapshot off the warm store ──────────
_REPORT_NOW = 100 * 86_400


def _seed_trends_db(tmp_path):
    """A temp FILE store with two in-window misses, checkpointed (TRUNCATE) so a
    later mode=ro reader sees committed rows (WAL frames flushed to the main file)."""
    import numpy as np

    from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
    from hive.adapters.sqlite_db import connect
    from hive.adapters.store_sqlite import SqliteEpisodeStore

    db = tmp_path / "shared.db"
    store = SqliteEpisodeStore(connect(str(db)), index=ExhaustiveCosineIndex(8))
    vec = np.eye(1, 8, 0, dtype=np.float32).ravel()
    store.record_miss("q1", vec.tobytes(), "a", "no_match", ts=_REPORT_NOW - 86_400)
    store.record_miss("q2", vec.tobytes(), "a", "abstained", ts=_REPORT_NOW - 2 * 86_400)
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.conn.close()
    return db


def test_report_trends_emits_envelope(tmp_path, capsys):
    db = _seed_trends_db(tmp_path)
    rc = H.main(["--trends"], env={"HIVE_STORE__DB_PATH": str(db)}, now=_REPORT_NOW)
    snap = json.loads(capsys.readouterr().out)
    assert snap["ok"] is True and snap["db_path"] == str(db)
    assert set(snap["trends"]) == {"current", "previous", "deltas"}
    assert snap["trends"]["current"]["misses_no_match"] == 1
    assert snap["trends"]["current"]["misses_abstained"] == 1
    # report mode reports KPIs; its exit reflects store reachability, not embedder residency
    assert rc == H.HEALTHY


def test_bare_healthcheck_unchanged_by_report_path(tmp_path, capsys):
    # no --trends ⇒ pure liveness: the Docker HEALTHCHECK contract is intact and NO
    # report JSON is written to stdout.
    db = _make_db(tmp_path, marker="1", pid=os.getpid())
    rc = H.main(env={"HIVE_STORE__DB_PATH": str(db)})
    assert rc == H.HEALTHY
    assert capsys.readouterr().out == ""


def test_report_trends_never_creates_db(tmp_path, capsys):
    missing = tmp_path / "absent.db"
    rc = H.main(["--trends"], env={"HIVE_STORE__DB_PATH": str(missing)}, now=_REPORT_NOW)
    snap = json.loads(capsys.readouterr().out)
    assert snap["ok"] is False and not missing.exists()   # report mode is read-only
    assert rc == H.UNHEALTHY
