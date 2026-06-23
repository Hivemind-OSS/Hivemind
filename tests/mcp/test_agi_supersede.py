"""AGI_OVERRIDE on the resolution door (hive_supersede). The SAME boundary gate as the
establish door: under AGI_MODE OFF the reserved ``approved_by="AGI_OVERRIDE"`` sentinel is
REFUSED fail-closed (nothing retired); under AGI_MODE ON the agent self-authorizes the
retirement (the supersede audit records actor=AGI_OVERRIDE, byte-distinguishable). A
human-named vouch is byte-identical in both modes."""
from __future__ import annotations

from hive.domain.agi import AGI_OVERRIDE
from tests.mcp._helpers import build_real_server, content, tool_call, write_text


def _two(server):
    a = write_text(server, "deploy with make deploy")["id"]
    b = write_text(server, "deploy with ship.sh since the migration")["id"]
    return a, b


def test_off_plus_sentinel_actor_is_refused_nothing_retired():
    server, _ = build_real_server()                       # OFF
    loser, winner = _two(server)
    out = content(tool_call(server, "hive_supersede",
                            {"loser": loser, "winner": winner, "approved_by": AGI_OVERRIDE}))
    assert out["status"] == "refused"
    assert server.store.get_episode(loser).trust == "established"   # NOT retired


def test_on_plus_sentinel_supersedes_with_override_actor():
    server, _ = build_real_server(agi_mode=True)          # ON
    loser, winner = _two(server)
    out = content(tool_call(server, "hive_supersede",
                            {"loser": loser, "winner": winner, "approved_by": AGI_OVERRIDE}))
    assert out["status"] == "superseded"
    ep = server.store.get_episode(loser)
    assert ep.trust == "deprecated" and ep.superseded_by == winner
    audit = server.store.conn.execute(
        "SELECT actor FROM evidence_events WHERE episode_id=? AND kind='supersede'",
        (loser,)).fetchall()
    assert len(audit) == 1 and audit[0]["actor"] == AGI_OVERRIDE


def test_human_supersede_unchanged_off_mode():
    server, _ = build_real_server()                       # OFF
    loser, winner = _two(server)
    out = content(tool_call(server, "hive_supersede",
                            {"loser": loser, "winner": winner, "approved_by": "human"}))
    assert out["status"] == "superseded"
    assert server.store.get_episode(loser).trust == "deprecated"


def test_human_supersede_unchanged_on_mode():
    server, _ = build_real_server(agi_mode=True)          # ON — a human vouch is not affected
    loser, winner = _two(server)
    out = content(tool_call(server, "hive_supersede",
                            {"loser": loser, "winner": winner, "approved_by": "human"}))
    assert out["status"] == "superseded"
    audit = server.store.conn.execute(
        "SELECT actor FROM evidence_events WHERE episode_id=? AND kind='supersede'",
        (loser,)).fetchall()
    assert audit[0]["actor"] == "human"                   # human actor, not the override
