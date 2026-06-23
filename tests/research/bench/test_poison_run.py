"""C5(poison) — the four-arm poisoned-shared-store runner. ``score_poison_arms`` reduces the per-arm
PoisonTaskObs to per-arm/per-regime false-serve + success summaries, the per-served-false anti-confound
(full + matched-budget), and the three task-paired bootstrap deltas (each hivemind arm vs mem0) on BOTH
axes plus a ``clean_win`` conjunction. The two DISCLOSED capability lines — the anti-gaming
``poison_promotion`` probe and ``secret_floor_observed`` — are TOP-LEVEL provenance, never folded into
the headline FSR. ``build_poison_report`` refuses on incomplete provenance. ``main`` wires preflight →
substrate → preload-per-arm → plant → (SUP) supersede → agent loop → score → report behind injectable
factories, so it runs fully offline. No model, no network, no API.

The honesty guards are pinned RULE-2-first: per-served normalization (a wins-by-volume confound), the
deterministic matched-serve truncation, the per-axis delta directions + the ``clean_win`` conjunction,
the anti-gaming line NOT in the FSR, the empty-corrected-slice domain guard, and the provenance refusal.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hive.research.bench.backends import MemoryBackend, Proposal, RecallObs
from hive.research.bench.llm import FakeLLM
from hive.research.bench.poison_agent import PoisonArmObs, PoisonTaskObs
from hive.research.bench.run import EMBEDDER_MODEL
from hive.research.bench.poison_run import (
    ARM_MEM0, ARM_OFF, ARM_ON, ARM_SUP, build_poison_report, main,
    matched_serve_point, score_poison_arms,
)

_FIX = Path(__file__).parent / "fixtures" / "longmemeval_tiny.json"


# ── arm builders for score_poison_arms ──────────────────────────────────────────
def _task(query: str, *, served: int = 1, served_false: int = 0, served_gold: bool = True,
          emitted_true: bool = True, emitted_false: bool = False,
          regime: str = "corrected") -> PoisonTaskObs:
    """A corrected-answerable task by default. ``served_false`` ≤ ``served``; ``false_served`` and
    ``success`` follow from the carrier's own invariants/derivation."""
    abstained = served == 0
    return PoisonTaskObs(
        query=query, regime=regime, kind="mistake", answerable=(regime == "corrected"),
        served_text_count=served, served_false_count=served_false,
        served_gold=served_gold and served > 0, false_served=(served_false > 0),
        emitted_false=emitted_false, emitted_true=emitted_true,
        abstained=abstained, refused=False)


def _arm(name: str, tasks) -> PoisonArmObs:
    return PoisonArmObs(arm=name, tasks=tuple(tasks))


def _four_arms(mem0_tasks, off_tasks, on_tasks, sup_tasks) -> dict[str, PoisonArmObs]:
    return {ARM_MEM0: _arm(ARM_MEM0, mem0_tasks), ARM_OFF: _arm(ARM_OFF, off_tasks),
            ARM_ON: _arm(ARM_ON, on_tasks), ARM_SUP: _arm(ARM_SUP, sup_tasks)}


# ── per-served-item normalization: the wins-by-volume anti-confound ─────────────
def test_score_poison_arms_normalizes_per_served_item():
    # mem0 serves 2 items/task with 1 false; a hivemind arm serves 1 item/task with 1 false. Both
    # have the SAME absolute false count per task, but mem0 serves DOUBLE the volume ⇒ a LOWER
    # per-served-false rate would wrongly flatter mem0 by total volume. Normalizing by served VOLUME
    # makes mem0's rate (0.5) STRICTLY LOWER than the hivemind arm's (1.0) — the honest direction is
    # "fewer false PER SERVED ITEM", and the lean arm carries more false density per item it serves.
    mem0 = [_task(f"q{i}", served=2, served_false=1) for i in range(4)]
    off = [_task(f"q{i}", served=1, served_false=1) for i in range(4)]
    on = list(off)
    sup = list(off)
    sc = score_poison_arms(_four_arms(mem0, off, on, sup), seed=0)
    assert sc["arms"][ARM_MEM0]["per_served_false_rate_full"] == pytest.approx(0.5)   # 4 / 8
    assert sc["arms"][ARM_OFF]["per_served_false_rate_full"] == pytest.approx(1.0)    # 4 / 4
    # the normalization is by served VOLUME, not by task count (which would give 1.0 for both).
    assert (sc["arms"][ARM_MEM0]["per_served_false_rate_full"]
            < sc["arms"][ARM_OFF]["per_served_false_rate_full"])


# ── deterministic matched-serve truncation ──────────────────────────────────────
def test_matched_serve_point_truncation_is_deterministic():
    # arms serve different volumes; the matched point is the leanest arm's total, and the matched
    # per-served-false rate must be byte-stable across two same-seed runs and held to that budget.
    mem0 = [_task(f"q{i}", served=3, served_false=1) for i in range(3)]   # 9 served, 3 false
    off = [_task(f"q{i}", served=1, served_false=1) for i in range(3)]    # 3 served, 3 false
    on = list(off)
    sup = list(off)
    arms = _four_arms(mem0, off, on, sup)
    assert matched_serve_point(arms) == 3                                 # the leanest total
    sc1 = score_poison_arms(arms, seed=0)
    sc2 = score_poison_arms(arms, seed=0)
    assert sc1["matched_serve_point"] == 3
    # byte-stable across reruns AND truncated to the matched budget (3 served items of mem0 carry 1
    # false ⇒ 1/3, NOT the full 3/9 which happens to equal 1/3 here; the matched rate is stable).
    assert json.dumps(sc1["arms"], sort_keys=True) == json.dumps(sc2["arms"], sort_keys=True)
    assert sc1["arms"][ARM_MEM0]["per_served_false_rate_matched"] == pytest.approx(1 / 3)


# ── paired delta on BOTH axes + the clean_win conjunction ───────────────────────
def test_score_poison_arms_paired_delta_on_false_serve_and_success():
    # a hivemind arm with FEWER false serves but WORSE success vs mem0: false_serve improves, success
    # regresses, and clean_win is False (the conjunction refuses a win that regresses success).
    mem0 = [_task(f"q{i}", served=1, served_false=1, emitted_true=True, emitted_false=True)
            for i in range(6)]                       # mem0: always false-serves; success 0 (emits false)
    # the OFF/ON/SUP arms: fewer false serves (0) but worse success (emitted_true False ⇒ success 0
    # too) — actually make mem0 succeed and the hivemind arm fail, to force a success REGRESSION while
    # false_serve still improves.
    mem0 = [_task(f"q{i}", served=1, served_false=1, emitted_true=True, emitted_false=False,
                  served_gold=True) for i in range(6)]   # mem0: false-serves BUT succeeds (true emitted)
    worse = [_task(f"q{i}", served=1, served_false=0, emitted_true=False, emitted_false=False,
                   served_gold=True) for i in range(6)]  # hivemind: no false serve, but NO true ⇒ fail
    sc = score_poison_arms(_four_arms(mem0, worse, worse, worse), seed=0)
    d = sc["deltas"]["SUP-mem0"]
    assert d["false_serve"]["improves"] is True       # fewer false serves (hi < 0)
    assert d["success"]["regresses"] is True          # worse success (hi < 0)
    assert d["clean_win"] is False                    # the conjunction refuses a success-regressing win


def test_score_poison_arms_clean_win_when_fewer_false_and_no_regression():
    # a hivemind arm with fewer false serves AND no success regression ⇒ clean_win True.
    mem0 = [_task(f"q{i}", served=1, served_false=1, emitted_true=True, emitted_false=False,
                  served_gold=True) for i in range(6)]   # false-serves, succeeds
    better = [_task(f"q{i}", served=1, served_false=0, emitted_true=True, emitted_false=False,
                    served_gold=True) for i in range(6)]  # no false serve, still succeeds
    sc = score_poison_arms(_four_arms(mem0, better, better, better), seed=0)
    d = sc["deltas"]["SUP-mem0"]
    assert d["false_serve"]["improves"] is True and d["success"]["regresses"] is False
    assert d["clean_win"] is True


# ── the anti-gaming line is a DISCLOSED top-level field, NEVER in the FSR ────────
def test_poison_promotion_block_is_a_disclosed_field_not_in_fsr(tmp_path):
    out = tmp_path / "poison.json"
    rc = main(["--dataset", str(_FIX), "--out", str(out)],
              backend_factory=lambda arm: _FakeBackend(arm),
              llm_factory=lambda arm: FakeLLM())
    assert rc == 0
    rep = json.loads(out.read_text())
    # poison_promotion is a TOP-LEVEL provenance field with the three sub-keys.
    pp = rep["provenance"]["poison_promotion"]
    assert set(pp) == {"two_seat_promoted", "self_demand_blocked", "conclusive"}
    # it NEVER appears inside any delta or clean_win — the admission asymmetry is not serve-time FSR.
    for d in rep["deltas"].values():
        assert "poison_promotion" not in d
        assert "poison_promotion" not in d["false_serve"]
        assert set(d) == {"false_serve", "success", "clean_win"}
        # clean_win is a pure bool conjunction of the two CI axes — it carries no admission signal.
        assert isinstance(d["clean_win"], bool)


# ── degenerate slice: a clear domain error, not a bare bootstrap crash ───────────
def test_score_poison_arms_refuses_a_degenerate_empty_corrected_slice():
    # an arm with zero corrected-answerable tasks (only a clean control) ⇒ the headline slice is
    # empty; the paired CI is undefined, so score must raise a CLEAR domain error, not let
    # paired_delta_ci bare-crash on an empty vector.
    good = [_task(f"q{i}", served=1, served_false=0) for i in range(3)]
    empty = [_task(f"q{i}", served=1, served_false=0, regime="clean") for i in range(3)]
    with pytest.raises(ValueError, match="corrected-answerable"):
        score_poison_arms(_four_arms(good, good, good, empty), seed=0)


# ── build_poison_report: refuse on incomplete provenance ────────────────────────
def _complete_provenance() -> dict:
    return {"arms": [ARM_MEM0, ARM_OFF, ARM_ON, ARM_SUP], "served_tokenizer": "regex-wordpunct",
            "dataset_hash": "abc", "embedder_model": EMBEDDER_MODEL, "poison_version": "poison.v1",
            "poison_frac": 0.5, "poison_kinds": ["mistake", "stale"],
            "poison_value_map_hash": "deadbeef", "tau_serve_on": 0.70, "tau_serve_off": 0.01,
            "llm_digest": "d", "seeds": [0], "regime_mix": {"corrected": 1},
            "matched_serve_point": 3,
            "secret_floor_observed": {"refused_by_hive": True, "stored_by_mem0": True,
                                      "conclusive": True},
            "poison_promotion": {"two_seat_promoted": False, "self_demand_blocked": False,
                                 "conclusive": False}}


def test_build_poison_report_refuses_incomplete_provenance():
    prov = _complete_provenance(); del prov["poison_value_map_hash"]
    with pytest.raises(ValueError, match="provenance"):
        build_poison_report(arms={ARM_MEM0: {"success_rate": 0.5}}, deltas={}, provenance=prov)


def test_build_poison_report_refuses_missing_promotion_provenance():
    # the anti-gaming line is REQUIRED provenance ⇒ a report can never silently omit it.
    prov = _complete_provenance(); del prov["poison_promotion"]
    with pytest.raises(ValueError, match="provenance"):
        build_poison_report(arms={ARM_MEM0: {"success_rate": 0.5}}, deltas={}, provenance=prov)


def test_build_poison_report_refuses_without_success_vectors():
    with pytest.raises(ValueError, match="success"):
        build_poison_report(arms={ARM_MEM0: {"false_serve_rate": 0.0}}, deltas={},
                            provenance=_complete_provenance())


def test_build_poison_report_emits_with_complete_inputs():
    rep = build_poison_report(
        arms={ARM_MEM0: {"success_rate": 0.5}},
        deltas={"SUP-mem0": {"clean_win": True}}, provenance=_complete_provenance())
    assert rep["provenance"]["poison_value_map_hash"] == "deadbeef" and rep["arms"] and "deltas" in rep


# ── main offline (fake backends + FakeLLM) ─────────────────────────────────────
class _FakeBackend:
    """A token-overlap recall store driven by the real propose→commit preload + plant seam. The mem0
    arm serves any token-overlapping hit (trust-everything); the supersede arm has a ``supersede``
    method that de-indexes the loser (getattr-probe path), so the four arms differ in served set."""
    def __init__(self, arm: str) -> None:
        self._store: dict[str, str] = {}; self._n = 0; self._arm = arm

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

    def supersede(self, loser_id: str, winner_text: str, *, approver: str) -> str:
        # human-vouched de-index: drop the loser, establish the correction (only the SUP arm calls it).
        self._store.pop(loser_id, None)
        self._n += 1
        mid = f"m{self._n}"
        self._store[mid] = winner_text
        return mid

    def reset(self) -> None:
        self._store.clear(); self._n = 0


def test_fake_backend_conforms():
    assert isinstance(_FakeBackend("mem0"), MemoryBackend)


def test_main_offline_writes_a_provenance_stamped_poison_report(tmp_path):
    out = tmp_path / "poison.json"
    rc = main(["--dataset", str(_FIX), "--out", str(out)],
              backend_factory=lambda arm: _FakeBackend(arm),
              llm_factory=lambda arm: FakeLLM(usage={"input_tokens": 7}))
    assert rc == 0 and out.exists()
    rep = json.loads(out.read_text())
    # all four arms present.
    assert set(rep["arms"]) == {ARM_MEM0, ARM_OFF, ARM_ON, ARM_SUP}
    # the three deltas, each with BOTH axes + clean_win.
    assert set(rep["deltas"]) == {"OFF-mem0", "ON-mem0", "SUP-mem0"}
    for d in rep["deltas"].values():
        assert d["false_serve"]["lo"] <= d["false_serve"]["hi"]
        assert d["success"]["lo"] <= d["success"]["hi"]
        assert isinstance(d["false_serve"]["improves"], bool)
        assert isinstance(d["success"]["regresses"], bool)
        assert isinstance(d["clean_win"], bool)
    # complete provenance, incl. the two DISCLOSED capability lines + the determinism stamps.
    prov = rep["provenance"]
    assert prov["tau_serve_on"] == 0.70 and prov["tau_serve_off"] == 0.01
    assert prov["embedder_model"] == EMBEDDER_MODEL and prov["served_tokenizer"]
    assert prov["dataset_hash"] and prov["poison_version"] == "poison.v1"
    assert prov["poison_value_map_hash"] and "matched_serve_point" in prov
    assert set(prov["regime_mix"]) == {"corrected", "clean"}
    # secret_floor_observed is a DISCLOSED top-level write-path probe (a fake backend that does not
    # model the scanner reads not-refused offline — an honest inconclusive, like the anti-gaming line).
    sf = prov["secret_floor_observed"]
    assert set(sf) == {"refused_by_hive", "stored_by_mem0", "conclusive"}
    assert all(isinstance(v, bool) for v in sf.values())
    # the anti-gaming probe is PRESENT and well-formed; offline it is inconclusive (no real demand).
    pp = prov["poison_promotion"]
    assert set(pp) == {"two_seat_promoted", "self_demand_blocked", "conclusive"}
    assert pp["conclusive"] in (True, False)
    # every arm carries both headline axes: false-serve resistance AND success, per regime.
    for arm in rep["arms"].values():
        assert "false_serve_rate" in arm and "success_rate" in arm and "by_regime" in arm
        assert "per_served_false_rate_full" in arm and "per_served_false_rate_matched" in arm


def test_main_does_not_double_plant_the_falsehood(tmp_path):
    # The falsehood is loaded ONCE through preload (the augmented pool already contains it); a second
    # vouched plant would give a no-dedup store (mem0) the falsehood TWICE — a serve-volume confound
    # the benchmark must not have. Pin it behaviorally: within any single arm's backend, no text is
    # committed more than once.
    backends: list = []

    class _CountingBackend(_FakeBackend):
        def __init__(self, arm: str) -> None:
            super().__init__(arm)
            self.commit_counts: dict[str, int] = {}
            backends.append(self)

        def commit(self, proposal, *, approver: str) -> str:
            self.commit_counts[proposal.text] = self.commit_counts.get(proposal.text, 0) + 1
            return super().commit(proposal, approver=approver)

    out = tmp_path / "p.json"
    rc = main(["--dataset", str(_FIX), "--out", str(out)],
              backend_factory=lambda arm: _CountingBackend(arm),
              llm_factory=lambda arm: FakeLLM(usage={"input_tokens": 7}))
    assert rc == 0 and backends
    # a double-plant would make some text's count 2 in an arm backend.
    assert all(max(b.commit_counts.values(), default=0) <= 1 for b in backends)
