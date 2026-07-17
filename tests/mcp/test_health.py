"""M06 hive_health: the happy snapshot, the fail-closed {ok,error,db_path}-only
subset on a probe failure, and the no-secret invariant on the serialized envelope."""
from __future__ import annotations

import json

from hive.app.onboard_ref import CONTRACT_VERSION
from tests.mcp._helpers import build_real_server, content, tool_call, write_text


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
    assert snap["d"] == 64
    assert snap["uptime_s"] >= 0


def test_health_fail_closed_subset():
    server, _ = build_real_server()

    def _boom():
        raise RuntimeError("db gone")
    server.store.counts = _boom
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is False
    # fail-closed subset ONLY (the leak guard) + the unconditional beacon stamp. EXACT, never a
    # subset: the beacon is the single additive key; no db internals may join it.
    assert set(snap.keys()) == {"ok", "error", "db_path", "contract_version"}
    assert snap["contract_version"] == CONTRACT_VERSION


def test_health_snapshot_has_no_secret_substring():
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {}))
    blob = json.dumps(snap)
    assert "AKIA" not in blob and "sk-" not in blob and "-----BEGIN" not in blob


def test_health_surfaces_secret_scan_disabled_when_off():
    # the loosened posture is never silent: a disabled credential floor shows in health.
    server, _ = build_real_server(secret_scan_enabled=False)
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is True
    assert snap["secret_scan_disabled"] is True


def test_health_omits_secret_scan_key_when_enabled():
    # default (floor ON) ⇒ the key is ABSENT ⇒ the health envelope is byte-identical.
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {}))
    assert "secret_scan_disabled" not in snap


def test_health_surfaces_store_ephemeral_when_in_memory():
    # the in-memory (:memory:) store loses all memory on restart: the loss-prone posture is
    # never silent — it shows in health (mirrors secret_scan_disabled).
    server, _ = build_real_server()                          # :memory: ⇒ db_path=""
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is True
    assert snap["store_ephemeral"] is True


def test_health_omits_store_ephemeral_when_persistent():
    # a persistent store (db_path set) ⇒ the key is ABSENT ⇒ the envelope is byte-identical.
    server, _ = build_real_server()
    server.db_path = "/data/shared.db"                       # the handler reads self.db_path
    snap = content(tool_call(server, "hive_health", {}))
    assert "store_ephemeral" not in snap


def test_health_has_no_contested():
    # the contested-memory review queue was cut; include_gaps still serves the demand-gap
    # report, but never a contested block (no _contested_report wiring remains).
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {"include_gaps": True}))
    assert "gaps" in snap                                     # the surviving demand-gap channel
    assert "contested" not in snap and "contested_note" not in snap
    assert not hasattr(server, "_contested_report")


def test_health_meta_versions_absent_by_default():
    # the meta envelope law's observability channel is byte-inert until requested:
    # dropping the request gate ⇒ the always-emits mutation this pins against.
    server, _ = build_real_server()
    write_text(server, "a memory", meta={"matrix/subgraph_fp": "matrix-subgraph-fp/1:aaa"})
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is True
    assert "meta_versions" not in snap


def test_health_meta_versions_histogram_end_to_end():
    # Seed through the REAL capture/write path: mixed versions, a malformed value, bare
    # rows, and a deprecated (retired) carrier — the histogram counts the LIVE corpus
    # (servable + quarantined), buckets per version prefix, and never reads a body.
    server, _ = build_real_server()
    write_text(server, "established v1 carrier",
               meta={"matrix/subgraph_fp": "matrix-subgraph-fp/1:aaa"})
    content(tool_call(server, "hive_capture", {                       # quarantined counts too
        "text": "quarantined v1 carrier",
        "meta": {"matrix/subgraph_fp": "matrix-subgraph-fp/1:aaa"}}))
    write_text(server, "future v2 carrier",
               meta={"matrix/subgraph_fp": "matrix-subgraph-fp/2:bbb"})
    write_text(server, "malformed carrier", meta={"matrix/subgraph_fp": "garbage"})
    write_text(server, "bare row, no meta")
    winner = write_text(server, "the superseding successor")["id"]
    loser = write_text(server, "retired v1 carrier",
                       meta={"matrix/subgraph_fp": "matrix-subgraph-fp/1:zzz"})["id"]
    content(tool_call(server, "hive_supersede",
                      {"loser": loser, "winner": winner, "approved_by": "user"}))
    snap = content(tool_call(server, "hive_health", {"include_meta_versions": True}))
    assert snap["ok"] is True
    # deprecated excluded (else "1" would read 3); absent counts the two bare live rows;
    # malformed is present because nonzero.
    assert snap["meta_versions"] == {
        "matrix/subgraph_fp": {"versions": {"1": 2, "2": 1}, "absent": 2,
                               "malformed": 1}}


def test_health_meta_versions_empty_corpus():
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {"include_meta_versions": True}))
    assert snap["ok"] is True
    assert snap["meta_versions"] == {}


def test_health_onboarding_absent_by_default_present_on_flag():
    # BUG-023's uncapped escape channel: byte-inert by default (dropping the flag ⇒ the
    # always-emits mutation this guards against), a complete install payload when requested.
    server, _ = build_real_server()
    assert "onboarding" not in content(tool_call(server, "hive_health", {}))
    snap = content(tool_call(server, "hive_health", {"include_onboarding": True}))
    payload = snap["onboarding"]
    for key in ("contract_version", "rules_block", "procedure", "hooks", "allowlist",
                "edge_cli", "identity", "kind_taxonomy"):
        assert key in payload, f"missing onboarding payload key: {key!r}"
