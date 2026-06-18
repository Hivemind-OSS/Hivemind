"""B3 — HivemindBackend: the MemoryBackend over the REAL in-process HiveMCPServer.

    recall  → hive_recall (envelope parsed: episode_ids + abstained + entropy_norm → RecallObs).
    propose → hive_capture under the SUBAGENT seat — lands QUARANTINED (a genuine subagent write).
    commit  → hive_write under the ORCHESTRATOR seat; post-B0 this dedups onto the quarantined
              capture and ESTABLISHES THAT ROW IN PLACE (same episode_id), so a denied proposal
              (never committed) simply decays. Per-seat identity goes to server.handle — no
              HTTP/token hop; the realism that matters (distinct seats over ONE shared store) holds.
    reset   → rebuild a fresh server from the injected factory (clean store per arm/seed).

The ``server_factory`` seam lets tests inject a fast FakeProvider-backed server
(``build_real_server``) and the real run inject a bge-small container — same backend code,
swapped embedder. The backend tunes nothing itself: the factory is responsible for making the
orchestrator the SOLE promotion authority (``autonomy.demand_m`` huge) so only ``commit`` serves.
"""
from __future__ import annotations

import itertools
import json
from typing import Callable

from hive.app.mcp_server import HiveMCPServer, MCPRequest, ServerIdentity
from hive.research.bench.backends import Proposal, RecallObs

_ORCHESTRATOR_SEAT = "orchestrator"


class HivemindBackend:
    def __init__(self, server_factory: Callable[[], HiveMCPServer]) -> None:
        self._factory = server_factory
        self._server = server_factory()
        self._ids = itertools.count(1)

    def _call(self, tool: str, args: dict, seat: str) -> dict:
        resp = self._server.handle(
            MCPRequest(next(self._ids), "tools/call", {"name": tool, "arguments": args}),
            identity=ServerIdentity("default", seat))
        result = resp.result or {}
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise RuntimeError(f"hivemind {tool} error: {text}")
        return json.loads(text)

    def recall(self, seat: str, query: str) -> RecallObs:
        env = self._call("hive_recall", {"query": query}, seat)
        hits = env.get("reference_context") or []
        abstained = bool(env.get("abstained", True))
        ranked = () if abstained else tuple(str(h["episode_id"]) for h in hits)
        top_score = float(hits[0]["sim"]) if hits else 0.0
        # native abstention signal: when Hivemind commits to an answer, 1 - entropy_norm (peaked
        # ⇒ confident); when it abstains (gate fired OR empty store, which reports entropy_norm
        # 0.0) it carries NO confidence to answer ⇒ 0.0. Never read 1 - entropy on an abstain.
        entropy = float(env.get("entropy_norm", 1.0))
        confidence = 0.0 if abstained else max(0.0, min(1.0, 1.0 - entropy))
        return RecallObs(query=query, ranked_ids=ranked, top_score=top_score,
                         confidence=confidence, abstained=abstained)

    def propose(self, seat: str, text: str, *, source_id: str) -> Proposal:
        res = self._call("hive_capture", {"text": text}, seat)
        # the secret floor may refuse a candidate ⇒ empty handle (the driver skips commit on "").
        pid = "" if res.get("status") == "refused" else str(res["id"])
        return Proposal(proposal_id=pid, seat=seat, text=text, source_id=source_id)

    def commit(self, proposal: Proposal, *, approver: str) -> str:
        res = self._call("hive_write",
                         {"text": proposal.text, "approved_by": approver}, _ORCHESTRATOR_SEAT)
        # hive_write OMITS "id" when the write doesn't establish — the secret scanner refused the
        # text (status="refused", real with conversational data) or autonomy is disabled. The fact
        # simply isn't stored; signal "not committed" with an empty handle, never KeyError on ["id"].
        mid = res.get("id")
        return str(mid) if mid is not None else ""

    def reset(self) -> None:
        self._server = self._factory()
        self._ids = itertools.count(1)
