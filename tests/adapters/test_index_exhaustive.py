"""P1.1 — M02 ExhaustiveCosineIndex: authoritative, signed, boot-rebuildable [B3]."""
from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex, build_index


def _unit(*xs) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def _idx(dim=3):
    return ExhaustiveCosineIndex(dim)


def test_search_ranks_by_descending_cosine():
    ix = _idx()
    ix.sync_approved(1, _unit(1, 0, 0))
    ix.sync_approved(2, _unit(0.9, 0.1, 0))
    ix.sync_approved(3, _unit(0, 1, 0))
    out = ix.search(_unit(1, 0, 0), k=3)
    assert [e for e, _ in out] == [1, 2, 3]
    assert out[0][1] >= out[1][1] >= out[2][1]


def test_search_returns_cosine_scores_not_bare_ids():
    ix = _idx()
    v, q = _unit(1, 2, 3), _unit(3, 2, 1)
    ix.sync_approved(1, v)
    score = ix.search(q, k=1)[0][1]
    assert abs(score - float(np.dot(v, q))) < 1e-6


def test_search_within_k_is_sorted():
    ix = _idx()
    for i, x in enumerate([_unit(1, 0, 0), _unit(0.8, 0.2, 0), _unit(0.5, 0.5, 0), _unit(0, 0, 1)], 1):
        ix.sync_approved(i, x)
    out = ix.search(_unit(1, 0, 0), k=2)
    assert [e for e, _ in out] == [1, 2]            # partition slice re-sorted descending


def test_anticorrelated_value_never_surfaces_first():
    ix = _idx()
    # id1 mildly correlated (cos +0.5); id2 strongly anti-correlated (cos −0.9).
    # signed cosine ⇒ id1 wins; |q·x| would WRONGLY rank id2 first (0.9 > 0.5).
    ix.sync_approved(1, _unit(0.5, 0.8660254, 0))
    ix.sync_approved(2, _unit(-0.9, 0.4358899, 0))
    out = ix.search(_unit(1, 0, 0), k=2)
    assert out[0][0] == 1 and out[1][0] == 2        # −v never beats a positive cos


def test_topk_truncates_and_empty_guards():
    ix = _idx()
    ix.sync_approved(1, _unit(1, 0, 0))
    assert len(ix.search(_unit(1, 0, 0), k=5)) == 1     # min(k, n)
    assert ix.search(_unit(1, 0, 0), k=0) == []
    assert _idx().search(_unit(1, 0, 0), k=3) == []     # empty index, no raise


def test_exhaustive_is_authoritative_and_has_no_ann_attrs():
    ix = _idx()
    assert ix.is_authoritative() is True
    for attr in ("_ann", "approx_threshold", "candidate_k"):
        assert not hasattr(ix, attr)


def test_growing_n_never_flips_path():
    rng = np.random.default_rng(0)
    ix = ExhaustiveCosineIndex(8)
    gold = _unit(*rng.standard_normal(8))
    ix.sync_approved(999999, gold)
    for i in range(10_050):                          # well past the legacy 10k threshold
        v = rng.standard_normal(8).astype(np.float32)
        ix.sync_approved(i, v / np.linalg.norm(v))
    assert ix.search(gold, k=1)[0][0] == 999999      # exact rank-1, no silent ANN flip


def test_dim_mismatch_and_nan_rejected():
    ix = _idx()
    ix.sync_approved(1, _unit(1, 0, 0))
    with pytest.raises(ValueError):
        ix.search(np.array([1.0, 0.0], dtype=np.float32), k=1)   # wrong dim
    with pytest.raises(ValueError):
        ix.search(np.array([np.nan, 0, 0], dtype=np.float32), k=1)  # NaN guard
    with pytest.raises(ValueError):
        ix.sync_approved(2, np.array([np.inf, 0, 0], dtype=np.float32))


def test_sync_approved_copies_value():
    ix = _idx()
    v = _unit(1, 0, 0)
    ix.sync_approved(1, v)
    v[:] = _unit(0, 1, 0)                            # mutate source after ingest
    assert ix.search(_unit(1, 0, 0), k=1)[0][0] == 1   # search unchanged (copy-on-ingest)


def test_index_rebuilds_from_approved_only():
    # simulate scan_approved() yielding only the 2 approved rows
    approved = [(1, _unit(1, 0, 0)), (2, _unit(0, 1, 0))]
    ix = _idx()
    ix.rebuild_from_store(iter(approved))
    assert ix.size() == 2
    assert ix.search(_unit(1, 0, 0), k=5)[0][0] == 1
    assert {e for e, _ in ix.search(_unit(1, 0, 0), k=5)} == {1, 2}   # pending (id 3) absent


def test_rebuild_is_idempotent():
    approved = [(1, _unit(1, 0, 0)), (2, _unit(0, 1, 0))]
    ix = _idx()
    ix.rebuild_from_store(list(approved))
    a = ix.search(_unit(1, 1, 0), k=2)
    ix.rebuild_from_store(list(approved))
    b = ix.search(_unit(1, 1, 0), k=2)
    assert a == b and ix.size() == 2


def test_remove_swap_keeps_search_correct():
    ix = _idx()
    ix.sync_approved(1, _unit(1, 0, 0))
    ix.sync_approved(2, _unit(0, 1, 0))
    ix.sync_approved(3, _unit(0, 0, 1))
    ix.remove(2)
    ids = {e for e, _ in ix.search(_unit(1, 1, 1), k=5)}
    assert ids == {1, 3} and ix.size() == 2


def test_factory_unknown_backend_fails_fast():
    assert isinstance(build_index("exhaustive", 4), ExhaustiveCosineIndex)
    with pytest.raises(ValueError):
        build_index("bogus", 4)
