"""M11 — the one ops floor (PORT of the reference `ops/dr.py`): an online, WAL-safe SQLite
snapshot via `sqlite3.backup()` + keep-N retention.

`backup()` is safe to call while writers are active and copies only COMMITTED state.
`prune_backups()` keeps the N most-recent `.db` files (sorts DESCending by mtime BEFORE
slicing — the newest survive). `run_daily_backup(cfg)` is the scheduled entry point.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hive.app.config import Config

_log = logging.getLogger("hive.backup")


@dataclass(frozen=True)
class BackupRecord:
    dest_path: str
    bytes_copied: int
    ts: float


def _unlink_quietly(path: str) -> None:
    """Best-effort remove of a file and its SQLite sidecars; never raises."""
    for p in (path, path + "-wal", path + "-shm"):
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:                  # pragma: no cover - defensive
            pass


def backup(src: str, dest: str) -> BackupRecord:
    """Atomic online snapshot via SQLite's backup API. Safe while writers are active; copies
    only committed state.

    The snapshot is written to a temp `dest.partial` and atomically `os.replace()`d into `dest`
    ONLY after a successful commit — so a failure mid-copy (non-SQLite source, disk-full, I/O
    error) can NEVER leave a partial/empty file at the canonical `dest` path (where prune would
    later keep it as the apparent newest backup and evict a good one). A pre-existing good `dest`
    is left untouched on failure. // O(db size)."""
    src = str(src)
    dest = str(dest)
    if not Path(src).exists():
        raise FileNotFoundError(f"source db not found: {src}")
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    tmp = dest + ".partial"
    t0 = time.perf_counter()
    src_conn = sqlite3.connect(src)
    try:
        _unlink_quietly(tmp)             # clear any stale partial from a prior crash
        dest_conn = sqlite3.connect(tmp)
        try:
            src_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
        os.replace(tmp, dest)            # atomic publish — a failed backup never reaches `dest`
    except Exception as exc:             # noqa: BLE001 — log, drop the partial, re-raise
        _log.error("backup.failed src=%s dest=%s kind=%s", src, dest, type(exc).__name__)
        _unlink_quietly(tmp)
        raise
    finally:
        src_conn.close()
    bytes_copied = Path(dest).stat().st_size
    dt = time.perf_counter() - t0
    _log.info("backup.ok src=%s dest=%s bytes=%d dt=%.3fs", src, dest, bytes_copied, dt)
    return BackupRecord(dest_path=dest,
                        bytes_copied=bytes_copied, ts=time.time())


def prune_backups(backup_dir: str, keep: int = 30) -> list[str]:
    """Keep the `keep` MOST-RECENT `*.db` files in `backup_dir`; delete the rest. Returns the
    removed paths. Sorts DESCending by mtime BEFORE slicing — so `[keep:]` is the OLDEST.
    // O(F log F), F = file count."""
    backup_dir = str(backup_dir)
    if not Path(backup_dir).is_dir():
        return []
    files = sorted(
        Path(backup_dir).glob("*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,                    # newest first ⇒ tail (oldest) is removed
    )
    removed: list[str] = []
    for f in files[keep:]:
        try:
            f.unlink()
            removed.append(str(f))
        except OSError as exc:
            _log.warning("backup.prune_failed path=%s kind=%s", f, type(exc).__name__)
    if removed:
        _log.info("backup.pruned removed=%d dir=%s kept=%d", len(removed), backup_dir, keep)
    return removed


def run_daily_backup(cfg: "Config") -> BackupRecord:
    """Scheduled entry: snapshot `cfg.db_path` into `cfg.retention.backup_dir` then prune to
    `cfg.retention.backup_keep`. The single ops floor."""
    src = cfg.runtime.db_path
    backup_dir = cfg.retention.backup_dir or os.path.join(
        os.path.dirname(os.path.abspath(src)) or ".", "backups")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest = os.path.join(backup_dir, f"hive-{stamp}.db")
    rec = backup(src, dest)
    prune_backups(backup_dir, cfg.retention.backup_keep)
    return rec
