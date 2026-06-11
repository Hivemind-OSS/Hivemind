"""Dev-time channel comparison: dense-only vs hybrid (dense + lexical RRF) over
the SAME hermetic store — the instrument that alone can justify flipping
``recall.hybrid``.

Two ``RecallPipeline``s share one corpus, one embedder, one index, one FTS
mirror; they differ ONLY in ``hybrid_enabled``/``lexical_index``, so every
per-query delta is attributable to the channel flip and nothing else. Scoring is
label-based (content-hash relevance, the ``replay()`` convention) — never an LLM
judge. The ship rule is CI-significance: **recommend activation iff the paired
per-query bootstrap CI on the recall@k delta has lo > 0** — a bare positive point
estimate never ships a channel.

Dev-time only — the runtime never imports ``hive.research`` (P0.0 AST fence).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from statistics import fmean
from typing import Optional, Sequence

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.scanner_regex import DefaultSecretScanner
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.adapters.utility_store_sqlite import SqliteUtilityStore
from hive.domain.models import AgentContext, content_hash
from hive.domain.recall import NormalizedEntropyGate, RecallPipeline
from hive.domain.surfacer import UtilitySurfacer
from hive.research.eval_membrane import HashEmbedder
from hive.research.metrics_ir import bootstrap_ci, ndcg_at_k, recall_at_k

# Ranker pass: the gate disabled at h_frac_max=1.0 (suppress iff entropy_norm >
# 1.0, impossible after the [0,1] clamp) — channel_eval measures the RANKING the
# channel flip changes; the abstention contract is owned by the gate's own
# tests/oracle and is structurally untouched by hybrid (gate before lexical I/O).
_RANKER_H_FRAC_MAX = 1.0


@dataclass(frozen=True)
class ChannelEvalResult:
    arms: dict                                       # 'dense'|'hybrid' → {'recall@k','ndcg@k'}
    hybrid_vs_dense_ci: tuple[float, float, float]   # (point, lo, hi) on per-query recall@k deltas
    recommend_hybrid: bool                           # lo > 0 — the CI-significance ship rule


def run_channel_eval(
    labeled: "Sequence[tuple[str, set[str]]]",       # (query, relevant content-hashes)
    *,
    k: int = 5,
    n_boot: int = 10_000,
    seed: int = 0,
    store_fixture: "Optional[Sequence[str]]" = None,
) -> ChannelEvalResult:
    """Run both arms over one hermetic store and score the channel flip.

    ``store_fixture`` is the corpus: each text is staged + approved (established)
    into a fresh ``:memory:`` store, so the FTS mirror and the dense index are
    populated through the production write path. ``labeled`` rows reference the
    corpus by ``content_hash`` (cross-run-stable, the eval_membrane convention).
    Empty ``labeled`` RAISES (``bootstrap_ci`` refuses an undefined CI — a gate
    green on zero signal would be decorative).
    """
    embedder = HashEmbedder(d=256)
    conn = connect(":memory:")
    try:
        util = SqliteUtilityStore(conn)
        index = ExhaustiveCosineIndex(embedder.d)
        store = SqliteEpisodeStore(conn, index)
        for i, text in enumerate(store_fixture or ()):
            eid, deduped = store.stage(text=text, weight=1.0, source="eval", tags="",
                                       proposed_by="eval", ts=i)
            if not deduped:
                store.approve(eid, "eval", embedder.encode(text),
                              expected_version=0, approved_ts=0)

        def _pipe(hybrid: bool) -> RecallPipeline:
            # autonomy off: the eval measures the READ path only — no exposure/miss
            # rows, so the two arms see an identical frozen store.
            return RecallPipeline(
                embedder=embedder, index=index,
                gate=NormalizedEntropyGate(_RANKER_H_FRAC_MAX, 16.0),
                surfacer=UtilitySurfacer(enabled=False, epsilon_explore=0.1,
                                         f_min=0.5, f_max=1.5,
                                         rng=random.Random(0)),
                reader=store, utility_store=util, recall_top_n=int(k),
                ledger=store, clock_now=lambda: 0,
                scanner=DefaultSecretScanner(), provisional_ttl_s=10**9,
                autonomy_enabled=False,
                lexical_index=store if hybrid else None, hybrid_enabled=hybrid)

        pipes = {"dense": _pipe(False), "hybrid": _pipe(True)}
        per_arm: dict[str, list[tuple[float, float]]] = {"dense": [], "hybrid": []}
        deltas: list[float] = []
        ctx = AgentContext()
        for query, relevant in labeled:
            rel = set(relevant)
            r_at_k: dict[str, float] = {}
            for arm, pipe in pipes.items():
                res = pipe.recall(query, agent_id="channel_eval", agent_ctx=ctx)
                retrieved = [content_hash(h.text) for h in res.hits]
                r_at_k[arm] = recall_at_k(retrieved, rel, k)
                per_arm[arm].append((r_at_k[arm], ndcg_at_k(retrieved, rel, k)))
            deltas.append(r_at_k["hybrid"] - r_at_k["dense"])

        arms = {arm: {"recall@k": fmean(r for r, _n in rows),
                      "ndcg@k": fmean(n for _r, n in rows)}
                for arm, rows in per_arm.items()} if deltas else {}
        ci = bootstrap_ci(deltas, n_boot=n_boot, seed=seed)
        return ChannelEvalResult(arms=arms, hybrid_vs_dense_ci=ci,
                                 recommend_hybrid=ci[1] > 0.0)
    finally:
        conn.close()
