"""B3 — HivemindBackend: the MemoryBackend over the REAL in-process HiveMCPServer.

    recall  → hive_recall (envelope parsed: episode_ids + abstained + entropy_norm → RecallObs).
    propose → hive_capture under the SUBAGENT seat — lands QUARANTINED (a genuine subagent write).
    commit  → hive_write under the ORCHESTRATOR seat; post-B0 this dedups onto the quarantined
              capture and ESTABLISHES THAT ROW IN PLACE (same episode_id), so a denied proposal
              (never committed) simply decays. Per-seat identity goes to server.handle — no
              HTTP/token hop; the realism that matters (distinct seats over ONE shared store) holds.
    reset   → rebuild a fresh server from the injected factory (clean store per arm/seed).

The ``server_factory`` seam lets tests inject a fast FakeProvider-backed server
(``build_real_server``) and the real run inject a Qwen3-Embedding container — same backend code,
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
        # Coerce shape BEFORE indexing: a JSON-RPC error reply carries `error` and NO `result`, so an
        # unconditional result["content"] would KeyError instead of raising the intended RuntimeError
        # (BUG-002 defensive-envelope-parse class). Tool results always carry `content`; only the
        # protocol-error paths (unknown method/tool, internal error) hit the empty branch.
        result = resp.result or {}
        content = result.get("content") or []
        if not content:
            raise RuntimeError(f"hivemind {tool} error: {getattr(resp, 'error', None) or 'empty result'}")
        text = content[0].get("text", "")
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
        # hive_capture OMITS "id" on ANY non-storing status — the secret floor refused the text
        # (status="refused"), OR autonomy is disabled (status="disabled"). Read defensively and
        # signal "not stored" with an empty handle (driver skips commit on ""), never KeyError on
        # res["id"] (BUG-002/005 defensive-envelope class).
        mid = res.get("id")
        pid = str(mid) if mid is not None else ""
        return Proposal(proposal_id=pid, seat=seat, text=text, source_id=source_id)

    def commit(self, proposal: Proposal, *, approver: str) -> str:
        res = self._call("hive_write",
                         {"text": proposal.text, "approved_by": approver}, _ORCHESTRATOR_SEAT)
        # hive_write OMITS "id" when the write doesn't establish — the secret scanner refused the
        # text (status="refused", real with conversational data) or autonomy is disabled. The fact
        # simply isn't stored; signal "not committed" with an empty handle, never KeyError on ["id"].
        mid = res.get("id")
        return str(mid) if mid is not None else ""

    def supersede(self, loser_id: str, winner_text: str, *, approver: str) -> str:
        """Human-vouched correction: establish ``winner_text`` AND retire ``loser_id`` in one call
        via the shipped hive_write ``replaces=`` path — ``store.supersede`` deprecates and de-indexes
        the loser so it can never be recalled again, while the correction serves. A concrete-adapter
        method, deliberately NOT on the ``MemoryBackend`` Protocol (mem0 has no retraction analog);
        the bench reaches it through a getattr probe. Returns the new episode id, or "" if the write
        was refused (same defensive parse as ``commit``)."""
        res = self._call("hive_write",
                         {"text": winner_text, "approved_by": approver, "replaces": int(loser_id)},
                         _ORCHESTRATOR_SEAT)
        mid = res.get("id")
        return str(mid) if mid is not None else ""

    def reset(self) -> None:
        self._server = self._factory()
        self._ids = itertools.count(1)
