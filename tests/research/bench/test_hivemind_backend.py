"""B3: HivemindBackend plumbing through the REAL in-process server (fast FakeProvider embedder,
no model download). Asserts the trust-ladder flow end-to-end: propose lands QUARANTINED
(unservable), commit ESTABLISHES THAT ROW IN PLACE (same episode_id — the B0 fix), denied
proposals never serve, recall parses the envelope, and the store is shared across seats."""
from __future__ import annotations

from types import SimpleNamespace

from hive.app.config import AutonomyConfig
from hive.research.bench.backends import MemoryBackend, Proposal
from hive.research.bench.hivemind_backend import HivemindBackend
from tests.mcp._helpers import build_real_server


def _make_server():
    # demand_m huge ⇒ demand-promotion can never fire ⇒ commit (the orchestrator) is the SOLE
    # path to a servable memory; enabled=True so propose still lands quarantined.
    server, _clock = build_real_server(autonomy=AutonomyConfig(demand_m=10**9))
    return server


def _backend():
    return HivemindBackend(_make_server)


def test_conformance():
    assert isinstance(_backend(), MemoryBackend)


def test_recall_on_empty_store_abstains():
    obs = _backend().recall("sub-a", "anything at all")
    assert obs.abstained is True and obs.ranked_ids == () and obs.confidence == 0.0


def test_propose_lands_quarantined_unservable():
    b = _backend()
    p = b.propose("sub-a", "staging reads config from /etc/app/staging.conf", source_id="s1")
    assert p.proposal_id and p.proposal_id.isdigit()        # a real quarantined episode id
    # quarantined ⇒ NOT servable: an exact-text recall still abstains
    assert b.recall("sub-a", "staging reads config from /etc/app/staging.conf").abstained is True


def test_commit_establishes_the_quarantined_row_in_place():
    b = _backend()
    text = "the migration renamed users.email to users.email_address"
    p = b.propose("sub-a", text, source_id="s1")
    mid = b.commit(p, approver="orchestrator")
    assert mid == p.proposal_id                             # B0: same row, promoted in place
    obs = b.recall("sub-b", text)                           # a DIFFERENT seat, one shared store
    assert obs.abstained is False and mid in obs.ranked_ids and obs.confidence > 0.0


def test_denied_proposal_never_serves():
    b = _backend()
    p = b.propose("sub-a", "a candidate the orchestrator will reject", source_id="s1")
    assert p.proposal_id                                    # it WAS written (a genuine subagent write)
    # DENY = the driver never commits ⇒ the quarantined capture stays unservable
    assert b.recall("sub-a", "a candidate the orchestrator will reject").abstained is True


def test_reset_clears_the_store():
    b = _backend()
    text = "use BEGIN IMMEDIATE for the writer lane"
    b.commit(b.propose("sub-a", text, source_id="s1"), approver="orchestrator")
    assert b.recall("sub-a", text).abstained is False
    b.reset()
    assert b.recall("sub-a", text).abstained is True


def test_commit_handles_a_refused_write_without_crashing():
    # hive_write returns {status:"refused"} (no "id") when the secret scanner rejects text — real
    # with conversational data. commit must return an empty handle, never KeyError on res["id"].
    class _RefuseServer:
        def handle(self, req, *, identity):
            return SimpleNamespace(result={"content": [
                {"text": '{"status": "refused", "reason": "secret-shaped"}'}]})
    b = HivemindBackend(lambda: _RefuseServer())
    mid = b.commit(Proposal(proposal_id="1", seat="sub-a", text="sk-proj-xxxx", source_id="s1"),
                   approver="orchestrator")
    assert mid == ""


def test_call_raises_runtime_error_on_a_jsonrpc_error_envelope():
    # a protocol-error reply has error=... and NO result; _call must raise the intended RuntimeError,
    # never KeyError on an unconditional result["content"] index (BUG-003 / BUG-002 class).
    import pytest

    class _ErrServer:
        def handle(self, req, *, identity):
            return SimpleNamespace(result=None, error={"code": -32601, "message": "method not found"})
    b = HivemindBackend(lambda: _ErrServer())
    with pytest.raises(RuntimeError, match="hivemind .* error"):
        b.recall("sub-a", "anything")
