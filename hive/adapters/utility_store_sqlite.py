"""SqliteUtilityStore — Beta-Bernoulli utility posterior on real SQLite [A3][A5][A7].

Shares the one WAL connection with the ledger store so the whole producer tick is
one transaction. Methods do NOT open their own transaction — the caller (producer
tick, or a test) owns the BEGIN/COMMIT via ``sqlite_db.tx``. Deliberately has NO
weight column and NO episodes handle: the loop NEVER writes episode.weight [A3].
"""
from __future__ import annotations

import sqlite3
from typing import Sequence

from hive.domain.attribution import CreditDelta
from hive.domain.models import SettledExposure, UtilityPosterior

# task_outcomes.outcome → SettledExposure.reward_sign (win delivered, loss regressed).
_SETTLED_STATE_SIGN = {"win": 1, "loss": -1}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS utility(
  episode_id INTEGER NOT NULL, family_scope TEXT NOT NULL,
  wins REAL NOT NULL DEFAULT 0, losses REAL NOT NULL DEFAULT 0,
  n_sources INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
  isolation INTEGER NOT NULL DEFAULT 0, cas_version INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(episode_id, family_scope));
CREATE TABLE IF NOT EXISTS utility_sources(
  episode_id INTEGER NOT NULL, family_scope TEXT NOT NULL, source_agent TEXT NOT NULL,
  PRIMARY KEY(episode_id, family_scope, source_agent));
CREATE TABLE IF NOT EXISTS utility_layer(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);
INSERT OR IGNORE INTO utility_layer(id, version) VALUES (1, 1);
"""


class SqliteUtilityStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.executescript(_SCHEMA)

    def apply_credit(self, deltas: Sequence[CreditDelta]) -> None:
        """Accumulate wins/losses, bump version + cas_version, record distinct
        corroborating agents. Assumes an open tx (atomic with the rest of the tick)."""
        for d in deltas:
            self.conn.execute(
                "INSERT INTO utility(episode_id, family_scope, wins, losses, version, cas_version) "
                "VALUES(?,?,?,?,1,1) "
                "ON CONFLICT(episode_id, family_scope) DO UPDATE SET "
                "  wins = wins + excluded.wins, losses = losses + excluded.losses, "
                "  version = version + 1, cas_version = cas_version + 1",
                (d.episode_id, d.family_scope, d.d_wins, d.d_losses),
            )
            if d.source_agent:
                self.conn.execute(
                    "INSERT OR IGNORE INTO utility_sources VALUES(?,?,?)",
                    (d.episode_id, d.family_scope, d.source_agent),
                )
            n = self.conn.execute(
                "SELECT COUNT(*) FROM utility_sources WHERE episode_id=? AND family_scope=?",
                (d.episode_id, d.family_scope),
            ).fetchone()[0]
            self.conn.execute(
                "UPDATE utility SET n_sources=? WHERE episode_id=? AND family_scope=?",
                (n, d.episode_id, d.family_scope),
            )

    def posterior(self, episode_id: int, family_scope: str) -> UtilityPosterior:
        r = self.conn.execute(
            "SELECT wins, losses, n_sources, version, isolation FROM utility "
            "WHERE episode_id=? AND family_scope=?", (episode_id, family_scope),
        ).fetchone()
        if r is None:
            return UtilityPosterior(episode_id=episode_id, family_scope=family_scope)
        return UtilityPosterior(
            episode_id=episode_id, family_scope=family_scope,
            wins=r["wins"], losses=r["losses"], n_sources=r["n_sources"],
            version=r["version"], isolation=bool(r["isolation"]),
        )

    def utility_map(self, family_scope: str, *, confident_only: bool = True) -> dict[int, float]:
        out: dict[int, float] = {}
        for r in self.conn.execute(
            "SELECT episode_id, wins, losses FROM utility WHERE family_scope=?", (family_scope,),
        ):
            post = UtilityPosterior(episode_id=r["episode_id"], family_scope=family_scope,
                                    wins=r["wins"], losses=r["losses"])
            if confident_only and not post.ci_excludes_half():
                continue
            out[r["episode_id"]] = post.mean()
        return out

    def settled_exposures_since(self, family_scope: str, since_ts: int) -> list[SettledExposure]:
        """Guardrail-3 (A6) settled stream the PredictionBiasMonitor reads: this scope's
        settled credit rows with ingested_ts ≥ since_ts. The credit-regime task_outcomes
        carries episode_id per row (one row per (commit_sha, episode_id)), so the old
        exposure join collapses — rows group by (commit_sha, trace_id) into one
        SettledExposure each (win→+1, loss→−1; the settle clock is ingested_ts, stable
        because re-scans never re-insert). ``repo`` is the family carrier. Inclusive
        floor — the SAME boundary semantics as FakeUtilityStore. O(k) via
        idx_task_outcomes_settle. REQUIRES the ledger schema on self.conn (the
        production single-DB wiring); a missing task_outcomes table fails LOUD rather
        than silently blinding the monitor — a guardrail that returns 'no divergence'
        on a wiring fault is the phantom this module exists to kill."""
        grouped: dict[tuple[str, str], dict] = {}        # (commit_sha, trace_id) → one outcome
        for r in self.conn.execute(
            "SELECT commit_sha, trace_id, outcome, ingested_ts, episode_id "
            "FROM task_outcomes "
            "WHERE repo = ? AND ingested_ts >= ? "
            "ORDER BY ingested_ts, commit_sha, trace_id, episode_id",
            (family_scope, since_ts),
        ):
            slot = grouped.setdefault(
                (r["commit_sha"], r["trace_id"]),
                {"outcome": r["outcome"], "ingested_ts": r["ingested_ts"], "eids": []})
            slot["eids"].append(int(r["episode_id"]))
        return [
            SettledExposure(
                family_scope=family_scope,
                reward_sign=_SETTLED_STATE_SIGN[s["outcome"]],
                exposed=tuple(s["eids"]),
                settle_ts=int(s["ingested_ts"]),
            )
            for s in grouped.values()
        ]

    def isolation_episode_ids(self) -> set[int]:
        return {row[0] for row in self.conn.execute(
            "SELECT DISTINCT episode_id FROM utility WHERE isolation=1")}

    def mark_isolation(self, episode_id: int, family_scope: str) -> None:
        self.conn.execute(
            "INSERT INTO utility(episode_id, family_scope, isolation) VALUES(?,?,1) "
            "ON CONFLICT(episode_id, family_scope) DO UPDATE SET isolation=1",
            (episode_id, family_scope))

    def zero_utility_layer(self) -> None:
        """Guardrail-4 human rollback: zero all posteriors, bump the layer version."""
        self.conn.execute("UPDATE utility SET wins=0, losses=0, cas_version=cas_version+1")
        self.conn.execute("UPDATE utility_layer SET version = version + 1 WHERE id=1")

    def layer_version(self) -> int:
        return self.conn.execute("SELECT version FROM utility_layer WHERE id=1").fetchone()[0]
