"""Chunk 1 — the per-request identity seam.

``handle(req, *, identity=...)`` threads the CALLER identity to the handlers so write
attribution (``proposed_by``) and recall logging (``agent_id``) are per-request, while
``identity=None`` falls back to the process ``ServerIdentity`` — the proof the stdio path
and the whole existing suite are unchanged. Attribution lives in the one module that owns
it (the handlers); the transport only resolves WHO is calling. The deliberate mutation
(``_handle_write`` reads ``self.identity`` instead of the passed ``identity``) makes
``test_write_attributes_proposed_by_to_passed_identity`` go red.
"""
from __future__ import annotations

from hive.app.mcp_server import MCPRequest, ServerIdentity
from tests.mcp._helpers import build_real_server, content


def _write_req(text: str, approved_by: str = "user") -> MCPRequest:
    return MCPRequest(1, "tools/call",
                      {"name": "hive_write",
                       "arguments": {"text": text, "approved_by": approved_by}})


def test_write_attributes_proposed_by_to_passed_identity():
    server, _ = build_real_server()
    ident = ServerIdentity(tenant_id="default", agent_id="alice-laptop")
    resp = server.handle(_write_req("a durable note about cache invalidation"), identity=ident)
    eid = content(resp)["id"]
    ep = server.store.get_episode(eid)
    assert ep is not None
    # the caller's VERIFIED label, never the process default — the mutation target
    assert ep.proposed_by == "alice-laptop"


def test_write_falls_back_to_process_identity_when_no_identity():
    server, _ = build_real_server()                  # process identity is ("default", "agent")
    resp = server.handle(_write_req("another durable note about backpressure"))
    eid = content(resp)["id"]
    assert server.store.get_episode(eid).proposed_by == server.identity.agent_id == "agent"


def test_recall_logs_under_passed_identity():
    """The agent_id reaching the recall pipeline is the per-request caller, not the process
    default — recall becomes per-caller-correct (the win the rejected design missed)."""
    server, _ = build_real_server()
    seen: dict[str, str] = {}
    real_recall = server.recall.recall

    def spy(query, *, agent_id):
        seen["agent_id"] = agent_id
        return real_recall(query, agent_id=agent_id)

    server.recall.recall = spy  # type: ignore[assignment]
    ident = ServerIdentity(tenant_id="default", agent_id="bob-desktop")
    server.handle(MCPRequest(2, "tools/call",
                  {"name": "hive_recall", "arguments": {"query": "anything"}}),
                  identity=ident)
    assert seen["agent_id"] == "bob-desktop"


def test_recall_falls_back_to_process_identity_when_no_identity():
    server, _ = build_real_server()
    seen: dict[str, str] = {}
    real_recall = server.recall.recall

    def spy(query, *, agent_id):
        seen["agent_id"] = agent_id
        return real_recall(query, agent_id=agent_id)

    server.recall.recall = spy  # type: ignore[assignment]
    server.handle(MCPRequest(3, "tools/call",
                  {"name": "hive_recall", "arguments": {"query": "anything"}}))
    assert seen["agent_id"] == server.identity.agent_id == "agent"
