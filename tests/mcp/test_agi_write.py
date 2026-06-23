"""AGI_OVERRIDE on the establish door (hive_write). Under AGI_MODE OFF (default) the reserved
``approved_by="AGI_OVERRIDE"`` sentinel is REFUSED fail-closed (0 rows) — the override is
unconstructable in the OFF build (Law 1/Law 6). Under AGI_MODE ON the agent self-authorizes:
the write establishes, the durable row is byte-DISTINGUISHABLE (``approved_by="AGI_OVERRIDE"``
+ ``provenance="agent_reasoned"``, the honest under-claim — Law 2/INV-2). A human-named vouch
is byte-IDENTICAL in both modes (the flag only governs the sentinel)."""
from __future__ import annotations

from hive.domain.agi import AGI_OVERRIDE
from tests.mcp._helpers import build_real_server, content, tool_call, write_text


# ── OFF + sentinel ⇒ refused, nothing written ──────────────────────────────────
def test_off_plus_sentinel_is_refused_zero_rows():
    server, _ = build_real_server()                       # agi_mode OFF (default)
    out = write_text(server, "an agent self-vouches while off", approved_by=AGI_OVERRIDE)
    assert out["status"] == "refused"
    assert "id" not in out                                # nothing established
    assert server.store.counts() == (0, 0)               # 0 rows: not approved, not pending


# ── ON + sentinel ⇒ established, byte-distinguishable durable row ───────────────
def test_on_plus_sentinel_establishes_with_override_marker():
    server, _ = build_real_server(agi_mode=True)
    text = "an agent-reasoned fact the agent self-authorized"
    out = write_text(server, text, approved_by=AGI_OVERRIDE)
    assert out["status"] == "approved"
    assert out["approved_by"] == AGI_OVERRIDE             # the override actor stamped on the wire
    ep = server.store.get_episode(out["id"])
    assert ep.approved_by == AGI_OVERRIDE                 # durable, byte-distinguishable
    assert ep.provenance == "agent_reasoned"             # under-claims origin (NOT human)
    assert ep.trust == "established"                      # served immediately like a human vouch
    # established ⇒ immediately recallable
    env = content(tool_call(server, "hive_recall", {"query": text}))
    assert env["abstained"] is False
    assert out["id"] in {h["episode_id"] for h in env["reference_context"]}


# ── a human-named vouch is byte-identical in BOTH modes (flag governs only sentinel) ─
def test_human_vouch_unchanged_off_mode():
    server, _ = build_real_server()                       # OFF
    w = write_text(server, "a human-authored convention here", approved_by="alice")
    assert w["status"] == "approved" and w["approved_by"] == "alice"
    assert server.store.get_episode(w["id"]).provenance == "human"


def test_human_vouch_unchanged_on_mode():
    server, _ = build_real_server(agi_mode=True)          # ON
    w = write_text(server, "a human-authored convention here", approved_by="alice")
    assert w["status"] == "approved" and w["approved_by"] == "alice"
    # the ON flag does NOT relabel a human vouch — provenance stays human (byte-inert).
    assert server.store.get_episode(w["id"]).provenance == "human"


# ── replaces= under override retires its target atomically (actor=AGI_OVERRIDE) ──
def test_replaces_under_override_retires_atomically():
    server, clock = build_real_server(agi_mode=True)
    old = write_text(server, "the stale convention", approved_by="alice")["id"]
    new = write_text(server, "the corrected convention", approved_by=AGI_OVERRIDE,
                     replaces=old)
    assert new["status"] == "approved"
    assert new["superseded"] == old                       # the target retired in the same call
    ep_old = server.store.get_episode(old)
    assert ep_old.trust == "deprecated" and ep_old.superseded_by == new["id"]
    # the retirement audit records the override actor (the byte-distinguishable retire vouch)
    audit = server.store.conn.execute(
        "SELECT actor FROM evidence_events WHERE episode_id=? AND kind='supersede'",
        (old,)).fetchall()
    assert len(audit) == 1 and audit[0]["actor"] == AGI_OVERRIDE


def test_off_plus_sentinel_with_replaces_retires_nothing():
    # the fail-closed refusal also leaves the named replaces= target untouched (0 rows total).
    server, _ = build_real_server()                       # OFF
    old = write_text(server, "a real memory", approved_by="alice")["id"]
    out = write_text(server, "an override correction while off", approved_by=AGI_OVERRIDE,
                     replaces=old)
    assert out["status"] == "refused"
    assert server.store.get_episode(old).trust == "established"   # target NOT retired
