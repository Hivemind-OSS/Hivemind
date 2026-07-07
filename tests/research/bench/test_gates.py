"""B5b: the three orchestrator gate policies that bracket curation value.
  AllowAllGate         — the no-curation floor (approve everything).
  OracleEvidenceGate   — the perfect-curation ceiling (approve iff the fact derives from a gold
                         evidence session); a LABELLED control, never a product number.
  LLMOrchestratorGate  — the realistic arm: a Claude-subscription LLM judges KEEP/DROP, fail-safe
                         to DROP on an ambiguous reply (don't poison shared memory), every decision
                         logged for replay.
All three satisfy GatePolicy; only the LLM gate makes an LLM call, through the single seam."""
from __future__ import annotations

from hive.research.bench.backends import Proposal, RecallObs
from hive.research.bench.llm import FakeLLM
from hive.research.bench.orchestrator import (
    AllowAllGate, GateContext, GatePolicy, LLMOrchestratorGate, OracleEvidenceGate,
)


def _ctx(*, abstained: bool = True, seat: str = "sub-a") -> GateContext:
    prior = RecallObs(query="q", ranked_ids=(), top_score=0.0, confidence=0.0, abstained=True) \
        if abstained else \
        RecallObs(query="q", ranked_ids=("m1",), top_score=0.9, confidence=0.9, abstained=False)
    return GateContext(prior=prior, seat=seat)


def _prop(text="staging uses port 8080", source_id="s1") -> Proposal:
    return Proposal(proposal_id="p1", seat="sub-a", text=text, source_id=source_id)


# ── conformance ────────────────────────────────────────────────────────────────
def test_all_three_satisfy_gatepolicy():
    assert isinstance(AllowAllGate(), GatePolicy)
    assert isinstance(OracleEvidenceGate({"s1"}), GatePolicy)
    assert isinstance(LLMOrchestratorGate(FakeLLM()), GatePolicy)


# ── AllowAll floor ──────────────────────────────────────────────────────────────
def test_allow_all_approves_everything():
    g = AllowAllGate()
    assert g.decide(_prop(), _ctx()) is True
    assert g.decide(_prop(source_id="not-evidence"), _ctx(abstained=False)) is True


# ── Oracle ceiling (labelled control) ──────────────────────────────────────────
def test_oracle_approves_only_gold_evidence_sources():
    g = OracleEvidenceGate({"s1", "s3"})
    assert g.decide(_prop(source_id="s1"), _ctx()) is True
    assert g.decide(_prop(source_id="s3"), _ctx()) is True
    assert g.decide(_prop(source_id="s2"), _ctx()) is False     # not a gold evidence session


def test_oracle_empty_evidence_rejects_all():
    g = OracleEvidenceGate(set())                                # an abstention question: no gold
    assert g.decide(_prop(source_id="s1"), _ctx()) is False


# ── LLM orchestrator (realistic arm) ───────────────────────────────────────────
def test_llm_gate_keeps_on_keep_and_drops_on_drop():
    assert LLMOrchestratorGate(FakeLLM(lambda p, s: "KEEP")).decide(_prop(), _ctx()) is True
    assert LLMOrchestratorGate(FakeLLM(lambda p, s: "DROP")).decide(_prop(), _ctx()) is False


def test_llm_gate_parses_word_in_a_sentence():
    g = LLMOrchestratorGate(FakeLLM(lambda p, s: "I would KEEP this — it's durable."))
    assert g.decide(_prop(), _ctx()) is True


def test_llm_gate_is_conservative_on_ambiguity():
    # neither token, or BOTH ⇒ DROP (never pollute shared memory on an unclear verdict)
    assert LLMOrchestratorGate(FakeLLM(lambda p, s: "maybe?")).decide(_prop(), _ctx()) is False
    assert LLMOrchestratorGate(FakeLLM(lambda p, s: "KEEP or DROP? DROP.")).decide(_prop(), _ctx()) is False


def test_llm_gate_sends_the_fact_text_and_logs_each_decision():
    llm = FakeLLM(lambda p, s: "DROP")
    log: list[dict] = []
    g = LLMOrchestratorGate(llm, log=log)
    g.decide(_prop(text="the cache TTL is 300 seconds"), _ctx())
    assert "the cache TTL is 300 seconds" in llm.calls[0][0]     # fact reached the prompt
    assert llm.calls[0][1]                                       # a system prompt was supplied
    assert len(log) == 1 and log[0]["decision"] is False
    assert log[0]["response"] == "DROP" and log[0]["proposal_id"] == "p1"


def test_llm_gate_prior_redundancy_signal_reaches_the_prompt():
    # the gate tells the LLM whether the store already holds something similar (drop-redundant cue)
    seen_prompts = []
    g = LLMOrchestratorGate(FakeLLM(lambda p, s: seen_prompts.append(p) or "KEEP"))
    g.decide(_prop(), _ctx(abstained=False))                    # prior is a confident hit
    # "related" appears ONLY in the redundancy wording — the abstain wording is "nothing similar",
    # so requiring "related" (not "similar") actually discriminates the two prior states.
    assert "related" in seen_prompts[0].lower()
