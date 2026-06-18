"""B6a: evaluate_arm — the per-case eval loop (reset → extract → ingest → query → collect). The
load-bearing fidelity rule: an ANSWERABLE question whose evidence was never committed (the gate or
extractor dropped it) has empty gold, and that is scored as a MISS, never silently skipped the way
a genuinely unanswerable question is. Tested over the tiny fixture with a verbatim-extractor FakeLLM
and an in-memory backend — no model, no network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hive.research.bench.backends import Proposal, RecallObs
from hive.research.bench.dataset import load_longmemeval
from hive.research.bench.llm import FakeLLM
from hive.research.bench.orchestrator import AllowAllGate, OracleEvidenceGate
from hive.research.bench.run import (
    ArmObservations, VerbatimLLM, _parse_args, build_report, compare_arms, evaluate_arm, main,
    preflight, score_arm,
)

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


# ── B6b: VerbatimLLM, preflight, score_arm, compare_arms, build_report, main ────
def test_verbatim_llm_returns_user_turns_as_facts():
    out = VerbatimLLM().complete("Conversation:\nuser: we use redis\nassistant: ok\nuser: port 6379\n")
    assert out == "we use redis\nport 6379"


class _StubLLM:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok; self.preflighted = False

    def preflight(self) -> None:
        self.preflighted = True
        if not self.ok:
            raise RuntimeError("not logged in")

    def complete(self, prompt: str, *, system=None) -> str:
        return ""


def test_preflight_missing_dataset_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="HIVE_BENCH_LME_PATH"):
        preflight(dataset_path=str(tmp_path / "nope.json"), llm=_StubLLM())


def test_preflight_runs_the_llm_preflight():
    s = _StubLLM()
    preflight(dataset_path=str(_FIX), llm=s)
    assert s.preflighted is True


def test_preflight_propagates_unauthenticated_llm():
    with pytest.raises(RuntimeError, match="not logged in"):
        preflight(dataset_path=str(_FIX), llm=_StubLLM(ok=False))


def test_score_arm_returns_means_and_per_question_vectors():
    sc = score_arm(_FakeBackend(), OracleEvidenceGate({"s1", "s3", "s4"}), _cases(),
                   llm=FakeLLM(_verbatim_extractor), seats=["sub-a"], ks=(5, 10))
    assert 0.0 <= sc["retrieval"]["hit_at_k"]["5"] <= 1.0 and sc["retrieval"]["n"] == 2
    assert len(sc["hit_at_k_per_q"]["5"]) == 2            # one entry per answerable question
    assert sc["abstention_auroc"] is None or 0.0 <= sc["abstention_auroc"] <= 1.0


def test_compare_arms_flags_ship_when_ci_excludes_zero():
    # primary strictly beats baseline on every question ⇒ paired CI lo > 0 ⇒ ships
    primary = {"hit_at_k_per_q": {"5": (1.0, 1.0, 1.0, 1.0)}, "mrr_per_q": (1.0, 1.0, 1.0, 1.0)}
    baseline = {"hit_at_k_per_q": {"5": (0.0, 0.0, 0.0, 0.0)}, "mrr_per_q": (0.0, 0.0, 0.0, 0.0)}
    cmps = {c["metric"]: c for c in compare_arms(primary, baseline, ks=(5,), seed=0)}
    assert cmps["hit_at_5"]["ships"] is True and cmps["hit_at_5"]["lo"] > 0.0


def test_compare_arms_no_ship_on_a_tie():
    flat = {"hit_at_k_per_q": {"5": (1.0, 0.0, 1.0, 0.0)}, "mrr_per_q": (0.5, 0.5, 0.5, 0.5)}
    cmps = {c["metric"]: c for c in compare_arms(flat, flat, ks=(5,), seed=0)}
    assert cmps["hit_at_5"]["ships"] is False            # identical arms ⇒ delta 0 ⇒ no ship


def test_build_report_refuses_incomplete_provenance():
    prov = {"dataset_hash": "abc", "n_cases": 3, "seeds": [0], "embedder_model": "bge",
            "extractor": "verbatim", "llm_digest": "n/a", "ks": [5, 10]}
    del prov["dataset_hash"]
    with pytest.raises(ValueError, match="provenance"):
        build_report(arms={}, comparisons=[], provenance=prov)


def test_main_offline_writes_a_provenance_stamped_report(tmp_path):
    out = tmp_path / "report.json"
    rc = main(["--backend", "x", "--gate", "allowall",
               "--baseline-backend", "y", "--baseline-gate", "oracle",
               "--extractor", "verbatim", "--dataset", str(_FIX), "--out", str(out)],
              backend_factory=lambda name: _FakeBackend(),
              llm_factory=lambda extractor: FakeLLM(_verbatim_extractor))
    assert rc == 0 and out.exists()
    rep = json.loads(out.read_text())
    assert set(rep) >= {"provenance", "arms", "comparisons"}
    assert rep["provenance"]["n_cases"] == 3 and rep["provenance"]["dataset_hash"]
    assert rep["provenance"]["recall_hfrac"] == 0.9          # the gate threshold is stamped
    assert len(rep["arms"]) == 2 and rep["comparisons"]


def test_parse_args_recall_hfrac_default_and_override():
    assert _parse_args(["--dataset", "d", "--out", "o"]).recall_hfrac == 0.9
    assert _parse_args(["--dataset", "d", "--out", "o", "--recall-hfrac", "0.95"]).recall_hfrac == 0.95
