"""hive_supersede (v3): the retirement-with-replacement verb — agent-called, MACHINE-
GATED (no approver exists). A qualified call retires the loser in favor of the winner
(actor = the calling identity) and de-serves it; an unqualified call is the §3.2 benign
noop; unknown-id / self-supersede keep their distinct noop shape. The deep gate matrix
lives in test_retirement_gate_boundary — this file pins the verb's envelope + effects."""

from __future__ import annotations

import json

from tests.mcp._helpers import (
    build_real_server,
    content,
    is_error,
    tool_call,
    write_text,
)


def _ids(server):
    a = write_text(server, "deploy with make deploy")["id"]
    b = write_text(server, "deploy with ship.sh since the migration")["id"]
    return a, b


def _qualify(server, eid):
    """Plant a qualifying machine signal (a server-written verified-hurt row)."""
    server.store.insert_audit(
        eid,
        "outcome_verified_hurt",
        "census",
        100,
        json.dumps({"stamp": {"head_sha": "c" * 40}}),
    )


def test_supersede_qualified_retires_loser_and_stamps_actor():
    server, _ = build_real_server()
    loser, winner = _ids(server)
    _qualify(server, loser)
    out = content(
        tool_call(server, "hive_supersede", {"loser": loser, "winner": winner})
    )
    assert out["status"] == "superseded"
    assert out["loser"] == loser and out["winner"] == winner
    assert "approved_by" not in out  # no approver field exists in v3
    assert out["signals"]  # the qualifying signal(s) named
    # the loser is deprecated, points at the winner, and is OUT of serving
    ep = server.store.get_episode(loser)
    assert ep.trust == "deprecated" and ep.superseded_by == winner
    # the retirement is attributed to the CALLING identity (the process default here)
    actors = [
        r["actor"]
        for r in server.store.conn.execute(
            "SELECT actor FROM evidence_events WHERE episode_id=? AND kind='supersede'",
            (loser,),
        ).fetchall()
    ]
    assert actors and all(a == "agent" for a in actors)


def test_supersede_loser_no_longer_recalled():
    server, _ = build_real_server()
    loser, winner = _ids(server)
    _qualify(server, loser)
    tool_call(server, "hive_supersede", {"loser": loser, "winner": winner})
    r = content(tool_call(server, "hive_recall", {"query": "deploy with make deploy"}))
    served = {h["episode_id"] for h in r["reference_context"]}
    assert loser not in served  # the retired version is gone from recall


def test_supersede_unqualified_is_gate_noop():
    server, _ = build_real_server()
    loser, winner = _ids(server)  # healthy, evidence-less
    out = content(
        tool_call(server, "hive_supersede", {"loser": loser, "winner": winner})
    )
    assert out["status"] == "noop" and out["signals"] == []
    assert "no qualifying machine signal" in out["reason"]
    assert server.store.get_episode(loser).trust == "provisional"


def test_supersede_unknown_id_is_noop():
    server, _ = build_real_server()
    loser, _winner = _ids(server)
    _qualify(server, loser)
    out = content(tool_call(server, "hive_supersede", {"loser": loser, "winner": 9999}))
    assert out["status"] == "noop"  # unknown winner ⇒ nothing retired
    assert server.store.get_episode(loser).trust == "provisional"


def test_supersede_self_is_noop():
    server, _ = build_real_server()
    a, _b = _ids(server)
    _qualify(server, a)
    out = content(tool_call(server, "hive_supersede", {"loser": a, "winner": a}))
    assert out["status"] == "noop"
    assert server.store.get_episode(a).trust == "provisional"


def test_supersede_idempotent_rerun_no_duplicate_audit():
    server, _ = build_real_server()
    loser, winner = _ids(server)
    _qualify(server, loser)
    assert (
        content(
            tool_call(server, "hive_supersede", {"loser": loser, "winner": winner})
        )["status"]
        == "superseded"
    )
    n_audits = server.store.conn.execute(
        "SELECT COUNT(*) AS c FROM evidence_events WHERE episode_id=?", (loser,)
    ).fetchone()["c"]
    again = content(
        tool_call(server, "hive_supersede", {"loser": loser, "winner": winner})
    )
    assert again["status"] == "superseded"  # idempotent echo
    assert (
        server.store.conn.execute(
            "SELECT COUNT(*) AS c FROM evidence_events WHERE episode_id=?", (loser,)
        ).fetchone()["c"]
        == n_audits
    )  # no duplicate audit rows


def test_supersede_schema_belt_requires_both_ids():
    server, _ = build_real_server()
    a, b = _ids(server)
    # missing winner ⇒ rejected before the handler
    assert is_error(tool_call(server, "hive_supersede", {"loser": a}))
    # a non-integer loser ⇒ rejected
    assert is_error(tool_call(server, "hive_supersede", {"loser": "x", "winner": b}))
    # NO approver is required (the field does not exist) — both ids alone pass the belt
    assert not is_error(tool_call(server, "hive_supersede", {"loser": a, "winner": b}))
