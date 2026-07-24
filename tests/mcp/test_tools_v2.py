"""The trust-lifecycle MCP surface (v3): the 8-tool set, the capture verb,
exposure/miss recording on the recall path, trust+ts labels end-to-end, the
per-hit servability belt (the authoritative freshness layer), health telemetry
(trust_counts / n_misses_7d / gaps with miss scope), and the autonomy config knobs."""

from __future__ import annotations

import math

import pytest

from hive.app.config import AutonomyConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.tool_defs import TOOL_NAMES
from hive.domain.lifecycle import PROVISIONAL
from hive.domain.ports import ExposureLedger
from tests.fakes import FakeLedger
from tests.mcp._helpers import (
    build_real_server,
    content,
    is_error,
    register_repo,
    tool_call,
)

_DAY_S = 86_400
SECRET = "AKIAIOSFODNN7EXAMPLE"


def tool_call_as(server, agent_id: str, name: str, args: dict, req_id=1):
    """A tools/call with a per-request identity (the HTTP daemon's verified caller)."""
    req = MCPRequest(req_id, "tools/call", {"name": name, "arguments": args})
    return server.handle(req, identity=ServerIdentity("default", agent_id))


def _count(server, table: str) -> int:
    return server.store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()[
        "n"
    ]


# ── surface ────────────────────────────────────────────────────────────────────
def test_tool_list_is_the_expected_verb_set():
    assert {"hive_supersede", "hive_flag"} <= TOOL_NAMES  # the conflict verbs
    assert {"hive_write", "hive_capture", "hive_recall", "hive_health"} <= TOOL_NAMES
    assert "hive_evidence" not in TOOL_NAMES  # no client-fed evidence exists
    assert "hive_init" not in TOOL_NAMES  # onboarding apparatus is GONE (v3)
    assert (
        "hive_fetch" not in TOOL_NAMES
    )  # fetch dropped (recall inlines verbatim text)


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
    assert env == {"status": "disabled"}  # v3: no beacon key exists
    assert _count(server, "episodes") == 0


def test_capture_tool_secret_refused():
    server, _ = build_real_server()
    env = content(tool_call(server, "hive_capture", {"text": f"key {SECRET}"}))
    assert env["status"] == "refused" and SECRET not in str(env)
    assert _count(server, "episodes") == 0


def test_capture_requires_text_via_schema_belt():
    server, _ = build_real_server()
    assert is_error(tool_call(server, "hive_capture", {}))  # text is required


# ── exposure + miss recording on the recall path ───────────────────────────────
def test_exposure_recorded_on_hit_not_on_abstain():
    server, _ = build_real_server()
    w = content(tool_call(server, "hive_write", {"text": "rotate the key quarterly"}))
    hit = content(
        tool_call_as(
            server, "agent-A", "hive_recall", {"query": "rotate the key quarterly"}
        )
    )
    assert hit["abstained"] is False
    rows = [dict(r) for r in server.store.conn.execute("SELECT * FROM exposure")]
    assert len(rows) == 1
    assert rows[0]["episode_id"] == w["id"] and rows[0]["agent_id"] == "agent-A"
    # an abstained recall records NO exposure (two distractors → flat sims)
    content(tool_call(server, "hive_write", {"text": "use WAL mode"}))
    miss = content(
        tool_call_as(
            server, "agent-B", "hive_recall", {"query": "what is the deploy cadence"}
        )
    )
    assert miss["abstained"] is True
    assert _count(server, "exposure") == 1  # unchanged


def test_miss_recorded_on_abstain_and_empty():
    server, _ = build_real_server()
    # EMPTY (cold store) — the miss must still carry its query VECTOR, or cold-start
    # demand could never accumulate and nothing would ever promote
    content(tool_call_as(server, "agent-A", "hive_recall", {"query": "cold question"}))
    rows = [
        dict(r)
        for r in server.store.conn.execute("SELECT * FROM recall_misses ORDER BY id")
    ]
    assert len(rows) == 1 and rows[0]["miss_type"] == "no_match"
    assert rows[0]["agent_id"] == "agent-A" and rows[0]["query_vector"] is not None
    # ABSTAIN (flat candidate set)
    content(tool_call(server, "hive_write", {"text": "alpha fact"}))
    content(tool_call(server, "hive_write", {"text": "beta fact"}))
    content(tool_call_as(server, "agent-B", "hive_recall", {"query": "gamma question"}))
    rows = [
        dict(r)
        for r in server.store.conn.execute("SELECT * FROM recall_misses ORDER BY id")
    ]
    assert [r["miss_type"] for r in rows] == ["no_match", "abstained"]


def test_scoped_miss_carries_its_repo_scope():
    server, _ = build_real_server()
    register_repo(server, "alpha")
    content(
        tool_call_as(
            server,
            "agent-A",
            "hive_recall",
            {"query": "an alpha question", "repos": ["alpha"]},
        )
    )
    row = server.store.conn.execute("SELECT repos FROM recall_misses").fetchone()
    assert row["repos"] == '["alpha"]'  # the §3.6 demand partition


def test_secret_query_miss_is_stripped():
    server, _ = build_real_server()
    content(
        tool_call_as(
            server, "agent-A", "hive_recall", {"query": f"where is {SECRET} used"}
        )
    )
    row = server.store.conn.execute("SELECT * FROM recall_misses").fetchone()
    assert row["miss_type"] == "secret_refused"
    assert (
        row["query_text"] == "" and row["query_vector"] is None
    )  # no content survives


# ── labels + the servability belt ──────────────────────────────────────────────
def test_recall_hits_carry_trust_and_ts():
    server, clock = build_real_server(t0=5_000)
    content(tool_call(server, "hive_write", {"text": "labeled fact"}))
    env = content(tool_call(server, "hive_recall", {"query": "labeled fact"}))
    hit = env["reference_context"][0]
    assert hit["trust"] == "provisional" and hit["ts"] == 5_000  # v3: serve-now


def test_recall_hits_carry_kind_and_anchors():
    # end-to-end: the write handler reads kind/anchors → admission → store → recall →
    # the recall handler wires them into the served envelope (§3.4).
    server, _ = build_real_server()
    register_repo(server, "alpha")
    content(
        tool_call(
            server,
            "hive_write",
            {
                "text": "a labeled bug fact",
                "kind": "bug",
                "anchors": [{"repo": "alpha", "anchor": "hive/domain/recall.py"}],
            },
        )
    )
    env = content(tool_call(server, "hive_recall", {"query": "a labeled bug fact"}))
    hit = env["reference_context"][0]
    assert hit["kind"] == "bug"
    assert hit["repos"] == ["alpha"]
    assert hit["anchors"] == [{"repo": "alpha", "anchor": "hive/domain/recall.py"}]


def test_recall_hit_defaults_kind_note_when_unlabeled():
    # omitted kind/anchors default server-side (SOFT): the hit under-claims, never errors.
    server, _ = build_real_server()
    content(tool_call(server, "hive_write", {"text": "an unlabeled fact"}))
    env = content(tool_call(server, "hive_recall", {"query": "an unlabeled fact"}))
    hit = env["reference_context"][0]
    assert hit["kind"] == "note" and hit["repos"] == [] and hit["anchors"] == []


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
    assert server.recall.index.size() == 1  # still in the warm index
    stale = content(tool_call(server, "hive_recall", {"query": "promoted insight"}))
    assert stale["abstained"] is True and stale["reference_context"] == []


# ── supersession (replaces=) — machine-gated in v3 ─────────────────────────────
def test_write_replaces_retires_target_only_when_qualified():
    server, _ = build_real_server()
    a = content(tool_call(server, "hive_write", {"text": "v one"}))
    # evidence-less target: the rider noops, the write still lands
    b = content(tool_call(server, "hive_write", {"text": "v two", "replaces": a["id"]}))
    assert b["superseded"] is None and b["supersede_noop"]
    # with a qualifying machine signal the SAME call retires
    import json as _json

    server.store.insert_audit(
        a["id"],
        "outcome_verified_hurt",
        "census",
        50,
        _json.dumps({"stamp": {"head_sha": "c" * 40}}),
    )
    c = content(
        tool_call(server, "hive_write", {"text": "v three", "replaces": a["id"]})
    )
    assert c["superseded"] == a["id"]


def test_write_replaces_unknown_target_is_tool_error():
    server, _ = build_real_server()
    r = tool_call(server, "hive_write", {"text": "fix", "replaces": 424242})
    assert is_error(r)
    assert _count(server, "episodes") == 0  # whole call failed before staging


# ── health telemetry ───────────────────────────────────────────────────────────
def test_health_trust_counts_misses_gaps():
    server, _ = build_real_server()
    # TWO provisional rows so an off-topic query reads as a flat (abstaining)
    # candidate set — a single-row index serves everything at sim≈0
    content(tool_call(server, "hive_write", {"text": "written now"}))
    content(tool_call(server, "hive_write", {"text": "also written"}))
    content(tool_call(server, "hive_capture", {"text": "unclear value"}))
    content(tool_call_as(server, "agent-A", "hive_recall", {"query": "same gap twice"}))
    content(tool_call_as(server, "agent-B", "hive_recall", {"query": "same gap twice"}))
    content(tool_call_as(server, "agent-C", "hive_recall", {"query": "another gap"}))
    snap = content(tool_call(server, "hive_health", {"include_gaps": True}))
    assert snap["ok"] is True
    assert set(snap["trust_counts"]) == {
        "quarantined",
        "provisional",
        "established",
        "deprecated",
    }
    assert snap["trust_counts"]["provisional"] == 2  # v3: writes land provisional
    assert snap["trust_counts"]["quarantined"] == 1
    assert snap["n_misses_7d"] == 3
    gaps = snap["gaps"]
    assert gaps[0]["representative_query"] == "same gap twice"
    assert gaps[0]["miss_count"] == 2  # identical queries clustered
    assert set(gaps[0]) == {
        "representative_query",
        "miss_count",
        "miss_types",
        "last_seen",
        "repos",
    }
    # gaps are opt-in: the plain snapshot has none
    assert "gaps" not in content(tool_call(server, "hive_health", {}))


def test_gap_report_carries_miss_scope():
    server, _ = build_real_server()
    register_repo(server, "alpha")
    for agent in ("agent-A", "agent-B", "agent-A"):
        content(
            tool_call_as(
                server,
                agent,
                "hive_recall",
                {"query": "how do i warm the alpha cache safely", "repos": ["alpha"]},
            )
        )
    snap = content(tool_call(server, "hive_health", {"include_gaps": True}))
    gaps = snap["gaps"]
    assert gaps and gaps[0]["repos"] == ["alpha"], (
        f"the alpha-scoped demand names alpha: {gaps}"
    )


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
