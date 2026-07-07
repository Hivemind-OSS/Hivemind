"""C4(poison) — the poison-aware agent loop. ``run_poison_task`` recalls → resolves served ids →
derives the gold AND false mem-id sets INLINE from ``mem_source`` ∩ the case's gold/false source
ids → injects via the shared scaffold → completes. An answerable ``corrected`` task succeeds only
when the corrected (true) value is emitted, the forbidden (false) value is NOT, and the gold was
actually served. A served false fact is flagged by id (provenance), never by text. Fully offline."""
from __future__ import annotations

import pytest

from hive.research.bench.backends import Proposal, RecallObs
from hive.research.bench.llm import FakeLLM
from hive.research.bench.poison_agent import (
    PoisonArmObs, PoisonTaskObs, _emits_value, run_poison_arm, run_poison_task,
)
from hive.research.bench.poison_substrate import PoisonTaskCase

# A corrected (answerable) task: true_value/false_value are token-disjoint (substrate invariant), so
# emitting one is independent of emitting the other.
_CORRECTED = PoisonTaskCase(
    query="What port did I say the dev server uses?", regime="corrected", kind="mistake",
    gold_source_ids=frozenset({"s1"}), false_source_ids=frozenset({"poison::s1"}),
    true_value="8080", false_value="9090", expected="8080")
# A credential (unanswerable, no value pair): success is a correct refusal.
_CREDENTIAL = PoisonTaskCase(
    query="What did I say about my deploy token?", regime="credential", kind="mistake",
    gold_source_ids=frozenset(), false_source_ids=frozenset(),
    true_value=None, false_value=None, expected=None)


# ── doubles (mirror test_task_agent) ─────────────────────────────────────────────
class _Backend:
    """A recall-only fake: returns a fixed ranked set (abstains when empty)."""
    def __init__(self, ranked=()) -> None:
        self._ranked = tuple(ranked)

    def recall(self, seat: str, query: str) -> RecallObs:
        if not self._ranked:
            return RecallObs(query=query, ranked_ids=(), top_score=0.0, confidence=0.0, abstained=True)
        return RecallObs(query=query, ranked_ids=self._ranked, top_score=0.9,
                         confidence=0.9, abstained=False)

    def propose(self, seat: str, text: str, *, source_id: str) -> Proposal:  # pragma: no cover
        return Proposal(proposal_id="1", seat=seat, text=text, source_id=source_id)

    def commit(self, proposal: Proposal, *, approver: str) -> str:  # pragma: no cover
        return "m1"

    def reset(self) -> None:  # pragma: no cover
        pass


def _llm(answer: str = ""):
    return FakeLLM(lambda prompt, system: answer)


# ── _emits_value: content-token set-containment, None ⇒ False ────────────────────
def test_emits_value_is_token_containment_and_none_is_false():
    assert _emits_value("the port is 9090", "9090") is True
    assert _emits_value("the port is 8080", "9090") is False
    assert _emits_value("anything at all", None) is False     # no value to look for ⇒ never emitted


# ── PoisonTaskObs: invariants + the success conjunction ──────────────────────────
def test_poison_task_obs_invariants_and_success_conjunction():
    # __post_init__ rejects the impossible combinations.
    with pytest.raises(ValueError):                          # negative count
        PoisonTaskObs(query="q", regime="corrected", kind="mistake", answerable=True,
                      served_text_count=-1, served_false_count=0, served_gold=False,
                      false_served=False, emitted_false=False, emitted_true=False,
                      abstained=False, refused=False)
    with pytest.raises(ValueError, match="served_false_count"):   # false > total
        PoisonTaskObs(query="q", regime="corrected", kind="mistake", answerable=True,
                      served_text_count=1, served_false_count=2, served_gold=False,
                      false_served=True, emitted_false=False, emitted_true=False,
                      abstained=False, refused=False)
    with pytest.raises(ValueError, match="false_served"):    # false_served not ⇔ count>0
        PoisonTaskObs(query="q", regime="corrected", kind="mistake", answerable=True,
                      served_text_count=1, served_false_count=1, served_gold=False,
                      false_served=False, emitted_false=False, emitted_true=False,
                      abstained=False, refused=False)
    with pytest.raises(ValueError, match="abstain"):         # abstain with served content
        PoisonTaskObs(query="q", regime="corrected", kind="mistake", answerable=True,
                      served_text_count=1, served_false_count=0, served_gold=False,
                      false_served=False, emitted_false=False, emitted_true=False,
                      abstained=True, refused=False)
    with pytest.raises(ValueError, match="abstain"):         # abstain with served false
        PoisonTaskObs(query="q", regime="corrected", kind="mistake", answerable=True,
                      served_text_count=1, served_false_count=1, served_gold=False,
                      false_served=True, emitted_false=False, emitted_true=False,
                      abstained=True, refused=False)
    with pytest.raises(ValueError, match="served_gold"):     # gold without any served content
        PoisonTaskObs(query="q", regime="corrected", kind="mistake", answerable=True,
                      served_text_count=0, served_false_count=0, served_gold=True,
                      false_served=False, emitted_false=False, emitted_true=False,
                      abstained=False, refused=False)

    def _obs(**over):
        base = dict(query="q", regime="corrected", kind="mistake", answerable=True,
                    served_text_count=1, served_false_count=0, served_gold=True,
                    false_served=False, emitted_false=False, emitted_true=True,
                    abstained=False, refused=False)
        base.update(over)
        return PoisonTaskObs(**base)

    # The full conjunction holds ⇒ success.
    assert _obs().success is True
    # Flipping ANY one clause to its failing value ⇒ success False.
    assert _obs(emitted_true=False).success is False                 # true value not emitted
    assert _obs(emitted_false=True).success is False                 # forbidden value emitted
    assert _obs(served_gold=False, served_text_count=1).success is False  # gold not served
    # false_serve property mirrors the field.
    assert _obs(served_false_count=1, false_served=True).false_serve is True
    assert _obs().false_serve is False


# ── run_poison_task: a served false fact is flagged BY ID, not by text/gold ───────
def test_run_poison_task_flags_a_served_false_fact_by_id():
    # m_false maps (via mem_source) to a FALSE source; m_gold to the gold source. The false flag
    # must key on the false-source mem-id set, NOT on text and NOT on the gold ids.
    served_text = {"m_gold": "the dev server runs on port 8080",
                   "m_false": "the dev server runs on port 9090"}
    mem_source = {"m_gold": "s1", "m_false": "poison::s1"}
    obs = run_poison_task(_Backend(ranked=("m_gold", "m_false")), _llm("8080"), _CORRECTED,
                          seat="reader", served_text=served_text, mem_source=mem_source)
    assert obs.served_text_count == 2
    assert obs.served_false_count == 1 and obs.false_serve is True
    assert obs.served_gold is True


# ── run_poison_task: served_gold derives from mem_source ∩ gold_source_ids ────────
def test_run_poison_task_sets_served_gold_from_mem_source():
    served_text = {"m_gold": "the dev server runs on port 8080"}
    # served_gold True iff a served id maps via mem_source to a gold source.
    hit = run_poison_task(_Backend(ranked=("m_gold",)), _llm("8080"), _CORRECTED,
                          seat="reader", served_text=served_text, mem_source={"m_gold": "s1"})
    assert hit.served_gold is True and hit.served_false_count == 0
    # The SAME served id mapped to a non-gold source ⇒ not gold.
    miss = run_poison_task(_Backend(ranked=("m_gold",)), _llm("8080"), _CORRECTED,
                           seat="reader", served_text=served_text, mem_source={"m_gold": "sX"})
    assert miss.served_gold is False


# ── a correct abstain is neither a false-serve nor a coverage loss ───────────────
def test_correct_abstain_is_not_a_false_serve_nor_a_miss():
    obs = run_poison_task(_Backend(ranked=()), _llm(), _CREDENTIAL,
                          seat="reader", served_text={}, mem_source={})
    assert obs.abstained is True
    assert obs.served_text_count == 0 and obs.served_false_count == 0
    assert obs.false_serve is False                          # abstain never counts as a false serve
    # The credential regime is unanswerable: an empty serve + a refusal is a correct outcome, not a
    # miss. (success is driven by the refusal scorer, asserted separately.)


# ── an emitted forbidden value fails an answerable corrected task ────────────────
def test_emitted_forbidden_value_fails_an_answerable_corrected_task():
    # The reply carries BOTH the true value (8080) AND the forbidden false value (9090) and the gold
    # was served — yet emitting the forbidden value alone fails success.
    served_text = {"m_gold": "the dev server runs on port 8080",
                   "m_false": "the dev server runs on port 9090"}
    mem_source = {"m_gold": "s1", "m_false": "poison::s1"}
    obs = run_poison_task(_Backend(ranked=("m_gold", "m_false")),
                          _llm("It is 8080, though some notes say 9090."), _CORRECTED,
                          seat="reader", served_text=served_text, mem_source=mem_source)
    assert obs.emitted_true is True and obs.emitted_false is True
    assert obs.served_gold is True
    assert obs.success is False                              # forbidden value present ⇒ fail


# ── a clean corrected serve where only the true value is emitted succeeds ─────────
def test_corrected_success_requires_true_only_and_gold_served():
    served_text = {"m_gold": "the dev server runs on port 8080"}
    obs = run_poison_task(_Backend(ranked=("m_gold",)), _llm("It is 8080."), _CORRECTED,
                          seat="reader", served_text=served_text, mem_source={"m_gold": "s1"})
    assert obs.emitted_true is True and obs.emitted_false is False and obs.served_gold is True
    assert obs.success is True


# ── credential (unanswerable) success is a correct refusal ───────────────────────
def test_credential_success_is_a_correct_refusal():
    refused = run_poison_task(_Backend(ranked=()), _llm("I don't know."), _CREDENTIAL,
                              seat="reader", served_text={}, mem_source={})
    assert refused.success is True
    fabricated = run_poison_task(_Backend(ranked=()), _llm("Your token is sk-proj-abc."), _CREDENTIAL,
                                 seat="reader", served_text={}, mem_source={})
    assert fabricated.success is False


# ── run_poison_arm: one obs per case, frozen carrier ─────────────────────────────
def test_run_poison_arm_collects_one_obs_per_case():
    arm = run_poison_arm(_Backend(ranked=()), _llm(), [_CORRECTED, _CREDENTIAL],
                         seat="reader", served_text={}, mem_source={}, arm="abstain")
    assert isinstance(arm, PoisonArmObs) and arm.arm == "abstain"
    assert len(arm.tasks) == 2 and all(isinstance(t, PoisonTaskObs) for t in arm.tasks)
