"""Outcome reinforcement: hive_outcome -> evidence rows -> the surfaces that
consume them, all through the real MCP handler. Nothing is seeded — every
evidence row here is written by production code reacting to a real tool call.

Consumers driven here:
  * the suspect-consensus martingale clause (settled_wins union) — the only place
    a self-reported ``helped`` changes anything an operator sees.
  * the retirement gate's hurt clause (identity diversity) — covered in
    test_retirement_gate_e2e.py.
"""

from __future__ import annotations

import json

import pytest

from hive.app.config import SuspectConsensusConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from tests.mcp._helpers import build_real_server

CAP = "prefer BEGIN IMMEDIATE for the writer transaction in the sqlite store"


def call(server, name, args, *, agent="agent-a"):
    resp = server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("t", agent),
    )
    return json.loads(resp.result["content"][0]["text"])


def rows(server, eid, kind=None):
    sql = "SELECT kind, actor, ts, payload FROM evidence_events WHERE episode_id=?"
    args = [eid]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return [dict(r) for r in server.store.conn.execute(sql + " ORDER BY id", args)]


@pytest.fixture
def server():
    s, _c = build_real_server(t0=1_000_000, suspect_consensus=SuspectConsensusConfig())
    return s


def test_outcome_records_ids_only_under_the_calling_identity(server):
    env = call(server, "hive_write", {"text": CAP}, agent="agent-a")
    eid = env["id"]
    out = call(server, "hive_outcome", {"helped": [eid], "hurt": []}, agent="agent-z")
    assert out == {"status": "recorded", "helped": [eid], "hurt": []}

    helped = rows(server, eid, "outcome_helped")
    assert len(helped) == 1
    assert helped[0]["actor"] == "agent-z", "the reporter must be the audit actor"
    assert json.loads(helped[0]["payload"]) == {}, "no memory text may ride the audit"
    assert server.store.get_episode(eid).trust == "provisional", (
        "hive_outcome must hold NO trust handle"
    )


def test_unknown_ids_are_skipped_not_forged(server):
    out = call(server, "hive_outcome", {"helped": [999_999]})
    assert out["helped"] == []
    # a non-integer item is refused at the schema belt, before the handler
    resp = server.handle(
        MCPRequest(
            1, "tools/call", {"name": "hive_outcome", "arguments": {"helped": ["x"]}}
        ),
        identity=ServerIdentity("t", "agent-a"),
    )
    assert resp.result["isError"] is True
    assert (
        server.store.conn.execute(
            "SELECT COUNT(*) c FROM evidence_events WHERE kind='outcome_helped'"
        ).fetchone()["c"]
        == 0
    )


def test_self_reported_help_flips_the_martingale_warning_an_operator_reads(server):
    """A thin-independence promotion is flagged; one hive_outcome(helped=) clears
    the martingale half of that flag. This is the observable outcome-reinforcement
    consequence on the operator surface.

    At the live demand_m=1 floor, the modal promotion fires on the FIRST
    non-writer miss (k=1, n_eff/k == 1.0 — mathematically unflaggable), so a
    thin promotion has to be driven on purpose here: the writer's own correlated
    misses accumulate first (matched, but never counted toward n_other), and the
    one non-writer miss that actually clears the bar lands with those already in
    the window. It is k at promotion time that drives the flag, not demand_m.
    """
    env = call(server, "hive_capture", {"text": CAP}, agent="agent-a")
    eid = env["id"]
    call(server, "hive_recall", {"query": CAP}, agent="agent-a")  # self: no promotion
    call(
        server, "hive_recall", {"query": CAP}, agent="agent-a"
    )  # self again: k=2, n_other=0
    assert server.store.get_episode(eid).trust == "quarantined"

    call(server, "hive_recall", {"query": CAP}, agent="agent-b")  # 1st non-writer miss:
    # n_other=1 >= demand_m=1 -> promotes, with 3 IDENTICAL asks already
    # matched (2 self + 1 foreign) -> n_eff = 1
    assert server.store.get_episode(eid).trust == "provisional"

    health = call(server, "hive_health", {"include_suspect_consensus": True})
    items = {i["episode_id"]: i for i in health["suspect_consensus"]}
    assert eid in items, health
    assert items[eid]["martingale_warning"] is True, items[eid]
    assert items[eid]["n_eff"] == pytest.approx(1.0, abs=1e-6)

    call(server, "hive_outcome", {"helped": [eid]}, agent="agent-e")
    health = call(server, "hive_health", {"include_suspect_consensus": True})
    items = {i["episode_id"]: i for i in health["suspect_consensus"]}
    assert items[eid]["martingale_warning"] is False, (
        f"a settled win did not reach the martingale clause: {items[eid]}"
    )


def test_hurt_alone_never_changes_trust_or_serving(server):
    env = call(server, "hive_write", {"text": CAP}, agent="agent-a")
    eid = env["id"]
    for agent in ("a", "b", "c", "d", "e"):
        call(server, "hive_outcome", {"hurt": [eid]}, agent=agent)
    assert server.store.get_episode(eid).trust == "provisional"
    served = call(server, "hive_recall", {"query": CAP}, agent="agent-x")
    assert eid in [h["episode_id"] for h in served["reference_context"]], (
        "hurt evidence must not silently unserve a row; only the gate retires"
    )
