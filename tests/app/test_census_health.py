"""The passive census staleness signal — hive_health(include_census_health=true).

A fail-open report of how long ago the last SHA-bound change_outcome evidence row
landed. Serves the raw day-count (no invented staleness threshold, THEORY §9 #14) so an
operator can tell whether the census feed has gone dark — plus, when the server-side
sync daemon has left ``sync:*`` meta in the store, a ``sync`` observability block (U1
intent 12: sync state rides the existing flag; no new CLI verb). With sync configured,
darkness is REINTERPRETED as ``status: "sync stalled"`` (census is server-automatic, so
"hooks unwired" is no longer the explanation).
"""
from __future__ import annotations

import sqlite3
import time

from hive.app.census_health import census_health_report
from hive.app.sync import (META_BACKFILLED_TOTAL, META_CANDIDATES_EVALUATED,
                           META_LAST_ERROR, META_LAST_TIP)
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


def _conn_with_meta() -> sqlite3.Connection:
    """The real store's shape for this module's reads: evidence_events + the plain
    key/value ``meta`` table (mirrors store_sqlite's ``meta(key TEXT PRIMARY KEY,
    value TEXT)``)."""
    conn = _conn_with_evidence()
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


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


# ── the sync observability block (U1 intent 12: sync state rides census health) ──

def test_sync_block_absent_without_sync_meta():
    # No sync:* meta ⇒ the report keeps TODAY'S exact shape — a non-sync deployment's
    # census_health payload never grows a sync block (the byte-inert family: key present
    # only when the condition is true).
    conn = _conn_with_meta()
    assert census_health_report(conn) == {"days_since_last_change_outcome": None}


def test_sync_block_absent_when_meta_table_missing():
    # The legacy/fault shape: no meta table reads exactly like no sync meta, and the
    # base day-count still serves (per-leg fail-open — the sync probe can never break
    # the base report, Law 6).
    conn = _conn_with_evidence()
    _insert(conn, EK_CHANGE_OUTCOME, int(time.time()) - 3 * _DAY_S)
    assert census_health_report(conn) == {"days_since_last_change_outcome": 3}


def test_sync_block_serves_the_seven_keys_when_configured():
    conn = _conn_with_meta()
    _insert(conn, EK_CHANGE_OUTCOME, int(time.time()) - _DAY_S)
    _set_meta(conn, META_LAST_TIP, "a" * 40)
    _set_meta(conn, META_LAST_ERROR, "mirror: _SyncFault: git clone failed")
    _set_meta(conn, META_CANDIDATES_EVALUATED, "4")
    _set_meta(conn, META_BACKFILLED_TOTAL, "7")
    report = census_health_report(conn)
    assert report["days_since_last_change_outcome"] == 1
    assert report["sync"] == {
        "configured": True,
        "tracked_ref": None,       # no sync:* meta key backs it — honest None, never invented
        "last_tip": "a" * 40,
        "last_sync_ts": None,      # the meta table carries no tick timestamp — honest None
        "last_error": "mirror: _SyncFault: git clone failed",
        "candidates_evaluated": 4,
        "backfilled_total": 7,
    }


def test_sync_configured_and_dark_reads_sync_stalled():
    # The reinterpretation (intent 12): with sync configured, a dark feed no longer
    # suggests unwired hooks (census is server-automatic) — it reads "sync stalled".
    # Dark stays threshold-free: no change_outcome evidence at all (THEORY §9 #14).
    conn = _conn_with_meta()
    _set_meta(conn, META_LAST_TIP, "a" * 40)
    report = census_health_report(conn)
    assert report["days_since_last_change_outcome"] is None
    assert report["sync"]["status"] == "sync stalled"


def test_sync_configured_and_live_carries_no_stalled_status():
    # status is present-only-when-dark (an unconditional attach is the mutation target).
    conn = _conn_with_meta()
    _insert(conn, EK_CHANGE_OUTCOME, int(time.time()))
    _set_meta(conn, META_LAST_TIP, "a" * 40)
    assert "status" not in census_health_report(conn)["sync"]


def test_sync_counters_absent_or_garbage_read_zero():
    # The counter tolerance mirrors sync's _bump_counter_locked: absent or non-digit
    # reads 0 — a count is never invented from junk.
    conn = _conn_with_meta()
    _set_meta(conn, META_LAST_TIP, "a" * 40)
    _set_meta(conn, META_BACKFILLED_TOTAL, "not-a-number")
    block = census_health_report(conn)["sync"]
    assert block["candidates_evaluated"] == 0
    assert block["backfilled_total"] == 0


def test_health_serves_sync_state_over_mcp_when_configured():
    # Intent 12 end-to-end: sync state rides hive_health(include_census_health=true) —
    # the EXISTING flag is the operator surface, no new CLI verb anywhere.
    server, _ = build_real_server()
    server.store.meta_set(META_LAST_TIP, "c" * 40)
    snap = content(tool_call(server, "hive_health", {"include_census_health": True}))
    block = snap["census_health"]["sync"]
    assert block["configured"] is True
    assert block["last_tip"] == "c" * 40
    assert block["status"] == "sync stalled"          # fresh feed: configured + dark


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
