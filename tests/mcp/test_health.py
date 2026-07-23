"""M06 hive_health: the happy snapshot, the fail-closed {ok,error,db_path}-only
subset on a probe failure, the no-secret invariant on the serialized envelope, and
the v3 deletions (no contract_version beacon, no include_onboarding channel)."""

from __future__ import annotations

import json

from tests.mcp._helpers import build_real_server, content, tool_call, write_text


def test_health_happy_snapshot():
    server, _ = build_real_server()
    # v3 writes land approved+provisional directly — no pending queue, n_pending stays 0
    write_text(server, "first written memory")
    write_text(server, "second written memory")
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is True
    assert snap["tenant_id"] == "default"
    assert snap["n_episodes"] == 2 and snap["n_pending"] == 0
    assert snap["index_authoritative"] is True
    assert snap["embedder_loaded"] is True
    assert snap["d"] == 64
    assert snap["uptime_s"] >= 0
    assert "contract_version" not in snap  # the beacon is DELETED (v3)


def test_health_fail_closed_subset():
    server, _ = build_real_server()

    def _boom():
        raise RuntimeError("db gone")

    server.store.counts = _boom
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is False
    # fail-closed subset ONLY (the leak guard). EXACT, never a superset: no db
    # internals — and no beacon (v3) — may join it.
    assert set(snap.keys()) == {"ok", "error", "db_path"}


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
    server, _ = build_real_server()  # :memory: ⇒ db_path=""
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is True
    assert snap["store_ephemeral"] is True


def test_health_omits_store_ephemeral_when_persistent():
    # a persistent store (db_path set) ⇒ the key is ABSENT ⇒ the envelope is byte-identical.
    server, _ = build_real_server()
    server.db_path = "/data/shared.db"  # the handler reads self.db_path
    snap = content(tool_call(server, "hive_health", {}))
    assert "store_ephemeral" not in snap


def test_health_has_no_contested():
    # the contested-memory review queue was cut; include_gaps still serves the demand-gap
    # report, but never a contested block (no _contested_report wiring remains).
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {"include_gaps": True}))
    assert "gaps" in snap  # the surviving demand-gap channel
    assert "contested" not in snap and "contested_note" not in snap
    assert not hasattr(server, "_contested_report")


def test_health_meta_versions_absent_by_default():
    # the meta envelope law's observability channel is byte-inert until requested:
    # dropping the request gate ⇒ the always-emits mutation this pins against.
    server, _ = build_real_server()
    write_text(
        server, "a memory", meta={"matrix/subgraph_fp": "matrix-subgraph-fp/1:aaa"}
    )
    snap = content(tool_call(server, "hive_health", {}))
    assert snap["ok"] is True
    assert "meta_versions" not in snap


def test_health_meta_versions_histogram_end_to_end():
    # Seed through the REAL capture/write path: mixed versions, a malformed value, bare
    # rows, and a deprecated (retired) carrier — the histogram counts the LIVE corpus
    # (servable + quarantined), buckets per version prefix, and never reads a body.
    server, clock = build_real_server()
    write_text(
        server,
        "provisional v1 carrier",
        meta={"matrix/subgraph_fp": "matrix-subgraph-fp/1:aaa"},
    )
    content(
        tool_call(
            server,
            "hive_capture",
            {  # quarantined counts too
                "text": "quarantined v1 carrier",
                "meta": {"matrix/subgraph_fp": "matrix-subgraph-fp/1:aaa"},
            },
        )
    )
    write_text(
        server,
        "future v2 carrier",
        meta={"matrix/subgraph_fp": "matrix-subgraph-fp/2:bbb"},
    )
    write_text(server, "malformed carrier", meta={"matrix/subgraph_fp": "garbage"})
    write_text(server, "bare row, no meta")
    winner = write_text(server, "the superseding successor")["id"]
    loser = write_text(
        server,
        "retired v1 carrier",
        meta={"matrix/subgraph_fp": "matrix-subgraph-fp/1:zzz"},
    )["id"]
    # retire at the STORE layer (the tool verb is machine-gated; the histogram's
    # live-corpus exclusion is what is under test here, not the gate)
    assert server.store.supersede(loser, winner, actor="test", ts=int(clock.now()))
    snap = content(tool_call(server, "hive_health", {"include_meta_versions": True}))
    assert snap["ok"] is True
    # deprecated excluded (else "1" would read 3); absent counts the two bare live rows;
    # malformed is present because nonzero.
    assert snap["meta_versions"] == {
        "matrix/subgraph_fp": {
            "versions": {"1": 2, "2": 1},
            "absent": 2,
            "malformed": 1,
        }
    }


def test_health_meta_versions_empty_corpus():
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {"include_meta_versions": True}))
    assert snap["ok"] is True
    assert snap["meta_versions"] == {}


def test_health_include_onboarding_channel_is_gone():
    # v3 (CT-11 twin): the install-payload channel is DELETED — the flag is not
    # advertised, and passing it anyway serves NO onboarding key (ignored-extra).
    from hive.app.tool_defs import TOOL_DEFINITIONS

    health = next(t for t in TOOL_DEFINITIONS if t["name"] == "hive_health")
    assert "include_onboarding" not in health["inputSchema"]["properties"]
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {"include_onboarding": True}))
    assert "onboarding" not in snap
