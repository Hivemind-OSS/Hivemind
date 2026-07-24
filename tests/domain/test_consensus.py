"""The pure suspect-consensus detector: a thin-n_eff provisional surfaces, a
fully-independent one does not, k==0 is skipped, deterministic sort + top_n cap,
the note self-asserts its numeric ranges, and the martingale_warning forward-compat
clause (thin AND not has_settled_win). PURE — hand-set numbers, no store, no embedder."""

from __future__ import annotations

import pytest

from hive.domain.consensus import (
    SuspectConsensusItem,
    SuspectConsensusNote,
    detect_suspect_consensus,
)


def _item(eid, *, n_eff, k, rho_bar=0.9, has_settled_win=False, anchor="f.py"):
    return SuspectConsensusItem(
        episode_id=eid,
        rho_bar=rho_bar,
        n_eff=n_eff,
        k=k,
        has_settled_win=has_settled_win,
        anchor=anchor,
    )


# ── SuspectConsensusNote invariants (illegal states unconstructable) ───────────
def test_note_rejects_out_of_range_rho_bar():
    with pytest.raises(ValueError):
        SuspectConsensusNote(
            episode_id=1, rho_bar=1.5, n_eff=1.0, k=3, martingale_warning=True
        )
    with pytest.raises(ValueError):
        SuspectConsensusNote(
            episode_id=1, rho_bar=float("nan"), n_eff=1.0, k=3, martingale_warning=True
        )


def test_note_rejects_negative_n_eff_and_k():
    with pytest.raises(ValueError):
        SuspectConsensusNote(
            episode_id=1, rho_bar=0.5, n_eff=-0.1, k=3, martingale_warning=True
        )
    with pytest.raises(ValueError):
        SuspectConsensusNote(
            episode_id=1, rho_bar=0.5, n_eff=float("inf"), k=3, martingale_warning=True
        )
    with pytest.raises(ValueError):
        SuspectConsensusNote(
            episode_id=1, rho_bar=0.5, n_eff=1.0, k=-1, martingale_warning=True
        )


def test_note_is_ids_and_numbers_only():
    # the carrier shape: episode_id + numbers + the warning bool — no text, no anchor.
    note = SuspectConsensusNote(
        episode_id=7, rho_bar=0.9, n_eff=1.0, k=4, martingale_warning=True
    )
    fields = set(note.__dataclass_fields__)
    assert fields == {"episode_id", "rho_bar", "n_eff", "k", "martingale_warning"}


# ── thin vs independent ────────────────────────────────────────────────────────
def test_thin_n_eff_surfaces():
    # n_eff/k = 1/4 = 0.25 < 0.5 ⇒ flagged
    notes = detect_suspect_consensus([_item(10, n_eff=1.0, k=4)], n_eff_frac_max=0.5)
    assert len(notes) == 1
    assert notes[0].episode_id == 10 and notes[0].n_eff == 1.0 and notes[0].k == 4


def test_fully_independent_does_not_surface():
    # n_eff/k = 4/4 = 1.0, not < 0.5 ⇒ NOT flagged
    notes = detect_suspect_consensus([_item(11, n_eff=4.0, k=4)], n_eff_frac_max=0.5)
    assert notes == ()


def test_boundary_is_strict_less_than():
    # n_eff/k == frac_max exactly ⇒ NOT flagged (strict <). This pins mutation #3
    # (flip < to <= or >).
    notes = detect_suspect_consensus([_item(12, n_eff=2.0, k=4)], n_eff_frac_max=0.5)
    assert notes == ()
    # nudge just below the floor ⇒ flagged
    notes2 = detect_suspect_consensus([_item(12, n_eff=1.99, k=4)], n_eff_frac_max=0.5)
    assert len(notes2) == 1


def test_k_zero_is_skipped():
    notes = detect_suspect_consensus([_item(13, n_eff=0.0, k=0)], n_eff_frac_max=0.5)
    assert notes == ()


# ── deterministic order + cap ──────────────────────────────────────────────────
def test_sort_by_frac_then_episode_id():
    # fracs: A=0.1 (n_eff=0.4,k=4), B=0.25 (1/4), C=0.1 (0.4/4, same as A) → tie broken by id
    items = [
        _item(30, n_eff=1.0, k=4),  # 0.25
        _item(10, n_eff=0.4, k=4),  # 0.10
        _item(20, n_eff=0.4, k=4),
    ]  # 0.10
    notes = detect_suspect_consensus(items, n_eff_frac_max=0.5)
    assert [n.episode_id for n in notes] == [
        10,
        20,
        30,
    ]  # (0.10,10),(0.10,20),(0.25,30)


def test_top_n_cap():
    items = [_item(i, n_eff=1.0, k=10) for i in range(20)]  # all 0.1 < 0.5
    notes = detect_suspect_consensus(items, n_eff_frac_max=0.5, top_n=3)
    assert len(notes) == 3


def test_degenerate_guards_return_empty():
    assert (
        detect_suspect_consensus(
            [_item(1, n_eff=1.0, k=4)], n_eff_frac_max=0.5, top_n=0
        )
        == ()
    )
    assert (
        detect_suspect_consensus(
            [_item(1, n_eff=1.0, k=4)], n_eff_frac_max=float("nan")
        )
        == ()
    )
    assert detect_suspect_consensus([], n_eff_frac_max=0.5) == ()


# ── martingale_warning forward-compat clause ───────────────────────────────────
def test_martingale_warning_thin_and_no_settled_win_is_true():
    notes = detect_suspect_consensus(
        [_item(40, n_eff=1.0, k=4, has_settled_win=False)], n_eff_frac_max=0.5
    )
    assert notes[0].martingale_warning is True


def test_martingale_warning_thin_but_settled_win_is_false():
    # forward-compat: a settled win on a thin promotion suppresses the warning, even though
    # the report always passes has_settled_win=False today.
    notes = detect_suspect_consensus(
        [_item(41, n_eff=1.0, k=4, has_settled_win=True)], n_eff_frac_max=0.5
    )
    assert len(notes) == 1  # still THIN ⇒ still surfaced
    assert notes[0].martingale_warning is False  # but not a martingale warning
