"""hive_outcome — the pure evidence verb. An agent logs which recalled episode_ids materially
HELPED or HURT its task; the server records ``outcome_helped`` / ``outcome_hurt`` evidence_events
audits and NOTHING else. It holds no establish/retire handle, so it structurally CANNOT mutate
trust (like ConflictFlagService cannot resolve — O7-safe by construction). Ids + audit kinds
only, no memory text (Law 4). v3: hurt rows are §3.2 gate feed — evidence, never an action."""
from __future__ import annotations

import json

from hive.domain.evidence_kinds import EVIDENCE_KINDS
from tests.mcp._helpers import build_real_server, content, is_error, tool_call, write_text


def _kinds_for(server, eid):
    return [r["kind"] for r in server.store.conn.execute(
        "SELECT kind FROM evidence_events WHERE episode_id=? ORDER BY id", (eid,)).fetchall()]


def test_records_helped_and_hurt_audits_for_known_ids():
    server, _ = build_real_server()
    a = write_text(server, "a memory that helped")["id"]
    b = write_text(server, "a memory that hurt")["id"]
    out = content(tool_call(server, "hive_outcome", {"helped": [a], "hurt": [b]}))
    assert out["status"] == "recorded"
    assert out["helped"] == [a] and out["hurt"] == [b]
    assert "outcome_helped" in _kinds_for(server, a)
    assert "outcome_hurt" in _kinds_for(server, b)
    # the write sites emit registry members (evidence_events has no DDL CHECK)
    assert set(_kinds_for(server, a) + _kinds_for(server, b)) <= EVIDENCE_KINDS
    # the recording agent is the audit actor
    actor = server.store.conn.execute(
        "SELECT actor FROM evidence_events WHERE episode_id=? AND kind='outcome_helped'",
        (a,)).fetchone()["actor"]
    assert actor == "agent"                               # the ServerIdentity agent_id


def test_skips_unknown_ids_without_crashing():
    server, _ = build_real_server()
    a = write_text(server, "the only real memory")["id"]
    out = content(tool_call(server, "hive_outcome", {"helped": [a, 9999], "hurt": [4242]}))
    assert out["helped"] == [a]                           # 9999 skipped (unknown)
    assert out["hurt"] == []                              # 4242 skipped
    # no audit row was forged for the unknown ids
    assert server.store.conn.execute(
        "SELECT COUNT(*) AS c FROM evidence_events WHERE episode_id IN (9999, 4242)"
    ).fetchone()["c"] == 0


def test_trust_is_unchanged_after_outcome():
    server, _ = build_real_server()
    a = write_text(server, "a provisional memory reported as hurt")["id"]
    assert server.store.get_episode(a).trust == "provisional"
    tool_call(server, "hive_outcome", {"hurt": [a]})
    # evidence changes NO trust — the row is byte-identical in its trust axis
    assert server.store.get_episode(a).trust == "provisional"


def test_no_memory_text_on_wire_or_in_audit():
    server, _ = build_real_server()
    text = "the distinctive marbled marmoset memory content must never leak"
    a = write_text(server, text)["id"]
    resp = tool_call(server, "hive_outcome", {"helped": [a]})
    assert text not in json.dumps(content(resp))          # not on the wire
    payloads = [r["payload"] for r in server.store.conn.execute(
        "SELECT payload FROM evidence_events WHERE episode_id=? AND kind='outcome_helped'",
        (a,)).fetchall()]
    assert all(text not in p for p in payloads)           # not in the audit (Law 4)


def test_recorded_envelope_exact_shape():
    # the exact v3 envelope — no beacon, no approver, ids echoed back
    server, _ = build_real_server()
    a = write_text(server, "a recorded memory")["id"]
    out = content(tool_call(server, "hive_outcome", {"helped": [a]}))
    assert out == {"status": "recorded", "helped": [a], "hurt": []}


def test_empty_call_records_nothing():
    server, _ = build_real_server()
    out = content(tool_call(server, "hive_outcome", {}))   # both arrays optional
    assert out == {"status": "recorded", "helped": [], "hurt": []}


def test_schema_belt_accepts_arrays_rejects_non_array():
    server, _ = build_real_server()
    a = write_text(server, "schema test memory")["id"]
    # an array passes the belt (reaches the handler)
    assert not is_error(tool_call(server, "hive_outcome", {"helped": [a]}))
    # a non-array helped is schema-rejected before the handler
    assert is_error(tool_call(server, "hive_outcome", {"helped": a}))
