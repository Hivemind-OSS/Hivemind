"""Shared builders for the M06 MCP-surface tests. ``build_real_server`` wires the
REAL stack (sqlite :memory: store + authoritative ExhaustiveCosineIndex + the real
AdmissionService + RecallPipeline) behind the boundary, so a tool call exercises the
true write→recall→fetch path (a clean write lands APPROVED in one call); targeted
doubles (a counting admission, a stub recall, a counts-raising store) are monkeypatched
onto the built server per test.
"""
from __future__ import annotations

import json
import random

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.config import AutonomyConfig
from hive.app.mcp_server import HiveMCPServer, MCPRequest, ServerIdentity
from hive.domain.admission import AdmissionService
from hive.domain.lifecycle import DemandRule, LifecycleService, SurvivalRule
from hive.domain.recall import NormalizedEntropyGate, RecallPipeline
from hive.domain.surfacer import UtilitySurfacer
from tests.fakes._fakes import (
    FakeClock, FakeInstallPlanner, FakeProvider, FakeScanner, FakeUtilityStore,
)

_DAY_S = 86_400


def build_real_server(*, d: int = 64, h: float = 0.5, beta: float = 16.0,
                      top_n: int = 10, t0: int = 1000,
                      scanner=None, planner=None, autonomy=None):
    """Return (server, clock). ``clock`` is mutable so tests can stamp distinct ts.
    The FULL trust-lifecycle is wired (real DemandRule + LifecycleService on the
    real store), mirroring build_container — pass ``autonomy`` to tune the knobs."""
    # production semantics (isolation_level=None, WAL, FKs) INCLUDING the thread
    # posture: build_container shares one conn across HTTP handler threads
    # (lock-serialized), so the helper mirrors check_same_thread=False too.
    conn = connect(":memory:", check_same_thread=False)
    index = ExhaustiveCosineIndex(dim=d)
    store = SqliteEpisodeStore(conn, index=index)
    embedder = FakeProvider(d=d)
    scanner = scanner or FakeScanner()
    clock = FakeClock(t0)
    aut = autonomy or AutonomyConfig()
    lifecycle = LifecycleService(
        store=store, index=index,
        rule=DemandRule(demand_m=aut.demand_m, demand_tau=aut.demand_tau,
                        competitor_tau=aut.competitor_tau,
                        solo_mode=aut.solo_mode,
                        solo_min_span_days=aut.solo_min_span_days),
        now=clock.now,
        demand_window_s=aut.demand_window_days * _DAY_S,
        quarantine_ttl_s=aut.quarantine_ttl_days * _DAY_S,
        provisional_ttl_s=aut.provisional_ttl_days * _DAY_S,
        enabled=aut.enabled,
        survival_rule=SurvivalRule(
            survival_e=aut.survival_e, survival_days=aut.survival_days,
            survival_min_exposures=aut.survival_min_exposures))
    admission = AdmissionService(store, scanner, embedder, now=clock.now,
                                 lifecycle=lifecycle, autonomy_enabled=aut.enabled)
    recall = RecallPipeline(
        embedder=embedder, index=index, gate=NormalizedEntropyGate(h, beta),
        surfacer=UtilitySurfacer(enabled=False, epsilon_explore=0.0, f_min=0.5,
                                 f_max=1.5, rng=random.Random(0)),
        reader=store, utility_store=FakeUtilityStore(),
        recall_top_n=top_n,
        ledger=store, clock_now=clock.now, scanner=scanner,
        provisional_ttl_s=aut.provisional_ttl_days * _DAY_S,
        lifecycle=lifecycle, autonomy_enabled=aut.enabled)
    planner = planner or FakeInstallPlanner()
    server = HiveMCPServer(
        admission=admission, recall=recall, store=store, embedder=embedder,
        install_planner=planner, identity=ServerIdentity("default", "agent"),
        now=clock.now, started_ts=t0, db_path="", autonomy=aut)
    return server, clock


# ── call helpers ──────────────────────────────────────────────────────────────
def tool_call(server, name, args=None, req_id=1):
    return server.handle(MCPRequest(req_id, "tools/call",
                                    {"name": name, "arguments": args or {}}))


def content(resp) -> dict:
    """Parse the tool-result content[0].text JSON payload."""
    return json.loads(resp.result["content"][0]["text"])


def is_error(resp) -> bool:
    return bool(resp.result.get("isError"))


def write_text(server, text, approved_by="user", **kw):
    """Client-gated write: approved_by is required by the schema belt, so default it
    here (callers can override via kw)."""
    return content(tool_call(server, "hive_write", {"text": text, "approved_by": approved_by, **kw}))
