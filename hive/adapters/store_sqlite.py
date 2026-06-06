"""SqliteEpisodeStore — Phase-0 ledger group [A7]: exposure + task_outcomes + the
meta watermark, all in ONE WAL DB. ``transaction()`` is the producer tick's single
BEGIN IMMEDIATE lane; the utility store shares this connection so its credit writes
commit/rollback atomically with the settlement. The telemetry sink is collapsed
into this DB — there is no separate telemetry.db [A7].

Episode-CRUD / admission methods are added in Phase 1.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

import numpy as np

from hive.adapters.sqlite_db import tx
from hive.domain.errors import SqliteBusyExhausted
from hive.domain.models import (
    Episode, OutcomeRow, RecallWindow, WindowTrace, content_hash,
)

_log = logging.getLogger("hive.store")

# The SINGLE definition of "recallable" — kills the verified 4-site tombstoned=0
# predicate-scatter. scan_approved() is the only candidate source for recall.
_RECALL_PREDICATE = "status='approved'"

# Sentinel family for episode-level held-out membership (guardrail-2, A5). Isolation
# is per-EPISODE, but the utility table is keyed (episode_id, family_scope); we stamp
# under "*" — a value no real family ("repo|lang|workflow") can collide with, so
# utility_map(<real family>) never sees it while isolation_episode_ids() (WHERE
# isolation=1, family-agnostic) does.
_ISOLATION_FAMILY = "*"


def _is_isolation(episode_id: int, isolation_frac: float) -> bool:
    """The SINGLE deterministic held-out predicate (guardrail-2, A5): a hash-stable
    ~isolation_frac slice of episode ids. Idempotent (same boolean every call — no RNG),
    stable across restarts (pure function of the id), O(1). digest()[:7] = 56 bits ⇒
    h/2^56 ∈ [0,1); the slice never reweights, so the loop can't self-confirm on it."""
    if isolation_frac <= 0.0:
        return False
    h = int.from_bytes(hashlib.sha256(str(episode_id).encode()).digest()[:7], "big")
    return (h / float(1 << 56)) < isolation_frac

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs(
  content_hash TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL, text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL, source TEXT, tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_episodes_hash ON episodes(content_hash);
CREATE TABLE IF NOT EXISTS exposure(
  trace_id TEXT NOT NULL, episode_id INTEGER NOT NULL, recall_margin REAL NOT NULL,
  task_ref TEXT, injected_ts INTEGER NOT NULL,
  PRIMARY KEY(trace_id, episode_id));
CREATE TABLE IF NOT EXISTS task_outcomes(
  task_ref TEXT NOT NULL, trace_id TEXT NOT NULL, family_scope TEXT NOT NULL, repo TEXT,
  files_touched TEXT, introduced_lines TEXT,
  state TEXT NOT NULL CHECK(state IN ('provisional','settled_pos','clawed_back')),
  reward REAL NOT NULL, merge_ts INTEGER NOT NULL, settle_at INTEGER NOT NULL,
  PRIMARY KEY(task_ref, trace_id));
CREATE INDEX IF NOT EXISTS idx_task_outcomes_settle ON task_outcomes(state, settle_at);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class SqliteEpisodeStore:
    def __init__(self, conn: sqlite3.Connection, index=None, *,
                 isolation_frac: float = 0.0) -> None:
        self.conn = conn
        self.index = index            # MutableVectorIndex (warm cache); None in ledger-only tests
        self._isolation_frac = isolation_frac   # guardrail-2 (A5) held-out slice; 0.0 ⇒ off
        conn.executescript(_SCHEMA)
        if isolation_frac > 0.0:
            # guardrail-2 (A5) stamps held-out membership into the co-located utility
            # table at approve(); that table is owned by SqliteUtilityStore. FAIL FAST +
            # LOUD here if it is absent (construct SqliteUtilityStore(conn) FIRST), rather
            # than letting the first held-out approval crash on a cryptic OperationalError
            # — a silent stamp failure would leave the held-out slice UNWRITTEN and credit
            # episodes the guardrail is meant to hold out (the guardrail broken invisibly).
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='utility'"
                            ).fetchone() is None:
                _log.error("store.isolation_misconfigured: isolation_frac=%s but no utility "
                           "table on this connection", isolation_frac)
                raise ValueError(
                    f"isolation_frac={isolation_frac} requires the utility table on this "
                    "connection — construct SqliteUtilityStore(conn) before "
                    "SqliteEpisodeStore(conn, isolation_frac=...) so guardrail-2 membership "
                    "can be stamped atomically at approve().")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with tx(self.conn):
            yield

    # ── episodes / blob group (admission CAS state machine) ───────────────────
    def stage(self, *, text: str, weight: float, source: str, tags: str,
              proposed_by: str, tenant_id: str = "default", ts: int = 0) -> tuple[int, bool]:
        """Insert a PENDING row (value NULL — not recallable, not indexed) + its blob.
        Dedup by content_hash: a repeat of identical text returns the existing id,
        deduped=True, no second row."""
        h = content_hash(text)
        with tx(self.conn):
            existing = self.conn.execute(
                "SELECT id FROM episodes WHERE content_hash=? LIMIT 1", (h,)).fetchone()
            if existing is not None:
                return int(existing["id"]), True
            self.conn.execute(
                "INSERT OR IGNORE INTO blobs(content_hash, text) VALUES(?,?)", (h, text))
            cur = self.conn.execute(
                "INSERT INTO episodes(tenant_id, text, value, weight, ts, source, tags, "
                "content_hash, status, proposed_by, version) "
                "VALUES(?,?,NULL,?,?,?,?,?,'pending',?,0)",
                (tenant_id, text, weight, ts, source, tags, h, proposed_by))
            return int(cur.lastrowid), False

    def approve(self, episode_id: int, approver: str, value: "np.ndarray",
                expected_version: int, approved_ts: int = 0) -> bool:
        """CAS-flip pending→approved (writes value) THEN best-effort index sync after
        commit [B3]. Idempotent (already-approved ⇒ no-op True); a stale expected_version
        ⇒ no flip (lost-update / double-admission blocked). The index add is a warm-cache
        side effect — durable truth is status='approved'; boot rebuild recovers divergence."""
        vbytes = np.ascontiguousarray(np.asarray(value, dtype=np.float32)).tobytes()
        with tx(self.conn):
            row = self.conn.execute(
                "SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
            if row is not None and row["status"] == "approved":
                return True   # idempotent
            n = self.conn.execute(
                "UPDATE episodes SET status='approved', approved_by=?, approved_ts=?, "
                "value=?, version=version+1 "
                "WHERE id=? AND version=? AND status='pending'",
                (approver, approved_ts, vbytes, episode_id, expected_version)).rowcount
            if n == 1 and _is_isolation(episode_id, self._isolation_frac):
                # guardrail-2 (A5): the SINGLE writer of isolation membership. Stamp the
                # held-out flag ATOMICALLY with the approval flip — same tx, co-located
                # utility table [A7] — so an approved episode can never be left un-stamped
                # (the loop's held-out slice is decided exactly once, at admission).
                self.conn.execute(
                    "INSERT INTO utility(episode_id, family_scope, isolation) VALUES(?,?,1) "
                    "ON CONFLICT(episode_id, family_scope) DO UPDATE SET isolation=1",
                    (episode_id, _ISOLATION_FAMILY))
        if n == 1 and self.index is not None:
            try:
                self.index.sync_approved(episode_id, np.asarray(value, dtype=np.float32))
            except Exception:
                # best-effort warm cache [B3]: durable truth is status='approved';
                # rebuild_index_from_store() on boot recovers the divergence.
                pass
        return n == 1

    def reject(self, episode_id: int, *, keep: bool = False) -> None:
        """Drop a pending row (default). keep_rejected retains it as non-recallable
        (never approved ⇒ never indexed)."""
        if keep:
            return
        with tx(self.conn):
            self.conn.execute("DELETE FROM episodes WHERE id=? AND status='pending'", (episode_id,))

    def scan_approved(self) -> list[tuple[int, "np.ndarray"]]:
        """The ONLY candidate source for recall — the single _RECALL_PREDICATE."""
        out: list[tuple[int, np.ndarray]] = []
        for r in self.conn.execute(
            f"SELECT id, value FROM episodes WHERE {_RECALL_PREDICATE} AND value IS NOT NULL"):
            out.append((r["id"], np.frombuffer(r["value"], dtype=np.float32).copy()))
        return out

    def rebuild_index_from_store(self) -> None:
        """Divergence-recovery guarantee [B3]: rebuild the warm cache from approved-only."""
        if self.index is not None:
            self.index.rebuild_from_store(self.scan_approved())

    def list_pending(self, since: int = 0) -> list[dict]:
        """Pending proposals with ts >= since (the durable cursor for hive_pending),
        ordered by id. since=0 ⇒ all pending."""
        return [{"id": r["id"], "text": r["text"], "proposed_by": r["proposed_by"], "ts": r["ts"]}
                for r in self.conn.execute(
                    "SELECT id, text, proposed_by, ts FROM episodes "
                    "WHERE status='pending' AND ts>=? ORDER BY id", (since,))]

    def get_episode(self, episode_id: int) -> Optional[Episode]:
        r = self.conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if r is None:
            return None
        value = np.frombuffer(r["value"], dtype=np.float32).copy() if r["value"] is not None else None
        return Episode(
            id=r["id"], tenant_id=r["tenant_id"], text=r["text"], value=value,
            weight=r["weight"], ts=r["ts"], source=r["source"] or "", tags=r["tags"] or "",
            content_hash=r["content_hash"], status=r["status"], proposed_by=r["proposed_by"] or "",
            approved_by=r["approved_by"], approved_ts=r["approved_ts"], version=r["version"])

    def fetch(self, content_hash_hex: str) -> Optional[str]:
        r = self.conn.execute(
            "SELECT text FROM blobs WHERE content_hash=?", (content_hash_hex,)).fetchone()
        return r["text"] if r else None

    def counts(self) -> tuple[int, int]:
        """(n_approved, n_pending) for hive_health — one grouped scan.  // O(N) time."""
        approved = pending = 0
        for r in self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM episodes GROUP BY status"):
            if r["status"] == "approved":
                approved = int(r["c"])
            elif r["status"] == "pending":
                pending = int(r["c"])
        return approved, pending

    # ── exposure ──────────────────────────────────────────────────────────────
    def record_exposure(self, trace_id: str, rows: Sequence[tuple[int, float]], ts: int) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO exposure(trace_id, episode_id, recall_margin, task_ref, injected_ts) "
            "VALUES(?,?,?,NULL,?)",
            [(trace_id, int(e), float(m), ts) for e, m in rows])

    def exposed_for(self, trace_id: str) -> list[tuple[int, float]]:
        return [(r["episode_id"], r["recall_margin"]) for r in self.conn.execute(
            "SELECT episode_id, recall_margin FROM exposure WHERE trace_id=? ORDER BY episode_id",
            (trace_id,))]

    def set_task_ref(self, trace_id: str, task_ref: str) -> None:
        self.conn.execute("UPDATE exposure SET task_ref=? WHERE trace_id=?", (task_ref, trace_id))

    def task_ref_of(self, trace_id: str) -> Optional[str]:
        r = self.conn.execute(
            "SELECT task_ref FROM exposure WHERE trace_id=? LIMIT 1", (trace_id,)).fetchone()
        return r["task_ref"] if r else None

    def recall_window(self, min_ts: int) -> RecallWindow:
        """Un-linked recent recall traces (task_ref IS NULL, injected_ts >= min_ts)
        — the window the producer associates commits against."""
        rows = self.conn.execute(
            "SELECT DISTINCT trace_id, MIN(injected_ts) AS ts FROM exposure "
            "WHERE task_ref IS NULL AND injected_ts>=? GROUP BY trace_id", (min_ts,))
        return RecallWindow(items=tuple(WindowTrace(r["trace_id"], r["ts"]) for r in rows))

    # ── task_outcomes state machine ───────────────────────────────────────────
    def link_task(self, row: OutcomeRow) -> None:
        self.conn.execute(
            "INSERT INTO task_outcomes(task_ref, trace_id, family_scope, repo, files_touched, "
            "introduced_lines, state, reward, merge_ts, settle_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_ref, trace_id) DO UPDATE SET "
            "  family_scope=excluded.family_scope, state=excluded.state, reward=excluded.reward, "
            "  settle_at=excluded.settle_at, introduced_lines=excluded.introduced_lines",
            (row.task_ref, row.trace_id, row.family_scope, row.repo,
             ",".join(row.files_touched), ",".join(map(str, sorted(row.introduced_lines))),
             row.state, row.reward, row.merge_ts, row.settle_at))

    def get_outcome(self, task_ref: str, trace_id: str) -> Optional[OutcomeRow]:
        r = self.conn.execute(
            "SELECT * FROM task_outcomes WHERE task_ref=? AND trace_id=?",
            (task_ref, trace_id)).fetchone()
        if r is None:
            return None
        lines = frozenset(int(x) for x in r["introduced_lines"].split(",") if x)
        files = tuple(f for f in r["files_touched"].split(",") if f)
        return OutcomeRow(task_ref=r["task_ref"], trace_id=r["trace_id"],
                          family_scope=r["family_scope"], state=r["state"], reward=r["reward"],
                          merge_ts=r["merge_ts"], settle_at=r["settle_at"],
                          introduced_lines=lines, files_touched=files, repo=r["repo"] or "")

    def due_settlements(self, now: int) -> list[str]:
        return [r["task_ref"] for r in self.conn.execute(
            "SELECT DISTINCT task_ref FROM task_outcomes WHERE state='provisional' AND settle_at<=? "
            "ORDER BY task_ref", (now,))]

    def outcomes_for_task(self, task_ref: str) -> list[OutcomeRow]:
        out = []
        for r in self.conn.execute("SELECT trace_id FROM task_outcomes WHERE task_ref=?", (task_ref,)):
            row = self.get_outcome(task_ref, r["trace_id"])
            if row:
                out.append(row)
        return out

    def all_live_outcomes(self) -> list[OutcomeRow]:
        out = []
        for r in self.conn.execute(
            "SELECT task_ref, trace_id FROM task_outcomes WHERE state IN ('provisional','settled_pos')"):
            row = self.get_outcome(r["task_ref"], r["trace_id"])
            if row:
                out.append(row)
        return out

    def settle(self, task_ref: str, trace_id: str, reward: float) -> None:
        self.conn.execute(
            "UPDATE task_outcomes SET state='settled_pos', reward=? "
            "WHERE task_ref=? AND trace_id=? AND state='provisional'",
            (reward, task_ref, trace_id))

    def clawback(self, task_ref: str, trace_id: str, reward: float) -> None:
        self.conn.execute(
            "UPDATE task_outcomes SET state='clawed_back', reward=? "
            "WHERE task_ref=? AND trace_id=? AND state IN ('provisional','settled_pos')",
            (reward, task_ref, trace_id))

    # ── meta watermark kv ─────────────────────────────────────────────────────
    def meta_get(self, key: str) -> Optional[str]:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
