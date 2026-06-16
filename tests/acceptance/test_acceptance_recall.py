"""#1/#2/#3/#5b acceptance — the read path end-to-end on the REAL bge stack:
recall@5 floor, honest-abstention AUROC, abstain-no-resurrect, secret-refused-never-written,
and reference-context framing. Wired through the real build_container; FakeOutcomeSource
is not needed here (no producer)."""
from __future__ import annotations

import json

import pytest

from hive.app.mcp_server import MCPRequest
from hive.domain.errors import SecretRefused
from hive.domain.models import CONFIDENT, AgentContext, content_hash
from hive.research.metrics_ir import abstention_auroc, recall_at_k
from tests.acceptance.conftest import (
    CORPUS, HIT_QUERIES, MISS_QUERIES, build_acc, seed_corpus,
)

_CTX = AgentContext(workflow="general")


def _topk_ids(container, query, k=5):
    r = container.recall.recall(query, agent_id="acc", agent_ctx=_CTX)
    return [h.episode_id for h in r.hits][:k], r


# ── #1 — recall@5 ≥ 0.33 ──────────────────────────────────────────────────────
def test_acceptance_recall_at_5_meets_floor(embedder_v1):
    c = build_acc(embedder_v1)
    eids = seed_corpus(c)
    scores = []
    for q, gold in HIT_QUERIES:
        ids, _ = _topk_ids(c, q, 5)
        scores.append(recall_at_k([str(i) for i in ids], {str(eids[gold])}, 5))
    mean_r5 = sum(scores) / len(scores)
    assert mean_r5 >= 0.33, f"recall@5={mean_r5:.3f} below the floor 0.33"


# ── #2 — honest-abstention AUROC in band ──────────────────────────────────────
def test_acceptance_abstention_auroc_in_band(embedder_v1):
    """The gate's confidence (1 − H/ln N_eff) separates retrieved-gold HITS from MISSES.
    Live-wired to the real abstention_auroc scorer [C2]. The synthetic fixture separates
    more cleanly than the real LongMemEval ≈0.77, so the band floor is a safe 0.70."""
    c = build_acc(embedder_v1)
    eids = seed_corpus(c)
    confidences: list[float] = []
    is_miss: list[bool] = []
    for q, gold in HIT_QUERIES:
        ids, r = _topk_ids(c, q, 5)
        confidences.append(1.0 - float(r.entropy_norm))
        is_miss.append(eids[gold] not in ids)        # a false-abstain counts as a miss
    for q in MISS_QUERIES:
        _, r = _topk_ids(c, q, 5)
        confidences.append(1.0 - float(r.entropy_norm))
        is_miss.append(True)                          # no gold in the corpus ⇒ should abstain
    auroc = abstention_auroc(confidences, is_miss)
    assert 0.70 <= auroc <= 1.0, f"abstention AUROC={auroc:.3f} outside the band"


# ── #3 — never-hallucinate / abstain-no-resurrect ─────────────────────────────
def test_acceptance_abstain_no_resurrect_end_to_end(embedder_v1):
    c = build_acc(embedder_v1)
    seed_corpus(c)
    for q in MISS_QUERIES:
        ids, r = _topk_ids(c, q, 5)
        assert r.state != CONFIDENT                   # off-distribution ⇒ abstain/empty
        assert ids == []                              # no hit can be resurrected past the gate


# ── #5b — a refused (secret) write reaches no row and is NEVER recallable ──────
def test_acceptance_secret_write_refused_and_never_recallable(embedder_v1):
    c = build_acc(embedder_v1)
    # a real credential — the deterministic scan must REFUSE it on the direct (queue-less)
    # write path; nothing is staged, so even its exact text can never be surfaced.
    secret_topic = ("rotate the leaked key AKIAIOSFODNN7EXAMPLE noted in the moonshot "
                    "quarterly revenue review")
    with pytest.raises(SecretRefused):
        c.admission.write(secret_topic, proposed_by="acc", approved_by="acc")
    c.build_index()
    ids, r = _topk_ids(c, secret_topic, 5)            # query its EXACT text
    assert ids == []                                  # nothing written ⇒ nothing recallable
    assert r.state != CONFIDENT
    assert c.store.conn.execute(                                  # no blob either
        "SELECT 1 FROM blobs WHERE content_hash=?", (content_hash(secret_topic),)).fetchone() is None


# ── #5c — recalled text is reference_context, never instructions ──────────────
def test_acceptance_reference_framing_and_trace_id(embedder_v1):
    c = build_acc(embedder_v1)
    seed_corpus(c)
    server = c.make_server()
    # a confident hit
    resp = server.handle(MCPRequest(1, "tools/call", {"name": "hive_recall",
            "arguments": {"query": HIT_QUERIES[0][0]}}))
    env = json.loads(resp.result["content"][0]["text"])
    assert "reference_context" in env and "instructions" not in env
    assert env["abstained"] is False and env["trace_id"]
    # an abstain STILL carries a trace_id (the join key) and never fabricates hits
    resp2 = server.handle(MCPRequest(2, "tools/call", {"name": "hive_recall",
            "arguments": {"query": MISS_QUERIES[0]}}))
    env2 = json.loads(resp2.result["content"][0]["text"])
    assert env2["abstained"] is True and env2["trace_id"]
    assert env2["reference_context"] == []
