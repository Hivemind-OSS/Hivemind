"""C1 — the pure conflict detector: opposing polarity → contradiction, near-dup
compatible → redundancy (+ loser_hint), below-τ → nothing, non-finite fail-closed,
deterministic order + top_n cap, canonical a_id<b_id, [] on degenerate input.

PURE: hand-crafted unit vectors give exact cosine control (no embedder). The
detector is the math; the app layer (recall carrier / health report) buckets and
prose-wraps it."""
from __future__ import annotations

import numpy as np

from hive.domain.conflict import (
    CONTRADICTION, REDUNDANCY, ConflictItem, ConflictNote, detect_conflicts,
)

ESTABLISHED, PROVISIONAL = "established", "provisional"


def _unit(*xs) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


def _item(eid, vec, *, polarity="neutral", anchor="f.py", ts=0, trust=ESTABLISHED):
    return ConflictItem(episode_id=eid, vector=vec, polarity=polarity, anchor=anchor,
                        ts=ts, trust=trust)


# near-dup pair (cosine ≈ 0.9997 ≥ τ); a far vector (cosine 0 < τ)
_V_A = _unit(1.0, 0.0, 0.0, 0.0)
_V_A2 = _unit(0.99, 0.02, 0.0, 0.0)
_V_FAR = _unit(0.0, 1.0, 0.0, 0.0)


# ── ConflictNote invariants (illegal states unconstructable) ───────────────────
def test_note_rejects_non_canonical_order():
    import pytest
    with pytest.raises(ValueError):
        ConflictNote(a_id=40, b_id=12, relation=REDUNDANCY, cosine=0.9,
                     anchor="", loser_hint=12)


def test_note_rejects_bad_relation_and_cosine():
    import pytest
    with pytest.raises(ValueError):
        ConflictNote(a_id=1, b_id=2, relation="bogus", cosine=0.9, anchor="", loser_hint=1)
    with pytest.raises(ValueError):
        ConflictNote(a_id=1, b_id=2, relation=REDUNDANCY, cosine=1.5, anchor="", loser_hint=1)
    with pytest.raises(ValueError):
        ConflictNote(a_id=1, b_id=2, relation=REDUNDANCY, cosine=float("nan"),
                     anchor="", loser_hint=1)


# ── opposing polarity → contradiction (symmetric, no winner) ───────────────────
def test_opposing_polarity_is_contradiction():
    items = [_item(10, _V_A, polarity="do"), _item(20, _V_A2, polarity="dont")]
    notes = detect_conflicts(items, tau=0.85)
    assert len(notes) == 1
    n = notes[0]
    assert n.relation == CONTRADICTION
    assert (n.a_id, n.b_id) == (10, 20)        # canonical
    assert n.loser_hint is None                 # contradiction has no winner
    assert n.cosine >= 0.85


def test_dont_vs_do_order_independent():
    # opposing detection is symmetric regardless of which side is do/dont
    items = [_item(10, _V_A, polarity="dont"), _item(20, _V_A2, polarity="do")]
    assert detect_conflicts(items, tau=0.85)[0].relation == CONTRADICTION


# ── compatible near-dup → redundancy + loser_hint ──────────────────────────────
def test_same_polarity_is_redundancy_with_loser_hint_by_trust():
    # trust dominates ts: keep the established (10), retire the provisional (20),
    # EVEN though 20 is newer — a human-vouched memory outranks a demand-promoted one.
    items = [_item(10, _V_A, polarity="do", trust=ESTABLISHED, ts=100),
             _item(20, _V_A2, polarity="do", trust=PROVISIONAL, ts=200)]
    n = detect_conflicts(items, tau=0.85)[0]
    assert n.relation == REDUNDANCY
    assert n.loser_hint == 20                    # provisional loses to established


def test_redundancy_loser_is_older_when_trust_ties():
    # same trust ⇒ ts breaks the tie: the OLDER (lower ts) row is the loser to retire.
    items = [_item(10, _V_A, trust=ESTABLISHED, ts=500),
             _item(20, _V_A2, trust=ESTABLISHED, ts=100)]
    n = detect_conflicts(items, tau=0.85)[0]
    assert n.relation == REDUNDANCY
    assert n.loser_hint == 20                    # id 20 is older (ts 100 < 500)


def test_neutral_neutral_is_redundancy():
    items = [_item(10, _V_A, polarity="neutral"), _item(20, _V_A2, polarity="neutral")]
    assert detect_conflicts(items, tau=0.85)[0].relation == REDUNDANCY


def test_do_vs_neutral_is_redundancy_not_contradiction():
    # only do↔dont is opposing; do↔neutral is compatible (redundancy)
    items = [_item(10, _V_A, polarity="do"), _item(20, _V_A2, polarity="neutral")]
    assert detect_conflicts(items, tau=0.85)[0].relation == REDUNDANCY


# ── below-τ → nothing ──────────────────────────────────────────────────────────
def test_below_tau_yields_no_note():
    items = [_item(10, _V_A), _item(20, _V_FAR)]
    assert detect_conflicts(items, tau=0.85) == ()


def test_tau_boundary_is_inclusive():
    # cosine exactly at τ counts (>= τ), not dropped
    items = [_item(10, _V_A), _item(20, _V_A)]      # identical ⇒ cosine 1.0
    assert len(detect_conflicts(items, tau=1.0)) == 1


# ── non-finite vector → that pair is skipped (fail-closed) ─────────────────────
def test_non_finite_vector_skips_pair():
    bad = np.array([np.nan, 0.0, 0.0, 0.0], dtype=np.float32)
    items = [_item(10, _V_A), _item(20, bad), _item(30, _V_A2)]
    notes = detect_conflicts(items, tau=0.85)
    # only the 10↔30 pair survives; any pair touching the NaN vector is dropped
    assert [(n.a_id, n.b_id) for n in notes] == [(10, 30)]


# ── deterministic order (cosine desc, a_id, b_id) + top_n cap ──────────────────
def test_deterministic_order_cosine_desc_then_ids():
    near = _unit(0.999, 0.001, 0.0, 0.0)   # ~1.0 vs _V_A
    less = _unit(0.9, 0.2, 0.0, 0.0)       # lower cosine vs _V_A but still ≥ τ=0.85
    items = [_item(10, _V_A), _item(20, near), _item(30, less)]
    notes = detect_conflicts(items, tau=0.85)
    cosines = [n.cosine for n in notes]
    assert cosines == sorted(cosines, reverse=True)            # cosine-descending


def test_top_n_caps_output():
    # 4 mutually near-dup vectors ⇒ C(4,2)=6 pairs; top_n=2 returns only the 2 best
    vecs = [_unit(1.0, k * 0.001, 0.0, 0.0) for k in range(4)]
    items = [_item(10 + i, v) for i, v in enumerate(vecs)]
    assert len(detect_conflicts(items, tau=0.85, top_n=2)) == 2


# ── degenerate input → [] ──────────────────────────────────────────────────────
def test_empty_and_singleton_yield_nothing():
    assert detect_conflicts([], tau=0.85) == ()
    assert detect_conflicts([_item(1, _V_A)], tau=0.85) == ()
