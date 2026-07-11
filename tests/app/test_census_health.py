"""The passive census staleness signal — hive_health(include_census_health=true).

A single-query, fail-open report of how long ago the last SHA-bound change_outcome
evidence row landed. Serves the raw day-count (no invented staleness threshold, THEORY
§9 #14) so an operator can tell whether the post-merge census feed has gone dark.
"""
from __future__ import annotations

import sqlite3
import time

from hive.app.census_health import census_health_report
from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME, EK_OUTCOME_HELPED
from tests.mcp._helpers import build_real_server, content, tool_call

_DAY_S = 86_400


def _conn_with_evidence() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE evidence_events(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "episode_id INTEGER NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, "
        "ts INTEGER NOT NULL, payload TEXT NOT NULL DEFAULT '{}')")
    return conn


def _insert(conn: sqlite3.Connection, kind: str, ts: int) -> None:
    conn.execute("INSERT INTO evidence_events(episode_id, kind, actor, ts) VALUES(?,?,?,?)",
                 (1, kind, "server", ts))
    conn.commit()


def test_empty_store_reports_none():
    conn = _conn_with_evidence()
    assert census_health_report(conn) == {"days_since_last_change_outcome": None}


def test_only_other_kinds_reports_none():
    # Only change_outcome counts — a helped/hurt row must not be read as a census outcome.
    conn = _conn_with_evidence()
    _insert(conn, EK_OUTCOME_HELPED, int(time.time()) - 5 * _DAY_S)
    assert census_health_report(conn) == {"days_since_last_change_outcome": None}


def test_one_change_outcome_at_known_ts_reports_days():
    conn = _conn_with_evidence()
    now = int(time.time())
    _insert(conn, EK_CHANGE_OUTCOME, now - 3 * _DAY_S)
    assert census_health_report(conn) == {"days_since_last_change_outcome": 3}


def test_reports_days_since_the_most_recent_change_outcome():
    conn = _conn_with_evidence()
    now = int(time.time())
    _insert(conn, EK_CHANGE_OUTCOME, now - 10 * _DAY_S)
    _insert(conn, EK_CHANGE_OUTCOME, now - 2 * _DAY_S)  # MAX(ts) wins
    assert census_health_report(conn) == {"days_since_last_change_outcome": 2}


def test_closed_conn_degrades_to_none():
    # The RULE-2 fault target: any store fault degrades to None, never raises (Law 6).
    conn = _conn_with_evidence()
    conn.close()
    assert census_health_report(conn) == {"days_since_last_change_outcome": None}


def test_health_omits_census_health_by_default():
    # Byte-inert: flag omitted ⇒ key absent (dropping the flag gate ⇒ the always-emits mutation).
    server, _ = build_real_server()
    assert "census_health" not in content(tool_call(server, "hive_health", {}))


def test_health_surfaces_census_health_on_flag():
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {"include_census_health": True}))
    assert "census_health" in snap
    assert snap["census_health"] == {"days_since_last_change_outcome": None}  # fresh store


def test_census_health_flag_is_advertised():
    # advertised == enforced: the flag joins its six include_* siblings in both the description
    # and the inputSchema, and the (capped) description still fits under METADATA_FIELD_LIMIT.
    from hive.app.onboard_ref import METADATA_FIELD_LIMIT
    from hive.app.tool_defs import TOOL_DEFINITIONS

    health = next(t for t in TOOL_DEFINITIONS if t["name"] == "hive_health")
    assert "include_census_health" in health["description"]
    assert health["inputSchema"]["properties"]["include_census_health"] == {"type": "boolean"}
    assert len(health["description"]) <= METADATA_FIELD_LIMIT
