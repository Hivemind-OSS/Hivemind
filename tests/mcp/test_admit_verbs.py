"""M06 admission relay: hive_pending (pending-only, ts>=since, preview truncation),
hive_approve (round-trip flips+indexes; unknown/replay skipped; a store-reported
approve-failure leaves the row NOT approved), hive_reject (drops pending,
unknown/approved skipped, rejected never recallable)."""
from __future__ import annotations

from tests.mcp._helpers import (
    approve, build_real_server, content, tool_call, write_text,
)


# ── hive_pending ────────────────────────────────────────────────────────────────
def test_pending_lists_pending_only_since():
    server, clock = build_real_server(t0=100)
    a = write_text(server, "insight A")
    clock.set(200)
    b = write_text(server, "insight B")
    clock.set(300)
    c = write_text(server, "insight C")
    all_p = content(tool_call(server, "hive_pending", {"since": 0}))
    assert all_p["count"] == 3
    since200 = content(tool_call(server, "hive_pending", {"since": 200}))
    ids = {r["id"] for r in since200["pending"]}
    assert ids == {b["id"], c["id"]} and since200["count"] == 2
    since250 = content(tool_call(server, "hive_pending", {"since": 250}))
    assert {r["id"] for r in since250["pending"]} == {c["id"]}
    # every listed row is pending-verdict PASS (clean text) and carries provenance
    assert all(r["scan_verdict"] == "PASS" for r in all_p["pending"])
    assert all("proposed_by" in r and "ts" in r for r in all_p["pending"])
    # approved rows drop out of pending
    approve(server, [a["id"]])
    assert content(tool_call(server, "hive_pending", {}))["count"] == 2


def test_pending_preview_is_truncated():
    server, _ = build_real_server()
    long = "x" * 500
    write_text(server, long)
    row = content(tool_call(server, "hive_pending", {}))["pending"][0]
    assert row["text_preview"].endswith("…")
    assert len(row["text_preview"]) <= 161                 # _PREVIEW_LEN(160) + ellipsis


# ── hive_approve ────────────────────────────────────────────────────────────────
def test_approve_flips_status_and_indexes():
    server, _ = build_real_server()
    w = write_text(server, "approve me and make me recallable")
    eid = w["id"]
    res = approve(server, [eid])
    assert res["approved"] == [eid] and res["skipped"] == []
    assert server.store.get_episode(eid).status == "approved"
    # indexed ⇒ recallable on the very next recall
    env = content(tool_call(server, "hive_recall",
                            {"query": "approve me and make me recallable"}))
    assert env["abstained"] is False
    assert eid in {h["episode_id"] for h in env["reference_context"]}


def test_approve_unknown_and_replay_skipped():
    server, _ = build_real_server()
    w = write_text(server, "real pending")
    eid = w["id"]
    assert approve(server, [9999])["skipped"] == [9999]    # unknown id
    approve(server, [eid])                                  # first approve
    replay = approve(server, [eid])                         # already approved → skip
    assert replay["approved"] == [] and replay["skipped"] == [eid]


def test_approve_index_failure_leaves_row_not_approved():
    """If the store reports approve failure (CAS/index), the id lands in `skipped`
    and the row stays pending — the boundary never reports a non-recallable row as
    approved (the atomicity relay)."""
    server, _ = build_real_server()
    w = write_text(server, "will fail to approve")
    eid = w["id"]
    server.store.approve = lambda *a, **k: False           # simulate CAS/index failure
    res = approve(server, [eid])
    assert res["approved"] == [] and res["skipped"] == [eid]
    assert server.store.get_episode(eid).status == "pending"


# ── hive_reject ─────────────────────────────────────────────────────────────────
def test_reject_drops_unknown_skipped():
    server, _ = build_real_server()
    a = write_text(server, "reject A")
    b = write_text(server, "keep B")
    res = content(tool_call(server, "hive_reject", {"ids": [a["id"], 9999]}))
    assert res["rejected"] == [a["id"]] and res["skipped"] == [9999]
    remaining = {r["id"] for r in content(tool_call(server, "hive_pending", {}))["pending"]}
    assert remaining == {b["id"]}


def test_reject_approved_is_skipped():
    server, _ = build_real_server()
    w = write_text(server, "approved, cannot unpublish")
    approve(server, [w["id"]])
    res = content(tool_call(server, "hive_reject", {"ids": [w["id"]]}))
    assert res["rejected"] == [] and res["skipped"] == [w["id"]]
