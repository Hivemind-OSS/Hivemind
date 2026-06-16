"""SqliteEpisodeStore — the durable episode store + meta watermark in ONE WAL DB.
``transaction()`` is the single BEGIN IMMEDIATE lane; the utility store shares this
connection so its credit writes commit/rollback atomically.

The Phase-0 trace↔outcome state machine was removed with the producer. Its two tables
live on with new drivers: ``exposure`` is written by the recall side-channel
(``record_exposure``), and ``task_outcomes`` is the settled-credit ledger the host-side
trailer scan feeds (one win/loss row per (commit_sha, episode_id) via
``record_outcome`` — the Phase-0 clawback shape is refused at construction).
Episode-CRUD / admission is below.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

import numpy as np

from hive.adapters.sqlite_db import tx
from hive.domain.errors import SqliteBusyExhausted
from hive.domain.lifecycle import (
    DEPRECATED, ESTABLISHED, PROVISIONAL, QUARANTINED, TRUST_STATES,
    ExposureRow, MissRow, decayed, is_servable,
)
from hive.domain.models import Episode, content_hash

_log = logging.getLogger("hive.store")

# The SQL PREFILTER under the single servability rule: only materialized rows can
# serve. The full predicate is hive.domain.lifecycle.is_servable — scan_servable()
# applies it per row, and it is the ONLY candidate source for recall (scan_approved()
# is its no-clock fail-closed alias).
_RECALL_PREDICATE = "status='approved' AND value IS NOT NULL"

# "no clock" sentinel for the fail-closed scan_approved() alias: with now pushed to
# the far future, a provisional row can never prove freshness ⇒ established-only.
_FAR_FUTURE = 1 << 62

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
  tenant_id TEXT NOT NULL,   -- constant label, never a query filter (single-tenant)
  text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL, source TEXT, tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0,
  trust TEXT NOT NULL DEFAULT 'quarantined',
  superseded_by INTEGER,
  last_active_ts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_episodes_hash ON episodes(content_hash);
CREATE INDEX IF NOT EXISTS idx_episodes_trust ON episodes(trust);
CREATE TABLE IF NOT EXISTS exposure(
  trace_id TEXT NOT NULL, episode_id INTEGER NOT NULL, recall_margin REAL NOT NULL,
  task_ref TEXT, injected_ts INTEGER NOT NULL, agent_id TEXT,
  PRIMARY KEY(trace_id, episode_id));
CREATE TABLE IF NOT EXISTS task_outcomes(
  commit_sha TEXT NOT NULL,                -- the settled artifact (main-side sha)
  episode_id INTEGER NOT NULL,             -- the credited memory
  trace_id TEXT NOT NULL,                  -- the recall envelope that exposed it
  repo TEXT,                               -- scope carrier (family in the credit regime)
  outcome TEXT NOT NULL CHECK(outcome IN ('win','loss')),
  recall_margin REAL NOT NULL,
  commit_ts INTEGER NOT NULL,
  ingested_ts INTEGER NOT NULL,            -- the settle clock (stable: first landing wins)
  PRIMARY KEY(commit_sha, episode_id));
CREATE INDEX IF NOT EXISTS idx_task_outcomes_settle ON task_outcomes(repo, ingested_ts);
CREATE INDEX IF NOT EXISTS idx_task_outcomes_episode ON task_outcomes(episode_id, outcome);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS recall_misses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query_text TEXT NOT NULL,
  query_vector BLOB,
  agent_id TEXT NOT NULL,
  miss_type TEXT NOT NULL CHECK(miss_type IN ('no_match','abstained','secret_refused')),
  ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_misses_ts ON recall_misses(ts);
CREATE TABLE IF NOT EXISTS evidence_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id INTEGER NOT NULL, kind TEXT NOT NULL,
  actor TEXT NOT NULL, ts INTEGER NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_evidence_episode ON evidence_events(episode_id, kind);
"""


class SqliteEpisodeStore:
    def __init__(self, conn: sqlite3.Connection, index=None, *,
                 isolation_frac: float = 0.0) -> None:
        self.conn = conn
        self.index = index            # MutableVectorIndex (warm cache); None in ledger-only tests
        self._isolation_frac = isolation_frac   # guardrail-2 (A5) held-out slice; 0.0 ⇒ off
        # No in-place migration path exists — this build starts from a clean, empty
        # store (prior-format memories are not carried over). CREATE IF NOT EXISTS
        # would leave an old-format episodes table untouched (and the trust-index DDL
        # would then crash cryptically), so refuse it BEFORE the script runs, with a
        # clear message. An absent table is fine — the script creates the v2 shape.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(episodes)")}
        if cols and "trust" not in cols:
            _log.error("store.schema_predates_lifecycle missing_column=trust")
            raise RuntimeError(
                "episodes table predates the trust-lifecycle schema (no trust column); "
                "this build has no migration — start from a clean store/volume")
        # Same guard for the settled-credit ledger: a Phase-0 clawback-shaped
        # task_outcomes (keyed task_ref/trace_id, no commit_sha) would silently
        # survive CREATE IF NOT EXISTS and break the credit ingest's idempotency key.
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(task_outcomes)")}
        if tcols and "commit_sha" not in tcols:
            _log.error("store.schema_predates_credit missing_column=commit_sha")
            raise RuntimeError(
                "task_outcomes table predates the settled-credit schema (no commit_sha "
                "column); this build has no migration — start from a clean store/volume")
        conn.executescript(_SCHEMA)
        # Lexical mirror of the SERVABLE set (plain FTS5, NOT contentless — per-row
        # DELETE must work on every SQLite that has FTS5). Probed, never assumed: a
        # stripped build degrades to fts_enabled=False (harmless while recall.hybrid
        # is off; the container fail-fasts at boot when hybrid is on).
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts "
                         "USING fts5(text, tokenize='porter unicode61')")
            self.fts_enabled = True
        except sqlite3.OperationalError:
            _log.warning("store.fts5_unavailable — lexical channel disabled")
            self.fts_enabled = False
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

    def complete(self, episode_id: int, value: "np.ndarray", *, expected_version: int,
                 trust: str, approver: Optional[str] = None, approved_ts: int = 0,
                 last_active_ts: int = 0) -> bool:
        """CAS-flip pending→approved (materialized: value written) with an explicit
        trust state, THEN best-effort index sync after commit [B3] — the index add
        happens IFF the row lands in a servable trust state (established/provisional;
        freshness at the stamp instant is definitional). A quarantined complete is
        embedded but absent from the index. Idempotent (already-approved ⇒ no-op True);
        a stale expected_version ⇒ no flip (lost-update / double-admission blocked).
        Durable truth is the row; boot rebuild recovers warm-cache divergence."""
        if trust not in TRUST_STATES:
            raise ValueError(f"bad trust {trust!r}")
        vbytes = np.ascontiguousarray(np.asarray(value, dtype=np.float32)).tobytes()
        with tx(self.conn):
            row = self.conn.execute(
                "SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
            if row is not None and row["status"] == "approved":
                return True   # idempotent
            n = self.conn.execute(
                "UPDATE episodes SET status='approved', approved_by=?, approved_ts=?, "
                "value=?, trust=?, last_active_ts=?, version=version+1 "
                "WHERE id=? AND version=? AND status='pending'",
                (approver, approved_ts, vbytes, trust, last_active_ts,
                 episode_id, expected_version)).rowcount
            if n == 1 and _is_isolation(episode_id, self._isolation_frac):
                # guardrail-2 (A5): the SINGLE writer of isolation membership. Stamp the
                # held-out flag ATOMICALLY with the materialization flip — same tx,
                # co-located utility table [A7] — so a materialized episode can never be
                # left un-stamped (the held-out slice is decided exactly once).
                self.conn.execute(
                    "INSERT INTO utility(episode_id, family_scope, isolation) VALUES(?,?,1) "
                    "ON CONFLICT(episode_id, family_scope) DO UPDATE SET isolation=1",
                    (episode_id, _ISOLATION_FAMILY))
            # lexical mirror, same tx as the flip (unlike the best-effort dense cache,
            # the FTS rows are durable — they must move with the trust state or never):
            # a quarantined complete stays absent, exactly like the dense index.
            if n == 1 and self.fts_enabled and trust in (ESTABLISHED, PROVISIONAL):
                self._fts_upsert(episode_id)
        if n == 1 and trust in (ESTABLISHED, PROVISIONAL) and self.index is not None:
            try:
                self.index.sync_approved(episode_id, np.asarray(value, dtype=np.float32))
            except Exception:
                # best-effort warm cache [B3]: durable truth is the row;
                # rebuild_index_from_store() on boot recovers the divergence.
                pass
        return n == 1

    def approve(self, episode_id: int, approver: str, value: "np.ndarray",
                expected_version: int, approved_ts: int = 0) -> bool:
        """The human-vouched flip — thin wrapper over ``complete(trust='established')``
        (served immediately; liveness clock starts at the approval stamp)."""
        return self.complete(episode_id, value, expected_version=expected_version,
                             trust=ESTABLISHED, approver=approver,
                             approved_ts=approved_ts, last_active_ts=approved_ts)

    def reject(self, episode_id: int, *, keep: bool = False) -> None:
        """Drop a pending row (default). keep_rejected retains it as non-recallable
        (never approved ⇒ never indexed)."""
        if keep:
            return
        with tx(self.conn):
            self.conn.execute("DELETE FROM episodes WHERE id=? AND status='pending'", (episode_id,))

    def scan_servable(self, *, now: int,
                      provisional_ttl_s: int) -> list[tuple[int, "np.ndarray"]]:
        """The ONLY candidate source for recall. SQL prefilters to materialized rows
        (_RECALL_PREDICATE); the full decision per row is the ONE pure predicate
        ``lifecycle.is_servable`` — established always, provisional iff fresh,
        quarantined/deprecated never.  // O(N) time."""
        out: list[tuple[int, np.ndarray]] = []
        for r in self.conn.execute(
                f"SELECT id, value, status, trust, last_active_ts "
                f"FROM episodes WHERE {_RECALL_PREDICATE}"):
            if is_servable(status=r["status"], trust=r["trust"],
                           last_active_ts=r["last_active_ts"], now=now,
                           provisional_ttl_s=provisional_ttl_s):
                out.append((r["id"], np.frombuffer(r["value"], dtype=np.float32).copy()))
        return out

    def scan_approved(self) -> list[tuple[int, "np.ndarray"]]:
        """Compat alias — the no-clock FAIL-CLOSED reading of ``scan_servable``: with
        ``now`` pushed to the far future a provisional row can never prove freshness,
        so this returns established-only (never over-serves)."""
        return self.scan_servable(now=_FAR_FUTURE, provisional_ttl_s=0)

    def rebuild_index_from_store(self, *, now: Optional[int] = None,
                                 provisional_ttl_s: Optional[int] = None) -> None:
        """Divergence-recovery guarantee [B3]: rebuild the warm cache from the
        servable set. Callers with a clock (the boot path) pass ``now`` +
        ``provisional_ttl_s`` so fresh provisional rows are included; with no clock
        the rebuild is fail-closed (established-only)."""
        if self.index is None:
            return
        if now is None or provisional_ttl_s is None:
            self.index.rebuild_from_store(self.scan_approved())
        else:
            self.index.rebuild_from_store(
                self.scan_servable(now=now, provisional_ttl_s=provisional_ttl_s))

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
            approved_by=r["approved_by"], approved_ts=r["approved_ts"], version=r["version"],
            trust=r["trust"], superseded_by=r["superseded_by"],
            last_active_ts=r["last_active_ts"])

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

    # ── trust lifecycle (promotion / supersession / decay) ────────────────────
    def set_trust(self, episode_id: int, new_trust: str, *, now: int) -> bool:
        """Transactional trust transition on a MATERIALIZED row. Promotion into a
        servable state stamps ``last_active_ts=now`` (a fresh promotion is never
        instantly dead); demotion leaves the clock untouched. Index sync after
        commit is best-effort [B3] — add on entering a servable trust state, remove
        on leaving (boot rebuild recovers divergence). False on unknown/unflipped
        rows; raises on an unknown trust label (caller bug, not data)."""
        if new_trust not in TRUST_STATES:
            raise ValueError(f"bad trust {new_trust!r}")
        servable_states = (ESTABLISHED, PROVISIONAL)
        with tx(self.conn):
            r = self.conn.execute(
                "SELECT status, trust, value FROM episodes WHERE id=?",
                (episode_id,)).fetchone()
            if r is None or r["status"] != "approved":
                return False
            old_trust = r["trust"]
            if new_trust in servable_states:
                self.conn.execute(
                    "UPDATE episodes SET trust=?, last_active_ts=? WHERE id=?",
                    (new_trust, now, episode_id))
            else:
                self.conn.execute(
                    "UPDATE episodes SET trust=? WHERE id=?", (new_trust, episode_id))
            # lexical mirror, same tx as the trust flip: enter servable ⇒ upsert,
            # leave servable ⇒ delete (mirrors the dense sync below, but atomic).
            if self.fts_enabled:
                if new_trust in servable_states:
                    self._fts_upsert(episode_id)
                elif old_trust in servable_states:
                    self._fts_delete(episode_id)
        if self.index is not None:
            try:
                if new_trust in servable_states and r["value"] is not None:
                    self.index.sync_approved(
                        episode_id, np.frombuffer(r["value"], dtype=np.float32).copy())
                elif old_trust in servable_states and new_trust not in servable_states:
                    self.index.remove(episode_id)
            except Exception:                      # noqa: BLE001 — warm cache only [B3]
                _log.warning("store.set_trust_index_sync_failed episode_id=%s", episode_id)
        return True

    def supersede(self, target_id: int, replacement_id: int, *, actor: str,
                  ts: int) -> bool:
        """The ONE owner of retirement-with-replacement: retire (deprecated) + stamp
        ``superseded_by`` + ONE audit row, in ONE tx; de-index after commit. Guards:
        self-supersede refused; unknown target/replacement refused; idempotent re-run
        (column already stamped with this replacement ⇒ no duplicate audit);
        re-supersede with a NEW replacement is last-write-wins on the column with
        history kept in the ledger."""
        if target_id == replacement_id:
            _log.warning("store.supersede_self_refused episode_id=%s", target_id)
            return False
        with tx(self.conn):
            t = self.conn.execute(
                "SELECT superseded_by FROM episodes WHERE id=?", (target_id,)).fetchone()
            if t is None:
                return False
            if self.conn.execute("SELECT 1 FROM episodes WHERE id=?",
                                 (replacement_id,)).fetchone() is None:
                return False
            if t["superseded_by"] == replacement_id:
                return True                        # idempotent re-run: no duplicate audit
            self.conn.execute(
                "UPDATE episodes SET trust=?, superseded_by=? WHERE id=?",
                (DEPRECATED, replacement_id, target_id))
            self.conn.execute(
                "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                "VALUES(?,?,?,?,?)",
                (target_id, "supersede", actor, ts,
                 json.dumps({"replacement_id": replacement_id})))
            if self.fts_enabled:                   # lexical mirror, same tx
                self._fts_delete(target_id)
        if self.index is not None:
            try:
                self.index.remove(target_id)
            except Exception:                      # noqa: BLE001 — warm cache only [B3]
                _log.warning("store.supersede_deindex_failed episode_id=%s", target_id)
        _log.info("store.superseded target=%s replacement=%s actor=%s",
                  target_id, replacement_id, actor)
        return True

    def terminal_successor(self, episode_id: int, *,
                           max_depth: int = 10) -> Optional[tuple[int, str]]:
        """Walk the ``superseded_by`` chain to its terminal row and return
        ``(episode_id, content_hash)`` — hash because fetch is hash-keyed. None if
        the row is not superseded (or unknown). Depth-capped + visited-set-bounded
        so a (raw-SQL-only) cycle terminates instead of hanging.  // O(depth)."""
        seen = {episode_id}
        cur = episode_id
        moved = False
        for _ in range(max_depth):
            r = self.conn.execute(
                "SELECT superseded_by FROM episodes WHERE id=?", (cur,)).fetchone()
            if r is None or r["superseded_by"] is None:
                break
            nxt = int(r["superseded_by"])
            if nxt in seen:                        # cycle: stop at the last new node
                break
            seen.add(nxt)
            cur = nxt
            moved = True
        if not moved:
            return None
        h = self.conn.execute(
            "SELECT content_hash FROM episodes WHERE id=?", (cur,)).fetchone()
        return (cur, h["content_hash"]) if h is not None else None

    def sweep_decayed(self, *, now: int, q_ttl_s: int, p_ttl_s: int) -> dict:
        """Materialize the lazy ``lifecycle.decayed`` rule: TTL-lapsed quarantined/
        provisional rows flip to deprecated, each with ONE ``ttl_expired`` audit row,
        in ONE tx; lapsed provisional rows are de-indexed after commit. Idempotent
        (deprecated rows never re-match); bounded by the live quarantined+provisional
        count.  // O(live) time."""
        flips: list[tuple[int, str]] = []          # (episode_id, old_trust)
        with tx(self.conn):
            for r in self.conn.execute(
                    "SELECT id, trust, ts, last_active_ts FROM episodes "
                    "WHERE trust IN (?,?)", (QUARANTINED, PROVISIONAL)):
                if decayed(trust=r["trust"], last_active_ts=r["last_active_ts"],
                           created_ts=r["ts"], now=now,
                           quarantine_ttl_s=q_ttl_s, provisional_ttl_s=p_ttl_s):
                    flips.append((int(r["id"]), r["trust"]))
            for eid, old_trust in flips:
                self.conn.execute(
                    "UPDATE episodes SET trust=? WHERE id=?", (DEPRECATED, eid))
                self.conn.execute(
                    "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                    "VALUES(?,?,?,?,?)",
                    (eid, "ttl_expired", "server", now,
                     json.dumps({"from": old_trust})))
                # lexical mirror, same tx as the flip (quarantined was never present)
                if self.fts_enabled and old_trust == PROVISIONAL:
                    self._fts_delete(eid)
        if self.index is not None:
            for eid, old_trust in flips:
                if old_trust == PROVISIONAL:       # quarantined rows were never indexed
                    try:
                        self.index.remove(eid)
                    except Exception:              # noqa: BLE001 — warm cache only [B3]
                        _log.warning("store.sweep_deindex_failed episode_id=%s", eid)
        out = {QUARANTINED: 0, PROVISIONAL: 0}
        for _eid, old_trust in flips:
            out[old_trust] += 1
        if flips:
            _log.info("store.sweep_decayed quarantined=%d provisional=%d now=%d",
                      out[QUARANTINED], out[PROVISIONAL], now)
        return out

    def quarantined_candidates(
            self, *, now: int, quarantine_ttl_s: int,
    ) -> list[tuple[int, "np.ndarray", str, int, int]]:
        """Live (non-decayed) quarantined rows — the promotion scan's candidate set:
        ``(id, value, proposed_by, ts, last_active_ts)``. Death is decided by the
        ONE pure ``lifecycle.decayed`` rule (don't promote the dead)."""
        out: list[tuple[int, np.ndarray, str, int, int]] = []
        for r in self.conn.execute(
                "SELECT id, value, proposed_by, ts, last_active_ts FROM episodes "
                "WHERE trust=? AND status='approved' AND value IS NOT NULL",
                (QUARANTINED,)):
            if not decayed(trust=QUARANTINED, last_active_ts=r["last_active_ts"],
                           created_ts=r["ts"], now=now,
                           quarantine_ttl_s=quarantine_ttl_s,
                           provisional_ttl_s=0):  # unused on the quarantined branch
                out.append((int(r["id"]),
                            np.frombuffer(r["value"], dtype=np.float32).copy(),
                            r["proposed_by"] or "", int(r["ts"]),
                            int(r["last_active_ts"])))
        return out

    def survival_candidates(self, *, since_ts: int,
                            min_exposures: int) -> list[tuple[int, str]]:
        """``(episode_id, proposed_by)`` of PROVISIONAL materialized rows with
        >= ``min_exposures`` exposures STRICTLY after ``since_ts`` — ONE aggregate
        (JOIN + GROUP BY + HAVING), the survival prefilter; never an N+1 per-row
        COUNT loop. The pure SurvivalRule then judges only these.  // O(exposure
        window) time in SQL."""
        return [(int(r["id"]), r["proposed_by"] or "") for r in self.conn.execute(
            "SELECT e.id AS id, e.proposed_by AS proposed_by FROM episodes e "
            "JOIN exposure x ON x.episode_id = e.id "
            "WHERE e.trust=? AND e.status='approved' AND x.injected_ts>? "
            "GROUP BY e.id HAVING COUNT(*)>=? ORDER BY e.id",
            (PROVISIONAL, since_ts, int(min_exposures)))]

    def exposures_for(self, episode_id: int, *,
                      since_ts: int) -> list[ExposureRow]:
        """One candidate's exposure events STRICTLY after ``since_ts``,
        ts-ascending — the carrier ``SurvivalRule.decide`` consumes. A NULL
        agent_id (pre-identity rows) coerces to '' so it can never alias a real
        writer label."""
        return [ExposureRow(agent_id=r["agent_id"] or "", ts=int(r["injected_ts"]))
                for r in self.conn.execute(
                    "SELECT agent_id, injected_ts FROM exposure "
                    "WHERE episode_id=? AND injected_ts>? ORDER BY injected_ts",
                    (int(episode_id), since_ts))]

    def insert_audit(self, episode_id: int, kind: str, actor: str, ts: int,
                     payload: str) -> int:
        """One server-written audit row (evidence_events is server-written ONLY —
        no tool writes here). Returns the row id."""
        with tx(self.conn):
            cur = self.conn.execute(
                "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                "VALUES(?,?,?,?,?)", (episode_id, kind, actor, ts, payload))
            return int(cur.lastrowid)

    def trust_counts(self) -> dict[str, int]:
        """Per-trust-state row counts for hive_health — ALL four states present
        (a zero is signal: quarantine pile-up must be visible, never silent)."""
        out = {t: 0 for t in TRUST_STATES}
        for r in self.conn.execute(
                "SELECT trust, COUNT(*) AS c FROM episodes GROUP BY trust"):
            if r["trust"] in out:
                out[r["trust"]] = int(r["c"])
        return out

    # ── ExposureLedger port (the recall side-channel writer) ──────────────────
    def record_exposure(self, trace_id: str, items: Sequence[tuple[int, float]],
                        *, agent_id: str, ts: int) -> None:
        """Persist WHO was served WHAT and refresh the served rows' liveness clocks
        — ONE tx, so an exposure can never land without its last_active bump (a
        served provisional row that failed to refresh would decay while in use)."""
        with tx(self.conn):
            for eid, margin in items:
                self.conn.execute(
                    "INSERT INTO exposure(trace_id, episode_id, recall_margin, "
                    "injected_ts, agent_id) VALUES(?,?,?,?,?)",
                    (trace_id, int(eid), float(margin), ts, agent_id))
            for eid, _margin in items:
                self.conn.execute(
                    "UPDATE episodes SET last_active_ts=? WHERE id=?", (ts, int(eid)))

    def exposures_by_trace(self, trace_id: str) -> list[tuple[int, float]]:
        """The credit set behind one recall envelope: the (episode_id, recall_margin)
        rows this trace actually served. Empty ⇒ unknown trace (the credit ingest
        reports it and writes nothing). // O(k) on the exposure PK."""
        rows = self.conn.execute(
            "SELECT episode_id, recall_margin FROM exposure WHERE trace_id=? "
            "ORDER BY episode_id", (trace_id,)).fetchall()
        return [(int(r["episode_id"]), float(r["recall_margin"])) for r in rows]

    def record_outcome(self, *, commit_sha: str, episode_id: int, trace_id: str,
                       repo: Optional[str], outcome: str, recall_margin: float,
                       commit_ts: int, ingested_ts: int) -> bool:
        """Upsert one settled credit row keyed (commit_sha, episode_id); True iff a NEW
        row landed (False ⇒ already credited — full-history re-scans are free). The
        conflict-ignore is scoped to the PK (ON CONFLICT … DO NOTHING) — NOT a blanket
        OR IGNORE, which would also swallow the CHECK and let a bad outcome label
        vanish silently; dropping the clause is the mutation the double-ingest
        test catches. ``outcome`` stays CHECK-constrained loud at the table boundary.
        // O(1)."""
        with tx(self.conn):
            cur = self.conn.execute(
                "INSERT INTO task_outcomes(commit_sha, episode_id, trace_id, "
                "repo, outcome, recall_margin, commit_ts, ingested_ts) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(commit_sha, episode_id) DO NOTHING",
                (commit_sha, int(episode_id), trace_id, repo, outcome,
                 float(recall_margin), int(commit_ts), int(ingested_ts)))
            return cur.rowcount > 0

    def settle_loss(self, commit_sha: str, *, ts: int) -> int:
        """One-way win→loss flip for every credit row of one commit (a revert names
        it). Returns the flip count. DO-NOTHING insert + this one-way flip keeps the
        ledger MONOTONE under stateless hourly rescans: a revert arriving after the
        win's ingest settles it, and a later re-scan re-inserting the win is ignored
        by the PK — no win/loss oscillation. Rides the PK prefix. // O(rows per sha)."""
        with tx(self.conn):
            cur = self.conn.execute(
                "UPDATE task_outcomes SET outcome='loss', ingested_ts=? "
                "WHERE commit_sha=? AND outcome='win'", (int(ts), commit_sha))
            return cur.rowcount

    def outcome_stats_for_episodes(
            self, episode_ids: Sequence[int]) -> dict[int, tuple[int, int]]:
        """(wins, losses) per requested episode id, zero-default — every requested id
        is present in the result so a consumer never KeyErrors on an uncredited
        memory. Rides idx_task_outcomes_episode. // O(k) grouped lookup."""
        out = {int(e): (0, 0) for e in episode_ids}
        if not out:
            return out
        placeholders = ",".join("?" for _ in out)
        for r in self.conn.execute(
                f"SELECT episode_id, outcome, COUNT(*) AS c FROM task_outcomes "
                f"WHERE episode_id IN ({placeholders}) GROUP BY episode_id, outcome",
                tuple(out)):
            wins, losses = out[int(r["episode_id"])]
            if r["outcome"] == "win":
                wins = int(r["c"])
            else:
                losses = int(r["c"])
            out[int(r["episode_id"])] = (wins, losses)
        return out

    def outcome_totals(self) -> dict:
        """Settled-credit totals for reports/status: row volume + distinct credited
        memories (the two readiness floor inputs). // O(n) scan, report-time only."""
        r = self.conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT episode_id) AS m "
            "FROM task_outcomes").fetchone()
        return {"settled_rows": int(r["n"]), "distinct_episodes": int(r["m"])}

    def record_miss(self, query_text: str, query_vector: Optional[bytes],
                    agent_id: str, miss_type: str, *, ts: int) -> None:
        """Persist one non-answer (the demand signal). The CALLER owns the secret
        floor — a refused query arrives here already stripped (empty text, None
        vector); a redacted one arrives masked with a vector re-encoded from the
        masked text. miss_type is CHECK-constrained by the DDL."""
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO recall_misses(query_text, query_vector, agent_id, "
                "miss_type, ts) VALUES(?,?,?,?,?)",
                (query_text, query_vector, agent_id, miss_type, ts))

    def episode_id_by_hash(self, content_hash_hex: str) -> Optional[int]:
        """Resolve a content hash to its episode id (fetch is hash-keyed; the
        supersession annotation needs the row behind the hash)."""
        r = self.conn.execute(
            "SELECT id FROM episodes WHERE content_hash=? LIMIT 1",
            (content_hash_hex,)).fetchone()
        return int(r["id"]) if r is not None else None

    def miss_count_since(self, since_ts: int) -> int:
        """Misses recorded strictly after ``since_ts`` (hive_health telemetry)."""
        return int(self.conn.execute(
            "SELECT COUNT(*) AS c FROM recall_misses WHERE ts>?",
            (since_ts,)).fetchone()["c"])

    def misses_detail_window(self, since_ts: int) -> list[dict]:
        """Full miss rows (text + type + ts + optional vector) for the demand-gap
        report — unlike ``misses_window`` this INCLUDES vector-less secret_refused
        rows (they count in telemetry; they can never drive promotion)."""
        out: list[dict] = []
        for r in self.conn.execute(
                "SELECT query_text, query_vector, miss_type, ts FROM recall_misses "
                "WHERE ts>? ORDER BY id", (since_ts,)):
            vec = (np.frombuffer(r["query_vector"], dtype=np.float32).copy()
                   if r["query_vector"] is not None else None)
            out.append({"query_text": r["query_text"], "vector": vec,
                        "miss_type": r["miss_type"], "ts": int(r["ts"])})
        return out

    def misses_window(self, since_ts: int) -> list[MissRow]:
        """The demand-window slice: misses STRICTLY after ``since_ts`` that carry a
        vector (secret-refused rows persist none and can never drive promotion)."""
        return [MissRow(vector=np.frombuffer(r["query_vector"], dtype=np.float32).copy(),
                        agent_id=r["agent_id"], ts=int(r["ts"]))
                for r in self.conn.execute(
                    "SELECT query_vector, agent_id, ts FROM recall_misses "
                    "WHERE ts>? AND query_vector IS NOT NULL ORDER BY id", (since_ts,))]

    # ── lexical (FTS5) mirror of the servable set ─────────────────────────────
    # The store owns every byte of episodes_fts SQL: membership moves in the SAME
    # transactions that move trust states (the 4 sync sites above), so the mirror
    # cannot diverge from the rows it was written with. The read side is the ONE
    # LexicalIndex port method; the boot rebuild is defense-in-depth. TTL lapse by
    # clock (no transition) is the one staleness mode no sync sees — the recall
    # resolve belt (is_servable at RESOLVE) stays the authoritative last word.
    def _fts_upsert(self, episode_id: int) -> None:
        """Idempotent delete-then-insert; INSERT-SELECT so the text never
        round-trips through Python. Caller holds the tx + the fts_enabled guard."""
        self.conn.execute("DELETE FROM episodes_fts WHERE rowid=?", (episode_id,))
        self.conn.execute(
            "INSERT INTO episodes_fts(rowid, text) "
            "SELECT id, text FROM episodes WHERE id=?", (episode_id,))

    def _fts_delete(self, episode_id: int) -> None:
        self.conn.execute("DELETE FROM episodes_fts WHERE rowid=?", (episode_id,))

    def search_text(self, query: str, k: int) -> list[tuple[int, float]]:
        """LexicalIndex port: BM25 over the FTS mirror. The raw query is reduced to
        its word tokens (capped at 64) and rebuilt as an OR of quoted tokens —
        quoting defines FTS5-syntax injection/errors out of existence (AND/NOT/
        parens/quotes in user text are inert). Returns (episode_id, score)
        score-DESCENDING (bm25() is smaller-is-better ⇒ score = −bm25). No tokens /
        fts disabled / engine error ⇒ [] (fail-open to the dense channel)."""
        if not self.fts_enabled:
            return []
        tokens = re.findall(r"\w+", query)[:64]
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"' for t in tokens)
        try:
            return [(int(r["rowid"]), -float(r["score"]))
                    for r in self.conn.execute(
                        "SELECT rowid, bm25(episodes_fts) AS score FROM episodes_fts "
                        "WHERE episodes_fts MATCH ? ORDER BY bm25(episodes_fts) "
                        "LIMIT ?", (match, int(k)))]
        except sqlite3.OperationalError:
            _log.warning("store.fts_query_failed — degrading to dense-only")
            return []

    def rebuild_fts(self, *, now: int, provisional_ttl_s: int) -> None:
        """Boot self-heal, sibling of ``rebuild_index_from_store``: delete-all +
        re-insert the servable set (the same per-row ``is_servable`` filter as
        ``scan_servable``), in ONE tx. No-op when fts is disabled."""
        if not self.fts_enabled:
            return
        with tx(self.conn):
            self.conn.execute("DELETE FROM episodes_fts")
            for r in self.conn.execute(
                    f"SELECT id, status, trust, last_active_ts "
                    f"FROM episodes WHERE {_RECALL_PREDICATE}"):
                if is_servable(status=r["status"], trust=r["trust"],
                               last_active_ts=r["last_active_ts"], now=now,
                               provisional_ttl_s=provisional_ttl_s):
                    self.conn.execute(
                        "INSERT INTO episodes_fts(rowid, text) "
                        "SELECT id, text FROM episodes WHERE id=?", (r["id"],))

    # ── meta watermark kv ─────────────────────────────────────────────────────
    def meta_get(self, key: str) -> Optional[str]:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
