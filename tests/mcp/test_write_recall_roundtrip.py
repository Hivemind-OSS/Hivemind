"""M06 v3 write path: a clean hive_write is scanned, then stored trust=provisional and
indexed in ONE call (no approver, no pending queue) — immediately recallable through the
MCP surface. Covers the write→recall round-trip, the transport-resolved attribution,
dedup, and the complete-failure relay (a store CAS failure surfaces as a tool error,
never a silently-pending row reported as written)."""

from __future__ import annotations

from tests.mcp._helpers import (
    build_real_server,
    content,
    is_error,
    tool_call,
    write_text,
)


# ── write → immediately recallable (collapsed admission, no separate approve) ──
def test_clean_write_is_immediately_recallable():
    server, _ = build_real_server()
    text = "use BEGIN IMMEDIATE for the single writer lane"
    w = write_text(server, text)
    assert w["status"] == "approved"
    assert w["trust"] == "provisional"  # v3: serve-as-provisional-then-heal
    assert "approved_by" not in w  # no approver field exists
    assert isinstance(w["id"], int)
    env = content(tool_call(server, "hive_recall", {"query": text}))
    assert env["abstained"] is False
    assert w["id"] in {h["episode_id"] for h in env["reference_context"]}


def test_written_row_is_provisional_and_materialized():
    server, _ = build_real_server()
    w = write_text(server, "a written memory lands provisional")
    ep = server.store.get_episode(w["id"])
    assert ep.status == "approved" and ep.trust == "provisional"
    assert ep.value is not None
    assert ep.proposed_by == "agent"  # the transport identity (INV-2)
    assert server.store.counts() == (1, 0)  # 1 approved, 0 pending


def test_write_dedup_returns_same_id_no_second_row():
    server, _ = build_real_server()
    a = write_text(server, "same durable insight")
    b = write_text(server, "same durable insight")
    assert a["id"] == b["id"]
    assert b["deduped"] is True
    assert server.store.counts()[0] == 1  # exactly one approved row


# ── polarity rides the recall hit: a `dont` write surfaces polarity:"dont" ──────
def test_dont_write_surfaces_polarity_on_recall_hit():
    server, _ = build_real_server()
    text = "never delete the shared volume on teardown"
    w = write_text(server, text, polarity="dont")
    assert w["status"] == "approved"
    env = content(tool_call(server, "hive_recall", {"query": text}))
    assert env["abstained"] is False
    hit = next(h for h in env["reference_context"] if h["episode_id"] == w["id"])
    assert hit["polarity"] == "dont"  # the prohibition label reaches the consumer


def test_recall_hit_defaults_polarity_neutral():
    server, _ = build_real_server()
    text = "use BEGIN IMMEDIATE for the writer lane"
    w = write_text(server, text)  # no polarity ⇒ neutral
    env = content(tool_call(server, "hive_recall", {"query": text}))
    hit = next(h for h in env["reference_context"] if h["episode_id"] == w["id"])
    assert hit["polarity"] == "neutral"


# ── complete-failure relay: a store CAS failure is a tool error, never a phantom write ──
def test_store_complete_failure_surfaces_as_tool_error_and_leaves_no_row():
    server, _ = build_real_server()
    server.store.complete = lambda *a, **k: False  # simulate a lost-update CAS race
    resp = tool_call(server, "hive_write", {"text": "will fail to complete"})
    assert is_error(resp)
    # the dangling staged row was dropped — nothing left approved OR pending
    assert server.store.counts() == (0, 0)
