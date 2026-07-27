"""SqliteEpisodeStore — the durable episode store + meta watermark in ONE WAL DB.
``sqlite_db.tx()`` is the single BEGIN IMMEDIATE lane, used directly by each mutating method.

Schema v3 (repo-partitioned memory): ``episode_anchors`` is the ONE owner of repo scope +
code bindings; ``episode_refs`` carries the memory's own DECLARED line, one ref per
(episode, repo); ``repos`` is the durable operational repo registry (the authctl-seat
pattern); ``anchor_baselines`` records the COMMIT each binding was observed from (plus
whether its symbol resolved there — ``symbol_ok``, measured once per immutable tip) and
``anchor_drift`` is the materialized drift-verdict cache keyed by every input to the
verdict, ``(repo, tip_sha, base_tip, anchor)``; ``ref_requests`` records recall-touched
refs wanting on-demand materialization, and ``ref_tips`` is the materializer's
resolved-tip watermark per non-canonical ref (the branch twin of the canonical
``sync:<repo>:last_tip`` meta key). Both anchor tables are rebuildable caches (Law 5) —
dropping either re-baselines and re-materializes within one tick. There is NO approver
concept — ``complete`` lands an explicit trust label and nothing on this surface accepts
an approver identity or vouch timestamp.

``episode_anchors.fp_meta`` is RETIRED: nothing writes it and nothing reads it. The
column stays because dropping it is a table rebuild — i.e. a migration — and this build
performs none.

The ``exposure`` table is the recall side-channel (``record_exposure`` / ``record_miss``):
WHO was served WHAT and which queries got no answer — the demand signal that drives
promotion. Episode-CRUD / admission is below.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

import numpy as np

from hive.adapters.sqlite_db import tx
from hive.domain.evidence_kinds import (
    EK_OUTCOME_HELPED,
    EK_OUTCOME_VERIFIED_HELPED,
    EK_PROMOTE,
    EK_PRUNE,
    EK_STALE_SUSPECT,
    EK_SUPERSEDE,
    EK_TTL_EXPIRED,
)
from hive.domain.kinds import DEFAULT_KIND, KIND_NAMES
from hive.domain.lifecycle import (
    DEPRECATED,
    ESTABLISHED,
    PROVISIONAL,
    QUARANTINED,
    TRUST_STATES,
    MissRow,
    decayed,
    is_servable,
)
from hive.domain.meta import meta_version_histogram
from hive.domain.models import AnchorRef, Episode, content_hash

_log = logging.getLogger("hive.store")

# The SQL PREFILTER under the single servability rule: only materialized rows can
# serve. The full predicate is hive.domain.lifecycle.is_servable — scan_servable()
# applies it per row, and it is the ONLY candidate source for recall (scan_approved()
# is its no-clock fail-closed alias).
_RECALL_PREDICATE = "status='approved' AND value IS NOT NULL"

# The NOT-RETIRED predicate every materializer work-list verb joins on, spelled ONCE
# (BUG-065: a retired memory must stop consuming the materializer's work forever).
# Interpolated by anchor_work_list / declared_refs — the aliased episodes row is `e` in
# both, so the fix stays in one place by construction rather than by near-identical
# joins agreeing. The two carrier-era sweeps that used to interpolate it collapsed into
# the single anchor_work_list query, so they can no longer fork at all.
_LIVE_EPISODE_PREDICATE = "e.status='approved' AND e.trust != 'deprecated'"

# "no clock" sentinel for the fail-closed scan_approved() alias: with now pushed to
# the far future, a provisional row can never prove freshness ⇒ established-only.
_FAR_FUTURE = 1 << 62

# The kind column DDL is built from the registry (sorted for a stable CHECK) so the stored
# vocabulary cannot drift from hive.domain.kinds — the single source every surface projects.
# Injected by sentinel below because _SCHEMA stays a plain string (it carries a literal
# JSON '{}' default that an f-string would misread).
_KIND_COLUMN_DDL = "kind TEXT NOT NULL DEFAULT '%s' CHECK(kind IN (%s))" % (
    DEFAULT_KIND,
    ", ".join(f"'{k}'" for k in sorted(KIND_NAMES)),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs(
  content_hash TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,   -- constant label, never a query filter (single-tenant)
  text TEXT NOT NULL, value BLOB,
  weight REAL NOT NULL, ts INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved')),
  proposed_by TEXT,
  version INTEGER NOT NULL DEFAULT 0,
  trust TEXT NOT NULL DEFAULT 'quarantined',
  superseded_by INTEGER,
  last_active_ts INTEGER NOT NULL DEFAULT 0,
  polarity TEXT NOT NULL DEFAULT 'neutral' CHECK(polarity IN ('do','dont','neutral')),
  __KIND_COLUMN__,
  meta TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_episodes_hash ON episodes(content_hash);
CREATE INDEX IF NOT EXISTS idx_episodes_trust ON episodes(trust);
CREATE TABLE IF NOT EXISTS episode_anchors(
  episode_id INTEGER NOT NULL, repo TEXT NOT NULL,
  anchor TEXT NOT NULL DEFAULT '',
  fp_meta TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(episode_id, repo, anchor));
CREATE TABLE IF NOT EXISTS episode_refs(
  episode_id INTEGER NOT NULL, repo TEXT NOT NULL, ref TEXT NOT NULL,
  PRIMARY KEY(episode_id, repo));
CREATE TABLE IF NOT EXISTS repos(
  name TEXT PRIMARY KEY,
  url TEXT NOT NULL, canonical_ref TEXT NOT NULL DEFAULT '',
  token_env TEXT NOT NULL DEFAULT '',
  added_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS anchor_baselines(
  episode_id INTEGER NOT NULL, repo TEXT NOT NULL, anchor TEXT NOT NULL,
  base_tip TEXT NOT NULL, ts INTEGER NOT NULL,
  -- did this binding's SYMBOL resolve at base_tip: -1 unprobed, 0 no, 1 yes. A fact
  -- about an immutable commit, so it is measured once and never invalidated; a
  -- file-scoped binding asks the question of nobody and stays -1 forever.
  symbol_ok INTEGER NOT NULL DEFAULT -1,
  PRIMARY KEY(episode_id, repo, anchor));
CREATE TABLE IF NOT EXISTS anchor_drift(
  repo TEXT NOT NULL, tip_sha TEXT NOT NULL, base_tip TEXT NOT NULL,
  anchor TEXT NOT NULL,
  verdict TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', ts INTEGER NOT NULL,
  PRIMARY KEY(repo, tip_sha, base_tip, anchor));
CREATE TABLE IF NOT EXISTS ref_requests(
  repo TEXT NOT NULL, ref TEXT NOT NULL, last_requested_ts INTEGER NOT NULL,
  PRIMARY KEY(repo, ref));
CREATE TABLE IF NOT EXISTS ref_tips(
  repo TEXT NOT NULL, ref TEXT NOT NULL, tip_sha TEXT NOT NULL, ts INTEGER NOT NULL,
  PRIMARY KEY(repo, ref));
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
  ts INTEGER NOT NULL,
  repos TEXT NOT NULL DEFAULT '');
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
CREATE TABLE IF NOT EXISTS ingested_ranges(
  repo TEXT NOT NULL,        -- "" = the legacy (pre-repo-key) receipt identity
  base_sha TEXT NOT NULL, head_sha TEXT NOT NULL,
  phase TEXT NOT NULL, ts INTEGER NOT NULL,
  PRIMARY KEY(repo, base_sha, head_sha, phase));
""".replace("__KIND_COLUMN__", _KIND_COLUMN_DDL)

# Legacy episodes columns whose PRESENCE marks a pre-v3 store; and the v3 columns whose
# ABSENCE marks an even older one. Either way the answer is the same: no in-place
# migration exists — the operator starts from a clean store/volume.
_LEGACY_EPISODE_COLUMNS = ("anchor", "approved_by", "approved_ts", "provenance", "tags")
_V3_EPISODE_COLUMNS = ("trust", "polarity", "kind", "meta")
_NO_MIGRATION_HINT = "this build has no migration — start from a clean store/volume"

# Registry names ride filesystem paths (mirror dirs) and scope labels — slug only.
# The lookahead refuses '.', '..', and every other all-dot name: those are path-special
# (a mirror path joined from one escapes, or IS, the mirrors dir), so a name this
# matches is genuinely path-safe — the charset alone would admit them.
_REPO_NAME_RE = re.compile(r"^(?!\.+$)[a-z0-9._-]+$")

# The columns ``anchor_drift`` must carry for its verdicts to be readable. A verdict is
# a total function of (repo, tip, base_tip, anchor); a store written before ``base_tip``
# joined that key holds rows whose key cannot be reconstructed, so the boot path drops
# and recreates the table rather than reading them. That is legal without a migration
# gate precisely because this is a DERIVED cache (Law 5): the loss degrades every read
# to ``unverifiable`` — the honest unknown — until the next tick re-materializes it. No
# episode row, no ``episode_anchors`` row and no memory text is read or rewritten.
_DRIFT_CACHE_COLUMNS = frozenset(
    {"repo", "tip_sha", "base_tip", "anchor", "verdict", "detail", "ts"}
)


@dataclass(frozen=True, slots=True)
class RepoRow:
    """One durable repo-registry row (operational data, the authctl-seat pattern).
    ``token_env`` is the NAME of the env var holding the git token ('' ⇒ the
    ``HIVE_SYNC__TOKEN`` default) — NEVER a secret byte."""

    name: str
    url: str
    canonical_ref: str
    token_env: str
    added_ts: int


class _WarmIndex(Protocol):
    """The warm-cache surface this store drives (typing-only): the
    MutableVectorIndex write side plus the upsert alias ``sync_approved``
    (= ``add``) the exhaustive index exposes."""

    def sync_approved(self, episode_id: int, value: "np.ndarray") -> None: ...
    def remove(self, episode_id: int) -> None: ...
    def rebuild_from_store(self, rows: Iterable[tuple[int, "np.ndarray"]]) -> None: ...


def _rowid(cur: sqlite3.Cursor) -> int:
    """``lastrowid`` immediately after a successful INSERT is always an int; the
    Optional in the sqlite3 stubs covers statements that produce no rowid."""
    rid = cur.lastrowid
    if rid is None:  # pragma: no cover — unreachable right after an INSERT
        raise RuntimeError("INSERT produced no rowid")
    return int(rid)


class SqliteEpisodeStore:
    def __init__(
        self, conn: sqlite3.Connection, index: "Optional[_WarmIndex]" = None
    ) -> None:
        self.conn = conn
        self.index = index  # MutableVectorIndex (warm cache); None in ledger-only tests
        # No in-place migration path exists — schema v3 starts from a clean, empty
        # store (prior-format memories are not carried over). CREATE IF NOT EXISTS
        # would leave an old-format episodes table untouched and limp to a cryptic
        # mid-query crash, so refuse BEFORE the script runs, with a clear message.
        # An absent episodes table is fine — the script creates the v3 shape.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(episodes)")}
        if cols:
            legacy = sorted(c for c in _LEGACY_EPISODE_COLUMNS if c in cols)
            if legacy:
                _log.error("store.schema_pre_v3 legacy_columns=%s", ",".join(legacy))
                raise RuntimeError(
                    f"episodes table carries the retired column(s) "
                    f"{', '.join(legacy)}; {_NO_MIGRATION_HINT}"
                )
            missing = next((c for c in _V3_EPISODE_COLUMNS if c not in cols), None)
            if missing:
                _log.error("store.schema_predates_v3 missing_column=%s", missing)
                raise RuntimeError(
                    f"episodes table predates schema v3 (no {missing} column); "
                    f"{_NO_MIGRATION_HINT}"
                )
            if (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='episode_anchors'"
                ).fetchone()
                is None
            ):
                _log.error("store.schema_missing_episode_anchors")
                raise RuntimeError(
                    "store carries an episodes table but no episode_anchors table; "
                    f"{_NO_MIGRATION_HINT}"
                )
        self._recreate_stale_drift_cache(conn)
        conn.executescript(_SCHEMA)

    @staticmethod
    def _recreate_stale_drift_cache(conn: sqlite3.Connection) -> None:
        """Drop an ``anchor_drift`` table whose key predates ``base_tip``.

        Unconditional and idempotent: a table already carrying the current columns is
        left alone, and an absent one is a no-op (the schema script creates it). This
        is the one destructive DDL in the store, and it is confined to a rebuildable
        cache — sqlite cannot alter a PRIMARY KEY in place, and keeping rows whose key
        omits an input to the verdict is exactly the defect the new key removes."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(anchor_drift)")}
        if cols and cols != _DRIFT_CACHE_COLUMNS:
            _log.info(
                "store.drift_cache_recreated old_columns=%s", ",".join(sorted(cols))
            )
            conn.execute("DROP TABLE anchor_drift")

    # ── episodes / blob group (admission CAS state machine) ───────────────────
    def stage(
        self,
        *,
        text: str,
        weight: float,
        proposed_by: str,
        tenant_id: str = "default",
        ts: int = 0,
        polarity: str = "neutral",
        kind: str = DEFAULT_KIND,
        meta: str = "",
        anchors: Sequence[tuple[str, str]] = (),
        repos: Sequence[tuple[str, str]] = (),
    ) -> tuple[int, bool]:
        """Insert a PENDING row (value NULL — not recallable, not indexed) + its blob
        + its ``episode_anchors``/``episode_refs`` rows, all in the SAME tx. Dedup by
        content_hash: a repeat of identical text returns the existing id, deduped=True,
        no second row AND no touch of the existing row's labels, anchor rows, or
        declared refs — identity is the text hash alone, so a re-write can never
        restate, or change, the line a memory already declared. ``polarity``
        (do|dont|neutral), ``kind`` (registry vocabulary) and ``meta`` (the serialized
        opaque map — meta.py owns the grammar) are the carried-not-interpreted consumer
        labels, never embedded; they default fail-safe (neutral / note / empty).
        ``anchors`` is the ``(repo, anchor)`` code-binding pairs (fingerprints are
        server-minted later, never staged); ``repos`` is the ``(repo, ref)`` declared-
        scope pairs — a repo already covered by an anchor pair gains no extra
        ``episode_anchors`` row (one canonical encoding: scope-only = an ``anchor=''``
        row, general = zero rows), and a repo whose ``ref`` is non-empty ALSO gets one
        ``episode_refs`` row — the memory's DECLARED line for that repo (first
        non-empty ref per repo name wins; ``ref=''`` is never stored — canonical = no
        declared line)."""
        h = content_hash(text)
        with tx(self.conn):
            existing = self.conn.execute(
                "SELECT id FROM episodes WHERE content_hash=? LIMIT 1", (h,)
            ).fetchone()
            if existing is not None:
                return int(existing["id"]), True
            self.conn.execute(
                "INSERT OR IGNORE INTO blobs(content_hash, text) VALUES(?,?)", (h, text)
            )
            cur = self.conn.execute(
                "INSERT INTO episodes(tenant_id, text, value, weight, ts, "
                "content_hash, status, proposed_by, version, polarity, kind, meta) "
                "VALUES(?,?,NULL,?,?,?,'pending',?,0,?,?,?)",
                (tenant_id, text, weight, ts, h, proposed_by, polarity, kind, meta),
            )
            eid = _rowid(cur)
            pairs: list[tuple[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for repo, anchor in anchors:
                key = (str(repo), str(anchor))
                if key not in seen:
                    seen.add(key)
                    pairs.append(key)
            for repo, anchor in pairs:
                self.conn.execute(
                    "INSERT INTO episode_anchors(episode_id, repo, anchor) "
                    "VALUES(?,?,?)",
                    (eid, repo, anchor),
                )
            covered = {repo for repo, _anchor in pairs}
            repo_names: list[str] = []
            seen_repo_names: set[str] = set()
            declared_ref_by_repo: dict[str, str] = {}
            for repo, ref in repos:
                repo = str(repo)
                if repo not in seen_repo_names:
                    seen_repo_names.add(repo)
                    repo_names.append(repo)
                ref = str(ref)
                if ref and repo not in declared_ref_by_repo:
                    declared_ref_by_repo[repo] = ref
            for repo in repo_names:
                if repo in covered:
                    continue
                self.conn.execute(
                    "INSERT INTO episode_anchors(episode_id, repo, anchor) "
                    "VALUES(?,?,'')",
                    (eid, repo),
                )
            for repo, ref in declared_ref_by_repo.items():
                self.conn.execute(
                    "INSERT INTO episode_refs(episode_id, repo, ref) VALUES(?,?,?)",
                    (eid, repo, ref),
                )
            return eid, False

    def complete(
        self,
        episode_id: int,
        value: "np.ndarray",
        *,
        expected_version: int,
        trust: str,
        last_active_ts: int = 0,
    ) -> bool:
        """CAS-flip pending→approved (materialized: value written) with an explicit
        trust state, THEN best-effort index sync after commit [B3] — the index add
        happens IFF the row lands in a servable trust state (established/provisional;
        freshness at the stamp instant is definitional). A quarantined complete is
        embedded but absent from the index. Idempotent (already-approved ⇒ no-op True);
        a stale expected_version ⇒ no flip (lost-update / double-admission blocked).
        There is no approver anywhere — trust is the ONLY standing label (v3).
        Durable truth is the row; boot rebuild recovers warm-cache divergence."""
        if trust not in TRUST_STATES:
            raise ValueError(f"bad trust {trust!r}")
        vbytes = np.ascontiguousarray(np.asarray(value, dtype=np.float32)).tobytes()
        with tx(self.conn):
            row = self.conn.execute(
                "SELECT status FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            if row is not None and row["status"] == "approved":
                return True  # idempotent
            n = self.conn.execute(
                "UPDATE episodes SET status='approved', "
                "value=?, trust=?, last_active_ts=?, version=version+1 "
                "WHERE id=? AND version=? AND status='pending'",
                (vbytes, trust, last_active_ts, episode_id, expected_version),
            ).rowcount
        if n == 1 and trust in (ESTABLISHED, PROVISIONAL) and self.index is not None:
            try:
                self.index.sync_approved(
                    episode_id, np.asarray(value, dtype=np.float32)
                )
            except Exception:
                # best-effort warm cache [B3]: durable truth is the row;
                # rebuild_index_from_store() on boot recovers the divergence.
                pass
        return n == 1

    def reject(self, episode_id: int, *, keep: bool = False) -> None:
        """Drop a pending row (default) AND its anchor + declared-ref rows in the same
        tx — a staged binding must never outlive its episode. keep_rejected retains
        the row as non-recallable (never approved ⇒ never indexed)."""
        if keep:
            return
        with tx(self.conn):
            cur = self.conn.execute(
                "DELETE FROM episodes WHERE id=? AND status='pending'", (episode_id,)
            )
            if cur.rowcount:
                self.conn.execute(
                    "DELETE FROM episode_anchors WHERE episode_id=?", (episode_id,)
                )
                self.conn.execute(
                    "DELETE FROM episode_refs WHERE episode_id=?", (episode_id,)
                )

    def scan_servable(
        self, *, now: int, provisional_ttl_s: int
    ) -> list[tuple[int, "np.ndarray"]]:
        """The ONLY candidate source for recall. SQL prefilters to materialized rows
        (_RECALL_PREDICATE); the full decision per row is the ONE pure predicate
        ``lifecycle.is_servable`` — established always, provisional iff fresh,
        quarantined/deprecated never.  // O(N) time."""
        out: list[tuple[int, np.ndarray]] = []
        for r in self.conn.execute(
            f"SELECT id, value, status, trust, last_active_ts "
            f"FROM episodes WHERE {_RECALL_PREDICATE}"
        ):
            if is_servable(
                status=r["status"],
                trust=r["trust"],
                last_active_ts=r["last_active_ts"],
                now=now,
                provisional_ttl_s=provisional_ttl_s,
            ):
                out.append(
                    (r["id"], np.frombuffer(r["value"], dtype=np.float32).copy())
                )
        return out

    def scan_servable_labeled(
        self,
        *,
        now: int,
        provisional_ttl_s: int,
    ) -> list[tuple[int, "np.ndarray", str, str, int, str]]:
        """The conflict scan's candidate source: every SERVABLE row with the labels the
        detector classifies by — ``(id, value, polarity, anchor, ts, trust)``. Same SQL
        prefilter + ONE ``lifecycle.is_servable`` decision as ``scan_servable`` (so a
        deprecated / quarantined / stale-provisional row is NEVER surfaced for a
        conflict — a superseded row already retired can't reappear). ``anchor`` is the
        v3 representative binding — the FIRST ``episode_anchors`` row by (repo, anchor)
        order, '' for a general/scope-only row (episodes no longer carry a single
        anchor column; the label survives for single-anchor rows, the dominant case).
        // O(N) time."""
        out: list[tuple[int, np.ndarray, str, str, int, str]] = []
        for r in self.conn.execute(
            f"SELECT id, value, status, trust, last_active_ts, polarity, ts, "
            f"(SELECT ea.anchor FROM episode_anchors ea "
            f" WHERE ea.episode_id = episodes.id AND ea.anchor != '' "
            f" ORDER BY ea.repo, ea.anchor LIMIT 1) AS anchor "
            f"FROM episodes WHERE {_RECALL_PREDICATE}"
        ):
            if is_servable(
                status=r["status"],
                trust=r["trust"],
                last_active_ts=r["last_active_ts"],
                now=now,
                provisional_ttl_s=provisional_ttl_s,
            ):
                out.append(
                    (
                        r["id"],
                        np.frombuffer(r["value"], dtype=np.float32).copy(),
                        r["polarity"],
                        r["anchor"] or "",
                        int(r["ts"]),
                        r["trust"],
                    )
                )
        return out

    def scan_approved(self) -> list[tuple[int, "np.ndarray"]]:
        """Compat alias — the no-clock FAIL-CLOSED reading of ``scan_servable``: with
        ``now`` pushed to the far future a provisional row can never prove freshness,
        so this returns established-only (never over-serves)."""
        return self.scan_servable(now=_FAR_FUTURE, provisional_ttl_s=0)

    def rebuild_index_from_store(
        self, *, now: Optional[int] = None, provisional_ttl_s: Optional[int] = None
    ) -> None:
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
                self.scan_servable(now=now, provisional_ttl_s=provisional_ttl_s)
            )

    def get_episode(self, episode_id: int) -> Optional[Episode]:
        r = self.conn.execute(
            "SELECT * FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if r is None:
            return None
        value = (
            np.frombuffer(r["value"], dtype=np.float32).copy()
            if r["value"] is not None
            else None
        )
        arows = self.conn.execute(
            "SELECT repo, anchor, fp_meta FROM episode_anchors WHERE episode_id=? "
            "ORDER BY repo, anchor",
            (episode_id,),
        ).fetchall()
        # the carrier projection: repos = sorted unique union of every row's repo
        # (anchor + scope-only rows); anchors = the non-empty-anchor rows only.
        repos = tuple(sorted({a["repo"] for a in arows}))
        anchors = tuple(
            AnchorRef(repo=a["repo"], anchor=a["anchor"], fp_meta=a["fp_meta"])
            for a in arows
            if a["anchor"]
        )
        return Episode(
            id=r["id"],
            tenant_id=r["tenant_id"],
            text=r["text"],
            value=value,
            weight=r["weight"],
            ts=r["ts"],
            content_hash=r["content_hash"],
            status=r["status"],
            proposed_by=r["proposed_by"] or "",
            version=r["version"],
            trust=r["trust"],
            superseded_by=r["superseded_by"],
            last_active_ts=r["last_active_ts"],
            polarity=r["polarity"],
            kind=r["kind"],
            meta=r["meta"],
            repos=repos,
            anchors=anchors,
        )

    def episode_refs(self, episode_id: int) -> dict[str, str]:
        """The memory's DECLARED line per repo — every ``episode_refs`` row of
        ``episode_id`` as ``{repo: ref}``. A repo absent here declared no line (the
        canonical reading, not a degenerate ``ref=''`` row — one never exists).
        Read-only, no DDL. // O(k)."""
        return {
            r["repo"]: r["ref"]
            for r in self.conn.execute(
                "SELECT repo, ref FROM episode_refs WHERE episode_id=?",
                (int(episode_id),),
            )
        }

    def counts(self) -> tuple[int, int]:
        """(n_approved, n_pending) for hive_health — one grouped scan.  // O(N) time."""
        approved = pending = 0
        for r in self.conn.execute(
            "SELECT status, COUNT(*) AS c FROM episodes GROUP BY status"
        ):
            if r["status"] == "approved":
                approved = int(r["c"])
            elif r["status"] == "pending":
                pending = int(r["c"])
        return approved, pending

    # ── repo scope reads (RepoScopeReader + the promotion rung's id feed) ──────
    def servable_scopes(
        self,
        *,
        now: int,
        provisional_ttl_s: int,
    ) -> dict[int, tuple[frozenset[str], tuple[tuple[str, str], ...]]]:
        """RepoScopeReader: every SERVABLE episode id → ``(repo scope, anchor pairs)``
        — the partition map recall filtering and the partition-scoped competitor read
        consume. Servability is the SAME two-step decision as ``scan_servable`` (SQL
        prefilter + ``lifecycle.is_servable``), so this map and the index can never
        disagree about who is in the field. Every servable id gets an entry: a general
        memory reads ``(frozenset(), ())`` explicitly (absent-means-general lives at
        the consumer; here absence only ever means non-servable). Anchor pairs are
        ``(repo, anchor)`` in (repo, anchor) order.  // O(N + rows)."""
        out: dict[int, tuple[frozenset[str], tuple[tuple[str, str], ...]]] = {}
        for r in self.conn.execute(
            f"SELECT id, status, trust, last_active_ts "
            f"FROM episodes WHERE {_RECALL_PREDICATE}"
        ):
            if is_servable(
                status=r["status"],
                trust=r["trust"],
                last_active_ts=r["last_active_ts"],
                now=now,
                provisional_ttl_s=provisional_ttl_s,
            ):
                out[int(r["id"])] = (frozenset(), ())
        if not out:
            return out
        scopes: dict[int, set[str]] = {}
        pairs: dict[int, list[tuple[str, str]]] = {}
        for r in self.conn.execute(
            "SELECT episode_id, repo, anchor FROM episode_anchors "
            "ORDER BY episode_id, repo, anchor"
        ):
            eid = int(r["episode_id"])
            if eid not in out:
                continue
            scopes.setdefault(eid, set()).add(r["repo"])
            if r["anchor"]:
                pairs.setdefault(eid, []).append((r["repo"], r["anchor"]))
        for eid, repo_set in scopes.items():
            out[eid] = (frozenset(repo_set), tuple(pairs.get(eid, ())))
        return out

    def provisional_ids(self) -> list[int]:
        """The established rung's candidate feed (lifecycle.promote_established):
        every materialized PROVISIONAL row id, ascending. Read-only, no DDL."""
        return [
            int(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM episodes WHERE status='approved' AND trust=? ORDER BY id",
                (PROVISIONAL,),
            )
        ]

    # ── trust lifecycle (promotion / supersession / decay) ────────────────────
    def set_trust(self, episode_id: int, new_trust: str, *, now: int) -> bool:
        """Transactional trust transition on a MATERIALIZED row. Promotion into a
        servable state stamps ``last_active_ts=now`` (a fresh promotion is never
        instantly dead); demotion leaves the clock untouched. Every transition is
        mechanical — there is no vouch identity to record (v3). Index sync after
        commit is best-effort [B3] — add on entering a servable trust state, remove
        on leaving (boot rebuild recovers divergence). False on unknown/unflipped
        rows; raises on an unknown trust label (caller bug, not data)."""
        if new_trust not in TRUST_STATES:
            raise ValueError(f"bad trust {new_trust!r}")
        servable_states = (ESTABLISHED, PROVISIONAL)
        with tx(self.conn):
            r = self.conn.execute(
                "SELECT status, trust, value FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            if r is None or r["status"] != "approved":
                return False
            old_trust = r["trust"]
            if new_trust in servable_states:
                self.conn.execute(
                    "UPDATE episodes SET trust=?, last_active_ts=? WHERE id=?",
                    (new_trust, now, episode_id),
                )
            else:
                self.conn.execute(
                    "UPDATE episodes SET trust=? WHERE id=?", (new_trust, episode_id)
                )
        if self.index is not None:
            try:
                if new_trust in servable_states and r["value"] is not None:
                    self.index.sync_approved(
                        episode_id, np.frombuffer(r["value"], dtype=np.float32).copy()
                    )
                elif old_trust in servable_states and new_trust not in servable_states:
                    self.index.remove(episode_id)
            except Exception:  # noqa: BLE001 — warm cache only [B3]
                _log.warning(
                    "store.set_trust_index_sync_failed episode_id=%s", episode_id
                )
        return True

    def supersede(
        self, target_id: int, replacement_id: int, *, actor: str, ts: int
    ) -> bool:
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
                "SELECT superseded_by FROM episodes WHERE id=?", (target_id,)
            ).fetchone()
            if t is None:
                return False
            if (
                self.conn.execute(
                    "SELECT 1 FROM episodes WHERE id=?", (replacement_id,)
                ).fetchone()
                is None
            ):
                return False
            if t["superseded_by"] == replacement_id:
                return True  # idempotent re-run: no duplicate audit
            self.conn.execute(
                "UPDATE episodes SET trust=?, superseded_by=? WHERE id=?",
                (DEPRECATED, replacement_id, target_id),
            )
            self.conn.execute(
                "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                "VALUES(?,?,?,?,?)",
                (
                    target_id,
                    EK_SUPERSEDE,
                    actor,
                    ts,
                    json.dumps({"replacement_id": replacement_id}),
                ),
            )
        if self.index is not None:
            try:
                self.index.remove(target_id)
            except Exception:  # noqa: BLE001 — warm cache only [B3]
                _log.warning("store.supersede_deindex_failed episode_id=%s", target_id)
        _log.info(
            "store.superseded target=%s replacement=%s actor=%s",
            target_id,
            replacement_id,
            actor,
        )
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
                "SELECT status, trust FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            if r is None or r["status"] != "approved":
                return False
            old_trust = r["trust"]
            if old_trust == DEPRECATED:
                return False  # idempotent: already retired, no duplicate audit
            self.conn.execute(
                "UPDATE episodes SET trust=? WHERE id=?", (DEPRECATED, episode_id)
            )
            self.conn.execute(
                "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                "VALUES(?,?,?,?,?)",
                (episode_id, EK_PRUNE, actor, ts, json.dumps({"from": old_trust})),
            )
        if self.index is not None and old_trust in (ESTABLISHED, PROVISIONAL):
            try:  # quarantined rows were never indexed
                self.index.remove(episode_id)
            except Exception:  # noqa: BLE001 — warm cache only [B3]
                _log.warning("store.deprecate_deindex_failed episode_id=%s", episode_id)
        _log.info(
            "store.deprecated episode_id=%s actor=%s from=%s",
            episode_id,
            actor,
            old_trust,
        )
        return True

    def sweep_decayed(self, *, now: int, q_ttl_s: int, p_ttl_s: int) -> dict[str, int]:
        """Materialize the lazy ``lifecycle.decayed`` rule: TTL-lapsed quarantined/
        provisional rows flip to deprecated, each with ONE ``ttl_expired`` audit row,
        in ONE tx; lapsed provisional rows are de-indexed after commit. Idempotent
        (deprecated rows never re-match); bounded by the live quarantined+provisional
        count.  // O(live) time."""
        flips: list[tuple[int, str]] = []  # (episode_id, old_trust)
        with tx(self.conn):
            for r in self.conn.execute(
                "SELECT id, trust, ts, last_active_ts FROM episodes "
                "WHERE trust IN (?,?)",
                (QUARANTINED, PROVISIONAL),
            ):
                if decayed(
                    trust=r["trust"],
                    last_active_ts=r["last_active_ts"],
                    created_ts=r["ts"],
                    now=now,
                    quarantine_ttl_s=q_ttl_s,
                    provisional_ttl_s=p_ttl_s,
                ):
                    flips.append((int(r["id"]), r["trust"]))
            for eid, old_trust in flips:
                self.conn.execute(
                    "UPDATE episodes SET trust=? WHERE id=?", (DEPRECATED, eid)
                )
                self.conn.execute(
                    "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                    "VALUES(?,?,?,?,?)",
                    (
                        eid,
                        EK_TTL_EXPIRED,
                        "server",
                        now,
                        json.dumps({"from": old_trust}),
                    ),
                )
        if self.index is not None:
            for eid, old_trust in flips:
                if old_trust == PROVISIONAL:  # quarantined rows were never indexed
                    try:
                        self.index.remove(eid)
                    except Exception:  # noqa: BLE001 — warm cache only [B3]
                        _log.warning("store.sweep_deindex_failed episode_id=%s", eid)
        out = {QUARANTINED: 0, PROVISIONAL: 0}
        for _eid, old_trust in flips:
            out[old_trust] += 1
        if flips:
            _log.info(
                "store.sweep_decayed quarantined=%d provisional=%d now=%d",
                out[QUARANTINED],
                out[PROVISIONAL],
                now,
            )
        return out

    def quarantined_candidates(
        self,
        *,
        now: int,
        quarantine_ttl_s: int,
    ) -> list[tuple[int, "np.ndarray", str, int, int]]:
        """Live (non-decayed) quarantined rows — the promotion scan's candidate set:
        ``(id, value, proposed_by, ts, last_active_ts)``. Death is decided by the
        ONE pure ``lifecycle.decayed`` rule (don't promote the dead)."""
        out: list[tuple[int, np.ndarray, str, int, int]] = []
        for r in self.conn.execute(
            "SELECT id, value, proposed_by, ts, last_active_ts FROM episodes "
            "WHERE trust=? AND status='approved' AND value IS NOT NULL",
            (QUARANTINED,),
        ):
            if not decayed(
                trust=QUARANTINED,
                last_active_ts=r["last_active_ts"],
                created_ts=r["ts"],
                now=now,
                quarantine_ttl_s=quarantine_ttl_s,
                provisional_ttl_s=0,
            ):  # unused on the quarantined branch
                out.append(
                    (
                        int(r["id"]),
                        np.frombuffer(r["value"], dtype=np.float32).copy(),
                        r["proposed_by"] or "",
                        int(r["ts"]),
                        int(r["last_active_ts"]),
                    )
                )
        return out

    def insert_audit(
        self, episode_id: int, kind: str, actor: str, ts: int, payload: str
    ) -> int:
        """One server-written audit row (evidence_events is server-written ONLY —
        no tool writes here). Returns the row id."""
        with tx(self.conn):
            cur = self.conn.execute(
                "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                "VALUES(?,?,?,?,?)",
                (episode_id, kind, actor, ts, payload),
            )
            return _rowid(cur)

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
            f"ORDER BY episode_id, ts DESC, id DESC",
            [EK_PROMOTE, *ids],
        ):
            eid = int(r["episode_id"])
            if eid in out:
                continue  # already have the newest for this id
            try:
                di = json.loads(r["payload"]).get("demand_independence")
                if not isinstance(di, dict):
                    continue  # pre-stamp row ⇒ omit (under-claim)
                out[eid] = (float(di["rho_bar"]), float(di["n_eff"]), int(di["k"]))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue  # malformed ⇒ skip, never raise
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
        return {
            int(r["episode_id"])
            for r in self.conn.execute(
                f"SELECT DISTINCT episode_id FROM evidence_events "
                f"WHERE kind IN (?,?) AND episode_id IN ({placeholders})",
                [EK_OUTCOME_HELPED, EK_OUTCOME_VERIFIED_HELPED, *ids],
            )
        }

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
        return {
            int(r["episode_id"])
            for r in self.conn.execute(
                f"SELECT DISTINCT episode_id FROM evidence_events "
                f"WHERE kind=? AND episode_id IN ({placeholders})",
                [EK_OUTCOME_VERIFIED_HELPED, *ids],
            )
        }

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
            "ORDER BY episode_id, ts DESC, id DESC",
            [EK_STALE_SUSPECT],
        ):
            eid = int(r["episode_id"])
            if eid in seen:
                continue  # already have the newest for this id
            seen.add(eid)
            out.append((eid, int(r["ts"]), r["payload"]))
        return out

    def anchored_episodes(self) -> list[tuple[int, str, str, str]]:
        """AnchoredEpisodeReader: ``(id, repo, anchor, polarity)`` for approved rows'
        non-empty anchor bindings — the change→episode join's candidate set (the
        census ingest filters ``repo == receipt repo``, so one repo's change can never
        join another repo's memory). One row per binding (an episode may carry
        several). ALL trust states included (mirrors hive_outcome's known-id rule:
        evidence on a deprecated row is honest ledger history); polarity rides for the
        verified-outcome classification only. Read-only, no DDL. // O(rows)."""
        return [
            (int(r["episode_id"]), r["repo"], r["anchor"], r["polarity"])
            for r in self.conn.execute(
                "SELECT ea.episode_id, ea.repo, ea.anchor, e.polarity "
                "FROM episode_anchors ea JOIN episodes e ON e.id = ea.episode_id "
                "WHERE e.status='approved' AND ea.anchor != '' "
                "ORDER BY ea.episode_id, ea.repo, ea.anchor"
            )
        ]

    def evidence_rows_for(
        self, episode_id: int, kinds: Sequence[str]
    ) -> list[tuple[str, str, int]]:
        """The retirement gate's ledger feed: the target's ``(kind, actor, ts)``
        rows restricted to ``kinds``, in insertion order — exactly the 3-sequence
        shape ``retirement.retirement_evidence`` consumes. Empty ``kinds`` → []
        (the caller names what qualifies; an unfiltered read here would hand the
        gate foreign kinds to ignore).

        The payload is deliberately NOT projected: the surviving gate clauses are
        decided by kind, actor and recency alone, so reading a payload grammar here
        would put a second parser between the ledger and the gate for a field no
        clause consumes. Read-only, no DDL. // O(rows)."""
        ks = [str(k) for k in kinds]
        if not ks:
            return []
        placeholders = ",".join("?" for _ in ks)
        return [
            (r["kind"], r["actor"], int(r["ts"]))
            for r in self.conn.execute(
                f"SELECT kind, actor, ts FROM evidence_events "
                f"WHERE episode_id=? AND kind IN ({placeholders}) ORDER BY id",
                [int(episode_id), *ks],
            )
        ]

    def append_evidence(
        self, rows: Sequence[tuple[int, str, str, int, str]]
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
                    (episode_id, kind, payload),
                ).fetchone()
                if dup is not None:
                    skipped += 1
                    continue
                cur = self.conn.execute(
                    "INSERT INTO evidence_events(episode_id, kind, actor, ts, payload) "
                    "VALUES(?,?,?,?,?)",
                    (episode_id, kind, actor, ts, payload),
                )
                inserted.append(_rowid(cur))
        if inserted or skipped:
            _log.info(
                "store.append_evidence inserted=%d skipped=%d", len(inserted), skipped
            )
        return inserted, skipped

    # ── RangeLedger port (the census feed's durable range-dedupe seam) ─────────
    def already_ingested_range(
        self, repo: str, base_sha: str, head_sha: str, phase: str
    ) -> bool:
        """RangeLedger: has this EXACT ``(repo, base_sha, head_sha, phase)`` range
        already been ingested? Exact-key equality only — no subsumption/overlap
        reasoning (legacy A..B + B..C and a sync A..C are distinct keys; transition
        noise all lands). Read-only, no DDL. // O(1) (PK probe)."""
        return (
            self.conn.execute(
                "SELECT 1 FROM ingested_ranges "
                "WHERE repo=? AND base_sha=? AND head_sha=? AND phase=? LIMIT 1",
                (repo, base_sha, head_sha, phase),
            ).fetchone()
            is not None
        )

    def record_ingested_range(
        self, repo: str, base_sha: str, head_sha: str, phase: str, ts: int
    ) -> bool:
        """RangeLedger: durably record one ingested range, idempotent on the exact key.
        True iff a NEW row was written; a repeat is a no-op False (the first write
        stands — its ts is never refreshed). Existence is checked explicitly (not
        INSERT OR IGNORE) so any other IntegrityError still RAISES rather than being
        silently swallowed (the record_conflict_flag idiom). ONE tx. // O(1)."""
        with tx(self.conn):
            if (
                self.conn.execute(
                    "SELECT 1 FROM ingested_ranges "
                    "WHERE repo=? AND base_sha=? AND head_sha=? AND phase=?",
                    (repo, base_sha, head_sha, phase),
                ).fetchone()
                is not None
            ):
                return False
            self.conn.execute(
                "INSERT INTO ingested_ranges(repo, base_sha, head_sha, phase, ts) "
                "VALUES(?,?,?,?,?)",
                (repo, base_sha, head_sha, phase, int(ts)),
            )
        return True

    # ── the staleness baseline (a server observation, not a writer declaration) ─
    def anchor_work_list(self, repo: str) -> list[tuple[int, str, str]]:
        """``(episode_id, anchor, base_tip)`` for every NON-RETIRED approved anchor
        binding of ``repo``, in (episode_id, anchor) order — the ONE work list both
        the baseline sweep and the drift materializer read. ``base_tip`` is ``''``
        when no baseline has been recorded yet (the sweep's own candidate predicate),
        so the two questions "what needs baselining" and "what needs judging" can no
        longer be asked by two queries that might disagree.

        Quarantined/provisional/established rows all stay in; only
        ``trust='deprecated'`` is excluded — a retired memory must stop consuming the
        materializer's work forever (BUG-065). Read-only, no DDL. // O(rows)."""
        return [
            (int(r["episode_id"]), r["anchor"], r["base_tip"] or "")
            for r in self.conn.execute(
                "SELECT ea.episode_id, ea.anchor, ab.base_tip FROM episode_anchors ea "
                "JOIN episodes e ON e.id = ea.episode_id "
                "LEFT JOIN anchor_baselines ab ON ab.episode_id = ea.episode_id "
                "  AND ab.repo = ea.repo AND ab.anchor = ea.anchor "
                f"WHERE ea.repo=? AND ea.anchor != '' "
                f"AND {_LIVE_EPISODE_PREDICATE} "
                f"ORDER BY ea.episode_id, ea.anchor",
                (repo,),
            )
        ]

    def anchor_baseline_put(
        self, rows: Sequence[tuple[int, str, str, str, int]]
    ) -> int:
        """Record ``(episode_id, repo, anchor, base_tip, ts)`` baselines, ONE tx.
        Returns how many rows were actually inserted.

        INSERT-IF-ABSENT ONLY: a recorded baseline is NEVER moved. Re-baselining an
        anchor at a later tip would erase a break that already landed between the two
        — the memory would read ``fresh`` forever about code that changed under it.
        Empty ``base_tip`` rows are skipped: a repo whose watermark the server has not
        observed yet has no baseline to record, and storing ``''`` would occupy the key
        the real observation must later fill.

        // marker: making this an upsert (ON CONFLICT DO UPDATE) reds
        test_a_recorded_baseline_is_never_moved."""
        insertable = [r for r in rows if r[3]]
        if not insertable:
            return 0
        with tx(self.conn):
            return self._insert_baselines_locked(insertable)

    def _insert_baselines_locked(
        self, rows: Sequence[tuple[int, str, str, str, int]]
    ) -> int:
        """The one INSERT the absent-only rule lives in. Caller owns the tx."""
        added = 0
        for episode_id, repo, anchor, base_tip, ts in rows:
            cur = self.conn.execute(
                "INSERT INTO anchor_baselines(episode_id, repo, anchor, base_tip, ts) "
                "VALUES(?,?,?,?,?) ON CONFLICT(episode_id, repo, anchor) DO NOTHING",
                (int(episode_id), repo, anchor, base_tip, int(ts)),
            )
            added += int(cur.rowcount or 0)
        return added

    def anchor_symbol_probes(self, repo: str) -> dict[tuple[str, str], int]:
        """``(base_tip, anchor) -> symbol_ok`` for every recorded baseline of ``repo``.

        The verdict cache is keyed by ``(base_tip, anchor)``, not by episode, so this
        collapses the per-episode rows onto the same key. It folds with MIN because the
        column is ordered by how much it claims — ``-1`` unprobed < ``0`` did not
        resolve < ``1`` resolved — so two rows that somehow disagree resolve to the
        weaker claim rather than the stronger one. Read-only, no DDL. // O(rows)."""
        return {
            (r["base_tip"], r["anchor"]): int(r["ok"])
            for r in self.conn.execute(
                "SELECT base_tip, anchor, MIN(symbol_ok) AS ok FROM anchor_baselines "
                "WHERE repo=? AND base_tip != '' GROUP BY base_tip, anchor",
                (repo,),
            )
        }

    def anchor_symbol_ok_put(
        self, repo: str, rows: Sequence[tuple[str, str, int]]
    ) -> int:
        """Record ``(base_tip, anchor, symbol_ok)`` resolutions, ONE tx. Returns the
        rows actually written.

        WRITE-ONCE: only a still-unprobed row is filled. Whether a symbol resolved at a
        given commit is a fact about a tree that can never change, so re-asking is waste
        and re-WRITING would let a transient inability to resolve (a missing funcname
        driver, an unreadable mirror) overwrite a real observation and turn a live
        binding into a false ``anchor_missing``.

        // marker: dropping the ``symbol_ok=-1`` clause reds
        test_a_recorded_symbol_resolution_is_never_moved."""
        written = 0
        if not rows:
            return 0
        with tx(self.conn):
            for base_tip, anchor, ok in rows:
                cur = self.conn.execute(
                    "UPDATE anchor_baselines SET symbol_ok=? "
                    "WHERE repo=? AND anchor=? AND base_tip=? AND symbol_ok=-1",
                    (int(ok), repo, anchor, base_tip),
                )
                written += int(cur.rowcount or 0)
        return written

    def anchor_baselines_get(
        self, episode_id: int, repo: str, anchors: Sequence[str]
    ) -> dict[str, str]:
        """The recall-side read: ``anchor -> base_tip`` for ONE episode's bindings in
        ``repo``. An anchor with no recorded baseline is simply ABSENT, and the caller
        reads absence as the honest unknown. Read-only, no DDL. // O(k)."""
        names = [str(a) for a in anchors]
        if not names:
            return {}
        placeholders = ",".join("?" for _ in names)
        return {
            r["anchor"]: r["base_tip"]
            for r in self.conn.execute(
                f"SELECT anchor, base_tip FROM anchor_baselines "
                f"WHERE episode_id=? AND repo=? AND anchor IN ({placeholders})",
                [int(episode_id), repo, *names],
            )
        }

    def anchor_baselines_prune(self, repo: str, keep: Sequence[tuple[int, str]]) -> int:
        """Drop a repo's baselines for every ``(episode_id, anchor)`` NOT in ``keep``
        (empty ⇒ drop the repo's whole baseline set) — the ``drift_prune`` twin, so a
        retired memory's baseline leaves with its anchor instead of outliving it.
        Other repos untouched. Returns the rows deleted. ONE tx. // O(rows)."""
        live = {(int(e), str(a)) for e, a in keep}
        with tx(self.conn):
            doomed = [
                (int(r["episode_id"]), r["anchor"])
                for r in self.conn.execute(
                    "SELECT episode_id, anchor FROM anchor_baselines WHERE repo=?",
                    (repo,),
                )
                if (int(r["episode_id"]), r["anchor"]) not in live
            ]
            for episode_id, anchor in doomed:
                self.conn.execute(
                    "DELETE FROM anchor_baselines "
                    "WHERE repo=? AND episode_id=? AND anchor=?",
                    (repo, episode_id, anchor),
                )
        return len(doomed)

    # ── the repo registry (operational data — the authctl-seat pattern) ────────
    def repo_registry(self) -> list[RepoRow]:
        """Every registered repo, name-ordered. The sync daemon re-reads this each
        tick (registering a repo needs no restart). Read-only, no DDL. // O(n)."""
        return [
            RepoRow(
                name=r["name"],
                url=r["url"],
                canonical_ref=r["canonical_ref"],
                token_env=r["token_env"],
                added_ts=int(r["added_ts"]),
            )
            for r in self.conn.execute("SELECT * FROM repos ORDER BY name")
        ]

    def repo_add(
        self,
        *,
        name: str,
        url: str,
        canonical_ref: str = "",
        token_env: str = "",
        added_ts: int,
    ) -> None:
        """Register one repo. ``name`` is slug-checked ([a-z0-9._-]+, never empty or
        all-dot like ``.``/``..`` — it rides mirror paths and scope labels); a bad
        slug, empty url, or duplicate name RAISES ValueError with the reason (nothing
        written — repoctl maps it to a non-zero exit). ``token_env`` stores the NAME
        of the env var holding the git token ('' ⇒ the HIVE_SYNC__TOKEN default) —
        indirection, never a secret byte. ONE tx. // O(1)."""
        if not isinstance(name, str) or _REPO_NAME_RE.match(name) is None:
            raise ValueError(
                f"bad repo name {name!r} — want a [a-z0-9._-]+ slug, no all-dot "
                "names like '.' or '..' (it rides mirror paths and scope labels)"
            )
        if not url:
            raise ValueError("repo url must be non-empty")
        with tx(self.conn):
            if (
                self.conn.execute(
                    "SELECT 1 FROM repos WHERE name=?", (name,)
                ).fetchone()
                is not None
            ):
                raise ValueError(f"repo {name!r} is already registered")
            self.conn.execute(
                "INSERT INTO repos(name, url, canonical_ref, token_env, added_ts) "
                "VALUES(?,?,?,?,?)",
                (name, url, canonical_ref, token_env, int(added_ts)),
            )
        _log.info("store.repo_added name=%s token_env=%s", name, token_env or "-")

    def repo_remove(self, name: str) -> bool:
        """Deregister one repo. True iff a row was deleted (False ⇒ no such name —
        idempotent-bool, the repo_record convention). The sync daemon prunes the
        mirror on its next tick; episode scope rows are UNTOUCHED — memories keep
        their partition (a re-registered repo picks them straight back up).

        Deregistration forgets the feed AND EVERYTHING DERIVED FROM IT, all in the
        same tx: the ``sync:<name>:*`` daemon state (BUG-060), the per-ref
        watermarks (``ref_tips``), the per-binding staleness baselines
        (``anchor_baselines`` — a server OBSERVATION of a commit this incarnation
        actually watched, never a writer declaration), the materialized verdicts
        keyed on both (``anchor_drift``), and the materializer's work list
        (``ref_requests``).
        Leaving any of them made re-registering the same name — a documented
        operator flow — answer from the PREVIOUS incarnation: a fully-populated
        health block before the daemon had ticked once, a ledger resumed from a
        watermark whose mirror no longer exists, and (BUG-068) a branch-scoped
        recall served ``fresh`` off a dead incarnation's tip, because ``ref_tips``
        is the branch twin of ``sync:<name>:last_tip`` living in a table the meta
        sweep cannot reach. All three caches are rebuildable (Law 5) and
        re-materialize on the re-registered repo's first tick, which is exactly
        what a fresh registration is defined to do.

        What survives is MEMORY, not feed: the episodes, their scope
        (``episode_anchors`` / ``episode_refs`` — the line the WRITER declared,
        never a daemon observation), and the append-only ledger. A re-registered
        repo picks them straight back up. // O(1)."""
        with tx(self.conn):
            cur = self.conn.execute("DELETE FROM repos WHERE name=?", (name,))
            self.conn.execute("DELETE FROM meta WHERE key LIKE ?", (f"sync:{name}:%",))
            self.conn.execute("DELETE FROM ref_tips WHERE repo=?", (name,))
            self.conn.execute("DELETE FROM anchor_baselines WHERE repo=?", (name,))
            self.conn.execute("DELETE FROM anchor_drift WHERE repo=?", (name,))
            self.conn.execute("DELETE FROM ref_requests WHERE repo=?", (name,))
        if cur.rowcount:
            _log.info("store.repo_removed name=%s", name)
        return cur.rowcount > 0

    # ── the materialized drift cache (rebuildable, Law 5) ──────────────────────
    def drift_get(
        self,
        repo: str,
        tip_sha: str,
        base_tips: Mapping[str, str],
        anchors: Sequence[str],
    ) -> dict[str, tuple[str, str]]:
        """The recall-side drift read: the materialized ``(verdict, detail)`` per
        requested anchor at EXACTLY ``(repo, tip_sha, base_tip, anchor)`` — every
        input to the verdict, so a row cached against a DIFFERENT baseline can never
        answer this question (BUG-081 unconstructable, not invalidated). ``base_tips``
        maps anchor → the baseline that anchor is judged from; an anchor absent from
        it is looked up at the empty baseline. An un-materialized anchor is simply
        ABSENT — the caller reads absence as ``unverifiable`` (fail-safe: never
        false-fresh, never false-stale). Verdict strings ride verbatim (the wire
        vocabulary is stamped upstream — this cache never interprets). Read-only,
        no DDL. // O(k)."""
        names = [str(a) for a in anchors]
        if not names:
            return {}
        pairs = ",".join("(?,?)" for _ in names)
        args: list[object] = [repo, tip_sha]
        for anchor in names:
            args += [anchor, base_tips.get(anchor, "")]
        return {
            r["anchor"]: (r["verdict"], r["detail"])
            for r in self.conn.execute(
                f"SELECT anchor, verdict, detail FROM anchor_drift "
                f"WHERE repo=? AND tip_sha=? AND (anchor, base_tip) IN (VALUES {pairs})",
                args,
            )
        }

    def drift_put(
        self, rows: Sequence[tuple[str, str, str, str, str, str, int]]
    ) -> None:
        """Materialize drift verdicts — ``(repo, tip_sha, base_tip, anchor, verdict,
        detail_json, ts)`` rows in DDL column order, ONE tx, last-write-wins on the
        ``(repo, tip_sha, base_tip, anchor)`` key (a re-materialization refreshes the
        cache row; the cache is rebuildable state, not a ledger). // O(batch)."""
        if not rows:
            return
        with tx(self.conn):
            for repo, tip_sha, base_tip, anchor, verdict, detail, ts in rows:
                self.conn.execute(
                    "INSERT INTO anchor_drift"
                    "(repo, tip_sha, base_tip, anchor, verdict, detail, ts) "
                    "VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(repo, tip_sha, base_tip, anchor) DO UPDATE SET "
                    "verdict=excluded.verdict, detail=excluded.detail, ts=excluded.ts",
                    (repo, tip_sha, base_tip, anchor, verdict, detail, int(ts)),
                )

    def drift_prune(
        self,
        repo: str,
        keep_tips: Sequence[str],
        keep_anchors: Sequence[str] | None = None,
        keep_base_tips: Sequence[str] | None = None,
    ) -> int:
        """Drop a repo's materialized verdicts for every row whose tip is NOT in
        ``keep_tips`` OR — when given — whose anchor is NOT in ``keep_anchors`` OR
        whose baseline is NOT in ``keep_base_tips``: bounded growth for a rebuildable
        cache (a pruned row re-materializes on demand), AND the false-fresh close —
        once an anchor or a baseline leaves the live work list, its cached rows at
        LIVE tips must not survive to answer a later episode that reuses the same
        anchor. A ``None`` bound is simply not applied; an empty-but-given one means
        nothing of that kind is live, so the whole repo cache drops. Other repos
        untouched. Returns the rows deleted. ONE tx. // O(rows)."""
        keep = [str(t) for t in keep_tips]
        clauses: list[str] = []
        args: list[object] = [repo]
        clauses.append(
            "tip_sha NOT IN ({})".format(",".join("?" for _ in keep)) if keep else "1=1"
        )
        args += keep
        for column, bound in (("anchor", keep_anchors), ("base_tip", keep_base_tips)):
            if bound is None:
                continue
            members = [str(b) for b in bound]
            clauses.append(
                "{} NOT IN ({})".format(column, ",".join("?" for _ in members))
                if members
                else "1=1"
            )
            args += members
        with tx(self.conn):
            cur = self.conn.execute(
                f"DELETE FROM anchor_drift WHERE repo=? AND ({' OR '.join(clauses)})",
                args,
            )
        return int(cur.rowcount)

    # ── the per-ref tip watermark (ref_tips: the branch twin of last_tip) ──────
    def ref_tip(self, repo: str, ref: str) -> str | None:
        """The materializer's last-resolved tip for a non-canonical ``(repo,
        ref)`` — the branch twin of the canonical ``sync:<repo>:last_tip`` meta
        key. None = never materialized (the fail-safe: the caller reads
        unverifiable, never a stale or fabricated tip). Read-only, no DDL.
        // O(1)."""
        row = self.conn.execute(
            "SELECT tip_sha FROM ref_tips WHERE repo=? AND ref=?", (repo, ref)
        ).fetchone()
        return row["tip_sha"] if row is not None else None

    def ref_tips_put(self, rows: Sequence[tuple[str, str, str, int]]) -> None:
        """Upsert the materializer's per-ref watermark — ``(repo, ref, tip_sha,
        ts)`` rows, ONE tx, last-write-wins on the ``(repo, ref)`` key (the
        ``drift_put`` idiom for its twin cache). // O(batch)."""
        if not rows:
            return
        with tx(self.conn):
            for repo, ref, tip_sha, ts in rows:
                self.conn.execute(
                    "INSERT INTO ref_tips(repo, ref, tip_sha, ts) VALUES(?,?,?,?) "
                    "ON CONFLICT(repo, ref) DO UPDATE SET "
                    "tip_sha=excluded.tip_sha, ts=excluded.ts",
                    (repo, ref, tip_sha, int(ts)),
                )

    def ref_tips_prune(self, repo: str, keep_refs: Sequence[str]) -> int:
        """Drop a repo's ``ref_tips`` rows for every ref NOT in ``keep_refs``
        (empty ⇒ drop the repo's whole ref-tip set) — the ``drift_prune`` twin,
        bounded growth for a rebuildable watermark. Other repos untouched. Returns
        the rows deleted. ONE tx. // O(rows)."""
        keep = [str(r) for r in keep_refs]
        with tx(self.conn):
            if keep:
                placeholders = ",".join("?" for _ in keep)
                cur = self.conn.execute(
                    f"DELETE FROM ref_tips WHERE repo=? AND ref NOT IN ({placeholders})",
                    [repo, *keep],
                )
            else:
                cur = self.conn.execute("DELETE FROM ref_tips WHERE repo=?", (repo,))
        return int(cur.rowcount)

    # ── ref requests (recall-touched refs wanting materialization) ─────────────
    def touch_ref_request(self, repo: str, ref: str, ts: int) -> None:
        """Record that recall wanted drift for ``(repo, ref)`` — the demand signal the
        materializer reads to cover non-canonical branch tips. Upsert keeping the
        NEWEST request stamp (monotonic — an out-of-order touch never rewinds the
        clock). ONE tx. // O(1)."""
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO ref_requests(repo, ref, last_requested_ts) VALUES(?,?,?) "
                "ON CONFLICT(repo, ref) DO UPDATE SET "
                "last_requested_ts=MAX(last_requested_ts, excluded.last_requested_ts)",
                (repo, ref, int(ts)),
            )

    def requested_refs(self, repo: str, since_ts: int) -> list[str]:
        """The refs of ``repo`` touched STRICTLY after ``since_ts`` (the miss-window
        convention), ref-ordered — the materializer's per-tick work list. Read-only,
        no DDL. // O(n)."""
        return [
            r["ref"]
            for r in self.conn.execute(
                "SELECT ref FROM ref_requests WHERE repo=? AND last_requested_ts>? "
                "ORDER BY ref",
                (repo, int(since_ts)),
            )
        ]

    def declared_refs(self, repo: str) -> list[str]:
        """Every DISTINCT ref DECLARED by a live episode of ``repo`` — the
        materializer's declared-coverage work list, ref-ordered (the
        ``requested_refs`` demand-side twin: this is coverage a memory's own line
        earns regardless of whether anyone ever recalled it). 'Live' = the same
        not-retired reading as the other work-list verbs — a retired memory's
        declared line stops demanding
        coverage — the shared ``_LIVE_EPISODE_PREDICATE``. Read-only, no DDL.
        // O(rows)."""
        return [
            r["ref"]
            for r in self.conn.execute(
                "SELECT DISTINCT er.ref FROM episode_refs er "
                "JOIN episodes e ON e.id = er.episode_id "
                f"WHERE er.repo=? AND {_LIVE_EPISODE_PREDICATE} "
                f"ORDER BY er.ref",
                (repo,),
            )
        ]

    # ── health reads ───────────────────────────────────────────────────────────
    def trust_counts(self) -> dict[str, int]:
        """Per-trust-state row counts for hive_health — ALL four states present
        (a zero is signal: quarantine pile-up must be visible, never silent)."""
        out = {t: 0 for t in TRUST_STATES}
        for r in self.conn.execute(
            "SELECT trust, COUNT(*) AS c FROM episodes GROUP BY trust"
        ):
            if r["trust"] in out:
                out[r["trust"]] = int(r["c"])
        return out

    def meta_version_counts(self) -> dict[str, dict[str, Any]]:
        """Per-meta-key token-version histogram for hive_health, over the LIVE corpus
        (status='approved' AND trust != 'deprecated' — servable + quarantined, retired
        tombstones excluded). Pure fold lives in domain meta.meta_version_histogram; this
        method only streams the rows (the ONE sanctioned prefix-only meta read — THEORY §5)."""
        rows = self.conn.execute(
            "SELECT meta FROM episodes WHERE status='approved' AND trust != ?",
            (DEPRECATED,),
        )
        return meta_version_histogram(r["meta"] for r in rows)

    # ── ExposureLedger port (the recall side-channel writer) ──────────────────
    def record_exposure(
        self,
        trace_id: str,
        items: Sequence[tuple[int, float]],
        *,
        agent_id: str,
        ts: int,
    ) -> None:
        """Persist WHO was served WHAT and refresh the served rows' liveness clocks
        — ONE tx, so an exposure can never land without its last_active bump (a
        served provisional row that failed to refresh would decay while in use)."""
        with tx(self.conn):
            for eid, margin in items:
                self.conn.execute(
                    "INSERT INTO exposure(trace_id, episode_id, recall_margin, "
                    "injected_ts, agent_id) VALUES(?,?,?,?,?)",
                    (trace_id, int(eid), float(margin), ts, agent_id),
                )
            for eid, _margin in items:
                self.conn.execute(
                    "UPDATE episodes SET last_active_ts=? WHERE id=?", (ts, int(eid))
                )

    def record_miss(
        self,
        query_text: str,
        query_vector: Optional[bytes],
        agent_id: str,
        miss_type: str,
        *,
        ts: int,
        repos_json: str = "",
    ) -> None:
        """Persist one non-answer (the demand signal). The CALLER owns the secret
        floor — a refused query arrives here already stripped (empty text, None
        vector); a redacted one arrives masked with a vector re-encoded from the
        masked text. ``repos_json`` is the query's repo scope as a serialized JSON
        array ('' = a GLOBAL miss, the §3.6 match-everything scope — also the
        fail-safe default for a scope-less caller); the boundary owns the grammar,
        the store round-trips it. miss_type is CHECK-constrained by the DDL."""
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO recall_misses(query_text, query_vector, agent_id, "
                "miss_type, ts, repos) VALUES(?,?,?,?,?,?)",
                (query_text, query_vector, agent_id, miss_type, ts, repos_json),
            )

    def miss_count_since(self, since_ts: int) -> int:
        """Misses recorded strictly after ``since_ts`` (hive_health telemetry)."""
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM recall_misses WHERE ts>?", (since_ts,)
            ).fetchone()["c"]
        )

    def misses_detail_window(self, since_ts: int) -> list[dict[str, Any]]:
        """Full miss rows (text + type + ts + optional vector) for the demand-gap
        report — unlike ``misses_window`` this INCLUDES vector-less secret_refused
        rows (they count in telemetry; they can never drive promotion)."""
        out: list[dict[str, Any]] = []
        for r in self.conn.execute(
            "SELECT query_text, query_vector, miss_type, ts FROM recall_misses "
            "WHERE ts>? ORDER BY id",
            (since_ts,),
        ):
            vec = (
                np.frombuffer(r["query_vector"], dtype=np.float32).copy()
                if r["query_vector"] is not None
                else None
            )
            out.append(
                {
                    "query_text": r["query_text"],
                    "vector": vec,
                    "miss_type": r["miss_type"],
                    "ts": int(r["ts"]),
                }
            )
        return out

    @staticmethod
    def _parse_miss_repos(raw: str) -> tuple[str, ...]:
        """The stored ``repos`` JSON array → the MissRow scope tuple. '' (the global
        miss / pre-scope default) and anything unparseable read as ``()`` — the
        legacy GLOBAL reading, identical to the column default (the writer owns the
        grammar; a corrupt scope never crashes the demand read)."""
        if not raw:
            return ()
        try:
            parsed = json.loads(raw)
        except ValueError:
            return ()
        if not isinstance(parsed, list):
            return ()
        return tuple(x for x in parsed if isinstance(x, str) and x)

    def misses_window(self, since_ts: int) -> list[MissRow]:
        """The demand-window slice: misses STRICTLY after ``since_ts`` that carry a
        vector (secret-refused rows persist none and can never drive promotion).
        Each row carries its repo scope (v3 §3.6) — ``()`` = a global miss."""
        return [
            MissRow(
                vector=np.frombuffer(r["query_vector"], dtype=np.float32).copy(),
                agent_id=r["agent_id"],
                ts=int(r["ts"]),
                repos=self._parse_miss_repos(r["repos"]),
            )
            for r in self.conn.execute(
                "SELECT query_vector, agent_id, ts, repos FROM recall_misses "
                "WHERE ts>? AND query_vector IS NOT NULL ORDER BY id",
                (since_ts,),
            )
        ]

    # ── advisory conflict flags (ConflictFlagStore port + the worklist read) ──
    def record_conflict_flag(
        self,
        *,
        kind: str,
        a_id: int,
        b_id: int,
        winner_id: Optional[int],
        resolution: str,
        proposed_by: str,
        ts: int,
    ) -> bool:
        """Record ONE advisory conflict flag, idempotent on the canonical (a_id, b_id, kind).
        Returns True iff a NEW row was written; a re-flag of the same pair+kind is a no-op
        (the first write stands — no overwrite). Existence is checked explicitly (not
        INSERT OR IGNORE) so a CHECK violation (bad kind/status) still RAISES rather than being
        silently swallowed. The caller (ConflictFlagService) owns canonicalization + the
        resolution secret scan. NEVER touches an episode's trust — this is advisory only."""
        with tx(self.conn):
            if (
                self.conn.execute(
                    "SELECT 1 FROM conflict_flags WHERE a_id=? AND b_id=? AND kind=?",
                    (int(a_id), int(b_id), kind),
                ).fetchone()
                is not None
            ):
                return False
            self.conn.execute(
                "INSERT INTO conflict_flags(kind, a_id, b_id, winner_id, resolution, "
                "proposed_by, ts) VALUES(?,?,?,?,?,?,?)",
                (
                    kind,
                    int(a_id),
                    int(b_id),
                    None if winner_id is None else int(winner_id),
                    resolution,
                    proposed_by,
                    int(ts),
                ),
            )
        return True

    def open_conflict_flags(self) -> list[dict[str, Any]]:
        """All status='open' advisory flags (the health worklist's advisory channel),
        ordered by id. Duck-typed app read (the misses_detail_window convention), NOT a
        domain port. A flag auto-clears from the worklist when either episode goes
        non-servable — that filter lives in the app report, so status keeps only the
        operator-driven 'dismissed'."""
        return [
            {
                "id": r["id"],
                "kind": r["kind"],
                "a_id": r["a_id"],
                "b_id": r["b_id"],
                "winner_id": r["winner_id"],
                "resolution": r["resolution"],
                "proposed_by": r["proposed_by"],
                "ts": int(r["ts"]),
            }
            for r in self.conn.execute(
                "SELECT * FROM conflict_flags WHERE status='open' ORDER BY id"
            )
        ]

    # ── meta watermark kv (write seam; reads go through raw SQL at healthcheck) ──
    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
