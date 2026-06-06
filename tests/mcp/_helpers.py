"""Shared builders for the M06 MCP-surface tests. ``build_real_server`` wires the
REAL stack (sqlite :memory: store + authoritative ExhaustiveCosineIndex + the real
AdmissionService + RecallPipeline) behind the boundary, so a tool call exercises the
true write→approve→recall→fetch path; targeted doubles (a counting admission, a stub
recall, a counts-raising store) are monkeypatched onto the built server per test.
"""
from __future__ import annotations

import json
import random
import sqlite3

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.mcp_server import HiveMCPServer, MCPRequest, ServerIdentity
from hive.domain.admission import AdmissionService
from hive.domain.recall import NormalizedEntropyGate, RecallPipeline
from hive.domain.surfacer import UtilitySurfacer
from tests.fakes._fakes import (
    FakeClock, FakeInstallPlanner, FakeProvider, FakeScanner, FakeUtilityStore,
)


def build_real_server(*, d: int = 64, h: float = 0.5, beta: float = 16.0,
                      top_n: int = 10, t0: int = 1000, trailer: str = "Hive-Trace",
                      scanner=None, planner=None):
    """Return (server, clock). ``clock`` is mutable so tests can stamp distinct ts."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    index = ExhaustiveCosineIndex(dim=d)
    store = SqliteEpisodeStore(conn, index=index)
    embedder = FakeProvider(d=d)
    scanner = scanner or FakeScanner()
    clock = FakeClock(t0)
    admission = AdmissionService(store, scanner, embedder, now=clock.now)
    recall = RecallPipeline(
        embedder=embedder, index=index, gate=NormalizedEntropyGate(h, beta),
        surfacer=UtilitySurfacer(enabled=False, epsilon_explore=0.0, f_min=0.5,
                                 f_max=1.5, rng=random.Random(0)),
        ledger=store, reader=store, utility_store=FakeUtilityStore(),
        recall_top_n=top_n, now=clock.now)
    planner = planner or FakeInstallPlanner(stamp_trailer=trailer)
    server = HiveMCPServer(
        admission=admission, recall=recall, store=store, embedder=embedder,
        install_planner=planner, identity=ServerIdentity("default", "agent"),
        now=clock.now, started_ts=t0, db_path="", trailer_key=trailer)
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


def write_text(server, text, **kw):
    return content(tool_call(server, "hive_write", {"text": text, **kw}))


def approve(server, ids, approver="user"):
    return content(tool_call(server, "hive_approve", {"ids": list(ids), "approver": approver}))
