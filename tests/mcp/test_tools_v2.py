"""The trust-lifecycle MCP surface: the 6-tool set (no hive_evidence), the capture
verb, exposure/miss recording on the recall path, trust+ts labels end-to-end, the
per-hit servability belt (the authoritative freshness layer), the fetch supersession
annotation, health telemetry (trust_counts / n_misses_7d / gaps / manifest_outdated),
and the autonomy config knobs."""
from __future__ import annotations

import math

import pytest

from hive.app.config import AutonomyConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.onboard import HOOK_MANIFEST, ONBOARDING_MANIFEST_VERSION
from hive.app.tool_defs import TOOL_NAMES
from hive.domain.lifecycle import PROVISIONAL
from hive.domain.ports import ExposureLedger
from tests.fakes import FakeLedger
from tests.mcp._helpers import build_real_server, content, is_error, tool_call

_DAY_S = 86_400
SECRET = "AKIAIOSFODNN7EXAMPLE"


def tool_call_as(server, agent_id: str, name: str, args: dict, req_id=1):
    """A tools/call with a per-request identity (the HTTP daemon's verified caller)."""
    req = MCPRequest(req_id, "tools/call", {"name": name, "arguments": args})
    return server.handle(req, identity=ServerIdentity("default", agent_id))


def _count(server, table: str) -> int:
    return server.store.conn.execute(
        f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# ── surface ────────────────────────────────────────────────────────────────────
def test_tool_list_is_exactly_6():
    assert TOOL_NAMES == {"hive_write", "hive_capture", "hive_recall",
                          "hive_fetch", "hive_init", "hive_health"}
    assert "hive_evidence" not in TOOL_NAMES        # no client-fed evidence exists


def test_fake_ledger_conforms_to_port():
    assert isinstance(FakeLedger(), ExposureLedger)


# ── capture verb ───────────────────────────────────────────────────────────────
def test_capture_tool_quarantine_roundtrip():
    server, _ = build_real_server()
    env = content(tool_call(server, "hive_capture", {"text": "a fleet insight"}))
    assert env["status"] == "quarantined" and env["deduped"] is False
    assert isinstance(env["id"], int) and env["scan"]["action"] == "clean"
    # quarantined ⇒ structurally unservable: the next recall of the SAME text
    # finds nothing (empty index ⇒ EMPTY, abstained envelope)
    r = content(tool_call(server, "hive_recall", {"query": "a fleet insight"}))
    assert r["abstained"] is True and r["reference_context"] == []


def test_capture_tool_disabled_envelope():
    server, _ = build_real_server(autonomy=AutonomyConfig(enabled=False))
    env = content(tool_call(server, "hive_capture", {"text": "anything"}))
    assert env == {"status": "disabled"}
    assert _count(server, "episodes") == 0


def test_capture_tool_secret_refused():
    server, _ = build_real_server()
    env = content(tool_call(server, "hive_capture", {"text": f"key {SECRET}"}))
    assert env["status"] == "refused" and SECRET not in str(env)
    assert _count(server, "episodes") == 0


def test_capture_requires_text_via_schema_belt():
    server, _ = build_real_server()
    assert is_error(tool_call(server, "hive_capture", {}))   # text is required


# ── exposure + miss recording on the recall path ───────────────────────────────
def test_exposure_recorded_on_hit_not_on_abstain():
    # h=0.01: any off-topic (flat) candidate set abstains deterministically while
    # exact-text matches stay sharply CONFIDENT (hash-vector embedder)
    server, _ = build_real_server(h=0.01)
    w = content(tool_call(server, "hive_write",
                          {"text": "rotate the key quarterly", "approved_by": "h"}))
    hit = content(tool_call_as(server, "agent-A", "hive_recall",
                               {"query": "rotate the key quarterly"}))
    assert hit["abstained"] is False
    rows = [dict(r) for r in server.store.conn.execute("SELECT * FROM exposure")]
    assert len(rows) == 1
    assert rows[0]["episode_id"] == w["id"] and rows[0]["agent_id"] == "agent-A"
    # an abstained recall records NO exposure (two distractors → flat sims)
    content(tool_call(server, "hive_write", {"text": "use WAL mode", "approved_by": "h"}))
    miss = content(tool_call_as(server, "agent-B", "hive_recall",
                                {"query": "what is the deploy cadence"}))
    assert miss["abstained"] is True
    assert _count(server, "exposure") == 1          # unchanged


def test_miss_recorded_on_abstain_and_empty():
    server, _ = build_real_server(h=0.01)
    # EMPTY (cold store) — the miss must still carry its query VECTOR, or cold-start
    # demand could never accumulate and nothing would ever promote
    content(tool_call_as(server, "agent-A", "hive_recall", {"query": "cold question"}))
    rows = [dict(r) for r in server.store.conn.execute(
        "SELECT * FROM recall_misses ORDER BY id")]
    assert len(rows) == 1 and rows[0]["miss_type"] == "no_match"
    assert rows[0]["agent_id"] == "agent-A" and rows[0]["query_vector"] is not None
    # ABSTAIN (flat candidate set)
    content(tool_call(server, "hive_write", {"text": "alpha fact", "approved_by": "h"}))
    content(tool_call(server, "hive_write", {"text": "beta fact", "approved_by": "h"}))
    content(tool_call_as(server, "agent-B", "hive_recall", {"query": "gamma question"}))
    rows = [dict(r) for r in server.store.conn.execute(
        "SELECT * FROM recall_misses ORDER BY id")]
    assert [r["miss_type"] for r in rows] == ["no_match", "abstained"]


def test_secret_query_miss_is_stripped():
    server, _ = build_real_server()
    content(tool_call_as(server, "agent-A", "hive_recall",
                         {"query": f"where is {SECRET} used"}))
    row = server.store.conn.execute("SELECT * FROM recall_misses").fetchone()
    assert row["miss_type"] == "secret_refused"
    assert row["query_text"] == "" and row["query_vector"] is None  # no content survives


# ── labels + the servability belt ──────────────────────────────────────────────
def test_recall_hits_carry_trust_and_ts():
    server, clock = build_real_server(t0=5_000)
    content(tool_call(server, "hive_write", {"text": "labeled fact", "approved_by": "h"}))
    env = content(tool_call(server, "hive_recall", {"query": "labeled fact"}))
    hit = env["reference_context"][0]
    assert hit["trust"] == "established" and hit["ts"] == 5_000


def test_recall_belt_drops_lapsed_provisional():
    server, clock = build_real_server()
    cap = content(tool_call(server, "hive_capture", {"text": "promoted insight"}))
    # promote it mechanically at the store layer (demand e2e lives in acceptance)
    assert server.store.set_trust(cap["id"], PROVISIONAL, now=clock.now()) is True
    served = content(tool_call(server, "hive_recall", {"query": "promoted insight"}))
    assert served["abstained"] is False
    assert served["reference_context"][0]["trust"] == "provisional"
    # let the provisional TTL lapse; the warm index is now STALE (no sweep ran) —
    # the per-hit belt is the authoritative layer and must drop it
    clock.advance((server.autonomy.provisional_ttl_days + 1) * _DAY_S)
    assert server.recall.index.size() == 1          # still in the warm index
    stale = content(tool_call(server, "hive_recall", {"query": "promoted insight"}))
    assert stale["abstained"] is True and stale["reference_context"] == []


# ── fetch supersession annotation ──────────────────────────────────────────────
def test_fetch_annotates_terminal_successor():
    server, _ = build_real_server()
    a = content(tool_call(server, "hive_write", {"text": "v one", "approved_by": "h"}))
    b = content(tool_call(server, "hive_write", {"text": "v two", "approved_by": "h",
                                                 "replaces": a["id"]}))
    assert b["superseded"] == a["id"]
    c = content(tool_call(server, "hive_write", {"text": "v three", "approved_by": "h",
                                                 "replaces": b["id"]}))
    got = content(tool_call(server, "hive_fetch", {"content_hash": a["content_hash"]}))
    assert got["found"] is True
    assert got["superseded_by"] == {"episode_id": c["id"],
                                    "content_hash": c["content_hash"]}
    tip = content(tool_call(server, "hive_fetch", {"content_hash": c["content_hash"]}))
    assert tip["found"] is True and "superseded_by" not in tip


def test_write_replaces_unknown_target_is_tool_error():
    server, _ = build_real_server()
    r = tool_call(server, "hive_write", {"text": "fix", "approved_by": "h",
                                         "replaces": 424242})
    assert is_error(r)
    assert _count(server, "episodes") == 0          # whole call failed before staging


# ── health telemetry ───────────────────────────────────────────────────────────
def test_health_trust_counts_misses_gaps():
    server, _ = build_real_server(h=0.01)
    # TWO established rows so an off-topic query reads as a flat (abstaining)
    # candidate set — a single-row index serves everything at sim≈0
    content(tool_call(server, "hive_write", {"text": "vouched", "approved_by": "h"}))
    content(tool_call(server, "hive_write", {"text": "also vouched", "approved_by": "h"}))
    content(tool_call(server, "hive_capture", {"text": "unvouched"}))
    content(tool_call_as(server, "agent-A", "hive_recall", {"query": "same gap twice"}))
    content(tool_call_as(server, "agent-B", "hive_recall", {"query": "same gap twice"}))
    content(tool_call_as(server, "agent-C", "hive_recall", {"query": "another gap"}))
    snap = content(tool_call(server, "hive_health", {"include_gaps": True}))
    assert snap["ok"] is True
    assert set(snap["trust_counts"]) == {"quarantined", "provisional",
                                         "established", "deprecated"}
    assert snap["trust_counts"]["established"] == 2
    assert snap["trust_counts"]["quarantined"] == 1
    assert snap["n_misses_7d"] == 3
    gaps = snap["gaps"]
    assert gaps[0]["representative_query"] == "same gap twice"
    assert gaps[0]["miss_count"] == 2               # identical queries clustered
    assert set(gaps[0]) == {"representative_query", "miss_count",
                            "miss_types", "last_seen"}
    # gaps are opt-in: the plain snapshot has none
    assert "gaps" not in content(tool_call(server, "hive_health", {}))


# ── manifest v2 + the outdated hint ────────────────────────────────────────────
def test_manifest_v2_wording_and_outdated_hint():
    assert ONBOARDING_MANIFEST_VERSION == 2
    assert HOOK_MANIFEST.manifest_version == 2
    by_event = {h.event: h for h in HOOK_MANIFEST.hooks}
    assert "hive_capture" in by_event["turn-end"].directive
    assert "no need to ask" in by_event["turn-end"].directive
    assert "replaces" in by_event["correction"].directive    # the human-correction hook
    assert "trust" in by_event["task-start"].directive

    class _V1Planner:
        def link_status(self, repo_path):
            return True, {"manifest_version": 1}

    server, _ = build_real_server(planner=_V1Planner())
    snap = content(tool_call(server, "hive_health", {"repo_path": "/r"}))
    assert snap["linked"] is True and snap["manifest_outdated"] is True

    class _V2Planner:
        def link_status(self, repo_path):
            return True, {"manifest_version": ONBOARDING_MANIFEST_VERSION}

    server2, _ = build_real_server(planner=_V2Planner())
    snap2 = content(tool_call(server2, "hive_health", {"repo_path": "/r"}))
    assert snap2["manifest_outdated"] is False


# ── autonomy config knobs ──────────────────────────────────────────────────────
def test_autonomy_config_validation_and_tiers():
    with pytest.raises(ValueError):
        AutonomyConfig(demand_m=0)
    with pytest.raises(ValueError):
        AutonomyConfig(demand_tau=0.0)
    with pytest.raises(ValueError):
        AutonomyConfig(competitor_tau=1.5)
    with pytest.raises(ValueError):
        AutonomyConfig(demand_tau=math.nan)
    with pytest.raises(ValueError):
        AutonomyConfig(provisional_ttl_days=0)
