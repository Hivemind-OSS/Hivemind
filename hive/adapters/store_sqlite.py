"""SqliteEpisodeStore — the durable episode store + meta watermark in ONE WAL DB.
``sqlite_db.tx()`` is the single BEGIN IMMEDIATE lane, used directly by each mutating method.

The ``exposure`` table is the recall side-channel (``record_exposure`` / ``record_miss``):
WHO was served WHAT and which queries got no answer — the demand signal that drives
promotion. Episode-CRUD / admission is below.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional, Sequence

import numpy as np

from hive.adapters.sqlite_db import tx
from hive.domain.evidence_kinds import (
    EK_OUTCOME_HELPED, EK_OUTCOME_VERIFIED_HELPED, EK_PROMOTE, EK_PRUNE,
    EK_STALE_SUSPECT, EK_SUPERSEDE, EK_TTL_EXPIRED, EK_VERIFY_CURRENT,
    EK_VERIFY_STALE,
)
from hive.domain.kinds import DEFAULT_KIND, KIND_NAMES
from hive.domain.lifecycle import (
    DEPRECATED, ESTABLISHED, PROVISIONAL, QUARANTINED, TRUST_STATES,
    MissRow, decayed, is_servable,
)
from hive.domain.models import Episode, content_hash
from hive.domain.provenance import DEFAULT_PROVENANCE, PROVENANCE_NAMES

_log = logging.getLogger("hive.store")

# The SQL PREFILTER under the single servability rule: only materialized rows can
# serve. The full predicate is hive.domain.lifecycle.is_servable — scan_servable()
# applies it per row, and it is the ONLY candidate source for recall (scan_approved()
# is its no-clock fail-closed alias).
_RECALL_PREDICATE = "status='approved' AND value IS NOT NULL"

# "no clock" sentinel for the fail-closed scan_approved() alias: with now pushed to
# the far future, a provisional row can never prove freshness ⇒ established-only.
_FAR_FUTURE = 1 << 62

# The kind column DDL is built from the registry (sorted for a stable CHECK) so the stored
# vocabulary cannot drift from hive.domain.kinds — the single source every surface projects.
# Injected by sentinel below because _SCHEMA stays a plain string (it carries a literal
# JSON '{}' default that an f-string would misread).
_KIND_COLUMN_DDL = ("kind TEXT NOT NULL DEFAULT '%s' CHECK(kind IN (%s))" % (
    DEFAULT_KIND, ", ".join(f"'{k}'" for k in sorted(KIND_NAMES))))

# The provenance column DDL is built from the registry the same way (sorted for a stable
# CHECK) so the stored origin vocabulary cannot drift from hive.domain.provenance.
_PROVENANCE_COLUMN_DDL = (
    "provenance TEXT NOT NULL DEFAULT '%s' CHECK(provenance IN (%s))" % (
        DEFAULT_PROVENANCE, ", ".join(f"'{p}'" for p in sorted(PROVENANCE_NAMES))))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs(
  content_hash TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,   -- constant label, never a query filter (single-tenant)
  text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL, __PROVENANCE_COLUMN__, tags TEXT,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT, approved_by TEXT, approved_ts INTEGER,
  version INTEGER NOT NULL DEFAULT 0,
  trust TEXT NOT NULL DEFAULT 'quarantined',
  superseded_by INTEGER,
  last_active_ts INTEGER NOT NULL DEFAULT 0,
  polarity TEXT NOT NULL DEFAULT 'neutral' CHECK(polarity IN ('do','dont','neutral')),
  __KIND_COLUMN__,
  anchor TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_episodes_hash ON episodes(content_hash);
CREATE INDEX IF NOT EXISTS idx_episodes_trust ON episodes(trust);
CREATE TABLE IF NOT EXISTS exposure(
  trace_id TEXT NOT NULL, episode_id INTEGER NOT NULL, recall_margin REAL NOT NULL,
  task_ref TEXT, injected_ts INTEGER NOT NULL, agent_id TEXT,
  PRIMARY KEY(trace_id, episode_id));
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
CREATE TABLE IF NOT EXISTS conflict_flags(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('conflict','supersedes')),
  a_id INTEGER NOT NULL, b_id INTEGER NOT NULL,   -- canonical a_id < b_id; hash derived on read
  winner_id INTEGER,
  resolution TEXT NOT NULL DEFAULT '',
  proposed_by TEXT NOT NULL, ts INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','dismissed')));
CREATE UNIQUE INDEX IF NOT EXISTS idx_conflict_flags_pair
  ON conflict_flags(a_id, b_id, kind);
""".replace("__KIND_COLUMN__", _KIND_COLUMN_DDL).replace(
    "__PROVENANCE_COLUMN__", _PROVENANCE_COLUMN_DDL)


class SqliteEpisodeStore:
    def __init__(self, conn: sqlite3.Connection, index=None) -> None:
        self.conn = conn
        self.index = index            # MutableVectorIndex (warm cache); None in ledger-only tests
        # No in-place migration path exists — this build starts from a clean, empty
        # store (prior-format memories are not carried over). CREATE IF NOT EXISTS
        # would leave an old-format episodes table untouched (and the trust-index DDL
        # would then crash cryptically), so refuse it BEFORE the script runs, with a
        # clear message. An absent table is fine — the script creates the v2 shape.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(episodes)")}
        missing = next(
            (c for c in ("trust", "polarity", "kind", "anchor", "provenance")
             if c not in cols), None)
        if cols and missing:
            _log.error("store.schema_predates_lifecycle missing_column=%s", missing)
            raise RuntimeError(
                f"episodes table predates the current schema (no {missing} column); "
                "this build has no migration — start from a clean store/volume")
        # The ONE explicit additive migration: a lifecycle-current table that merely
        # predates `meta` gains the column in place — additive + defaulted (provably
        # lossless), loud, one column, one direction. Refuse-and-reset here would
        # brick `hive restore` of any pre-meta backup (the restored file would
        # re-trigger the refusal); genuinely pre-lifecycle tables still refuse above.
        if cols and "meta" not in cols:
            _log.warning(
                "store.migrate_add_meta adding episodes.meta TEXT NOT NULL DEFAULT '' "
                "(one-way additive column migration; existing rows read back '')")
            conn.execute(
                "ALTER TABLE episodes ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
        conn.executescript(_SCHEMA)

    # ── episodes / blob group (admission CAS state machine) ───────────────────
    def stage(self, *, text: str, weight: float, tags: str,
              proposed_by: str, tenant_id: str = "default", ts: int = 0,
              provenance: str = DEFAULT_PROVENANCE,
              polarity: str = "neutral", kind: str = DEFAULT_KIND,
              anchor: str = "", meta: str = "") -> tuple[int, bool]:
        """Insert a PENDING row (value NULL — not recallable, not indexed) + its blob.
        Dedup by content_hash: a repeat of identical text returns the existing id,
        deduped=True, no second row. ``provenance`` (the ORIGIN — provenance.py),
        ``polarity`` (do|dont|neutral), ``kind`` (registry vocabulary), ``anchor``
        (the WHERE — file/module/symbol), and ``meta`` (the serialized opaque map —
        meta.py owns the grammar) are the carried-not-interpreted consumer labels,
        never embedded; they default fail-safe (agent_reasoned / neutral / note / empty).
        ``provenance`` and ``kind`` are CHECK-constrained by the DDL; ``anchor`` is free
        text. On dedup the existing row's labels are preserved — including ``meta``,
        which is never merged/overwritten (identity is the text hash alone)."""
        h = content_hash(text)
        with tx(self.conn):
            existing = self.conn.execute(
                "SELECT id FROM episodes WHERE content_hash=? LIMIT 1", (h,)).fetchone()
            if existing is not None:
                return int(existing["id"]), True
            self.conn.execute(
                "INSERT OR IGNORE INTO blobs(content_hash, text) VALUES(?,?)", (h, text))
            cur = self.conn.execute(
                "INSERT INTO episodes(tenant_id, text, value, weight, ts, provenance, tags, "
                "content_hash, status, proposed_by, version, polarity, kind, anchor, meta) "
                "VALUES(?,?,NULL,?,?,?,?,?,'pending',?,0,?,?,?,?)",
                (tenant_id, text, weight, ts, provenance, tags, h, proposed_by, polarity,
                 kind, anchor, meta))
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

    def scan_servable_labeled(
            self, *, now: int, provisional_ttl_s: int,
    ) -> list[tuple[int, "np.ndarray", str, str, int, str]]:
        """The conflict scan's candidate source: every SERVABLE row with the labels the
        detector classifies by — ``(id, value, polarity, anchor, ts, trust)``. Same SQL
        prefilter + ONE ``lifecycle.is_servable`` decision as ``scan_servable`` (so a
        deprecated / quarantined / stale-provisional row is NEVER surfaced for a
        conflict — a superseded row already retired can't reappear).  // O(N) time."""
        out: list[tuple[int, np.ndarray, str, str, int, str]] = []
        for r in self.conn.execute(
                f"SELECT id, value, status, trust, last_active_ts, polarity, anchor, ts "
                f"FROM episodes WHERE {_RECALL_PREDICATE}"):
            if is_servable(status=r["status"], trust=r["trust"],
                           last_active_ts=r["last_active_ts"], now=now,
                           provisional_ttl_s=provisional_ttl_s):
                out.append((r["id"],
                            np.frombuffer(r["value"], dtype=np.float32).copy(),
                            r["polarity"], r["anchor"], int(r["ts"]), r["trust"]))
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

    def get_episode(self, episode_id: int) -> Optional[Episode]:
        r = self.conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if r is None:
            return None
        value = np.frombuffer(r["value"], dtype=np.float32).copy() if r["value"] is not None else None
        return Episode(
            id=r["id"], tenant_id=r["tenant_id"], text=r["text"], value=value,
            weight=r["weight"], ts=r["ts"], tags=r["tags"] or "",
            content_hash=r["content_hash"], status=r["status"], proposed_by=r["proposed_by"] or "",
            approved_by=r["approved_by"], approved_ts=r["approved_ts"], version=r["version"],
            trust=r["trust"], superseded_by=r["superseded_by"],
            last_active_ts=r["last_active_ts"], polarity=r["polarity"],
            kind=r["kind"], anchor=r["anchor"], provenance=r["provenance"],
            meta=r["meta"])

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
    def set_trust(self, episode_id: int, new_trust: str, *, now: int,
                  approver: Optional[str] = None, approved_ts: int = 0) -> bool:
        """Transactional trust transition on a MATERIALIZED row. Promotion into a
        servable state stamps ``last_active_ts=now`` (a fresh promotion is never
        instantly dead); demotion leaves the clock untouched. ``approver`` is set only
        when a human vouch establishes an already-materialized row (BUG-001): it also
        records ``approved_by``/``approved_ts``. Mechanical promotion passes none and
        leaves the approver untouched. Index sync after commit is best-effort [B3] —
        add on entering a servable trust state, remove on leaving (boot rebuild
        recovers divergence). False on unknown/unflipped rows; raises on an unknown
        trust label (caller bug, not data)."""
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
                if approver is not None:
                    self.conn.execute(
                        "UPDATE episodes SET trust=?, last_active_ts=?, approved_by=?, "
                        "approved_ts=? WHERE id=?",
                        (new_trust, now, approver, approved_ts, episode_id))
                else:
                    self.conn.execute(
                        "UPDATE episodes SET trust=?, last_active_ts=? WHERE id=?",
                        (new_trust, now, episode_id))
            else:
                self.conn.execute(
                    "UPDATE episodes SET trust=? WHERE id=?", (new_trust, episode_id))
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
                (target_id, EK_SUPERSEDE, actor, ts,
                 json.dumps({"replacement_id": replacement_id})))
        if self.index is not None:
            try:
                self.index.remove(target_id)
            except Exception:                      # noqa: BLE001 — warm cache only [B3]
                _log.warning("store.supersede_deindex_failed episode_id=%s", target_id)
        _log.info("store.superseded target=%s replacement=%s actor=%s",
                  target_id, replacement_id, actor)
        return True

    def deprecate(self, episode_id: int, *, actor: str, ts: int) -> bool:
        """The SECOND retirement owner (beside ``supersede``): a BARE deprecation — no
        replacement — reserved for an INCORRECT / MALICIOUS / MISLEADING memory (behind
        ``hive_prune``). Flips trust→deprecated + writes ONE ``prune`` audit, leaving
        ``superseded_by`` NULL (the distinction from supersede: no successor), in ONE tx;
        de-indexes after commit. NOT a hard delete — the row and its ``evidence_events`` stay in
        the append-only ledger (an orphaned-evidence delete would break the honesty model).
        Guards: unknown id or a non-materialized (status!='approved') row → False; idempotent —
        an already-deprecated row is a no-op (no duplicate audit) returning False (THEORY §6:
        the bool reports whether anything actually changed). True iff this call retired a live
        row. Deleting the trust flip is the prune mutation (the flip test reds)."""
        with tx(self.conn):
            r = self.conn.execute(
                "SELECT status, trust FROM episodes WHERE id=?", (episode_id,)).fetchone()
            if r is None or r["status"] != "approved":
                return False
            old_trust = r["trust"]
            if old_trust == DEPRECATED:
                return False                       # idempotent: already retired, no duplicate audit
            self.conn.execute(
                "UPDATE episodes SET trust=? WHERE id=?", (DEPRECATED, episode_id))
            self.conn.execute(
                "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                "VALUES(?,?,?,?,?)",
                (episode_id, EK_PRUNE, actor, ts, json.dumps({"from": old_trust})))
        if self.index is not None and old_trust in (ESTABLISHED, PROVISIONAL):
            try:                                   # quarantined rows were never indexed
                self.index.remove(episode_id)
            except Exception:                      # noqa: BLE001 — warm cache only [B3]
                _log.warning("store.deprecate_deindex_failed episode_id=%s", episode_id)
        _log.info("store.deprecated episode_id=%s actor=%s from=%s",
                  episode_id, actor, old_trust)
        return True


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
                    (eid, EK_TTL_EXPIRED, "server", now,
                     json.dumps({"from": old_trust})))
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

    def insert_audit(self, episode_id: int, kind: str, actor: str, ts: int,
                     payload: str) -> int:
        """One server-written audit row (evidence_events is server-written ONLY —
        no tool writes here). Returns the row id."""
        with tx(self.conn):
            cur = self.conn.execute(
                "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                "VALUES(?,?,?,?,?)", (episode_id, kind, actor, ts, payload))
            return int(cur.lastrowid)

    def promotion_provenance(
            self, episode_ids: Sequence[int]
    ) -> dict[int, tuple[float, float, int]]:
        """PromotionProvenanceReader: the newest ``promote`` audit's stamped
        ``demand_independence`` per episode → ``{eid: (rho_bar, n_eff, k)}``. Read-only, no DDL.
        DEFENSIVE: a malformed/None payload, or a payload lacking ``demand_independence`` (a
        pre-stamp promotion row), or a non-coercible field is SKIPPED — never raises, never
        fabricates a stamp. An id absent from any ``promote`` audit is simply omitted. // O(rows)."""
        ids = [int(e) for e in episode_ids]
        if not ids:
            return {}
        out: dict[int, tuple[float, float, int]] = {}
        placeholders = ",".join("?" for _ in ids)
        # newest-first per episode: ORDER BY ts DESC, id DESC, take the first stamp we can parse.
        for r in self.conn.execute(
                f"SELECT episode_id, payload FROM evidence_events "
                f"WHERE kind=? AND episode_id IN ({placeholders}) "
                f"ORDER BY episode_id, ts DESC, id DESC", [EK_PROMOTE, *ids]):
            eid = int(r["episode_id"])
            if eid in out:
                continue                                  # already have the newest for this id
            try:
                di = json.loads(r["payload"]).get("demand_independence")
                if not isinstance(di, dict):
                    continue                              # pre-stamp row ⇒ omit (under-claim)
                out[eid] = (float(di["rho_bar"]), float(di["n_eff"]), int(di["k"]))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue                                  # malformed ⇒ skip, never raise
        return out

    def settled_wins(self, episode_ids: Sequence[int]) -> set[int]:
        """SettledWinReader: the subset of ``episode_ids`` carrying >= 1 settled-win
        audit — ``outcome_helped`` (self-reported via ``hive_outcome``) OR
        ``outcome_verified_helped`` (SHA-bound census corroboration): the UNION form,
        so the martingale clause honors a verified win with zero consumer change.
        Read-only, no DDL. Powers the suspect-consensus martingale clause (thin AND no
        settled win = the sharper 'popular-but-uncorroborated' signal). An id with no
        win audit is simply ABSENT from the set (under-claim). Empty input → empty
        set. // O(rows)."""
        ids = [int(e) for e in episode_ids]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        return {int(r["episode_id"]) for r in self.conn.execute(
            f"SELECT DISTINCT episode_id FROM evidence_events "
            f"WHERE kind IN (?,?) AND episode_id IN ({placeholders})",
            [EK_OUTCOME_HELPED, EK_OUTCOME_VERIFIED_HELPED, *ids])}

    def verified_wins(self, episode_ids: Sequence[int]) -> set[int]:
        """The verified-ONLY settled-win read (no port — consumed by the composition
        root as the verified-promotion rung's reader): the subset of ``episode_ids``
        carrying >= 1 ``outcome_verified_helped`` audit. Self-reported helps never
        count here — the rung keys on the non-forgeable SHA-bound artifact alone.
        Empty input → empty set. // O(rows)."""
        ids = [int(e) for e in episode_ids]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        return {int(r["episode_id"]) for r in self.conn.execute(
            f"SELECT DISTINCT episode_id FROM evidence_events "
            f"WHERE kind=? AND episode_id IN ({placeholders})",
            [EK_OUTCOME_VERIFIED_HELPED, *ids])}

    def last_verification(self, episode_ids: Sequence[int]
                          ) -> dict[int, tuple[int, str, str]]:
        """LastVerificationReader: the newest ``verify_current``/``verify_stale``
        ledger row per requested id → ``{eid: (ts, head_sha, state)}``, state derived
        from the kind. Read-only, no DDL. DEFENSIVE payload parse (the
        ``promotion_provenance`` idiom): a malformed payload or a missing
        ``stamp.head_sha`` SKIPS that row — an older parseable row may still answer;
        an id with nothing parseable is simply ABSENT (under-claim, never a raise).
        // O(rows)."""
        ids = [int(e) for e in episode_ids]
        if not ids:
            return {}
        out: dict[int, tuple[int, str, str]] = {}
        placeholders = ",".join("?" for _ in ids)
        # newest-first per episode: ORDER BY ts DESC, id DESC, first parseable wins.
        for r in self.conn.execute(
                f"SELECT episode_id, kind, ts, payload FROM evidence_events "
                f"WHERE kind IN (?,?) AND episode_id IN ({placeholders}) "
                f"ORDER BY episode_id, ts DESC, id DESC",
                [EK_VERIFY_CURRENT, EK_VERIFY_STALE, *ids]):
            eid = int(r["episode_id"])
            if eid in out:
                continue                              # already have the newest for this id
            try:
                stamp = json.loads(r["payload"]).get("stamp")
                head_sha = stamp.get("head_sha") if isinstance(stamp, dict) else None
                if not isinstance(head_sha, str) or not head_sha:
                    continue                          # stamp-less row ⇒ skip (under-claim)
                state = "current" if r["kind"] == EK_VERIFY_CURRENT else "stale"
                out[eid] = (int(r["ts"]), head_sha, state)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue                              # malformed ⇒ skip, never raise
        return out

    def stale_suspect_rows(self) -> list[tuple[int, int, str]]:
        """The newest ``stale_suspect`` ledger row per episode →
        ``[(episode_id, ts, payload)]`` (no port — consumed by the hive_health
        ``include_stale_suspects`` report, which owns the servable filter and the
        defensive payload parse; the payload rides OPAQUE here). Newest per id:
        ``ORDER BY ts DESC, id DESC``, first row per id wins. Read-only, no DDL.
        // O(rows)."""
        out: list[tuple[int, int, str]] = []
        seen: set[int] = set()
        for r in self.conn.execute(
                "SELECT episode_id, ts, payload FROM evidence_events WHERE kind=? "
                "ORDER BY episode_id, ts DESC, id DESC", [EK_STALE_SUSPECT]):
            eid = int(r["episode_id"])
            if eid in seen:
                continue                              # already have the newest for this id
            seen.add(eid)
            out.append((eid, int(r["ts"]), r["payload"]))
        return out

    def anchored_episodes(self) -> list[tuple[int, str, str]]:
        """AnchoredEpisodeReader: ``(id, anchor, polarity)`` for approved rows with a
        non-empty anchor — the change→episode join's candidate set. ALL trust states
        included (mirrors hive_outcome's known-id rule: evidence on a deprecated row is
        honest ledger history); polarity rides for the verified-outcome classification
        only. Read-only, no DDL. // O(rows)."""
        return [(int(r["id"]), r["anchor"], r["polarity"]) for r in self.conn.execute(
            "SELECT id, anchor, polarity FROM episodes "
            "WHERE status='approved' AND anchor != '' ORDER BY id")]

    def append_evidence(self, rows: Sequence[tuple[int, str, str, int, str]]
                        ) -> tuple[list[int], int]:
        """ChangeEvidenceAppender: batch-append ``(episode_id, kind, actor, ts, payload)``
        rows in ONE ``tx()`` — an atomic receipt (a fault on ANY row rolls back ALL;
        ``tx()`` is non-reentrant, so looping ``insert_audit`` could never be made atomic
        from outside — the batch method is required, not stylistic). A row whose exact
        ``(episode_id, kind, payload)`` already exists is SKIPPED — idempotency keyed on
        content, never on ts/actor, so a re-ingest of the same receipt cannot duplicate
        the ledger. Returns (inserted row ids, skipped count). Append-only: this module
        still contains no UPDATE/DELETE against evidence_events. // O(batch)."""
        inserted: list[int] = []
        skipped = 0
        with tx(self.conn):
            for episode_id, kind, actor, ts, payload in rows:
                dup = self.conn.execute(
                    "SELECT 1 FROM evidence_events "
                    "WHERE episode_id=? AND kind=? AND payload=? LIMIT 1",
                    (episode_id, kind, payload)).fetchone()
                if dup is not None:
                    skipped += 1
                    continue
                cur = self.conn.execute(
                    "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                    "VALUES(?,?,?,?,?)", (episode_id, kind, actor, ts, payload))
                inserted.append(int(cur.lastrowid))
        if inserted or skipped:
            _log.info("store.append_evidence inserted=%d skipped=%d",
                      len(inserted), skipped)
        return inserted, skipped

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

    # ── advisory conflict flags (ConflictFlagStore port + the worklist read) ──
    def record_conflict_flag(self, *, kind: str, a_id: int, b_id: int,
                             winner_id: Optional[int], resolution: str,
                             proposed_by: str, ts: int) -> bool:
        """Record ONE advisory conflict flag, idempotent on the canonical (a_id, b_id, kind).
        Returns True iff a NEW row was written; a re-flag of the same pair+kind is a no-op
        (the first write stands — no overwrite). Existence is checked explicitly (not
        INSERT OR IGNORE) so a CHECK violation (bad kind/status) still RAISES rather than being
        silently swallowed. The caller (ConflictFlagService) owns canonicalization + the
        resolution secret scan. NEVER touches an episode's trust — this is advisory only."""
        with tx(self.conn):
            if self.conn.execute(
                    "SELECT 1 FROM conflict_flags WHERE a_id=? AND b_id=? AND kind=?",
                    (int(a_id), int(b_id), kind)).fetchone() is not None:
                return False
            self.conn.execute(
                "INSERT INTO conflict_flags(kind, a_id, b_id, winner_id, resolution, "
                "proposed_by, ts) VALUES(?,?,?,?,?,?,?)",
                (kind, int(a_id), int(b_id),
                 None if winner_id is None else int(winner_id),
                 resolution, proposed_by, int(ts)))
        return True

    def open_conflict_flags(self) -> list[dict]:
        """All status='open' advisory flags (the health worklist's advisory channel),
        ordered by id. Duck-typed app read (the misses_detail_window convention), NOT a
        domain port. A flag auto-clears from the worklist when either episode goes
        non-servable — that filter lives in the app report, so status keeps only the
        operator-driven 'dismissed'."""
        return [{"id": r["id"], "kind": r["kind"], "a_id": r["a_id"], "b_id": r["b_id"],
                 "winner_id": r["winner_id"], "resolution": r["resolution"],
                 "proposed_by": r["proposed_by"], "ts": int(r["ts"])}
                for r in self.conn.execute(
                    "SELECT * FROM conflict_flags WHERE status='open' ORDER BY id")]

    # ── meta watermark kv (write seam; reads go through raw SQL at healthcheck) ──
    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
