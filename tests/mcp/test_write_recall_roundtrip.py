"""M06 client-gated capture: a clean hive_write is scanned, then stored APPROVED and
indexed in ONE call (no pending queue) — immediately recallable through the MCP surface.
Covers the write→recall round-trip, the approved_by attestation, dedup, and the
approve-failure relay (a store CAS failure surfaces as a tool error, never a
silently-pending row reported as written)."""
from __future__ import annotations

from tests.mcp._helpers import build_real_server, content, is_error, tool_call, write_text


# ── write → immediately recallable (collapsed admission, no separate approve) ──
def test_clean_write_is_immediately_recallable():
    server, _ = build_real_server()
    text = "use BEGIN IMMEDIATE for the single writer lane"
    w = write_text(server, text, approved_by="alice")
    assert w["status"] == "approved"
    assert w["approved_by"] == "alice"
    assert isinstance(w["id"], int)
    # NO approve call — recall returns it on the very next query
    env = content(tool_call(server, "hive_recall", {"query": text}))
    assert env["abstained"] is False
    assert w["id"] in {h["episode_id"] for h in env["reference_context"]}


def test_written_row_is_approved_with_its_approver():
    server, _ = build_real_server()
    w = write_text(server, "an approved memory carries its approver", approved_by="bob")
    ep = server.store.get_episode(w["id"])
    assert ep.status == "approved" and ep.approved_by == "bob" and ep.value is not None
    assert server.store.counts() == (1, 0)             # 1 approved, 0 pending


def test_write_dedup_returns_same_id_no_second_row():
    server, _ = build_real_server()
    a = write_text(server, "same durable insight")
    b = write_text(server, "same durable insight")
    assert a["id"] == b["id"]
    assert server.store.counts()[0] == 1               # exactly one approved row


# ── approve-failure relay: a store CAS failure is a tool error, never a phantom write ──
def test_store_approve_failure_surfaces_as_tool_error_and_leaves_no_row():
    server, _ = build_real_server()
    server.store.approve = lambda *a, **k: False       # simulate a lost-update CAS race
    resp = tool_call(server, "hive_write",
                     {"text": "will fail to approve", "approved_by": "u"})
    assert is_error(resp)
    # the dangling staged row was dropped — nothing left approved OR pending
    assert server.store.counts() == (0, 0)
