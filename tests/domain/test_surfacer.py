"""P0.3 — pure UtilitySurfacer [A1]."""
from __future__ import annotations

import random

from hive.domain.models import Scored
from hive.domain.surfacer import UtilitySurfacer, utility_factor

FAM = "r|python|bugfix"


def _surf(enabled=True, eps=0.0):
    return UtilitySurfacer(enabled=enabled, epsilon_explore=eps,
                           f_min=0.5, f_max=1.5, rng=random.Random(0))


def test_f_band_points():
    assert utility_factor(0.5, 0.5, 1.5) == 1.0
    assert utility_factor(1.0, 0.5, 1.5) == 1.5
    assert utility_factor(0.0, 0.5, 1.5) == 0.5


def test_weight_is_base_multiplier_not_alpha():
    a = Scored(episode_id=1, weight=2.0, sim=0.9)
    b = Scored(episode_id=2, weight=1.0, sim=0.95)
    out = _surf().order([a, b], utility_map={}, family_scope=FAM)
    assert out[0].episode_id == 1   # weight 2.0 > 1.0 (base is weight, NOT sim 0.95)


def test_confident_negative_demotes():
    a = Scored(1, weight=1.0, sim=0.9)   # confident-negative ⇒ f<1 ⇒ demoted
    b = Scored(2, weight=1.0, sim=0.9)   # un-credited tie ⇒ f=1
    out = _surf().order([a, b], utility_map={1: 0.1}, family_scope=FAM)
    assert out[0].episode_id == 2 and out[1].episode_id == 1


def test_utility_none_is_identity():
    a = Scored(1, 2.0, 0.9)
    b = Scored(2, 1.0, 0.9)
    out = _surf().order([a, b], utility_map={}, family_scope=FAM)
    assert [s.episode_id for s in out] == [1, 2]   # unchanged (all f=1, weight order)


def test_surfacer_disabled_is_passthrough():
    scored = [Scored(1, 1.0, 0.5), Scored(2, 5.0, 0.9)]
    out = _surf(enabled=False).order(scored, utility_map={2: 0.9}, family_scope=FAM)
    assert out == scored   # byte-identical, no reorder (Phase-1 inert)


def test_epsilon_ignores_utility():
    a = Scored(1, 1.0, 0.9)
    b = Scored(2, 1.0, 0.9)
    out = _surf(eps=1.0).order([a, b], utility_map={1: 0.99}, family_scope=FAM)
    assert [s.episode_id for s in out] == [1, 2]   # utility ignored this call (guardrail-1)


def test_empty_candidate_list_stays_empty():
    assert _surf().order([], utility_map={1: 0.9}, family_scope=FAM) == []
