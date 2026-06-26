"""M06 hive_health: the happy snapshot, the fail-closed {ok,error,db_path}-only
subset on a probe failure, and the no-secret invariant on the serialized envelope."""
from __future__ import annotations

import json

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
    assert set(snap.keys()) == {"ok", "error", "db_path"}  # fail-closed subset ONLY


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


def test_health_has_no_contested():
    # the contested-memory review queue was cut; include_gaps still serves the demand-gap
    # report, but never a contested block (no _contested_report wiring remains).
    server, _ = build_real_server()
    snap = content(tool_call(server, "hive_health", {"include_gaps": True}))
    assert "gaps" in snap                                     # the surviving demand-gap channel
    assert "contested" not in snap and "contested_note" not in snap
    assert not hasattr(server, "_contested_report")
