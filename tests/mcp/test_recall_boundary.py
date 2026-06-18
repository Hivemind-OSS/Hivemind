"""M06 hive_recall boundary: never-hallucinate (abstain ⇒ [] + trace_id, no
resurrect), the approved-only belt (★ — re-filters independent of the index),
neutral reference_context framing (never 'instructions'), and the trace_id join
key on hit AND abstain (the request_id→trace_id rename pinned)."""
from __future__ import annotations

from hive.domain.models import (
    ABSTAIN, CONFIDENT, EMPTY_NO_DATA, RecallDraft, RecallHit, RecallResult,
)
from tests.fakes._fakes import FakeIndex
from tests.mcp._helpers import build_real_server, content, tool_call, write_text


class _StubRecall:
    """A recall double returning a fixed RecallResult (decouples the belt from the
    gate). ``index`` is present only so an unrelated health probe would not crash."""
    index = FakeIndex()

    def __init__(self, result):
        self._result = result

    def recall(self, query, *, agent_id):
        return self._result


def _confident(eid, text, *, trace="T-abc", sim=0.9):
    return RecallResult(CONFIDENT, trace, (RecallHit(eid, text, sim),), 0.05, 0.5)


# ── abstain / empty ─────────────────────────────────────────────────────────────
def test_recall_empty_store_returns_empty_list_with_trace_id():
    server, _ = build_real_server()                        # nothing approved
    env = content(tool_call(server, "hive_recall", {"query": "anything"}))
    assert env["reference_context"] == []
    assert env["abstained"] is True
    assert env["state"] == EMPTY_NO_DATA
    assert isinstance(env["trace_id"], str) and env["trace_id"]
    assert "note" in env                                   # neutral note


def test_recall_abstain_returns_empty_list_with_trace_id():
    server, _ = build_real_server()
    server.recall = _StubRecall(RecallResult.abstain("T-xyz", 0.91, 0.01))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert env["reference_context"] == []
    assert env["abstained"] is True
    assert env["state"] == ABSTAIN
    assert env["trace_id"] == "T-xyz"


def test_recall_abstain_no_resurrect():
    """An abstained recall is never repopulated by the boundary — hits stay []."""
    server, _ = build_real_server()
    server.recall = _StubRecall(RecallResult.abstain("T-1", 0.8, 0.0))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert env["reference_context"] == [] and env["abstained"] is True


# ── ★ approved-only belt (the mutation target) ──────────────────────────────────
def test_recall_filters_to_approved_only():
    """A CONFIDENT hit whose row is still PENDING is dropped by the boundary belt;
    an empty post-belt set is an ABSTAIN, never a confident-empty. Deleting the
    belt guard surfaces the pending row ⇒ this assertion fails (mutation #2)."""
    server, _ = build_real_server()
    # a genuine PENDING row via the store substrate (the tool path always approves now);
    # the belt must still drop a non-approved candidate the stub recall surfaces.
    eid, _ = server.store.stage(text="staged but NOT approved", weight=1.0,
                                source="", tags="", proposed_by="x")
    server.recall = _StubRecall(_confident(eid, "staged but NOT approved"))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert env["reference_context"] == []                  # pending row filtered out
    assert env["abstained"] is True


def test_recall_surfaces_approved_hit_through_belt():
    server, _ = build_real_server()
    w = write_text(server, "an approved memory")           # lands approved in one call
    eid = w["id"]
    server.recall = _StubRecall(_confident(eid, "an approved memory"))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert len(env["reference_context"]) == 1
    assert env["reference_context"][0]["episode_id"] == eid
    assert env["abstained"] is False


# ── neutral framing + trace key ─────────────────────────────────────────────────
def test_recall_framed_as_reference_context_not_instructions():
    server, _ = build_real_server()
    write_text(server, "the gold memory about retries")    # approved on write
    env = content(tool_call(server, "hive_recall", {"query": "the gold memory about retries"}))
    assert "reference_context" in env
    assert "instructions" not in env and "command" not in env
    assert env["abstained"] is False
    hit = env["reference_context"][0]
    # trust + ts are the additive lifecycle labels (consumers discount provisional
    # content and order coexisting versions by them); polarity (do|dont|neutral) is the
    # carried-not-interpreted prohibition label.
    assert set(hit) == {"episode_id", "text", "sim", "trust", "ts", "polarity"}
    assert hit["polarity"] == "neutral"             # default when unset on write


def test_recall_trace_id_present_on_hit_and_abstain():
    server, _ = build_real_server()
    write_text(server, "memory for trace check")           # approved on write
    hit_env = content(tool_call(server, "hive_recall", {"query": "memory for trace check"}))
    empty_env = content(tool_call(server, "hive_recall", {"query": "x"}, req_id=2))
    # confident path (use a fresh server so the empty case is genuinely empty)
    assert hit_env["trace_id"]
    assert empty_env["trace_id"]


def test_recall_envelope_uses_trace_id_key_not_request_id():
    server, _ = build_real_server()
    env = content(tool_call(server, "hive_recall", {"query": "x"}))
    assert "trace_id" in env
    assert "request_id" not in env                         # the rename is pinned



# ── self_quarantine: the self-resurfacing draft channel (separate envelope key) ─
def test_recall_self_quarantine_serialized_with_quarantined_trust():
    """A RecallResult carrying drafts serializes them under a SEPARATE envelope key
    `self_quarantine`, each labeled trust='quarantined' — never mixed into
    reference_context (the trusted channel)."""
    server, _ = build_real_server()
    server.recall = _StubRecall(RecallResult.abstain(
        "T-d", 0.9, 0.0, (RecallDraft(7, "my own draft", 0.83, ts=11),)))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert env["reference_context"] == []                  # trusted channel empty
    assert env["self_quarantine"] == [
        {"episode_id": 7, "text": "my own draft", "sim": 0.83,
         "trust": "quarantined", "ts": 11}]


def test_recall_abstained_true_unaffected_by_drafts():
    """A populated self_quarantine on an abstained:true envelope is the contract —
    `abstained` is computed from the TRUSTED hits only, never from drafts."""
    server, _ = build_real_server()
    server.recall = _StubRecall(RecallResult.abstain(
        "T-d", 0.9, 0.0, (RecallDraft(7, "draft", 0.83),)))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert env["abstained"] is True and env["self_quarantine"]


def test_recall_self_quarantine_bypasses_servable_belt():
    """Drafts skip the approved-only/servable belt that gates trusted hits: a row
    that the belt WOULD drop (genuinely PENDING) still surfaces as a draft."""
    server, _ = build_real_server()
    eid, _ = server.store.stage(text="pending draft row", weight=1.0,
                                source="", tags="", proposed_by="agent")
    # same pending eid as a HIT would be belt-dropped (cf. approved-only test); as a
    # DRAFT it bypasses the belt entirely (no get_episode / is_servable gate)
    server.recall = _StubRecall(RecallResult.abstain(
        "T-d", 0.9, 0.0, (RecallDraft(eid, "pending draft row", 0.9),)))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert [d["episode_id"] for d in env["self_quarantine"]] == [eid]


def test_recall_self_quarantine_absent_when_no_drafts():
    """No drafts ⇒ the key is absent (the wire is byte-identical to pre-feature)."""
    server, _ = build_real_server()
    eid = write_text(server, "an approved memory")["id"]
    server.recall = _StubRecall(_confident(eid, "an approved memory"))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert "self_quarantine" not in env


def test_recall_confident_carries_both_channels():
    """CONFIDENT hits AND drafts coexist on distinct keys; drafts never enter
    reference_context."""
    server, _ = build_real_server()
    eid = write_text(server, "a trusted memory")["id"]
    server.recall = _StubRecall(RecallResult(
        CONFIDENT, "T-d", (RecallHit(eid, "a trusted memory", 0.95),), 0.05, 0.5,
        (RecallDraft(7, "my draft", 0.7),)))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert [h["episode_id"] for h in env["reference_context"]] == [eid]
    assert [d["episode_id"] for d in env["self_quarantine"]] == [7]
    assert env["abstained"] is False
