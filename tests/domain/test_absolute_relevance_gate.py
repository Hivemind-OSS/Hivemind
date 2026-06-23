"""M04 AbsoluteRelevanceGate: abstain unless the field carries ABSOLUTE relevance.

The replacement for the shift-invariant entropy gate. ``suppress`` iff fewer than
``k_min`` candidates clear ``tau_serve`` on exact unit-norm vectors. These tests pin the
three honesty classes the old gate got wrong — flat-but-all-relevant SERVES, flat-but-weak
ABSTAINS, and (the new pin) **peaked-but-absolutely-weak ABSTAINS** (the entropy gate
wrongly served it) — plus the load-bearing non-finite fail-closed guard (the new gate has
no softmax to raise, so the explicit guard is the only thing standing between a NaN sim and
a fabricated CONFIDENT verdict) and the GateVerdict self-assertion.
"""
from __future__ import annotations

import math

import pytest

from hive.domain.recall import AbsoluteRelevanceGate, GateVerdict


# ── GateVerdict: frozen + self-asserting ──────────────────────────────────────
def test_gate_verdict_rejects_out_of_range_top_cos():
    for bad in (1.5, -1.5, float("nan"), float("inf")):
        with pytest.raises(ValueError, match=r"top_cos"):
            GateVerdict(False, bad, 1)
    # the endpoints are legal
    assert GateVerdict(True, -1.0, 0).top_cos == -1.0
    assert GateVerdict(False, 1.0, 1).top_cos == 1.0


def test_gate_verdict_rejects_negative_n_relevant():
    with pytest.raises(ValueError, match=r"n_relevant"):
        GateVerdict(False, 0.9, -1)


def test_gate_verdict_is_frozen():
    import dataclasses
    v = GateVerdict(False, 0.9, 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.suppress = True   # type: ignore[misc]


# ── ctor validation (B1 fail-fast) ────────────────────────────────────────────
def test_tau_serve_ctor_validation():
    for bad in (0.0, 1.5, float("nan"), float("inf"), -0.1):
        with pytest.raises(ValueError, match=r"tau_serve"):
            AbsoluteRelevanceGate(bad)
    assert AbsoluteRelevanceGate(1.0).tau_serve == 1.0
    assert AbsoluteRelevanceGate(0.70).tau_serve == 0.70


def test_k_min_ctor_validation():
    for bad in (0, -1):
        with pytest.raises(ValueError, match=r"k_min"):
            AbsoluteRelevanceGate(0.70, bad)
    g = AbsoluteRelevanceGate(0.70, 2)
    assert g.k_min == 2 and isinstance(g.k_min, int)


# ── the three honesty classes ─────────────────────────────────────────────────
def test_flat_all_relevant_serves():
    # a FLAT field (near-uniform sims) where ALL clear the floor SERVES — shift-invariance
    # is gone: "flat" no longer means "uncertain". (The entropy gate abstained on this.)
    v = AbsoluteRelevanceGate(0.70).evaluate([0.85, 0.84, 0.83])
    assert v.suppress is False
    assert v.top_cos == pytest.approx(0.85)
    assert v.n_relevant == 3


def test_flat_weak_abstains():
    # FLAT and all-weak (none clear the floor) ABSTAINS — the field carries no answer.
    v = AbsoluteRelevanceGate(0.70).evaluate([0.30, 0.29, 0.28])
    assert v.suppress is True
    assert v.n_relevant == 0
    assert v.top_cos == pytest.approx(0.30)


def test_peaked_but_weak_abstains():
    # ★ the new-honesty pin: a PEAKED-but-absolutely-weak field ABSTAINS (top cos 0.50 <
    # 0.70). The old entropy gate read this as "low entropy ⇒ confident" and WRONGLY SERVED.
    v = AbsoluteRelevanceGate(0.70).evaluate([0.50, 0.05, 0.02])
    assert v.suppress is True
    assert v.n_relevant == 0
    assert v.top_cos == pytest.approx(0.50)


def test_peaked_strong_serves():
    v = AbsoluteRelevanceGate(0.70).evaluate([0.95, 0.1, 0.05])
    assert v.suppress is False and v.n_relevant == 1
    assert v.top_cos == pytest.approx(0.95)


# ── single-candidate fields (the entropy gate's ln-1 blind spot) ──────────────
def test_single_strong_serves():
    v = AbsoluteRelevanceGate(0.70).evaluate([0.8])
    assert v.suppress is False and v.n_relevant == 1 and v.top_cos == pytest.approx(0.8)


def test_single_weak_abstains():
    # a lone weak candidate ABSTAINS — the entropy gate served it (n_eff=1 ⇒ entropy 0).
    v = AbsoluteRelevanceGate(0.70).evaluate([0.5])
    assert v.suppress is True and v.n_relevant == 0


# ── tau_serve is an INCLUSIVE floor ───────────────────────────────────────────
def test_tau_serve_inclusive_boundary():
    # a sim EXACTLY at tau_serve clears it (>=). Lowering the field below abstains.
    assert AbsoluteRelevanceGate(0.70).evaluate([0.70]).suppress is False
    assert AbsoluteRelevanceGate(0.70).evaluate([0.6999]).suppress is True


# ── k_min boundary (the < vs <= mutation target) ──────────────────────────────
def test_k_min_boundary_is_strict_lt():
    g = AbsoluteRelevanceGate(0.70, 2)
    # exactly k_min relevant ⇒ SERVE (n_relevant < k_min is strict). `<=` would suppress.
    assert g.evaluate([0.8, 0.75]).suppress is False
    # one short ⇒ abstain
    assert g.evaluate([0.8, 0.5]).suppress is True
    assert g.evaluate([0.8, 0.5]).n_relevant == 1


def test_high_k_min_requires_a_quorum():
    g = AbsoluteRelevanceGate(0.70, 3)
    assert g.evaluate([0.9, 0.8, 0.75, 0.1]).suppress is False   # 3 clear
    assert g.evaluate([0.9, 0.8, 0.1]).suppress is True          # only 2 clear


# ── empty / non-finite ⇒ fail-closed SUPPRESS (Law 1) ─────────────────────────
def test_empty_sims_fail_closed():
    assert AbsoluteRelevanceGate(0.70).evaluate([]) == GateVerdict(True, -1.0, 0)


def test_nonfinite_in_multiple_positions_fails_closed():
    # closes `recall-gate-nonfinite-failopen`: a NaN/inf sim is undecidable ⇒ fail-closed
    # SUPPRESS regardless of POSITION (the new gate has no softmax to raise — the explicit
    # guard is the only protection).
    g = AbsoluteRelevanceGate(0.70)
    nan, inf = float("nan"), float("inf")
    for sims in ([1.0, nan], [nan, 1.0], [0.9, 0.8, nan], [inf, 0.9], [0.9, inf, 0.8]):
        assert g.evaluate(sims) == GateVerdict(True, -1.0, 0), sims


# ── top_cos is clamped (float drift never reds the GateVerdict invariant) ─────
def test_top_cos_clamped_into_unit_interval():
    # a self-cosine that drifts slightly above 1.0 (float accumulation) must NOT raise in
    # GateVerdict.__post_init__ and fail-closed — it is clamped to 1.0 and the field serves.
    v = AbsoluteRelevanceGate(0.70).evaluate([1.0000003, 0.2])
    assert v.suppress is False and v.top_cos == 1.0 and v.n_relevant == 1


def test_negative_cosines_abstain_with_valid_top_cos():
    v = AbsoluteRelevanceGate(0.70).evaluate([-0.2, -0.3, -0.4])
    assert v.suppress is True and v.n_relevant == 0
    assert v.top_cos == pytest.approx(-0.2)   # within [-1,1], no fail-closed


# ── from_recall: floor-by-identity ────────────────────────────────────────────
class _StubRecall:
    def __init__(self, tau_serve, k_min=1):
        self.tau_serve = tau_serve
        self.k_min = k_min


def test_from_recall_reads_floor_and_holds_identity():
    rc = _StubRecall(0.55, 2)
    g = AbsoluteRelevanceGate.from_recall(rc)
    assert g.tau_serve == 0.55 and g.k_min == 2
    assert g._recall is rc          # IDENTITY, not a float copy (CONFIG_DRIFT killed)


def test_from_recall_defaults_k_min_for_older_config():
    class _NoKMin:
        tau_serve = 0.70
    g = AbsoluteRelevanceGate.from_recall(_NoKMin())
    assert g.k_min == 1             # getattr default keeps the duck-typed contract alive
