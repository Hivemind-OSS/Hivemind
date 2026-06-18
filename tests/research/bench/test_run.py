"""B6a: evaluate_arm — the per-case eval loop (reset → extract → ingest → query → collect). The
load-bearing fidelity rule: an ANSWERABLE question whose evidence was never committed (the gate or
extractor dropped it) has empty gold, and that is scored as a MISS, never silently skipped the way
a genuinely unanswerable question is. Tested over the tiny fixture with a verbatim-extractor FakeLLM
and an in-memory backend — no model, no network."""
from __future__ import annotations

from pathlib import Path

from hive.research.bench.backends import Proposal, RecallObs
from hive.research.bench.dataset import load_longmemeval
from hive.research.bench.llm import FakeLLM
from hive.research.bench.orchestrator import AllowAllGate, OracleEvidenceGate
from hive.research.bench.run import ArmObservations, evaluate_arm

_FIX = Path(__file__).parent / "fixtures" / "longmemeval_tiny.json"


class _FakeBackend:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}; self._n = 0; self.resets = 0

    def recall(self, seat: str, query: str) -> RecallObs:
        q = set(query.lower().split())
        hits = [mid for mid, txt in self._store.items() if q & set(txt.lower().split())]
        if not hits:
            return RecallObs(query=query, ranked_ids=(), top_score=0.0, confidence=0.0, abstained=True)
        return RecallObs(query=query, ranked_ids=tuple(hits), top_score=0.5, confidence=0.5, abstained=False)

    def propose(self, seat: str, text: str, *, source_id: str) -> Proposal:
        self._n += 1
        return Proposal(proposal_id=str(self._n), seat=seat, text=text, source_id=source_id)

    def commit(self, proposal: Proposal, *, approver: str) -> str:
        mid = f"m{proposal.proposal_id}"; self._store[mid] = proposal.text; return mid

    def reset(self) -> None:
        self._store.clear(); self._n = 0; self.resets += 1


def _verbatim_extractor(prompt: str, system) -> str:
    """Return each user turn verbatim as a fact (the transcript is embedded in the prompt)."""
    return "\n".join(line[len("user: "):] for line in prompt.splitlines() if line.startswith("user: "))


def _cases():
    return load_longmemeval(str(_FIX))


def test_evaluate_arm_shapes_retrieval_and_abstention():
    arm = evaluate_arm(_FakeBackend(), OracleEvidenceGate({"s1", "s3", "s4"}), _cases(),
                       llm=FakeLLM(_verbatim_extractor), seats=["sub-a", "sub-b"])
    assert isinstance(arm, ArmObservations)
    # q1, q2 are answerable ⇒ retrieval; q3_abs is unanswerable ⇒ excluded from retrieval
    assert len(arm.retrieval) == 2
    # abstention covers EVERY question, with exactly one unanswerable
    assert len(arm.abstention) == 3
    assert sum(1 for _obs, un in arm.abstention if un) == 1


def test_answerable_question_retrieves_its_evidence_fact():
    arm = evaluate_arm(_FakeBackend(), OracleEvidenceGate({"s1", "s3", "s4"}), _cases(),
                       llm=FakeLLM(_verbatim_extractor), seats=["sub-a"])
    # the q1 obs (its query mentions the dev server port) hits the committed s1 fact
    q1 = next(obs for obs, gold in arm.retrieval if "dev server" in obs.query)
    hit = next((obs, gold) for obs, gold in arm.retrieval if obs is q1)
    assert hit[0].abstained is False and hit[1] and hit[1] & set(hit[0].ranked_ids)


def test_answerable_with_lost_evidence_is_a_counted_miss_not_skipped():
    # an extractor that yields NOTHING ⇒ no fact committed ⇒ empty gold for every answerable q.
    arm = evaluate_arm(_FakeBackend(), AllowAllGate(), _cases(),
                       llm=FakeLLM(lambda p, s: ""), seats=["sub-a"])
    assert len(arm.retrieval) == 2                       # the answerable questions are STILL scored
    for obs, gold in arm.retrieval:
        assert gold and all(g.startswith("__unretrievable__") for g in gold)   # sentinel ⇒ guaranteed miss
        assert not (gold & set(obs.ranked_ids))          # the sentinel can never be retrieved


def test_unanswerable_excluded_from_retrieval_but_present_in_abstention():
    arm = evaluate_arm(_FakeBackend(), AllowAllGate(), _cases(),
                       llm=FakeLLM(_verbatim_extractor), seats=["sub-a"])
    assert all("dragon" not in obs.query for obs, _ in arm.retrieval)           # q3_abs not scored for retrieval
    assert any(un for _obs, un in arm.abstention)                              # but it IS in abstention


def test_store_is_reset_per_case():
    b = _FakeBackend()
    evaluate_arm(b, AllowAllGate(), _cases(), llm=FakeLLM(_verbatim_extractor), seats=["sub-a"])
    assert b.resets == 3                                  # one fresh store per question
