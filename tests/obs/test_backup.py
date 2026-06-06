"""P1.9 — M11 backup floor: online sqlite3.backup() snapshot + keep-N retention,
WAL-safe under a concurrent uncommitted writer.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from hive.ops.backup import backup, prune_backups, run_daily_backup


def _make_db(path: str, rows: int = 3) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t(v) VALUES(?)", [(f"row{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def test_backup_roundtrip(tmp_path):
    src = str(tmp_path / "src.db")
    dest = str(tmp_path / "snap.db")
    _make_db(src, rows=5)
    rec = backup(src, dest)
    assert os.path.exists(dest)
    assert rec.bytes_copied > 0
    conn = sqlite3.connect(dest)
    n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert n == 5


def test_backup_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup(str(tmp_path / "nope.db"), str(tmp_path / "out.db"))


def test_failed_backup_leaves_no_junk_at_dest(tmp_path):
    # a valid-but-non-SQLite source makes src_conn.backup() raise mid-copy; the canonical dest
    # must NOT be left as a partial/empty file (which prune would later keep as the newest backup
    # and use to evict a real one). Regression for the HIGH audit finding.
    src = str(tmp_path / "notdb.bin")
    dest = str(tmp_path / "snap.db")
    with open(src, "wb") as fh:
        fh.write(b"this is definitely not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        backup(src, dest)
    assert not os.path.exists(dest)               # no junk at the canonical path
    assert not os.path.exists(dest + ".partial")  # the temp was cleaned up too


def test_failed_backup_preserves_existing_good_dest(tmp_path):
    # if a good backup already sits at dest, a later failed backup must leave it intact.
    good_src = str(tmp_path / "good.db")
    dest = str(tmp_path / "snap.db")
    _make_db(good_src, rows=4)
    backup(good_src, dest)                         # dest now holds a real 4-row snapshot
    bad_src = str(tmp_path / "bad.bin")
    with open(bad_src, "wb") as fh:
        fh.write(b"corrupt")
    with pytest.raises(sqlite3.DatabaseError):
        backup(bad_src, dest)
    conn = sqlite3.connect(dest)
    n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert n == 4                                  # the prior good snapshot survived untouched


def test_run_daily_backup_snapshots_and_prunes(tmp_path):
    from hive.app.config import Config
    db = str(tmp_path / "live.db")
    _make_db(db, rows=3)
    backup_dir = str(tmp_path / "bk")
    cfg = Config.load(db_path=db, retention={"backup_keep": 5, "backup_dir": backup_dir})
    rec = run_daily_backup(cfg)
    assert os.path.exists(rec.dest_path)
    assert rec.dest_path.startswith(backup_dir)
    assert len(list((tmp_path / "bk").glob("*.db"))) == 1


def test_prune_keeps_n_most_recent(tmp_path):
    # create 6 backup files with strictly increasing mtimes
    d = tmp_path / "backups"
    d.mkdir()
    paths = []
    for i in range(6):
        p = d / f"hive-{i:02d}.db"
        p.write_bytes(b"x")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))  # ascending mtime
        paths.append(p)
    removed = prune_backups(str(d), keep=2)
    survivors = sorted(p.name for p in d.glob("*.db"))
    # the TWO most-recent (highest mtime) survive: indices 04, 05
    assert survivors == ["hive-04.db", "hive-05.db"]
    assert len(removed) == 4


def test_prune_empty_dir_is_noop(tmp_path):
    assert prune_backups(str(tmp_path / "absent"), keep=3) == []


def test_backup_wal_safe(tmp_path):
    src = str(tmp_path / "wal.db")
    dest = str(tmp_path / "wal-snap.db")
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t(v) VALUES('committed')")
    conn.commit()

    # hold an OPEN uncommitted write on a second connection during the backup
    writer = sqlite3.connect(src)
    writer.execute("BEGIN")
    writer.execute("INSERT INTO t(v) VALUES('uncommitted')")
    try:
        backup(src, dest)
    finally:
        writer.rollback()
        writer.close()
        conn.close()

    check = sqlite3.connect(dest)
    integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    # the snapshot must be internally consistent and must NOT contain the uncommitted row
    n = check.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    check.close()
    assert integrity == "ok"
    assert n == 1
