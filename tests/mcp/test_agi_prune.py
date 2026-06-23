"""hive_prune — the bare-retirement door (the SECOND retirement owner beside hive_supersede).
Human-vouched by default; agent-self-authorized under AGI_MODE (approved_by=AGI_OVERRIDE),
gated by the SAME fail-closed _override_refused belt as write/supersede. "Prune" = trust→
deprecated with NO replacement, retained in the audit ledger (never a hard delete), de-recalled."""
from __future__ import annotations

from hive.domain.agi import AGI_OVERRIDE
from tests.mcp._helpers import build_real_server, content, is_error, tool_call, write_text


def test_human_prune_deprecates_and_de_recalls():
    server, _ = build_real_server()
    text = "a misleading memory a human kills"
    eid = write_text(server, text, approved_by="alice")["id"]
    out = content(tool_call(server, "hive_prune", {"episode_id": eid, "approved_by": "human"}))
    assert out["status"] == "pruned" and out["episode_id"] == eid
    ep = server.store.get_episode(eid)
    assert ep.trust == "deprecated" and ep.superseded_by is None    # bare retirement
    # the prune audit records the human actor
    audit = server.store.conn.execute(
        "SELECT actor FROM evidence_events WHERE episode_id=? AND kind='prune'",
        (eid,)).fetchall()
    assert len(audit) == 1 and audit[0]["actor"] == "human"
    # de-recalled
    r = content(tool_call(server, "hive_recall", {"query": text}))
    assert eid not in {h["episode_id"] for h in r["reference_context"]}


def test_off_plus_sentinel_is_refused_nothing_retired():
    server, _ = build_real_server()                       # OFF
    eid = write_text(server, "a memory an agent tries to kill while off", approved_by="alice")["id"]
    out = content(tool_call(server, "hive_prune",
                            {"episode_id": eid, "approved_by": AGI_OVERRIDE}))
    assert out["status"] == "refused"
    assert server.store.get_episode(eid).trust == "established"     # untouched


def test_on_agent_prune_with_override_actor():
    server, _ = build_real_server(agi_mode=True)          # ON
    eid = write_text(server, "an agent-killed poison", approved_by="alice")["id"]
    out = content(tool_call(server, "hive_prune",
                            {"episode_id": eid, "approved_by": AGI_OVERRIDE}))
    assert out["status"] == "pruned"
    assert server.store.get_episode(eid).trust == "deprecated"
    audit = server.store.conn.execute(
        "SELECT actor FROM evidence_events WHERE episode_id=? AND kind='prune'",
        (eid,)).fetchall()
    assert audit[0]["actor"] == AGI_OVERRIDE              # byte-distinguishable override actor


def test_unknown_id_is_noop():
    server, _ = build_real_server()
    out = content(tool_call(server, "hive_prune", {"episode_id": 9999, "approved_by": "human"}))
    assert out["status"] == "noop"


def test_second_prune_is_idempotent_noop():
    server, _ = build_real_server()
    eid = write_text(server, "prune me twice over the wire", approved_by="alice")["id"]
    assert content(tool_call(server, "hive_prune",
                             {"episode_id": eid, "approved_by": "human"}))["status"] == "pruned"
    assert content(tool_call(server, "hive_prune",
                             {"episode_id": eid, "approved_by": "human"}))["status"] == "noop"


def test_prune_schema_belt_requires_episode_id_and_approver():
    server, _ = build_real_server()
    eid = write_text(server, "a memory", approved_by="alice")["id"]
    assert is_error(tool_call(server, "hive_prune", {"episode_id": eid}))        # no approver
    assert is_error(tool_call(server, "hive_prune", {"approved_by": "human"}))   # no id
    assert is_error(tool_call(server, "hive_prune",
                              {"episode_id": "x", "approved_by": "human"}))       # non-int id
