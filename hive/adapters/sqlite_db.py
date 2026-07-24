"""Shared SQLite connection + single-writer transaction discipline.

One WAL file, one writer. ``tx(conn)`` is the BEGIN IMMEDIATE lane: all of a
multi-hop write's steps (e.g. a demand-promotion tick: stamp trust → record the
exposure → refresh the index row) run inside ONE transaction on ONE connection,
so a failure anywhere rolls the whole tick back — a half-applied promotion is
impossible. isolation_level=None ⇒ we drive BEGIN/COMMIT
explicitly rather than relying on the implicit Python transaction machinery.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator


def connect(
    path: str = ":memory:", *, check_same_thread: bool = True
) -> sqlite3.Connection:
    # check_same_thread=False lets the warm HTTP daemon share ONE conn across handler threads
    # (thread-safety is then the caller's global lock, not sqlite's per-thread guard). The
    # default True keeps single-threaded callers guarded; the param is keyword-only so every
    # existing positional caller is unchanged.
    conn = sqlite3.connect(
        path, isolation_level=None, check_same_thread=check_same_thread
    )
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
