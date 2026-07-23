"""The ``solo_hint`` in hive_health: single-IDENTITY traffic wasting demand
(≥ demand_m window misses, ≤1 distinct identity) is a silent promotion stall; the hint
converts it into a self-describing, actionable one (register distinct agents). Fires
PRECISELY: absent on an empty/quiet store, absent when autonomy is disabled, absent with
≥2 identities. With the solo-mode bypass removed, this is the SOLE identity-collapse signal."""

from __future__ import annotations

from hive.app.config import AutonomyConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from tests.mcp._helpers import build_real_server, content, tool_call


def _miss_recalls(server, n: int, *, agent: str = "agent"):
    """Drive n empty-store recalls (each records one vector-bearing no_match
    miss) under the given identity."""
    for i in range(n):
        server.handle(
            MCPRequest(
                100 + i,
                "tools/call",
                {"name": "hive_recall", "arguments": {"query": f"unanswered need {i}"}},
            ),
            identity=ServerIdentity("default", agent),
        )


def _health(server) -> dict:
    return content(tool_call(server, "hive_health", {}))


def test_health_solo_hint_fires_precisely():
    server, _clock = build_real_server()
    assert "solo_hint" not in _health(server)  # empty store: no hint
    _miss_recalls(server, 2)
    assert "solo_hint" not in _health(server)  # below the demand_m floor
    _miss_recalls(server, 1)
    snap = _health(server)  # 3 misses, ONE identity
    assert (
        "X-Hive-Agent-Id" in snap["solo_hint"]
    )  # the remedy: distinct per-session ids
    assert "hive connect" in snap["solo_hint"]
    assert "SOLO_MODE" not in snap["solo_hint"]  # the deleted knob is never advised


def test_health_solo_hint_absent_with_identity_diversity():
    server, _clock = build_real_server()
    _miss_recalls(server, 3, agent="seat-a")
    _miss_recalls(server, 1, agent="seat-b")  # a second identity exists
    assert "solo_hint" not in _health(server)


def test_health_solo_hint_absent_when_autonomy_disabled():
    off_server, _ = build_real_server(autonomy=AutonomyConfig(enabled=False))
    _miss_recalls(off_server, 3)  # autonomy off: no misses recorded
    assert "solo_hint" not in _health(off_server)
