"""C5b — hive_supersede: the human-vouched resolution verb (ALWAYS on, not gated by
conflict.enabled). A thin trust-boundary wrapper over store.supersede: retire the loser in
favor of the winner, record the human vouch (actor=approved_by), and de-serve the loser. It
removes the write(replaces=) verbatim-text footgun for the candidate→resolution path."""
from __future__ import annotations

from tests.mcp._helpers import build_real_server, content, is_error, tool_call, write_text


def _ids(server):
    a = write_text(server, "deploy with make deploy")["id"]
    b = write_text(server, "deploy with ship.sh since the migration")["id"]
    return a, b


def test_supersede_retires_loser_and_records_vouch():
    server, _ = build_real_server()
    loser, winner = _ids(server)
    out = content(tool_call(server, "hive_supersede",
                            {"loser": loser, "winner": winner, "approved_by": "human"}))
    assert out["status"] == "superseded"
    assert out["loser"] == loser and out["winner"] == winner
    # the loser is deprecated, points at the winner, and is OUT of serving
    ep = server.store.get_episode(loser)
    assert ep.trust == "deprecated" and ep.superseded_by == winner
    # the human vouch is recorded with actor=approved_by
    audit = server.store.conn.execute(
        "SELECT actor FROM evidence_events WHERE episode_id=? AND kind='supersede'",
        (loser,)).fetchall()
    assert len(audit) == 1 and audit[0]["actor"] == "human"


def test_supersede_loser_no_longer_recalled():
    server, _ = build_real_server()
    loser, winner = _ids(server)
    tool_call(server, "hive_supersede",
              {"loser": loser, "winner": winner, "approved_by": "human"})
    r = content(tool_call(server, "hive_recall", {"query": "deploy with make deploy"}))
    served = {h["episode_id"] for h in r["reference_context"]}
    assert loser not in served            # the retired version is gone from recall


def test_supersede_unknown_id_is_noop():
    server, _ = build_real_server()
    loser, _winner = _ids(server)
    out = content(tool_call(server, "hive_supersede",
                            {"loser": loser, "winner": 9999, "approved_by": "human"}))
    assert out["status"] == "noop"        # unknown winner ⇒ nothing retired
    assert server.store.get_episode(loser).trust == "established"


def test_supersede_self_is_noop():
    server, _ = build_real_server()
    a, _b = _ids(server)
    out = content(tool_call(server, "hive_supersede",
                            {"loser": a, "winner": a, "approved_by": "human"}))
    assert out["status"] == "noop"
    assert server.store.get_episode(a).trust == "established"


def test_supersede_schema_belt_requires_all_three():
    server, _ = build_real_server()
    a, b = _ids(server)
    # missing approved_by ⇒ schema-rejected before the handler
    assert is_error(tool_call(server, "hive_supersede", {"loser": a, "winner": b}))
    # missing winner ⇒ rejected
    assert is_error(tool_call(server, "hive_supersede", {"loser": a, "approved_by": "h"}))
    # a non-integer loser ⇒ rejected
    assert is_error(tool_call(server, "hive_supersede",
                              {"loser": "x", "winner": b, "approved_by": "h"}))
