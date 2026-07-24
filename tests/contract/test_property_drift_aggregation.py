"""CT-4's property module — the drift-aggregation total order (plan §3.4, single
owner ``hive/app/drift.py``).

Any multiset of per-anchor verdicts aggregates to EXACTLY ONE verdict under the
most-severe-wins order ``anchor_missing > anchor_changed > blast_radius_changed >
branch_scoped > unverifiable > fresh``; ``fresh`` only when ALL anchors are fresh;
the empty set (a general memory) is ``n/a``.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.contract.conftest import (
    DRIFT_FRESH,
    DRIFT_NA,
    DRIFT_SEVERITY,
    DRIFT_TYPES,
    load_module,
)

verdicts = st.lists(st.sampled_from(DRIFT_SEVERITY), max_size=8)
nonempty_verdicts = st.lists(st.sampled_from(DRIFT_SEVERITY), min_size=1, max_size=8)


def _aggregate():
    mod = load_module("hive.app.drift")
    assert mod is not None, (
        "v3 module hive.app.drift does not exist yet (plan §6 step 4)"
    )
    assert hasattr(mod, "aggregate_verdicts"), (
        "hive.app.drift must own the most-severe-wins aggregation as "
        "aggregate_verdicts(verdicts) -> str"
    )
    return mod.aggregate_verdicts


@settings(max_examples=200, deadline=None)
@given(vs=verdicts)
def test_total_exactly_one_aggregate(vs):
    agg = _aggregate()
    out = agg(vs)
    assert out in DRIFT_TYPES, f"aggregate must be one wire verdict, got {out!r}"


def test_empty_is_na():
    agg = _aggregate()
    assert agg([]) == DRIFT_NA, "no anchors (a general memory) aggregates to n/a"


@settings(max_examples=100, deadline=None)
@given(v=st.sampled_from(DRIFT_SEVERITY))
def test_singleton_is_itself(v):
    agg = _aggregate()
    assert agg([v]) == v


@settings(max_examples=200, deadline=None)
@given(vs=nonempty_verdicts)
def test_most_severe_wins_total_order(vs):
    agg = _aggregate()
    expected = DRIFT_SEVERITY[min(DRIFT_SEVERITY.index(v) for v in vs)]
    assert agg(vs) == expected, (
        f"aggregation must follow the §3.4 severity order for {vs!r}"
    )


@settings(max_examples=200, deadline=None)
@given(vs=nonempty_verdicts)
def test_fresh_only_when_all_fresh(vs):
    agg = _aggregate()
    if agg(vs) == DRIFT_FRESH:
        assert all(v == DRIFT_FRESH for v in vs), (
            f"fresh may aggregate ONLY from an all-fresh multiset: {vs!r}"
        )


@settings(max_examples=100, deadline=None)
@given(vs=nonempty_verdicts)
def test_permutation_invariant_and_idempotent(vs):
    agg = _aggregate()
    assert agg(list(reversed(vs))) == agg(vs), "order must not matter"
    assert agg([agg(vs)]) == agg(vs), "aggregation is idempotent"
