"""M06 hive_fetch (clean-miss contract), hive_health (happy snapshot + fail-closed
{ok,error,db_path}-only subset + no-secret invariant), hive_init (2-phase confirm,
unsupported-harness rejected)."""
from __future__ import annotations

import json

from hive.domain.models import content_hash
from tests.mcp._helpers import build_real_server, content, is_error, tool_call, write_text


# ── hive_fetch ──────────────────────────────────────────────────────────────────
def test_fetch_round_trip():
    server, _ = build_real_server()
    text = "fetch me by my content hash"
    w = write_text(server, text)                           # stores blob (approved)
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
    # client-gated writes land approved directly — no pending queue, so n_pending stays 0
    write_text(server, "first approved memory")
    write_text(server, "second approved memory")
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is True
    assert snap["tenant_id"] == "default"
    assert snap["n_episodes"] == 2 and snap["n_pending"] == 0
    assert snap["index_authoritative"] is True
    assert snap["embedder_loaded"] is True
    assert snap["d"] == 64 and snap["W_version"] == 1
    assert snap["uptime_s"] >= 0
    assert "linked" not in snap                             # no repo_path given
    assert "onboarding" not in snap                         # ...so no first-touch hint either


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


# ── self-onboard hint (first-touch onboarding on an unlinked repo) ────────────────
def test_health_unlinked_repo_returns_onboarding_hint():
    """A repo with NO link record gets the onboarding block back so the agent knows
    its ONE next call (hive_init) — no skill discovery, the server hands it the step.
    Removing the unlinked branch in _handle_health drops this key (deliberate mutation)."""
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {"repo_path": "/tmp/fresh-unlinked"}))
    assert snap["ok"] is True
    assert snap["linked"] is False                         # unlinked, yet healthy
    ob = snap["onboarding"]
    assert ob["required"] is True
    assert isinstance(ob["manifest_version"], int)
    assert "hive_init" in ob["next"]
    assert "setup" not in snap                              # the linked-only short-circuit is absent


def test_health_linked_repo_has_no_onboarding_hint():
    """A re-touch of an already-linked repo finds linked:true and gets NO hint —
    the onboarding block is the first-touch signal only, never repeated."""
    server, _ = build_real_server()
    repo = "/tmp/linked-repo"
    plan = content(tool_call(server, "hive_init", {"repo_path": repo, "harness": "generic"}))
    content(tool_call(server, "hive_init",
                      {"repo_path": repo, "harness": "generic",
                       "confirm_hash": plan["expected_confirm_hash"]}))   # phase-2 links
    snap = content(tool_call(server, "hive_health", {"repo_path": repo}))
    assert snap["linked"] is True
    assert "onboarding" not in snap


# ── hive_init ───────────────────────────────────────────────────────────────────
def test_init_phase1_returns_block_and_hash():
    server, _ = build_real_server()
    plan = content(tool_call(server, "hive_init",
                             {"repo_path": "/tmp/r", "harness": "claude-code"}))
    assert plan["phase"] == 1
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


def test_init_phase1_surfaces_manifest_and_recipe():
    server, _ = build_real_server()
    plan = content(tool_call(server, "hive_init",
                             {"repo_path": "/tmp/r", "harness": "claude-code"}))
    assert plan["phase"] == 1
    assert plan["manifest"]["manifest_version"] >= 1 and plan["manifest"]["hooks"]
    rec = plan["recipe"]
    assert rec["harness"] == "claude-code" and rec["resolved_tier"] == 2
    assert rec["hook_files"] and rec["rules_addendum"] and rec["playbook"]


def test_init_phase2_link_records_manifest_version_and_tier():
    server, _ = build_real_server()
    repo = "/tmp/r-tiered"
    plan = content(tool_call(server, "hive_init", {"repo_path": repo, "harness": "claude-code"}))
    confirmed = content(tool_call(server, "hive_init",
                                  {"repo_path": repo, "harness": "claude-code",
                                   "confirm_hash": plan["expected_confirm_hash"]}))
    assert confirmed["phase"] == 2 and confirmed["linked"] is True
    assert confirmed["link"]["manifest_version"] >= 1
    assert confirmed["link"]["tier"] == 2 and confirmed["link"]["harness"] == "claude-code"


# ── chunk-6: verify gate substrate (banner field · setup short-circuit · V4) ──────
def _link_repo(server, repo, harness="generic"):
    """Drive both phases so ``repo`` is linked (mirrors the two-phase handshake)."""
    plan = content(tool_call(server, "hive_init", {"repo_path": repo, "harness": harness}))
    content(tool_call(server, "hive_init",
                      {"repo_path": repo, "harness": harness,
                       "confirm_hash": plan["expected_confirm_hash"]}))
    return plan


def test_init_phase1_returns_provenance_banner():
    """Phase-1 hands over the completion banner so the agent stamps it LAST. It is
    a marker OUTSIDE the hashed block — it must carry no hive-init delimiter."""
    server, _ = build_real_server()
    plan = content(tool_call(server, "hive_init",
                             {"repo_path": "/tmp/r", "harness": "claude-code"}))
    banner = plan["provenance_banner"]
    assert banner.startswith("<!-- hive-setup:") and banner.endswith("-->")
    assert "harness=claude-code" in banner and "tier=2" in banner
    assert "DO NOT re-run" in banner
    assert "hive-init:start" not in banner and "hive-init:end" not in banner


def test_health_linked_repo_reports_setup_complete():
    """A linked repo's health carries the bootstrap short-circuit (complete/next)
    and — being linked — NO onboarding hint."""
    server, _ = build_real_server()
    repo = "/tmp/linked-setup"
    _link_repo(server, repo)
    snap = content(tool_call(server, "hive_health", {"repo_path": repo}))
    assert snap["linked"] is True
    assert snap["setup"] == {"complete": True, "next": None}
    assert "onboarding" not in snap


def test_health_status_probe_writes_nothing():
    """★ mutation #5: the bootstrap status probe is a PURE READ. A health call on a
    linked repo must issue ZERO meta_set writes — inserting any write into the linked
    branch trips this spy."""
    server, _ = build_real_server()
    repo = "/tmp/seeded-complete"
    _link_repo(server, repo)
    calls: list[str] = []
    orig = server.store.meta_set
    server.store.meta_set = lambda k, v: (calls.append(k), orig(k, v))[1]
    snap = content(tool_call(server, "hive_health", {"repo_path": repo}))
    assert snap["linked"] is True and snap["setup"]["complete"] is True
    assert calls == []                                       # the probe wrote nothing


def test_health_hooks_seen_advances_after_capture():
    """V4 substrate: hive_write stamps meta[hook:last_seen:hive_write]; hooks_seen
    surfaces it and it ADVANCES across captures (a pure-read verb would not move it)."""
    server, clock = build_real_server()
    assert content(tool_call(server, "hive_health", {}))["hooks_seen"] == {}   # nothing yet
    write_text(server, "first durable insight")
    t1 = content(tool_call(server, "hive_health", {}))["hooks_seen"]["hive_write"]
    clock.advance(5)
    write_text(server, "second durable insight")
    t2 = content(tool_call(server, "hive_health", {}))["hooks_seen"]["hive_write"]
    assert t2 > t1


def test_recall_does_not_stamp_hook_seen():
    """Only the capture verb is stamped — a recall (read path) must NOT advance hooks_seen,
    so the read path stays write-free (the dispatcher anti-pattern guard)."""
    server, _ = build_real_server()
    tool_call(server, "hive_recall", {"query": "anything"})
    assert content(tool_call(server, "hive_health", {}))["hooks_seen"] == {}
