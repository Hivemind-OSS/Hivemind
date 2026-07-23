"""M06 hive_recall boundary: never-hallucinate (abstain ⇒ [] + trace_id, no
resurrect), the approved-only belt (★ — re-filters independent of the index),
neutral reference_context framing (never 'instructions'), and the trace_id join
key on hit AND abstain (the request_id→trace_id rename pinned)."""
from __future__ import annotations

import dataclasses

from hive.domain.models import (
    ABSTAIN, CONFIDENT, EMPTY_NO_DATA, RecallHit,
    RecallResult,
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
    return RecallResult(CONFIDENT, trace, (RecallHit(eid, text, sim),), sim)


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
    server.recall = _StubRecall(RecallResult.abstain("T-xyz", 0.91))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert env["reference_context"] == []
    assert env["abstained"] is True
    assert env["state"] == ABSTAIN
    assert env["trace_id"] == "T-xyz"


def test_recall_abstain_no_resurrect():
    """An abstained recall is never repopulated by the boundary — hits stay []."""
    server, _ = build_real_server()
    server.recall = _StubRecall(RecallResult.abstain("T-1", 0.8))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert env["reference_context"] == [] and env["abstained"] is True


# ── ★ approved-only belt (the mutation target) ──────────────────────────────────
def test_recall_filters_to_approved_only():
    """A CONFIDENT hit whose row is still PENDING is dropped by the boundary belt;
    an empty post-belt set is an ABSTAIN, never a confident-empty. Deleting the
    belt guard surfaces the pending row ⇒ this assertion fails (mutation #2)."""
    server, _ = build_real_server()
    # a genuine PENDING row via the store substrate (the tool path always materializes);
    # the belt must still drop a non-materialized candidate the stub recall surfaces.
    eid, _ = server.store.stage(text="staged but NOT approved", weight=1.0,
                                proposed_by="x")
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
    # trust + ts are the lifecycle labels (consumers discount provisional content and
    # order coexisting versions by them); polarity/kind are carried-not-interpreted;
    # repos/anchors/drift are the v3 §3.4 scope + staleness carriers on EVERY hit.
    assert set(hit) == {"episode_id", "text", "sim", "trust", "ts", "polarity",
                        "kind", "repos", "anchors", "drift"}
    assert hit["polarity"] == "neutral"             # defaults when unset on write
    assert hit["kind"] == "note"
    assert hit["repos"] == [] and hit["anchors"] == []
    assert hit["drift"] == {"type": "n/a", "detail": {"per_anchor": []}}


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



# ── NEW GUARD: the self-quarantine drafts channel is GONE ──────────────────────
def test_recallresult_has_no_drafts_field():
    """The self-quarantine resurfacing channel was removed — RecallResult no longer
    carries a `drafts` field (re-adding it REDS this). Surgical: asserts the field's
    absence directly on the dataclass."""
    assert "drafts" not in {f.name for f in dataclasses.fields(RecallResult)}


def test_confident_envelope_has_no_self_quarantine_key():
    """A recall envelope never carries a `self_quarantine` key — the drafts channel
    was removed (re-adding the mcp serialization block REDS this)."""
    server, _ = build_real_server()
    eid = write_text(server, "a trusted memory")["id"]
    server.recall = _StubRecall(_confident(eid, "a trusted memory"))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert "self_quarantine" not in env


# ── NEW GUARD: the associations channel is GONE (the cut is total) ──────────────
def test_confident_envelope_has_no_associations_key():
    """A CONFIDENT recall envelope never carries an `associations` key — the
    co-access neighbor channel was removed. The model field itself is gone, so
    re-adding the field (and the mcp serialization block it feeds) REDS this."""
    # structural: re-adding the RecallResult.associations field reds here directly
    assert "associations" not in {f.name for f in dataclasses.fields(RecallResult)}
    server, _ = build_real_server()
    eid = write_text(server, "a trusted memory")["id"]
    server.recall = _StubRecall(_confident(eid, "a trusted memory"))
    env = content(tool_call(server, "hive_recall", {"query": "q"}))
    assert "associations" not in env
