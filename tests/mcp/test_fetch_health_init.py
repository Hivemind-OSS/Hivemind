"""M06 hive_fetch (clean-miss contract), hive_health (happy snapshot + fail-closed
{ok,error,db_path}-only subset + no-secret invariant), hive_init (trailer_key
single-sourced from the producer ★, 2-phase confirm, unsupported-harness rejected)."""
from __future__ import annotations

import json

from hive.domain.models import content_hash
from tests.mcp._helpers import build_real_server, content, is_error, tool_call, write_text


# ── hive_fetch ──────────────────────────────────────────────────────────────────
def test_fetch_round_trip():
    server, _ = build_real_server()
    text = "fetch me by my content hash"
    w = write_text(server, text)                           # stages blob (pending)
    res = content(tool_call(server, "hive_fetch", {"content_hash": w["content_hash"]}))
    assert res["found"] is True and res["text"] == text
    assert w["content_hash"] == content_hash(text)


def test_fetch_unknown_hash_clean_miss():
    server, _ = build_real_server()
    res = content(tool_call(server, "hive_fetch", {"content_hash": "deadbeef"}))
    assert res == {"found": False, "text": None}           # never raises


# ── hive_health ─────────────────────────────────────────────────────────────────
def test_health_happy_snapshot():
    server, _ = build_real_server()
    w = write_text(server, "one approved one pending")
    content(tool_call(server, "hive_approve", {"ids": [w["id"]], "approver": "u"}))
    write_text(server, "still pending")
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is True
    assert snap["tenant_id"] == "default"
    assert snap["n_episodes"] == 1 and snap["n_pending"] == 1
    assert snap["index_authoritative"] is True
    assert snap["embedder_loaded"] is True
    assert snap["d"] == 64 and snap["W_version"] == 1
    assert snap["trailer_key"] == "Hive-Trace"
    assert snap["uptime_s"] >= 0
    assert "linked" not in snap                             # no repo_path given


def test_health_fail_closed_subset():
    server, _ = build_real_server()

    def _boom():
        raise RuntimeError("db gone")
    server.store.counts = _boom
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is False
    assert set(snap.keys()) == {"ok", "error", "db_path"}  # fail-closed subset ONLY


def test_health_snapshot_has_no_secret_substring():
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {"repo_path": "/tmp/repo"}))
    blob = json.dumps(snap)
    assert "AKIA" not in blob and "sk-" not in blob and "-----BEGIN" not in blob
    assert snap["linked"] is False and snap["link"] is None


# ── hive_init ───────────────────────────────────────────────────────────────────
def test_init_trailer_key_sourced_from_producer():
    server, _ = build_real_server(trailer="Hive-Trace-XYZ")
    plan = content(tool_call(server, "hive_init",
                             {"repo_path": "/tmp/r", "harness": "claude-code"}))
    assert plan["phase"] == 1
    assert plan["trailer_key"] == "Hive-Trace-XYZ"         # == producer.stamp_trailer
    assert plan["rules_block"]
    assert plan["expected_confirm_hash"]                   # hex


def test_init_phase2_confirm_links():
    server, _ = build_real_server()
    plan = content(tool_call(server, "hive_init",
                             {"repo_path": "/tmp/r", "harness": "generic"}))
    confirmed = content(tool_call(server, "hive_init",
                                  {"repo_path": "/tmp/r", "harness": "generic",
                                   "confirm_hash": plan["expected_confirm_hash"]}))
    assert confirmed["phase"] == 2 and confirmed["linked"] is True


def test_init_phase2_wrong_hash_refused():
    # the lie-proof contract through the relay: a stale/wrong confirm_hash must NOT link
    # (the install-planner double mirrors the real adapter's hash compare).
    server, _ = build_real_server()
    content(tool_call(server, "hive_init", {"repo_path": "/tmp/r", "harness": "generic"}))
    res = content(tool_call(server, "hive_init",
                            {"repo_path": "/tmp/r", "harness": "generic",
                             "confirm_hash": "00" * 32}))   # never-rendered hash
    assert res["phase"] == 2 and res["linked"] is False
    assert res.get("error") == "stale_or_wrong_block"


def test_init_unsupported_harness_rejected_by_schema():
    server, _ = build_real_server()
    r = tool_call(server, "hive_init", {"repo_path": "/tmp/r", "harness": "emacs"})
    assert is_error(r)
