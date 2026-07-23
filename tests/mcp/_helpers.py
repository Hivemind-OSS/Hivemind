"""Shared builders for the M06 MCP-surface tests. ``build_real_server`` wires the
REAL stack (sqlite :memory: store + authoritative ExhaustiveCosineIndex + the real
AdmissionService + RecallPipeline) behind the boundary, so a tool call exercises the
true write→recall→fetch path (a clean v3 write lands trust=provisional, servable
NOW, in one call); targeted doubles (a counting admission, a stub recall, a
counts-raising store) are monkeypatched onto the built server per test.
"""

from __future__ import annotations

import json

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.config import AutonomyConfig
from hive.app.mcp_server import HiveMCPServer, MCPRequest, ServerIdentity
from hive.domain.admission import AdmissionService
from hive.domain.lifecycle import DemandRule, LifecycleService
from hive.domain.recall import AbsoluteRelevanceGate, RecallPipeline
from tests.fakes._fakes import FakeClock, FakeProvider, FakeScanner

_DAY_S = 86_400


def build_real_server(
    *,
    d: int = 64,
    tau_serve: float = 0.70,
    k_min: int = 1,
    top_n: int = 10,
    t0: int = 1000,
    scanner=None,
    autonomy=None,
    embedder=None,
    conflict=None,
    suspect_consensus=None,
    secret_scan_enabled: bool = True,
    canonical_ref: str = "",
):
    """Return (server, clock). ``clock`` is mutable so tests can stamp distinct ts.
    The FULL trust-lifecycle is wired (real DemandRule + LifecycleService on the
    real store), mirroring build_container — pass ``autonomy`` to tune the knobs.
    Pass ``conflict`` (a ConflictConfig) to enable conflict surfacing + the advisory
    flag service; ``embedder`` overrides the default FakeProvider (e.g. a
    FakeClusterProvider for controllable near-dups)."""
    # production semantics (isolation_level=None, WAL, FKs) INCLUDING the thread
    # posture: build_container shares one conn across HTTP handler threads
    # (lock-serialized), so the helper mirrors check_same_thread=False too.
    conn = connect(":memory:", check_same_thread=False)
    index = ExhaustiveCosineIndex(dim=d)
    store = SqliteEpisodeStore(conn, index=index)
    embedder = embedder or FakeProvider(d=d)
    scanner = scanner or FakeScanner()
    clock = FakeClock(t0)
    aut = autonomy or AutonomyConfig()
    lifecycle = LifecycleService(
        store=store,
        index=index,
        rule=DemandRule(
            demand_m=aut.demand_m,
            demand_tau=aut.demand_tau,
            competitor_tau=aut.competitor_tau,
        ),
        now=clock.now,
        demand_window_s=aut.demand_window_days * _DAY_S,
        quarantine_ttl_s=aut.quarantine_ttl_days * _DAY_S,
        provisional_ttl_s=aut.provisional_ttl_days * _DAY_S,
        enabled=aut.enabled,
        anomaly_tau=aut.anomaly_tau,
        anomaly_min_cluster=aut.anomaly_min_cluster,
        verified_reader=store if aut.verified_promotion else None,
    )
    admission = AdmissionService(
        store,
        scanner,
        embedder,
        now=clock.now,
        lifecycle=lifecycle,
        autonomy_enabled=aut.enabled,
    )
    recall = RecallPipeline(
        embedder=embedder,
        index=index,
        gate=AbsoluteRelevanceGate(tau_serve, k_min),
        reader=store,
        recall_top_n=top_n,
        ledger=store,
        clock_now=clock.now,
        scanner=scanner,
        provisional_ttl_s=aut.provisional_ttl_days * _DAY_S,
        lifecycle=lifecycle,
        autonomy_enabled=aut.enabled,
        dup_tau=(conflict.tau if conflict is not None else 0.80),
        conflict_enabled=(conflict.enabled if conflict is not None else False),
        conflict_top_n=(conflict.top_n if conflict is not None else 10),
    )
    flag_service = None
    if conflict is not None:
        from hive.domain.conflict import ConflictFlagService

        flag_service = ConflictFlagService(
            flag_store=store,
            scanner=scanner,
            reader=store,
            now=clock.now,
            enabled=conflict.enabled,
        )
    server = HiveMCPServer(
        admission=admission,
        recall=recall,
        store=store,
        embedder=embedder,
        identity=ServerIdentity("default", "agent"),
        now=clock.now,
        started_ts=t0,
        db_path="",
        autonomy=aut,
        conflict=conflict,
        flag_service=flag_service,
        suspect_consensus=suspect_consensus,
        secret_scan_enabled=secret_scan_enabled,
        canonical_ref=canonical_ref,
    )
    return server, clock


# ── call helpers ──────────────────────────────────────────────────────────────
def tool_call(server, name, args=None, req_id=1):
    return server.handle(
        MCPRequest(req_id, "tools/call", {"name": name, "arguments": args or {}})
    )


def content(resp) -> dict:
    """Parse the tool-result content[0].text JSON payload."""
    return json.loads(resp.result["content"][0]["text"])


def is_error(resp) -> bool:
    return bool(resp.result.get("isError"))


def write_text(server, text, **kw):
    """The v3 write: ``hive_write {text, ...}`` — no approver field exists; the row
    lands trust=provisional and serves NOW."""
    return content(tool_call(server, "hive_write", {"text": text, **kw}))


def register_repo(server, name: str, *, canonical_ref: str = "", ts: int = 0):
    """Register a repo through the v3 store surface so anchors/repos scope args pass
    the boundary grammar gate."""
    server.store.repo_add(
        name=name,
        url=f"https://example.invalid/{name}.git",
        canonical_ref=canonical_ref,
        added_ts=int(ts),
    )
