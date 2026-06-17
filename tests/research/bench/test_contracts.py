"""B-scaffold: the bench contracts import, enforce their frozen invariants, and are
satisfiable. The runtime→research purity fence (``tests/test_purity.py::
test_research_not_imported_by_runtime``) already covers ``hive.research.bench`` — any
``hive.research`` import in a runtime module fails it — so no extra fence is needed here."""
from __future__ import annotations

import pytest

from hive.research.bench.backends import MemoryBackend, Proposal, RecallObs
from hive.research.bench.orchestrator import GateContext, GatePolicy


# ── RecallObs frozen invariants (can't-construct a lying observation) ──────────
def test_recall_obs_rejects_out_of_range_confidence():
    with pytest.raises(ValueError):
        RecallObs(query="q", ranked_ids=(), top_score=0.0, confidence=1.5, abstained=True)


def test_recall_obs_abstained_cannot_carry_hits():
    with pytest.raises(ValueError):
        RecallObs(query="q", ranked_ids=("1",), top_score=0.9, confidence=0.9, abstained=True)


def test_recall_obs_confident_hit_is_constructable():
    obs = RecallObs(query="q", ranked_ids=("7", "3"), top_score=0.8, confidence=0.8,
                    abstained=False)
    assert obs.ranked_ids[0] == "7" and obs.abstained is False


# ── a minimal conformer proves the Protocol is satisfiable + seeds B5 tests ────
class _FakeBackend:
    def __init__(self) -> None:
        self._served: dict[str, str] = {}
        self._proposed: dict[str, Proposal] = {}
        self._n = 0

    def recall(self, seat: str, query: str) -> RecallObs:
        ids = tuple(mid for mid, txt in self._served.items() if query in txt)
        return RecallObs(query=query, ranked_ids=ids, top_score=1.0 if ids else 0.0,
                         confidence=1.0 if ids else 0.0, abstained=not ids)

    def propose(self, seat: str, text: str, *, source_id: str) -> Proposal:
        self._n += 1
        p = Proposal(proposal_id=f"p{self._n}", seat=seat, text=text, source_id=source_id)
        self._proposed[p.proposal_id] = p
        return p

    def commit(self, proposal: Proposal, *, approver: str) -> str:
        mid = f"m{proposal.proposal_id}"
        self._served[mid] = proposal.text
        return mid

    def reset(self) -> None:
        self._served.clear()
        self._proposed.clear()
        self._n = 0


def test_fake_backend_satisfies_protocol():
    assert isinstance(_FakeBackend(), MemoryBackend)


def test_propose_is_not_served_until_committed():
    b = _FakeBackend()
    assert b.recall("s1", "alpha").abstained is True
    p = b.propose("s1", "alpha fact", source_id="sess-1")
    assert b.recall("s1", "alpha").abstained is True          # deny path: not yet approved
    mid = b.commit(p, approver="orchestrator")
    obs = b.recall("s1", "alpha")
    assert obs.abstained is False and mid in obs.ranked_ids   # approved ⇒ served
    b.reset()
    assert b.recall("s1", "alpha").abstained is True


# ── a minimal gate proves the GatePolicy contract is satisfiable ───────────────
class _AllowAll:
    def decide(self, proposal: Proposal, ctx: GateContext) -> bool:
        return True


def test_fake_gate_satisfies_protocol():
    assert isinstance(_AllowAll(), GatePolicy)
    obs = RecallObs(query="q", ranked_ids=(), top_score=0.0, confidence=0.0, abstained=True)
    decision = _AllowAll().decide(
        Proposal("p1", "s1", "t", "src"), GateContext(prior=obs, seat="s1"))
    assert decision is True
