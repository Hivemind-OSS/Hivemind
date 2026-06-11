"""rrf_fuse — pure Reciprocal Rank Fusion (the only fusion math in the tree) +
the LexicalIndex port shape.

Contracts: rank-based (a deeper rank contributes strictly less, so fusion cannot
degenerate into a presence count), exact ties keep first-seen order (stable,
deterministic), a duplicate id within one ranking counts once, empty ⇒ [].
"""
from __future__ import annotations

from hive.domain.fusion import rrf_fuse
from hive.domain.ports import LexicalIndex


# ── rank-based + stable (the RULE-2 target: 1/(k+rank) must use the rank) ─────
def test_rrf_is_rank_based_and_stable():
    # presence at a TOP rank in one list beats presence DEEP in two lists once the
    # depth penalty dominates (k=1 makes it visible at toy size):
    # id2: 1/(1+0)=1.0 > id1: 1/(1+2)+1/(1+2)=2/3. A rank-blind 1/(k+1) scores
    # id1 as 2/(k+1) > id2's 1/(k+1) and flips this order.
    fused = rrf_fuse([[10, 11, 1], [20, 21, 1], [2]], k=1)
    assert fused.index(2) < fused.index(1)

    # multi-list presence at comparable ranks beats single-list presence (default k)
    assert rrf_fuse([[1, 2], [2]]) == [2, 1]

    # exact tie ⇒ first-seen order, deterministically — both input orders pinned
    assert rrf_fuse([[4], [5]]) == [4, 5]
    assert rrf_fuse([[5], [4]]) == [5, 4]

    # same id set, reversed in the second list: ids 1 and 3 tie exactly
    # (1/60 + 1/62 each, float-commutative); id 2 sits below (2/61); the top tie
    # breaks first-seen toward 1.
    assert rrf_fuse([[1, 2, 3], [3, 2, 1]]) == [1, 3, 2]


def test_rrf_empty_inputs():
    assert rrf_fuse([]) == []
    assert rrf_fuse([[], []]) == []
    assert rrf_fuse([[], [7]]) == [7]


def test_rrf_duplicate_id_in_one_ranking_counts_once():
    # a triple occurrence in ONE list is ONE contribution (at the first
    # occurrence's rank): id5 = 1/60 < id6 = 2/60 — double-counting the
    # duplicate (1/60 + 1/61 + 1/62) would flip this order.
    assert rrf_fuse([[5, 5, 5], [6], [6]]) == [6, 5]
    # first occurrence sets the duplicate's rank; order within the list holds
    assert rrf_fuse([[5, 5, 7]]) == [5, 7]


# ── LexicalIndex port: runtime-checkable shape ─────────────────────────────────
def test_lexical_index_port_is_runtime_checkable():
    class _Lex:
        def search_text(self, query: str, k: int) -> list[tuple[int, float]]:
            return []

    class _NotLex:
        pass

    assert isinstance(_Lex(), LexicalIndex)
    assert not isinstance(_NotLex(), LexicalIndex)
