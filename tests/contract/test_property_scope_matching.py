"""CT-2/CT-3's property module — the §3.6 scope-matching algebra, driven through
the REAL demand rule (``DemandRule.decide(candidate_repos=…)`` over scope-carrying
``MissRow``s — plan §6 step 1 signatures).

The clause: miss scope S matches candidate scope R iff
``S == [] or R == [] or S ∩ R != ∅``. Hypothesis-generated scope sets pin the
invariants: global-matches-all, general-matched-by-all, symmetry — plus the full
intersection algebra.
"""

from __future__ import annotations

import dataclasses
import inspect

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from hive.domain.lifecycle import DemandRule, MissRow

NAMES = ("a", "b", "c", "d")

scope_sets = st.frozensets(st.sampled_from(NAMES), max_size=3).map(
    lambda s: tuple(sorted(s))
)

_VEC = np.ones(4, dtype=np.float32) / 2.0  # exact unit vector


def _require_v3() -> None:
    assert "repos" in {f.name for f in dataclasses.fields(MissRow)}, (
        "v3 MissRow lacks the repos scope field (plan §6 step 1)"
    )
    assert "candidate_repos" in inspect.signature(DemandRule.decide).parameters, (
        "v3 DemandRule.decide lacks the candidate_repos scope clause (plan §6 step 1)"
    )


def _matches(miss_scope: tuple[str, ...], candidate_scope: tuple[str, ...]) -> bool:
    """One miss of scope S against one candidate of scope R: with every OTHER
    clause satisfied (enough demand, another identity, no competitor), the verdict
    isolates the scope clause. The v3-only kwargs (``MissRow.repos`` /
    ``candidate_repos``) are guarded by ``_require_v3`` and threaded dynamically —
    against today's tree this fails as an assertion, never a TypeError."""
    _require_v3()
    rule = DemandRule(demand_m=1, demand_tau=0.5, competitor_tau=1.0)
    miss_kwargs: dict = {
        "vector": _VEC,
        "agent_id": "another",
        "ts": 0,
        "repos": miss_scope,
    }
    decision = rule.decide(
        candidate_vec=_VEC,
        candidate_writer="the-writer",
        misses=[MissRow(**miss_kwargs)],
        competitor_top_sim=0.0,
        **{"candidate_repos": candidate_scope},
    )
    return bool(decision.promote)


@settings(max_examples=60, deadline=None)
@given(candidate=scope_sets)
def test_global_miss_matches_every_candidate_scope(candidate):
    _require_v3()
    assert _matches((), candidate), (
        f"a GLOBAL miss (S == []) must match candidate scope {candidate!r}"
    )


@settings(max_examples=60, deadline=None)
@given(miss=scope_sets)
def test_general_candidate_matched_by_every_miss_scope(miss):
    _require_v3()
    assert _matches(miss, ()), (
        f"a GENERAL candidate (R == []) must be matched by miss scope {miss!r}"
    )


@settings(max_examples=60, deadline=None)
@given(s=scope_sets, r=scope_sets)
def test_scope_matching_is_symmetric(s, r):
    _require_v3()
    assert _matches(s, r) == _matches(r, s), (
        f"S∩R is symmetric — matching must not depend on direction ({s!r}, {r!r})"
    )


@settings(max_examples=100, deadline=None)
@given(s=scope_sets, r=scope_sets)
def test_full_intersection_algebra(s, r):
    _require_v3()
    expected = (not s) or (not r) or bool(set(s) & set(r))
    assert _matches(s, r) == expected, (
        f"scope-matching must be exactly `S==[] or R==[] or S∩R≠∅` for S={s!r}, R={r!r}"
    )
