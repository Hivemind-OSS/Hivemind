"""Chunk 4 — connect(check_same_thread=…): the warm HTTP daemon shares ONE conn across
handler threads (serialized by the global lock), so the daemon's conn must disable sqlite's
per-thread guard. The default stays True (existing single-threaded callers keep the guard,
so a stray cross-thread use is still caught loud). Keyword-only ⇒ every positional caller is
unchanged.
"""
from __future__ import annotations

import sqlite3
import threading

from hive.adapters.sqlite_db import connect


def _use_from_other_thread(conn) -> dict:
    err: dict = {}

    def use():
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
            conn.execute("INSERT INTO t VALUES(1)")
        except Exception as e:           # noqa: BLE001 — capture the type for the assertion
            err["type"] = type(e).__name__

    th = threading.Thread(target=use)
    th.start()
    th.join()
    return err


def test_check_same_thread_false_allows_cross_thread_use():
    conn = connect(":memory:", check_same_thread=False)
    err = _use_from_other_thread(conn)
    assert err == {}                                         # usable from another thread (daemon need)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_default_keeps_single_thread_guard():
    conn = connect(":memory:")                               # default check_same_thread=True
    err = _use_from_other_thread(conn)
    assert err.get("type") == "ProgrammingError"            # cross-thread use still fails loud


def test_connect_still_sets_production_pragmas_with_kwarg(tmp_path):
    # the new kwarg must not regress the WAL/row-factory contract on a file DB
    conn = connect(str(tmp_path / "x.db"), check_same_thread=False)
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
