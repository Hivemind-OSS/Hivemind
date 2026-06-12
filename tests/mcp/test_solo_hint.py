"""CV1 §3.5 — the ``solo_hint`` in hive_health: single-seat traffic wasting
demand (≥ demand_m window misses, ≤1 distinct identity) is a silent autonomy
stall; the hint converts it into a self-describing one. Fires PRECISELY:
absent on an empty/quiet store, absent in solo_mode, absent with ≥2 identities."""
from __future__ import annotations

from hive.app.config import AutonomyConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from tests.mcp._helpers import build_real_server, content, tool_call


def _miss_recalls(server, n: int, *, agent: str = "agent"):
    """Drive n empty-store recalls (each records one vector-bearing no_match
    miss) under the given identity."""
    for i in range(n):
        server.handle(MCPRequest(100 + i, "tools/call",
                                 {"name": "hive_recall",
                                  "arguments": {"query": f"unanswered need {i}"}}),
                      identity=ServerIdentity("default", agent))


def _health(server) -> dict:
    return content(tool_call(server, "hive_health", {}))


def test_health_solo_hint_fires_precisely():
    server, _clock = build_real_server()
    assert "solo_hint" not in _health(server)            # empty store: no hint
    _miss_recalls(server, 2)
    assert "solo_hint" not in _health(server)            # below the demand_m floor
    _miss_recalls(server, 1)
    snap = _health(server)                               # 3 misses, ONE identity
    assert "SOLO_MODE" in snap["solo_hint"]
    assert "hive token <seat>" in snap["solo_hint"]


def test_health_solo_hint_absent_with_identity_diversity():
    server, _clock = build_real_server()
    _miss_recalls(server, 3, agent="seat-a")
    _miss_recalls(server, 1, agent="seat-b")             # a second seat exists
    assert "solo_hint" not in _health(server)


def test_health_solo_hint_absent_in_solo_mode_and_disabled():
    solo_server, _ = build_real_server(autonomy=AutonomyConfig(solo_mode=True))
    _miss_recalls(solo_server, 3)
    assert "solo_hint" not in _health(solo_server)       # already solo: nothing to say
    off_server, _ = build_real_server(autonomy=AutonomyConfig(enabled=False))
    _miss_recalls(off_server, 3)                         # autonomy off: no misses recorded
    assert "solo_hint" not in _health(off_server)
