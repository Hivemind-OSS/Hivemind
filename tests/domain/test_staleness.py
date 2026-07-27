"""The ``decide`` truth table + ``symbol_lookups`` cases, plus the Hypothesis
properties that pin the ladder's direction over the whole combinatorial input space
(seven independent fields — enumerating it by hand would be a worse test than a
generator).
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hive.domain.staleness import (
    ANCHOR_CHANGED,
    ANCHOR_MISSING,
    BASE_UNKNOWN,
    COMMIT_EVIDENCE_CAP,
    FRESH,
    PATH_UNSEEN,
    PROBE_FAILED,
    SYMBOL_UNSEEN,
    UNVERIFIABLE,
    AnchorFacts,
    decide,
    symbol_lookups,
)

BASE = "b" * 40
SHA = "c" * 40


def facts(**kw) -> AnchorFacts:
    """A baselined, present, unchanged anchor whose symbol was seen at its baseline —
    the fresh floor every case perturbs. ``symbol_resolved_at_base`` is part of the
    floor rather than of the perturbations because it is the observation ``fresh``
    stands on for a symbol anchor: without it there is nothing to be fresh ABOUT."""
    kw.setdefault("symbol_resolved_at_base", True)
    return AnchorFacts(base_tip=BASE, in_base_tree=True, **kw)


# ── symbol_lookups ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("", ()),
        ("greet", ("greet",)),
        ("Store.repo_remove", ("Store.repo_remove", "repo_remove")),
        ("Ns::Store.m", ("Ns::Store.m", "m")),
        ("a.b.c", ("a.b.c", "c")),
        ("trailing.", ("trailing.",)),  # empty tail ⇒ no second lookup
        (".leading", (".leading", "leading")),
    ],
)
def test_symbol_lookups_cases(symbol, expected):
    assert symbol_lookups(symbol) == expected


# ── the truth table ───────────────────────────────────────────────────────────


def test_a_probe_fault_is_unverifiable():
    verdict, detail = decide(facts(probe_failed=True, status="M"), symbol="greet")
    assert verdict == UNVERIFIABLE
    assert detail == {"reason": PROBE_FAILED}


def test_an_unbaselined_anchor_is_unverifiable():
    verdict, detail = decide(AnchorFacts(in_base_tree=True), symbol="")
    assert verdict == UNVERIFIABLE
    assert detail == {"reason": BASE_UNKNOWN}


def test_a_path_that_never_existed_is_unverifiable():
    """marker 4's test: returning ``fresh`` when ``in_base_tree`` is False claims a
    path we never observed is current."""
    verdict, detail = decide(
        AnchorFacts(base_tip=BASE, in_base_tree=False, status=""), symbol=""
    )
    assert verdict == UNVERIFIABLE, "silence about an unseen path is not freshness"
    assert detail == {"reason": PATH_UNSEEN}


def test_a_path_the_range_left_alone_is_fresh():
    assert decide(facts(), symbol="")[0] == FRESH
    assert decide(facts(), symbol="greet")[0] == FRESH


@pytest.mark.parametrize("status", ["D", "R100", "R", "R087"])
def test_a_departed_path_is_missing(status):
    for symbol in ("", "greet"):
        verdict, detail = decide(
            facts(status=status, path_commits=(SHA,)), symbol=symbol
        )
        assert verdict == ANCHOR_MISSING
        assert detail["since"] == BASE
        assert detail["commits"] == [SHA]


def test_a_changed_file_scoped_anchor_is_changed_with_its_commits():
    verdict, detail = decide(facts(status="M", path_commits=(SHA, BASE)), symbol="")
    assert verdict == ANCHOR_CHANGED
    assert detail == {"since": BASE, "commits": [SHA, BASE], "n_commits": 2}


def test_a_symbol_whose_own_lines_moved_is_changed():
    verdict, detail = decide(
        facts(status="M", symbol_commits=(SHA,), path_commits=("x" * 40,)),
        symbol="greet",
    )
    assert verdict == ANCHOR_CHANGED
    assert detail["commits"] == [SHA], "the SYMBOL's commits win over the path's"


def test_a_symbol_whose_own_lines_did_not_move_is_fresh():
    assert decide(facts(status="M", path_commits=(SHA,)), symbol="greet")[0] == FRESH


def test_a_deleted_symbol_that_resolved_at_the_baseline_is_missing():
    verdict, detail = decide(
        facts(status="M", symbol_resolved_at_tip=False, path_commits=(SHA,)),
        symbol="greet",
    )
    assert verdict == ANCHOR_MISSING
    assert detail["commits"] == [SHA]


def test_an_anchor_that_never_resolved_is_never_missing():
    """An anchor git cannot resolve at EITHER end is unverifiable — calling it missing
    would qualify a retirement the evidence does not support."""
    verdict, detail = decide(
        facts(
            status="M",
            symbol_resolved_at_tip=False,
            symbol_resolved_at_base=False,
            path_commits=(SHA,),
        ),
        symbol="gre3t",
    )
    assert verdict == UNVERIFIABLE, (
        "an unresolvable symbol is the honest unknown, never provable absence"
    )
    assert detail == {"reason": SYMBOL_UNSEEN}


@pytest.mark.parametrize("status", ["", "M", "A", "D", "R100"])
def test_an_anchor_that_never_resolved_is_never_fresh_either(status):
    """The rung's other half, and the reason it sits ABOVE the diff check: a QUIET file
    says nothing about a symbol nobody ever saw inside it, so the whole diff column
    must read the same unknown. ``fresh`` here would be the over-claim the file tier
    cannot see, and ``anchor_missing`` would be a retirement nothing measured."""
    verdict, detail = decide(
        facts(status=status, symbol_resolved_at_base=False, path_commits=(SHA,)),
        symbol="gre3t",
    )
    assert verdict == UNVERIFIABLE
    assert detail == {"reason": SYMBOL_UNSEEN}


def test_a_file_scoped_anchor_never_asks_the_symbol_question():
    """The rung is symbol-only: a file anchor's claim IS the file, and its baseline row
    is never probed, so reading its unprobed default would silence every one of them."""
    assert decide(facts(symbol_resolved_at_base=False), symbol="")[0] == FRESH
    assert (
        decide(facts(status="M", symbol_resolved_at_base=False), symbol="")[0]
        == ANCHOR_CHANGED
    )


def test_the_commit_evidence_is_capped_but_the_count_is_not():
    shas = tuple(f"{i:040d}" for i in range(25))
    _verdict, detail = decide(facts(status="M", path_commits=shas), symbol="")
    assert detail["commits"] == list(shas[:COMMIT_EVIDENCE_CAP])
    assert detail["n_commits"] == 25, "the total is honest, not the cap"


def test_the_ladder_is_total_over_hostile_input():
    """Every field at a hostile value at once still returns a member of the enum."""
    hostile = AnchorFacts(
        base_tip="\x00" * 5,
        in_base_tree=True,
        status="\n\t?!",
        symbol_commits=("",),
        path_commits=("",),
    )
    verdict, detail = decide(hostile, symbol="\x00")
    assert verdict in (FRESH, ANCHOR_CHANGED, ANCHOR_MISSING, UNVERIFIABLE)
    assert isinstance(detail, dict)


# ── properties (the input space is combinatorial; it earns a generator) ───────

_text = st.text(max_size=6)
_shas = st.tuples(*[]) | st.lists(_text, max_size=4).map(tuple)

_facts = st.builds(
    AnchorFacts,
    base_tip=_text,
    in_base_tree=st.booleans(),
    status=_text,
    symbol_commits=_shas,
    path_commits=_shas,
    symbol_resolved_at_tip=st.booleans(),
    symbol_resolved_at_base=st.booleans(),
    probe_failed=st.booleans(),
)


@settings(max_examples=400)
@given(f=_facts, symbol=_text)
def test_decide_never_raises_and_always_returns_the_enum(f, symbol):
    verdict, detail = decide(f, symbol=symbol)
    assert verdict in (FRESH, ANCHOR_CHANGED, ANCHOR_MISSING, UNVERIFIABLE)
    assert isinstance(detail, dict)


@settings(max_examples=400)
@given(f=_facts, symbol=_text)
def test_an_unknown_can_never_read_fresh_or_stale(f, symbol):
    """The fail-direction ledger, mechanised: a faulted probe or an anchor with no
    baseline has nothing to claim in either direction."""
    verdict, _detail = decide(f, symbol=symbol)
    if f.probe_failed or not f.base_tip:
        assert verdict == UNVERIFIABLE


@settings(max_examples=400)
@given(f=_facts, symbol=_text)
def test_fresh_requires_an_observation_that_actually_happened(f, symbol):
    """``fresh`` is a positive claim: it is reachable only when the path was seen at
    the baseline AND the thing the anchor names demonstrably did not move."""
    verdict, _detail = decide(f, symbol=symbol)
    if verdict == FRESH:
        assert f.base_tip and f.in_base_tree and not f.probe_failed
        assert not f.status or (symbol and f.symbol_resolved_at_tip)
        assert not symbol or f.symbol_resolved_at_base


@settings(max_examples=400)
@given(f=_facts, symbol=_text)
def test_a_symbol_unseen_at_its_baseline_only_ever_reads_unverifiable(f, symbol):
    """The escalation this rung closes, as a property rather than a row: no diff
    status, no commit list and no tip resolution can move a symbol nobody observed at
    its baseline off the unknown."""
    verdict, _detail = decide(f, symbol=symbol)
    if symbol and not f.symbol_resolved_at_base:
        assert verdict == UNVERIFIABLE


@settings(max_examples=400)
@given(f=_facts, symbol=_text)
def test_stale_tier_always_carries_its_evidence_keys(f, symbol):
    verdict, detail = decide(f, symbol=symbol)
    if verdict in (ANCHOR_CHANGED, ANCHOR_MISSING):
        assert set(detail) == {"since", "commits", "n_commits"}
        assert detail["since"] == f.base_tip
        assert len(detail["commits"]) <= COMMIT_EVIDENCE_CAP
