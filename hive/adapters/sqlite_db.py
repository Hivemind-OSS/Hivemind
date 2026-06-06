"""Shared SQLite connection + single-writer transaction discipline.

One WAL file, one writer. ``tx(conn)`` is the BEGIN IMMEDIATE lane: all of a
producer tick's hops (settle → clawback → credit → watermark) run inside ONE
transaction on ONE connection [A7], so a failure anywhere rolls the whole tick
back — partial credit is impossible. isolation_level=None ⇒ we drive BEGIN/COMMIT
explicitly rather than relying on the implicit Python transaction machinery.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE … COMMIT, with ROLLBACK on any exception (atomic tick)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
