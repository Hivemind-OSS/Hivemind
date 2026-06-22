"""P1.5 — M04 NormalizedEntropyGate [B1/C4]: PORT+EXTEND of the reference gate.

The reference `gate_bundle.py` read `Candidate.alpha` (softmax mass computed
upstream); the v-min surface is `evaluate(sims: list[float])` over RAW cosine ∈
[-1,1]. Turning cosines into a distribution requires `mass = softmax(beta·sim)`
(max-shifted) — NEW code, with `beta` entering the constructor. Everything
downstream of `mass` is byte-identical to the reference: `top_margin`, `n_eff`,
`H`, `entropy_norm = H/ln(n_eff)` (ln-1 guard), `[0,1]` clamp, `suppress =
entropy_norm > h_frac_max`, and the fail-closed `except → (True, 1.0, 0.0)`.

These tests pin: β is actually applied (not dead); both reference fallbacks; and
the abstain comparison direction (the invert mutation lives in the build).
"""
from __future__ import annotations

import math

import pytest

from hive.domain.recall import NormalizedEntropyGate, _softmax_mass_from_sims


def _manual_softmax(sims, beta):
    m = max(sims)
    raw = [math.exp(beta * s - beta * m) for s in sims]
    tot = sum(raw)
    return [r / tot for r in raw]


# ── β enters via the constructor and is actually applied (★ B1) ───────────────
def test_gate_softmax_mass_uses_beta():
    sims = [0.9, 0.3, 0.1]
    # the transform equals softmax(beta*sim) elementwise within 1e-6
    for beta in (4.0, 32.0):
        got = _softmax_mass_from_sims(sims, beta)
        want = _manual_softmax(sims, beta)
        assert all(abs(a - b) < 1e-6 for a, b in zip(got, want))
        assert abs(sum(got) - 1.0) < 1e-9
    # sharper beta ⇒ strictly more peaked ⇒ strictly LOWER normalized entropy
    _, h32, _ = NormalizedEntropyGate(h_frac_max=0.5, beta=32.0).evaluate(sims)
    _, h4, _ = NormalizedEntropyGate(h_frac_max=0.5, beta=4.0).evaluate(sims)
    assert h32 < h4  # β=32 distribution is strictly more peaked than β=4 (β is live)


def test_gate_beta_must_be_positive():
    with pytest.raises(ValueError):
        NormalizedEntropyGate(h_frac_max=0.5, beta=0.0)
    with pytest.raises(ValueError):
        NormalizedEntropyGate(h_frac_max=0.5, beta=-3.0)
    with pytest.raises(ValueError):
        NormalizedEntropyGate(h_frac_max=float("nan"), beta=16.0)


# ── uniform ⇒ max entropy ⇒ suppress; peaked ⇒ low entropy ⇒ pass ─────────────
def test_uniform_high_entropy_suppresses():
    suppress, h_norm, _ = NormalizedEntropyGate(0.5, 16.0).evaluate([0.4, 0.4, 0.4, 0.4])
    assert h_norm == pytest.approx(1.0, abs=1e-9)
    assert suppress is True  # entropy_norm 1.0 > 0.5


def test_peaked_low_entropy_passes():
    suppress, h_norm, top_margin = NormalizedEntropyGate(0.5, 16.0).evaluate([0.95, 0.1, 0.05])
    assert h_norm < 0.5
    assert suppress is False
    assert top_margin > 0.0  # the dominant hit carries most of the mass


def test_gate_degenerate_uniform_fallback():
    # all-equal sims ⇒ FALLBACK-2 territory / uniform distribution ⇒ max entropy ⇒ suppress
    suppress, h_norm, _ = NormalizedEntropyGate(0.5, 16.0).evaluate([0.2, 0.2, 0.2])
    assert h_norm == pytest.approx(1.0, abs=1e-9) and suppress is True


def test_single_candidate_zero_entropy():
    # n_eff == 1 ⇒ ln(1) guard ⇒ entropy_norm == 0.0 (no div-by-zero / NaN), pass
    suppress, h_norm, top_margin = NormalizedEntropyGate(0.5, 16.0).evaluate([0.8])
    assert h_norm == 0.0 and suppress is False
    assert top_margin == pytest.approx(1.0)  # mass_sorted[0] - 0.0


def test_empty_sims_is_no_suppress_zero():
    assert NormalizedEntropyGate(0.5, 16.0).evaluate([]) == (False, 0.0, 0.0)


def test_gate_fail_closed_on_bad_input():
    # a non-numeric sim makes the math raise ⇒ fail-closed SUPPRESS, never fabricate
    suppress, h_norm, top_margin = NormalizedEntropyGate(0.5, 16.0).evaluate([0.5, "x"])  # type: ignore[list-item]
    assert (suppress, h_norm, top_margin) == (True, 1.0, 0.0)


def test_gate_nan_or_inf_sim_fails_closed():
    # AUDIT wf_e5fdbb3c-5f1 (#A): a non-finite INPUT cosine is UNDECIDABLE — the gate
    # must fail-closed (suppress), never fabricate a CONFIDENT verdict. Previously the
    # exp-floor only caught a non-finite RESULT, so a NaN sim slipped through as
    # suppress=False (and its position even flipped the verdict nondeterministically).
    g = NormalizedEntropyGate(0.5, 16.0)
    nan, inf = float("nan"), float("inf")
    for sims in ([1.0, nan], [nan, 1.0], [1.0, inf], [inf, 0.5, 0.2], [0.9, 0.3, nan]):
        assert g.evaluate(sims) == (True, 1.0, 0.0), sims


def test_softmax_helper_rejects_non_finite_input():
    for bad in ([1.0, float("nan")], [float("inf"), 0.2], [float("-inf")]):
        with pytest.raises(ValueError):
            _softmax_mass_from_sims(bad, 16.0)


def test_entropy_norm_always_in_unit_interval():
    for sims in ([0.9, 0.3], [0.1, 0.1, 0.1, 0.1, 0.1], [0.99, -0.99], [-0.2, -0.3, -0.4]):
        _, h_norm, _ = NormalizedEntropyGate(0.5, 16.0).evaluate(sims)
        assert 0.0 <= h_norm <= 1.0


# ── tau_top1 score-gap floor (inert at 0.0; strengthens Law 1) ────────────────
def test_tau_top1_default_inert():
    # a peaked field that PASSES the entropy gate stays served at the default floor (0.0)
    # and at an explicit 0.0 — the floor adds no abstention when off (byte-stable decision).
    peaked = [0.95, 0.1, 0.05]
    base = NormalizedEntropyGate(0.5, 16.0).evaluate(peaked)
    assert base[0] is False
    assert NormalizedEntropyGate(0.5, 16.0, 0.0).evaluate(peaked) == base  # explicit 0.0 == default


def test_tau_top1_floor_suppresses():
    # a field that PASSES the entropy gate (entropy_norm 0.41 < 0.5) but whose top-1 mass
    # (0.917) is below tau_top1 is suppressed by the floor ALONE (the OR clause adds the
    # abstention the entropy gate did not).
    sims = [0.65, 0.5]
    assert NormalizedEntropyGate(0.5, 16.0).evaluate(sims)[0] is False        # entropy passes
    suppress, _h, _m = NormalizedEntropyGate(0.5, 16.0, 0.95).evaluate(sims)
    assert suppress is True                                                   # floor abstains


def test_tau_top1_nonfinite_still_fails_closed():
    # Law 1 placement: a non-finite sim raises in _softmax_mass_from_sims BEFORE mass_sorted
    # exists, so the fail-closed except still returns (True, 1.0, 0.0) WITH the floor armed.
    nan = float("nan")
    assert NormalizedEntropyGate(0.5, 16.0, 0.5).evaluate([1.0, nan]) == (True, 1.0, 0.0)


def test_tau_top1_ctor_rejects_negative():
    with pytest.raises(ValueError):
        NormalizedEntropyGate(0.5, 16.0, -0.1)
    with pytest.raises(ValueError):
        NormalizedEntropyGate(0.5, 16.0, float("inf"))


# ── tau_thresh flat-field relevance override (domain default 1.0 ⇒ no effect) ──
# A FLAT field (entropy_norm > h_frac_max) is served instead of abstained iff its top-1
# ABSOLUTE cosine ≥ tau_thresh — an orthogonal axis from tau_top1 (a softmax-MASS floor).
def test_flat_relevant_serves_below_thresh():
    # flat (h_norm≈0.99 > 0.5) but the best candidate is absolutely relevant (cos 0.85 ≥ 0.70):
    # the override serves what the entropy gate alone would have abstained on.
    flat_relevant = [0.85, 0.84, 0.83]
    assert NormalizedEntropyGate(0.5, 16.0).evaluate(flat_relevant)[0] is True   # abstains w/o override
    suppress, h_norm, _ = NormalizedEntropyGate(0.5, 16.0, 0.0, 0.70).evaluate(flat_relevant)
    assert h_norm > 0.5            # genuinely flat
    assert suppress is False       # served by the absolute-cosine override


def test_flat_weak_still_abstains():
    # flat AND low-relevance (top cos 0.30 < 0.70) ⇒ the override does NOT fire; still abstain.
    flat_weak = [0.30, 0.29, 0.28]
    suppress, h_norm, _ = NormalizedEntropyGate(0.5, 16.0, 0.0, 0.70).evaluate(flat_weak)
    assert h_norm > 0.5 and suppress is True


def test_tau_thresh_1p0_is_todays_behavior():
    # tau_thresh=1.0 (the domain default) ⇒ byte-identical tuple to the no-arg gate on BOTH a
    # flat-weak and a flat-strong field — the override never fires (top cos < 1.0), so the
    # off path is provably today's entropy gate.
    for sims in ([0.30, 0.29, 0.28], [0.85, 0.84, 0.83]):
        base = NormalizedEntropyGate(0.5, 16.0).evaluate(sims)
        assert NormalizedEntropyGate(0.5, 16.0, 0.0, 1.0).evaluate(sims) == base, sims


def test_non_flat_field_unaffected_by_tau_thresh():
    # a PEAKED field (entropy_norm 0.39 < 0.5) is decided by the entropy branch alone; the
    # override only touches the flat branch, so the decision is identical across tau_thresh values.
    peaked = [0.85, 0.70, 0.65]
    base = NormalizedEntropyGate(0.5, 16.0).evaluate(peaked)
    assert base[0] is False
    for tau in (1.0, 0.70, 0.2):
        assert NormalizedEntropyGate(0.5, 16.0, 0.0, tau).evaluate(peaked) == base, tau


def test_tau_thresh_boundary_is_exclusive():
    # the override is a strict `>` (serve only ABOVE the threshold): a flat field whose top cos is
    # EXACTLY tau_thresh abstains; lowering tau_thresh below the top cos serves. So tau_thresh=1.0
    # abstains on every flat field BY CONSTRUCTION (a cosine ≤ 1 can never exceed it). (`>=` would
    # serve at the boundary — the boundary mutation.)
    flat = [0.85, 0.84, 0.83]                                          # top cos is exactly 0.85
    at = NormalizedEntropyGate(0.5, 16.0, 0.0, 0.85).evaluate(flat)    # tau_thresh == top cos
    below = NormalizedEntropyGate(0.5, 16.0, 0.0, 0.84).evaluate(flat)  # tau_thresh < top cos
    assert at[1] > 0.5 and at[0] is True        # genuinely flat; boundary abstains
    assert below[0] is False                    # threshold below the top cos: served


def test_override_does_not_rescue_nonfinite():
    # Law 1: a non-finite sim raises in _softmax_mass_from_sims BEFORE top_cos=max(sims) is read,
    # so the fail-closed except still wins WITH the override armed — the override never sees a
    # non-finite sim and cannot fabricate a served result on an undecidable set.
    nan = float("nan")
    g = NormalizedEntropyGate(0.5, 16.0, 0.0, 0.70)
    for sims in ([1.0, nan], [nan, 1.0], [0.85, 0.84, nan]):
        assert g.evaluate(sims) == (True, 1.0, 0.0), sims


def test_override_does_not_relax_tau_top1():
    # PRECEDENCE: tau_top1 is unconditional. A flat field whose top cos clears tau_thresh but whose
    # top-1 MASS is below tau_top1 is STILL suppressed — the override relaxes only the entropy term.
    flat_relevant = [0.85, 0.84, 0.83]      # top cos 0.85 ≥ tau_thresh, but masses are near-uniform
    suppress, _h, _m = NormalizedEntropyGate(0.5, 16.0, 0.95, 0.70).evaluate(flat_relevant)
    assert suppress is True                 # tau_top1 floor abstains despite the relevance override


def test_tau_thresh_ctor_rejects_out_of_range():
    for bad in (0.0, 1.5, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            NormalizedEntropyGate(0.5, 16.0, 0.0, bad)
    # the in-range endpoints construct (1.0 = no effect, 0.70 = the product posture)
    assert NormalizedEntropyGate(0.5, 16.0, 0.0, 1.0).tau_thresh == 1.0
    assert NormalizedEntropyGate(0.5, 16.0, 0.0, 0.70).tau_thresh == 0.70
