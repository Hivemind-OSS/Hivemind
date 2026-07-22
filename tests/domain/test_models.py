"""Frozen value-carrier invariants for the recall surface — the carried labels
(``polarity``/``kind``) and the v3 repo-partition carriers (``repos``/``anchors``
with ``AnchorRef``). Labels are carried-not-interpreted (mirrors ``trust``/``ts``):
the domain validates the enum and defaults fail-safe so a forgotten wire
under-claims — ``neutral`` never mislabels a prohibition, ``()`` never over-scopes
a memory into a repo partition.
"""
from __future__ import annotations

import dataclasses

import pytest

from hive.domain.kinds import KIND_NAMES
from hive.domain.models import AnchorRef, Episode, RecallHit, content_hash


def _ep(**kw):
    base = dict(id=1, tenant_id="t", text="hello", weight=1.0, ts=0,
                content_hash=content_hash("hello"), status="pending", proposed_by="a")
    base.update(kw)
    return Episode(**base)


def _approved(**kw):
    return _ep(status="approved", **kw)


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


# ── Episode.kind: carried label validated like polarity ──
def test_episode_defaults_kind_note():
    assert _ep().kind == "note"                   # fail-safe under-claiming default


def test_episode_accepts_every_registered_kind():
    for k in KIND_NAMES:
        assert _ep(kind=k).kind == k


def test_episode_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        _ep(kind="rumor")                         # out-of-enum ⇒ unconstructable


def test_recall_hit_defaults_kind_note():
    assert RecallHit(episode_id=1, text="t", sim=0.9).kind == "note"


# ═══ AnchorRef: the (repo, anchor) code binding — illegal states unconstructable ══
def test_anchor_ref_carries_its_fields_and_defaults_fp_meta_empty():
    a = AnchorRef(repo="alpha", anchor="hive/app.py::greet")
    assert (a.repo, a.anchor, a.fp_meta) == ("alpha", "hive/app.py::greet", "")
    b = AnchorRef(repo="alpha", anchor="hive/app.py", fp_meta='{"v":1}')
    assert b.fp_meta == '{"v":1}'


def test_anchor_ref_is_frozen():
    a = AnchorRef(repo="alpha", anchor="x.py")
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.repo = "beta"


def test_anchor_ref_requires_non_empty_repo_and_anchor():
    with pytest.raises(ValueError):
        AnchorRef(repo="", anchor="x.py")
    with pytest.raises(ValueError):
        # scope-only membership rides Episode.repos, never a degenerate anchor
        AnchorRef(repo="alpha", anchor="")


# ═══ Episode.repos / .anchors: the v3 partition invariants ════════════════════
def test_episode_defaults_general():
    ep = _ep()
    assert ep.repos == () and ep.anchors == ()    # fail-safe: general, never scoped


def test_episode_carries_scope_and_anchors():
    a = AnchorRef(repo="alpha", anchor="app.py::greet")
    ep = _ep(repos=("alpha", "beta"), anchors=(a,))
    assert ep.repos == ("alpha", "beta")
    assert ep.anchors == (a,)


def test_episode_repos_must_be_canonical_sorted_unique():
    with pytest.raises(ValueError):
        _ep(repos=("beta", "alpha"))              # unsorted encoding refused
    with pytest.raises(ValueError):
        _ep(repos=("alpha", "alpha"))             # duplicate encoding refused
    with pytest.raises(ValueError):
        _ep(repos=("",))                          # empty repo name refused


def test_episode_anchor_repo_must_ride_in_repos():
    # repos == sorted(anchor repos ∪ declared scope): an anchor whose repo the
    # episode does not claim is UNCONSTRUCTABLE.
    a = AnchorRef(repo="alpha", anchor="app.py::greet")
    with pytest.raises(ValueError):
        _ep(repos=(), anchors=(a,))
    with pytest.raises(ValueError):
        _ep(repos=("beta",), anchors=(a,))
    ok = _ep(repos=("alpha",), anchors=(a,))      # the consistent form constructs
    assert ok.repos == ("alpha",)


def test_episode_anchors_must_be_anchor_refs():
    with pytest.raises(ValueError):
        _ep(repos=("alpha",), anchors=(("alpha", "app.py"),))   # bare tuple refused


# ═══ the dropped v2 fields are GONE (no approver, no free-text anchor, no origin) ═
@pytest.mark.parametrize("kw", [
    {"approved_by": "alice"}, {"approved_ts": 5}, {"tags": ""},
    {"anchor": "x.py"}, {"provenance": "human"},
])
def test_episode_dropped_fields_are_unconstructable(kw):
    with pytest.raises(TypeError):
        _ep(**kw)


@pytest.mark.parametrize("kw", [
    {"anchor": "x.py"}, {"provenance": "human"},
])
def test_recall_hit_dropped_fields_are_unconstructable(kw):
    with pytest.raises(TypeError):
        RecallHit(episode_id=1, text="t", sim=0.9, **kw)


def test_recall_hit_defaults_general_and_carries_scope():
    h = RecallHit(episode_id=1, text="t", sim=0.9)
    assert h.repos == () and h.anchors == ()      # fail-safe: general
    a = AnchorRef(repo="alpha", anchor="app.py::greet")
    scoped = RecallHit(episode_id=1, text="t", sim=0.9,
                       repos=("alpha",), anchors=(a,))
    assert scoped.repos == ("alpha",) and scoped.anchors == (a,)


# ═══ the kept trust/status invariants ═════════════════════════════════════════
def test_servable_trust_requires_approved_status():
    # kept from v2: a servable-trust episode must be materialized.
    for trust in ("provisional", "established"):
        with pytest.raises(ValueError):
            _ep(status="pending", trust=trust)
        assert _approved(trust=trust).trust == trust


def test_superseded_by_requires_deprecated():
    with pytest.raises(ValueError):
        _approved(trust="provisional", superseded_by=9)
    ok = _approved(trust="deprecated", superseded_by=9)
    assert ok.superseded_by == 9


def test_content_hash_binds_text():
    with pytest.raises(ValueError):
        Episode(id=1, tenant_id="t", text="hello", weight=1.0, ts=0,
                content_hash=content_hash("other"), status="pending",
                proposed_by="a")
