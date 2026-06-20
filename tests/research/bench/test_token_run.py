"""C4: the three-arm token runner. ``score_token_arms`` reduces the per-arm TaskObs to per-arm +
per-regime token/success summaries and the three task-paired bootstrap deltas (C−B = the abstain
gate's pure contribution, B−A = store/geometry, C−A = total). ``build_token_report`` refuses to emit
without token totals, the success vectors, and complete provenance. ``main`` wires preflight →
substrate → preload-per-arm → agent loop → score → report behind injectable factories, so it runs
fully offline. No model, no network, no API."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hive.research.bench.backends import MemoryBackend, Proposal, RecallObs
from hive.research.bench.llm import FakeLLM
from hive.research.bench.run import EMBEDDER_MODEL
from hive.research.bench.task_agent import ArmTokenObs, TaskObs
from hive.research.bench.token_run import (
    ARM_MEM0, ARM_OFF, ARM_ON, build_token_report, main, score_token_arms,
)

_FIX = Path(__file__).parent / "fixtures" / "longmemeval_tiny.json"


# ── arm builders for score_token_arms ──────────────────────────────────────────
def _arm(name: str, specs) -> ArmTokenObs:
    """specs: list of (regime, input_tokens, answer_match, abstained_on_answerable). A served
    answerable task is taken to have served its gold (served_gold), so success == answer_match
    there; an answerable-abstain has served nothing (served_gold False) ⇒ a counted failure."""
    tasks = []
    for i, (r, tk, am, abst) in enumerate(specs):
        answerable = r != "no-relevant"
        served_ct = 0 if abst else 1
        served_gold = answerable and not abst
        tasks.append(TaskObs(query=f"q{i}", regime=r, answerable=answerable,
                             served_text_count=served_ct, input_tokens=tk, answer_match=am,
                             served_gold=served_gold, abstained_on_answerable=abst))
    tasks = tuple(tasks)
    usage = {"input_tokens": sum(t.input_tokens for t in tasks), "output_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
             "total_cost_usd": 0.0, "n_served": len(tasks)}
    return ArmTokenObs(arm=name, tasks=tasks, usage_total=usage)


def test_score_token_arms_computes_three_paired_deltas():
    # C serves far fewer tokens than B == A on every task; success equal everywhere
    A = _arm(ARM_MEM0, [("no-relevant", 100, True, False)] * 5)
    B = _arm(ARM_OFF, [("no-relevant", 100, True, False)] * 5)
    C = _arm(ARM_ON, [("no-relevant", 10, True, False)] * 5)
    sc = score_token_arms({ARM_MEM0: A, ARM_OFF: B, ARM_ON: C}, seed=0)
    assert set(sc["deltas"]) == {"C-B", "B-A", "C-A"}
    cb = sc["deltas"]["C-B"]["tokens"]
    assert cb["point"] == pytest.approx(-90.0) and cb["hi"] < 0.0 and cb["saves"] is True
    assert sc["deltas"]["B-A"]["tokens"]["point"] == pytest.approx(0.0)   # B and A identical


def test_token_delta_does_not_claim_savings_when_tokens_tie():
    A = _arm(ARM_MEM0, [("single-answer", 50, True, False)] * 4)
    sc = score_token_arms({ARM_MEM0: A, ARM_OFF: A, ARM_ON: A}, seed=0)
    assert sc["deltas"]["C-B"]["tokens"]["saves"] is False               # a tie is not a saving


def test_token_delta_flags_costs_more_when_an_arm_spends_strictly_more():
    A = _arm(ARM_MEM0, [("no-relevant", 10, True, False)] * 5)
    B = _arm(ARM_OFF, [("no-relevant", 10, True, False)] * 5)
    C = _arm(ARM_ON, [("no-relevant", 100, True, False)] * 5)            # C spends MORE than B
    sc = score_token_arms({ARM_MEM0: A, ARM_OFF: B, ARM_ON: C}, seed=0)
    cb = sc["deltas"]["C-B"]["tokens"]
    assert cb["costs_more"] is True and cb["saves"] is False             # R1/R3: the symmetric verdict


def test_success_delta_flags_an_improvement():
    A = _arm(ARM_MEM0, [("single-answer", 50, True, False)] * 5)
    B = _arm(ARM_OFF, [("single-answer", 50, False, False)] * 5)         # B never answers correctly
    C = _arm(ARM_ON, [("single-answer", 50, True, False)] * 5)           # C always does
    sc = score_token_arms({ARM_MEM0: A, ARM_OFF: B, ARM_ON: C}, seed=0)
    cb = sc["deltas"]["C-B"]["success"]
    assert cb["improves"] is True and cb["regresses"] is False


def test_success_delta_flags_a_regression():
    # C abstains on answerable queries (success 0) while B serves them (success 1) ⇒ C regresses
    B = _arm(ARM_OFF, [("single-answer", 100, True, False)] * 5)
    C = _arm(ARM_ON, [("single-answer", 0, True, True)] * 5)             # answerable-abstain ⇒ success 0
    A = _arm(ARM_MEM0, [("single-answer", 100, True, False)] * 5)
    sc = score_token_arms({ARM_MEM0: A, ARM_OFF: B, ARM_ON: C}, seed=0)
    assert sc["deltas"]["C-B"]["success"]["regresses"] is True           # fewer tokens, but WORSE


def test_per_arm_summary_breaks_tokens_and_success_down_by_regime():
    A = _arm(ARM_MEM0, [("single-answer", 50, True, False), ("no-relevant", 30, True, False),
                        ("broad-relevant", 40, False, False)])
    sc = score_token_arms({ARM_MEM0: A, ARM_OFF: A, ARM_ON: A}, seed=0)
    sm = sc["arms"][ARM_MEM0]
    assert set(sm["by_regime"]) == {"single-answer", "broad-relevant", "no-relevant"}
    assert sm["by_regime"]["single-answer"]["input_tokens"] == 50
    assert sm["by_regime"]["broad-relevant"]["success_rate"] == pytest.approx(0.0)
    assert sm["input_tokens_total"] == 120 and sm["success_rate"] == pytest.approx(2 / 3)
    assert sm["served_text_count"] == 3 and sm["by_regime"]["single-answer"]["served_text_count"] == 1
    assert sm["served_gold"] == 2 and sm["by_regime"]["single-answer"]["served_gold"] == 1


def test_per_regime_abstained_on_answerable_is_tallied():
    # an answerable-abstain in the broad-relevant regime must be machine-readable per-regime (AC10)
    A = _arm(ARM_MEM0, [("single-answer", 10, True, False), ("broad-relevant", 0, False, True)])
    sc = score_token_arms({ARM_MEM0: A, ARM_OFF: A, ARM_ON: A}, seed=0)
    sm = sc["arms"][ARM_MEM0]
    assert sm["by_regime"]["broad-relevant"]["abstained_on_answerable"] == 1
    assert sm["abstained_on_answerable"] == 1


def test_combined_digest_changes_when_any_arm_differs():
    from hive.research.bench.llm import FakeLLM
    from hive.research.bench.token_run import _combined_digest

    class _D(FakeLLM):
        def __init__(self, d): super().__init__(); self._d = d
        def digest(self): return self._d
    base = {ARM_MEM0: _D("x"), ARM_OFF: _D("y"), ARM_ON: _D("z")}
    same = {ARM_MEM0: _D("x"), ARM_OFF: _D("y"), ARM_ON: _D("z")}
    one_diff = {ARM_MEM0: _D("x"), ARM_OFF: _D("CHANGED"), ARM_ON: _D("z")}
    assert _combined_digest(base) == _combined_digest(same)          # stable when identical
    assert _combined_digest(base) != _combined_digest(one_diff)      # a tamper in ANY arm shows


def test_score_token_arms_requires_aligned_arms():
    A = _arm(ARM_MEM0, [("single-answer", 1, True, False), ("no-relevant", 1, True, False)])
    mis = _arm(ARM_ON, [("no-relevant", 1, True, False), ("single-answer", 1, True, False)])  # reordered
    with pytest.raises(ValueError, match="align"):
        score_token_arms({ARM_MEM0: A, ARM_OFF: A, ARM_ON: mis}, seed=0)


# ── build_token_report: refuse on incomplete inputs ────────────────────────────
def _complete_provenance() -> dict:
    return {"token_totals": {ARM_MEM0: 1}, "h_frac_on": 0.5, "h_frac_off": 1.0,
            "dataset_hash": "abc", "embedder_model": EMBEDDER_MODEL,
            "extractor": "substrate-raw-turns", "llm_digest": "d", "seeds": [0],
            "regime_mix": {"single-answer": 1}}


def test_build_token_report_refuses_incomplete_provenance():
    prov = _complete_provenance(); del prov["token_totals"]
    with pytest.raises(ValueError, match="provenance"):
        build_token_report(arms={ARM_MEM0: {"success_rate": 0.5}}, deltas={}, provenance=prov)


def test_build_token_report_refuses_without_success_vectors():
    with pytest.raises(ValueError, match="success"):
        build_token_report(arms={ARM_MEM0: {"input_tokens_total": 1}}, deltas={},
                           provenance=_complete_provenance())


def test_build_token_report_refuses_a_degenerate_empty_arm():
    # _arm_summary emits success_rate=None for a zero-task arm; that must not slip past the guard
    with pytest.raises(ValueError, match="success"):
        build_token_report(arms={ARM_MEM0: {"success_rate": None, "input_tokens_total": 0}},
                           deltas={}, provenance=_complete_provenance())


def test_build_token_report_emits_with_complete_inputs():
    rep = build_token_report(
        arms={ARM_MEM0: {"success_rate": 0.5, "input_tokens_total": 1}},
        deltas={"C-B": {}}, provenance=_complete_provenance())
    assert rep["provenance"]["h_frac_off"] == 1.0 and rep["arms"] and "deltas" in rep


# ── main offline (fake backends + FakeLLM) ─────────────────────────────────────
class _FakeBackend:
    """A token-overlap recall store driven by the real propose→commit preload seam."""
    def __init__(self) -> None:
        self._store: dict[str, str] = {}; self._n = 0

    def recall(self, seat: str, query: str) -> RecallObs:
        q = set(query.lower().split())
        hits = [mid for mid, txt in self._store.items() if q & set(txt.lower().split())]
        if not hits:
            return RecallObs(query=query, ranked_ids=(), top_score=0.0, confidence=0.0, abstained=True)
        return RecallObs(query=query, ranked_ids=tuple(hits), top_score=0.5,
                         confidence=0.5, abstained=False)

    def propose(self, seat: str, text: str, *, source_id: str) -> Proposal:
        self._n += 1
        return Proposal(proposal_id=str(self._n), seat=seat, text=text, source_id=source_id)

    def commit(self, proposal: Proposal, *, approver: str) -> str:
        mid = f"m{proposal.proposal_id}"; self._store[mid] = proposal.text; return mid

    def reset(self) -> None:
        self._store.clear(); self._n = 0


def test_fake_backend_conforms():
    assert isinstance(_FakeBackend(), MemoryBackend)


def test_main_offline_writes_a_provenance_stamped_token_report(tmp_path):
    out = tmp_path / "tok.json"
    rc = main(["--dataset", str(_FIX), "--out", str(out)],
              backend_factory=lambda arm: _FakeBackend(),
              llm_factory=lambda arm: FakeLLM(usage={"input_tokens": 7}))
    assert rc == 0 and out.exists()
    rep = json.loads(out.read_text())
    assert set(rep["arms"]) == {ARM_MEM0, ARM_OFF, ARM_ON}
    assert set(rep["deltas"]) == {"C-B", "B-A", "C-A"}
    prov = rep["provenance"]
    assert prov["h_frac_on"] == 0.5 and prov["h_frac_off"] == 1.0
    assert prov["token_totals"] and prov["regime_mix"] and prov["dataset_hash"]
    assert prov["embedder_model"] == EMBEDDER_MODEL
    # every arm carries both axes (Pareto): tokens AND success, per regime
    for arm in rep["arms"].values():
        assert "input_tokens_total" in arm and "success_rate" in arm and "by_regime" in arm
