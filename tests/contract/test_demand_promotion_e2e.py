"""Demand promotion (quarantined -> provisional) through the real MCP handler.
Nothing is seeded — every miss is a REAL hive_recall that abstained; nothing is
written into recall_misses by hand, and identity comes from the transport
identity the handler actually reads.
"""

from __future__ import annotations

import json

import pytest

from hive.app.config import AutonomyConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from tests.mcp._helpers import build_real_server

CAP = "the sync watermark is sync:<repo>:last_tip, never a global key"


def call(server, name, args, *, agent):
    resp = server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("t", agent),
    )
    return json.loads(resp.result["content"][0]["text"])


def trust(server, eid):
    return server.store.get_episode(eid).trust


def miss(server, query, agent):
    return call(server, "hive_recall", {"query": query}, agent=agent)


@pytest.fixture
def server():
    s, _clock = build_real_server(t0=1_000_000)
    return s


def test_fleet_demand_promotes_and_the_row_then_serves(server):
    env = call(server, "hive_capture", {"text": CAP}, agent="agent-a")
    assert env["status"] == "quarantined", env
    eid = env["id"]
    assert trust(server, eid) == "quarantined", (
        "not yet promoted: no non-writer miss has landed yet"
    )

    # a quarantined row is structurally unservable — even the ONE miss that
    # clears the live demand_m=1 floor still reads empty here, because serving
    # for a query is decided before that same query's miss can promote anything
    assert miss(server, CAP, "agent-b").get("reference_context", []) == []
    assert trust(server, eid) == "provisional", (
        "one non-writer miss must promote at the live demand_m=1 floor"
    )

    served = miss(server, CAP, "agent-c")
    ids = [h["episode_id"] for h in served.get("reference_context", [])]
    assert eid in ids, f"the promoted row does not serve: {served}"
    hit = next(h for h in served["reference_context"] if h["episode_id"] == eid)
    assert hit["trust"] == "provisional"

    audits = [
        dict(r)
        for r in server.store.conn.execute(
            "SELECT kind, actor, payload FROM evidence_events "
            "WHERE episode_id=? AND kind='promote'",
            (eid,),
        )
    ]
    assert len(audits) == 1, audits
    body = json.loads(audits[0]["payload"])
    assert body["rule"] == "demand" and body["n_misses"] >= 1
    assert body["n_other_identities"] >= 1


def test_writer_only_demand_never_promotes(server):
    env = call(server, "hive_capture", {"text": CAP}, agent="agent-a")
    eid = env["id"]
    for _ in range(8):
        miss(server, CAP, "agent-a")
    assert trust(server, eid) == "quarantined", (
        "the writer manufactured both supply and demand — anti-gaming clause dead"
    )


def test_writer_misses_do_not_count_toward_the_demand_bar(tmp_path):
    """THEORY §9 #6: subtract self-identity from the demand count — the writer's
    own misses contribute nothing toward ``demand_m``. The rule under test is
    ``n_other >= demand_m``; ``demand_m=2`` is set explicitly here so the
    assertion is about the RULE, independent of the operator-config default."""
    server, _clock = build_real_server(
        t0=1_000_000, autonomy=AutonomyConfig(demand_m=2)
    )
    env = call(server, "hive_capture", {"text": CAP}, agent="agent-a")
    eid = env["id"]
    miss(server, CAP, "agent-a")
    miss(server, CAP, "agent-a")
    assert trust(server, eid) == "quarantined"

    miss(server, CAP, "agent-b")  # ONE foreign ask: n_other=1 < demand_m=2
    assert trust(server, eid) == "quarantined", (
        "one non-writer identity is not enough demand at demand_m=2 once the "
        "writer's own misses stop counting toward the bar"
    )
    assert (
        server.store.conn.execute(
            "SELECT COUNT(*) c FROM evidence_events WHERE episode_id=? AND kind='promote'",
            (eid,),
        ).fetchone()["c"]
        == 0
    ), "no promotion may be audited when the bar was never actually cleared"


def test_two_other_identities_promote_and_the_row_then_serves(tmp_path):
    """The healthy twin of the test above: TWO non-writer identities meet the
    demand_m=2 bar on their own, with no help from the writer's own misses, and
    the row then serves."""
    server, _clock = build_real_server(
        t0=1_000_000, autonomy=AutonomyConfig(demand_m=2)
    )
    env = call(server, "hive_capture", {"text": CAP}, agent="agent-a")
    eid = env["id"]
    miss(server, CAP, "agent-b")
    miss(server, CAP, "agent-c")
    assert trust(server, eid) == "provisional", (
        "two non-writer misses must promote at demand_m=2"
    )

    served = miss(server, CAP, "agent-d")
    ids = [h["episode_id"] for h in served.get("reference_context", [])]
    assert eid in ids, f"the promoted row does not serve: {served}"


def test_out_of_partition_misses_never_count(server):
    server.store.repo_add(
        name="alpha", url="https://x.invalid/a.git", canonical_ref="", added_ts=0
    )
    server.store.repo_add(
        name="beta", url="https://x.invalid/b.git", canonical_ref="", added_ts=0
    )
    env = call(
        server, "hive_capture", {"text": CAP, "repos": ["alpha"]}, agent="agent-a"
    )
    eid = env["id"]
    for agent in ("agent-b", "agent-c", "agent-d", "agent-e"):
        call(server, "hive_recall", {"query": CAP, "repos": ["beta"]}, agent=agent)
    assert trust(server, eid) == "quarantined", (
        "beta-scoped demand promoted an alpha-scoped candidate"
    )
    for agent in ("agent-b", "agent-c", "agent-d"):
        call(server, "hive_recall", {"query": CAP, "repos": ["alpha"]}, agent=agent)
    assert trust(server, eid) == "provisional"


def test_ttl_decay_retires_a_lapsed_quarantine(server):
    s, clock = build_real_server(t0=1_000_000)
    env = call(s, "hive_capture", {"text": CAP}, agent="agent-a")
    eid = env["id"]
    clock.advance(15 * 86_400)  # quarantine_ttl_days=14
    swept = s.store.sweep_decayed(
        now=clock.now(), q_ttl_s=14 * 86_400, p_ttl_s=45 * 86_400
    )
    assert swept, swept
    assert trust(s, eid) == "deprecated"
    rows = [
        dict(r)
        for r in s.store.conn.execute(
            "SELECT kind FROM evidence_events WHERE episode_id=?", (eid,)
        )
    ]
    assert any(r["kind"] == "ttl_expired" for r in rows), rows
