"""The geometry re-embed acceptance — the round-trip end-to-end: a W_version bump
re-projects every approved row from its blob text through a FRESH (different) PCA head,
rewrites ``value``, rebuilds the index, and REPRODUCES recall — with the text and
content_hash untouched. Uses two real bge geometries (W_version 1 vs 2)."""
from __future__ import annotations

import numpy as np

from hive.domain.models import AgentContext
from hive.ops.migration import reembed_from_text
from tests.acceptance.conftest import HIT_QUERIES, build_acc, seed_corpus

_CTX = AgentContext(workflow="general")


def _topk(container, query, k=5):
    r = container.recall.recall(query, agent_id="acc", agent_ctx=_CTX)
    return [h.episode_id for h in r.hits][:k]


def test_acceptance_w_version_bump_reproduces_recall(embedder_v1, embedder_v2):
    c = build_acc(embedder_v1)
    eids = seed_corpus(c)
    q, gold = HIT_QUERIES[0]
    gold_eid = eids[gold]

    assert gold_eid in _topk(c, q)                      # baseline recall finds the gold
    ep_before = c.store.get_episode(gold_eid)
    value_before = ep_before.value.copy()
    hash_before = ep_before.content_hash

    # ── the W_version bump: re-embed every approved row through the v2 geometry ──
    n = reembed_from_text(c.store, embedder=embedder_v2)
    assert n == len(eids)                               # every approved row re-projected
    # restart semantics: the query side now uses the SAME (v2) geometry as the stored values
    c.recall.embedder = embedder_v2

    assert gold_eid in _topk(c, q)                      # recall REPRODUCED across the geometry change
    ep_after = c.store.get_episode(gold_eid)
    assert ep_after.content_hash == hash_before         # text/hash untouched — only `value` rewritten
    assert not np.array_equal(ep_after.value, value_before)   # the geometry genuinely changed
    assert embedder_v2.w_version != embedder_v1.w_version


def test_acceptance_reembed_preserves_full_recall_at_5(embedder_v1, embedder_v2):
    """Not just one query: the whole hit set still meets the recall@5 floor after the
    geometry migration (the round-trip preserves retrieval quality, not just a single gold)."""
    from hive.research.metrics_ir import recall_at_k
    c = build_acc(embedder_v1)
    eids = seed_corpus(c)
    reembed_from_text(c.store, embedder=embedder_v2)
    c.recall.embedder = embedder_v2
    scores = [recall_at_k([str(i) for i in _topk(c, q)], {str(eids[g])}, 5)
              for q, g in HIT_QUERIES]
    assert sum(scores) / len(scores) >= 0.33
