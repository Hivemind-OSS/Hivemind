"""Frozen value-carrier invariants for the recall surface — focused on the
``polarity`` label (do|dont|neutral) that rides Episode and RecallHit. Polarity is
carried-not-interpreted (mirrors ``trust``/``ts``): the domain validates the enum and
defaults fail-safe to ``neutral`` so a forgotten wire under-claims, never mislabels a
prohibition as a ``do``.
"""
from __future__ import annotations

import pytest

from hive.domain.models import Episode, RecallHit, content_hash


def _ep(**kw):
    base = dict(id=1, tenant_id="t", text="hello", weight=1.0, ts=0, source="s", tags="",
                content_hash=content_hash("hello"), status="pending", proposed_by="a")
    base.update(kw)
    return Episode(**base)


# ── Episode.polarity: the three valid labels accept; a fourth is unconstructable ──
def test_episode_accepts_the_three_polarity_values():
    for p in ("do", "dont", "neutral"):
        assert _ep(polarity=p).polarity == p


def test_episode_defaults_polarity_neutral():
    assert _ep().polarity == "neutral"            # fail-safe default (never a guessed do)


def test_episode_rejects_a_fourth_polarity():
    with pytest.raises(ValueError):
        _ep(polarity="maybe")                     # out-of-enum ⇒ unconstructable


# ── RecallHit.polarity: defaulted fail-safe like trust/ts; the three labels ride ──
def test_recall_hit_defaults_polarity_neutral():
    h = RecallHit(episode_id=1, text="t", sim=0.9)
    assert h.polarity == "neutral"


def test_recall_hit_carries_each_polarity():
    for p in ("do", "dont", "neutral"):
        assert RecallHit(episode_id=1, text="t", sim=0.9, polarity=p).polarity == p
