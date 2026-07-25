"""hive.domain.retirement — the §3.2 retirement-evidence gate: pure, total,
fail-closed. Retirement is agent-CALLED but machine-GATED — ``retirement_evidence``
says whether a qualifying machine signal exists for the target, and an undecidable
input under-claims (that clause stays unsatisfied; a whole-gate fault returns
ineligible). Clause coverage: drift at the memory's own line (anchored only),
verify_stale vs verify_current recency, outcome-hurt with the identity-diversity
rule (same-identity two-call self-destruction blocked), mechanical contradiction +
the supersede near-dup winner, and general memories on clauses 2–3 only. Advisory
``hive_flag`` rows never qualify."""

from __future__ import annotations

import dataclasses

import pytest

from hive.domain.conflict import ConflictNote
from hive.domain.evidence_kinds import (
    EK_OUTCOME_HELPED,
    EK_OUTCOME_HURT,
    EK_OUTCOME_VERIFIED_HURT,
    EK_VERIFY_CURRENT,
    EK_VERIFY_STALE,
)
from hive.domain.retirement import (
    Eligibility,
    EvidenceRow,
    retirement_evidence,
)
from tests.fakes._fakes import make_episode

CALLER = "session-A"
OTHER = "session-B"

ANCHORED = make_episode(
    7, "the greet helper trims its input", anchors=[("alpha", "app.py::greet")]
)
GENERAL = make_episode(7, "a general memory with no anchors")


def _gate(
    episode=ANCHORED,
    *,
    caller=CALLER,
    drift=(),
    rows=(),
    pairs=(),
    winner=None,
    own_lines=None,
) -> Eligibility:
    return retirement_evidence(
        episode=episode,
        caller_identity=caller,
        drift_verdicts=drift,
        evidence_rows=rows,
        conflict_pairs=pairs,
        winner_cosine=winner,
        own_lines=own_lines,
    )


def _note(a: int, b: int, relation: str = "contradiction") -> ConflictNote:
    return ConflictNote(
        a_id=min(a, b),
        b_id=max(a, b),
        relation=relation,
        cosine=0.9,
        anchor="",
        loser_hint=None,
    )


# ── the carrier ────────────────────────────────────────────────────────────────
def test_eligibility_is_frozen():
    e = Eligibility(True, ("verify_stale",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.eligible = False


def test_healthy_evidence_less_target_is_ineligible():
    # the noop-on-healthy floor: no feed carries anything ⇒ nothing may retire.
    assert _gate() == Eligibility(False, ())
    assert _gate(GENERAL) == Eligibility(False, ())


# ── clause 1a: materialized drift at the memory's own line (anchored only) ────
@pytest.mark.parametrize(
    "verdict", ["anchor_missing", "anchor_changed", "blast_radius_changed"]
)
def test_qualifying_drift_verdicts_qualify_an_anchored_target(verdict):
    got = _gate(drift=(verdict,))
    assert got.eligible is True
    assert got.signals == (f"drift:{verdict}",)
    assert any("drift" in s for s in got.signals)  # the CT-7 substring contract


@pytest.mark.parametrize(
    "verdict",
    ["fresh", "branch_scoped", "unverifiable", "made_up_verdict", "", None, 42],
)
def test_non_qualifying_or_malformed_drift_never_qualifies(verdict):
    assert _gate(drift=(verdict,)).eligible is False


def test_duplicate_drift_verdicts_stamp_one_signal():
    got = _gate(drift=("anchor_missing", "anchor_missing", "anchor_changed"))
    assert got.signals == ("drift:anchor_missing", "drift:anchor_changed")


def test_drift_is_na_for_a_general_memory():
    # §3.2 clause 4: no repo, no anchor ⇒ clauses 2–3 only — a drift verdict fed
    # for a general target is ignored (n/a), never a retirement license.
    assert _gate(GENERAL, drift=("anchor_missing",)).eligible is False


# ── clause 1b: verify_stale strictly newer than every verify_current ───────────
def test_verify_stale_alone_qualifies():
    got = _gate(rows=[EvidenceRow(EK_VERIFY_STALE, "census", 100)])
    assert got.eligible is True and got.signals == ("verify_stale",)
    assert any("stale" in s for s in got.signals)  # the CT-7 substring contract


def test_verify_stale_newer_than_current_qualifies():
    got = _gate(
        rows=[
            EvidenceRow(EK_VERIFY_CURRENT, "census", 100),
            EvidenceRow(EK_VERIFY_STALE, "census", 200),
        ]
    )
    assert got.eligible is True and "verify_stale" in got.signals


def test_newer_verify_current_disqualifies_the_stale_row():
    got = _gate(
        rows=[
            EvidenceRow(EK_VERIFY_STALE, "census", 100),
            EvidenceRow(EK_VERIFY_CURRENT, "census", 200),
        ]
    )
    assert got.eligible is False


def test_verify_tie_disqualifies_fail_closed():
    # equal timestamps: the re-verification wins — stale must be STRICTLY newer.
    got = _gate(
        rows=[
            EvidenceRow(EK_VERIFY_STALE, "census", 100),
            EvidenceRow(EK_VERIFY_CURRENT, "census", 100),
        ]
    )
    assert got.eligible is False


def test_verify_clause_is_anchor_agnostic_in_the_pure_function():
    # The PURE clause does not read anchors — but that is a property of this
    # function, NOT a system fact: no production writer can put a verify row on
    # an anchor-less memory (the census join reads anchored_episodes(), which
    # filters anchor != ''). The reachability twin is
    # tests/contract/test_retirement_gate_e2e.py::
    # test_an_anchorless_memory_never_acquires_a_verify_row.
    got = _gate(GENERAL, rows=[EvidenceRow(EK_VERIFY_STALE, "census", 100)])
    assert got.eligible is True and got.signals == ("verify_stale",)


# ── clause 1b's line filter: own_lines is a TRI-STATE, and it under-claims ─────
def test_own_lines_none_reads_every_row_exactly_as_before_declared_lines():
    # the overwhelmingly common case: the memory named no line anywhere, so
    # canonical rows ARE its own and the clause is byte-identical to its
    # pre-declared-line behaviour.
    rows = [EvidenceRow(EK_VERIFY_STALE, "census", 100, "main")]
    assert _gate(rows=rows, own_lines=None).signals == ("verify_stale",)


def test_own_lines_matching_the_rows_line_qualifies():
    rows = [EvidenceRow(EK_VERIFY_STALE, "census", 100, "feature")]
    assert _gate(rows=rows, own_lines=frozenset({"feature"})).signals == (
        "verify_stale",
    )


def test_own_lines_on_another_line_never_qualifies():
    rows = [EvidenceRow(EK_VERIFY_STALE, "census", 100, "main")]
    assert _gate(rows=rows, own_lines=frozenset({"feature"})).eligible is False


def test_own_lines_empty_set_attributes_nothing():
    # the under-claim state: the boundary could not decide WHOSE line the row
    # measured (a memory anchored in more than one repo — the payload stamps the
    # line but not the repo), so no ledger row qualifies. Clause 1a, which IS
    # repo-keyed, still judges it.
    rows = [EvidenceRow(EK_VERIFY_STALE, "census", 100, "feature")]
    assert _gate(rows=rows, own_lines=frozenset()).eligible is False


def test_a_ref_less_row_never_qualifies_under_an_engaged_filter():
    # a legacy pre-stamp row carries no line; undecidable is NOT "matches".
    rows = [EvidenceRow(EK_VERIFY_STALE, "census", 100, "")]
    assert _gate(rows=rows, own_lines=frozenset({"feature"})).eligible is False
    assert _gate(rows=rows, own_lines=None).eligible is True  # unfiltered, as before


def test_an_off_line_current_row_still_refuses_a_matching_stale_row():
    # the verify_current arm is deliberately UNfiltered: a current row only ever
    # REFUSES, so dropping an off-line one is the OVER-claiming direction — the
    # one direction this gate may never take.
    rows = [
        EvidenceRow(EK_VERIFY_STALE, "census", 100, "feature"),
        EvidenceRow(EK_VERIFY_CURRENT, "census", 200, "main"),
    ]
    assert _gate(rows=rows, own_lines=frozenset({"feature"})).eligible is False


# ── clause 2: outcome-hurt (identity-diverse for the agent-reported kind) ──────
def test_outcome_verified_hurt_qualifies_any_actor():
    # server-written, non-forgeable — the recorded actor is irrelevant (census).
    got = _gate(rows=[EvidenceRow(EK_OUTCOME_VERIFIED_HURT, "census", 5)])
    assert got.eligible is True and got.signals == ("outcome_verified_hurt",)
    assert any("hurt" in s for s in got.signals)  # the CT-7 substring contract


def test_outcome_hurt_from_other_identity_qualifies():
    got = _gate(rows=[EvidenceRow(EK_OUTCOME_HURT, OTHER, 5)])
    assert got.eligible is True
    assert got.signals == ("outcome_hurt_other_identity",)


def test_outcome_hurt_same_identity_never_qualifies():
    # THE two-call self-destruction guard: hive_outcome(hurt=[id]) + hive_prune(id)
    # from the SAME identity must stay a noop (the CT-7 twin).
    got = _gate(rows=[EvidenceRow(EK_OUTCOME_HURT, CALLER, 5)], caller=CALLER)
    assert got.eligible is False and got.signals == ()


def test_outcome_helped_rows_never_qualify():
    assert _gate(rows=[EvidenceRow(EK_OUTCOME_HELPED, OTHER, 5)]).eligible is False


def test_outcome_clause_reaches_general_memories():
    got = _gate(GENERAL, rows=[EvidenceRow(EK_OUTCOME_HURT, OTHER, 5)])
    assert got.eligible is True


# ── clause 3: mechanical contradiction + the supersede near-dup winner ─────────
def test_conflict_pair_naming_the_target_qualifies():
    got = _gate(pairs=[_note(7, 9)])
    assert got.eligible is True and got.signals == ("contradiction",)
    assert any("contradiction" in s for s in got.signals)  # CT-7 substring


def test_conflict_pair_naming_the_target_as_b_side_qualifies():
    assert _gate(pairs=[_note(3, 7)]).eligible is True


def test_conflict_pair_not_naming_the_target_never_qualifies():
    assert _gate(pairs=[_note(3, 9)]).eligible is False


def test_malformed_conflict_pair_never_fabricates_a_contradiction():
    assert _gate(pairs=["garbage", {"a": 7}, (7,), None]).eligible is False


def test_winner_cosine_qualifies_a_supersede():
    # the boundary passes winner_cosine ONLY when it already cleared conflict.tau —
    # here the gate validates finiteness/range and stamps the near-dup signal.
    got = _gate(winner=0.92)
    assert got.eligible is True and got.signals == ("winner_near_dup",)


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf"), 1.5, -1.5, "high", object()]
)
def test_non_finite_or_out_of_range_winner_cosine_never_qualifies(bad):
    assert _gate(winner=bad).eligible is False


def test_contradiction_clause_reaches_general_memories():
    assert _gate(GENERAL, pairs=[_note(7, 9)]).eligible is True
    assert _gate(GENERAL, winner=0.9).eligible is True


# ── advisory hive_flag rows NEVER qualify ──────────────────────────────────────
def test_flag_kind_rows_never_qualify():
    # a conflict_flags row smuggled into the ledger feed is a foreign kind — the
    # gate reads only its own vocabulary, so agent-asserted flags are inert.
    rows = [
        EvidenceRow("conflict_flag", OTHER, 5),
        EvidenceRow("flag", OTHER, 5),
        EvidenceRow("hive_flag", OTHER, 5),
    ]
    assert _gate(rows=rows).eligible is False
    assert _gate(GENERAL, rows=rows).eligible is False


# ── signal accumulation: every satisfied clause, clause order ──────────────────
def test_all_satisfied_clauses_are_stamped_in_clause_order():
    got = _gate(
        drift=("anchor_missing",),
        rows=[
            EvidenceRow(EK_VERIFY_STALE, "census", 100),
            EvidenceRow(EK_OUTCOME_VERIFIED_HURT, "census", 5),
            EvidenceRow(EK_OUTCOME_HURT, OTHER, 6),
        ],
        pairs=[_note(7, 9)],
        winner=0.9,
    )
    assert got.eligible is True
    assert got.signals == (
        "drift:anchor_missing",
        "verify_stale",
        "outcome_verified_hurt",
        "outcome_hurt_other_identity",
        "contradiction",
        "winner_near_dup",
    )


# ── total + fail-closed: undecidable ⇒ ineligible, never a raise ───────────────
def test_malformed_evidence_rows_are_skipped_while_good_rows_qualify():
    got = _gate(
        rows=["garbage", 42, ("only-kind",), None, (EK_OUTCOME_HURT, OTHER, 5)]
    )  # the 3-sequence form
    assert got.eligible is True
    assert got.signals == ("outcome_hurt_other_identity",)


def test_attr_shaped_rows_are_read():
    class Row:
        kind = EK_VERIFY_STALE
        actor = "census"
        ts = 9

    assert _gate(rows=[Row()]).eligible is True


def test_none_feeds_are_ineligible_never_a_raise():
    got = retirement_evidence(
        episode=ANCHORED,
        caller_identity=CALLER,
        drift_verdicts=None,
        evidence_rows=None,
        conflict_pairs=None,
        winner_cosine=None,
    )
    assert got == Eligibility(False, ())


def test_whole_gate_fault_fails_closed_to_ineligible():
    # an episode whose id read raises makes the WHOLE gate undecidable — the gate
    # returns ineligible even though a qualifying row is present (never a retire).
    class Broken:
        anchors = ()

        @property
        def id(self):
            raise RuntimeError("carrier exploded")

    got = retirement_evidence(
        episode=Broken(),
        caller_identity=CALLER,
        drift_verdicts=(),
        conflict_pairs=(),
        evidence_rows=[EvidenceRow(EK_VERIFY_STALE, "c", 1)],
    )
    assert got == Eligibility(False, ())


def test_caller_identity_is_compared_as_text():
    # the identity-diversity compare coerces both sides to str — a caller handed
    # as a non-str object still blocks its own report and passes another's.
    got_same = _gate(rows=[EvidenceRow(EK_OUTCOME_HURT, "1234", 5)], caller=1234)
    assert got_same.eligible is False
    got_other = _gate(rows=[EvidenceRow(EK_OUTCOME_HURT, OTHER, 5)], caller=1234)
    assert got_other.eligible is True
