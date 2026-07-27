"""hive.domain.retirement — the §3.2 retirement-evidence gate: pure, total,
fail-closed. Retirement is agent-CALLED but machine-GATED — ``retirement_evidence``
says whether a qualifying machine signal exists for the target, and an undecidable
input under-claims (that clause stays unsatisfied; a whole-gate fault returns
ineligible). Clause coverage: drift at the memory's own line (anchored only — the
SOLE drift feed), outcome-hurt with the identity-diversity rule (same-identity
two-call self-destruction blocked), the supersede near-dup winner, and general
memories on clauses 2–3 only. Advisory ``hive_flag`` rows never qualify, and no
detected near-dup PAIR is a signal at all — that feed is gone."""

from __future__ import annotations

import dataclasses

import pytest

from hive.domain.evidence_kinds import (
    EK_OUTCOME_HELPED,
    EK_OUTCOME_HURT,
    EK_OUTCOME_VERIFIED_HURT,
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
    winner=None,
) -> Eligibility:
    return retirement_evidence(
        episode=episode,
        caller_identity=caller,
        drift_verdicts=drift,
        evidence_rows=rows,
        winner_cosine=winner,
    )


# ── the carrier ────────────────────────────────────────────────────────────────
def test_eligibility_is_frozen():
    e = Eligibility(True, ("drift:anchor_missing",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.eligible = False


def test_healthy_evidence_less_target_is_ineligible():
    # the noop-on-healthy floor: no feed carries anything ⇒ nothing may retire.
    assert _gate() == Eligibility(False, ())
    assert _gate(GENERAL) == Eligibility(False, ())


# ── clause 1: materialized drift at the memory's own line (anchored only) ─────
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


# ── clause 3: the supersede near-dup winner (and NOTHING else) ────────────────
def test_the_gate_accepts_no_conflict_pair_feed_at_all():
    """A detected near-dup PAIR is not a signal and cannot be made one from outside:
    the parameter is gone, so a boundary trying to re-open the attack raises at the
    call instead of quietly widening the gate."""
    import inspect

    params = set(inspect.signature(retirement_evidence).parameters)
    assert "conflict_pairs" not in params and "own_lines" not in params, params
    with pytest.raises(TypeError):
        retirement_evidence(  # type: ignore[call-arg]
            episode=ANCHORED,
            caller_identity=CALLER,
            drift_verdicts=(),
            evidence_rows=(),
            conflict_pairs=[(7, 9)],
        )


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


def test_the_winner_clause_reaches_general_memories():
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
            EvidenceRow(EK_OUTCOME_VERIFIED_HURT, "census", 5),
            EvidenceRow(EK_OUTCOME_HURT, OTHER, 6),
        ],
        winner=0.9,
    )
    assert got.eligible is True
    assert got.signals == (
        "drift:anchor_missing",
        "outcome_verified_hurt",
        "outcome_hurt_other_identity",
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
        kind = EK_OUTCOME_VERIFIED_HURT
        actor = "census"
        ts = 9

    assert _gate(rows=[Row()]).eligible is True


def test_a_wider_sequence_row_is_read_by_its_first_three_elements():
    """A caller still projecting a 4th element (the retired payload line) must not
    crash the gate — the extra rides unread."""
    got = _gate(rows=[(EK_OUTCOME_HURT, OTHER, 5, "main")])
    assert got.signals == ("outcome_hurt_other_identity",)


def test_none_feeds_are_ineligible_never_a_raise():
    got = retirement_evidence(
        episode=ANCHORED,
        caller_identity=CALLER,
        drift_verdicts=None,
        evidence_rows=None,
        winner_cosine=None,
    )
    assert got == Eligibility(False, ())


def test_whole_gate_fault_fails_closed_to_ineligible():
    # an episode whose anchors read raises makes the WHOLE gate undecidable — the
    # gate returns ineligible even though a qualifying row is present (never a
    # retire). ``anchors`` is the one carrier attribute the gate still reads, so it
    # is where a broken carrier has to be injected for this property to be tested
    # at all rather than passing because nothing was ever touched.
    class Broken:
        @property
        def anchors(self):
            raise RuntimeError("carrier exploded")

    with pytest.raises(RuntimeError):
        _ = Broken().anchors  # the fault is real, not a silently-absent attribute

    got = retirement_evidence(
        episode=Broken(),
        caller_identity=CALLER,
        drift_verdicts=("anchor_missing",),
        evidence_rows=[EvidenceRow(EK_OUTCOME_VERIFIED_HURT, "c", 1)],
    )
    assert got == Eligibility(False, ())


def test_caller_identity_is_compared_as_text():
    # the identity-diversity compare coerces both sides to str — a caller handed
    # as a non-str object still blocks its own report and passes another's.
    got_same = _gate(rows=[EvidenceRow(EK_OUTCOME_HURT, "1234", 5)], caller=1234)
    assert got_same.eligible is False
    got_other = _gate(rows=[EvidenceRow(EK_OUTCOME_HURT, OTHER, 5)], caller=1234)
    assert got_other.eligible is True
