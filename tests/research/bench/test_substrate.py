"""C2: the preloaded-memory substrate. ``build_substrate`` derives a single pooled memory set +
the per-question ``TaskCase``s (three regimes) from a LongMemEval slice; ``preload`` establishes
that content through the backend's propose→commit seam (BUG-002-hardened: refused stages/commits
are skipped, never leaked as empty handles); ``assert_model_parity`` is the equal-footing belt
(MODEL must match; the 1024-vs-768 geometry asymmetry is intended, NOT asserted). Fully offline:
a fake backend, no model, no network."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hive.research.bench.backends import MemoryBackend, Proposal, RecallObs
from hive.research.bench.dataset import load_longmemeval
from hive.research.bench.run import EMBEDDER_MODEL
from hive.research.bench.substrate import (
    Preloaded, TaskCase, assert_model_parity, build_substrate, preload,
)

_FIX = Path(__file__).parent / "fixtures" / "longmemeval_tiny.json"


def _cases():
    return load_longmemeval(str(_FIX))


# ── a recording fake backend (records approver; can refuse a stage or a commit) ─
class _RecordingBackend:
    def __init__(self, *, refuse_stage=(), refuse_commit=()) -> None:
        self._n = 0
        self.committed: dict[str, tuple[str, str]] = {}     # mem_id → (text, approver)
        self._refuse_stage = set(refuse_stage)
        self._refuse_commit = set(refuse_commit)

    def recall(self, seat: str, query: str) -> RecallObs:
        return RecallObs(query=query, ranked_ids=(), top_score=0.0, confidence=0.0, abstained=True)

    def propose(self, seat: str, text: str, *, source_id: str) -> Proposal:
        self._n += 1
        pid = "" if text in self._refuse_stage else str(self._n)
        return Proposal(proposal_id=pid, seat=seat, text=text, source_id=source_id)

    def commit(self, proposal: Proposal, *, approver: str) -> str:
        if proposal.text in self._refuse_commit:
            return ""                                       # write refused at commit (BUG-002)
        mid = f"m{proposal.proposal_id}"
        self.committed[mid] = (proposal.text, approver)
        return mid

    def reset(self) -> None:
        self._n = 0
        self.committed.clear()


def test_recording_backend_conforms():
    assert isinstance(_RecordingBackend(), MemoryBackend)


# ── build_substrate: regime derivation ─────────────────────────────────────────
def test_build_substrate_classifies_three_regimes():
    _mems, cases = build_substrate(_cases(), seed=0)
    by_regime = {c.regime: c for c in cases}
    assert set(by_regime) == {"single-answer", "broad-relevant", "no-relevant"}
    # q1 has ONE evidence session ⇒ single-answer; q2 has TWO ⇒ broad-relevant; q3_abs ⇒ no-relevant
    assert by_regime["single-answer"].answerable is True
    assert len(by_regime["single-answer"].gold_source_ids) == 1
    assert by_regime["broad-relevant"].answerable is True
    assert len(by_regime["broad-relevant"].gold_source_ids) >= 2
    assert by_regime["no-relevant"].answerable is False
    assert by_regime["no-relevant"].gold_source_ids == frozenset()
    assert by_regime["no-relevant"].expected is None


def test_answerable_cases_carry_their_gold_answer_as_expected():
    _mems, cases = build_substrate(_cases(), seed=0)
    single = next(c for c in cases if c.regime == "single-answer")
    assert single.expected == "8080"                        # the q1 gold answer


def test_preloaded_memories_contain_the_gold_content():
    mems, _cases_out = build_substrate(_cases(), seed=0)
    # q1's gold session s1 carries the evidence turn — its content must be in the pool
    assert any(src == "s1" and "8080" in txt for src, txt in mems)
    # q2's two gold sessions s3, s4 are both present (broad-relevant needs the whole set)
    assert any(src == "s3" for src, _ in mems) and any(src == "s4" for src, _ in mems)


def test_queries_are_not_lifted_from_memory_tokens():
    # anti-reward-hacking: the query is the independently-authored QUESTION, never a memory's text
    mems, cases = build_substrate(_cases(), seed=0)
    mem_texts = {txt for _src, txt in mems}
    q1 = next(c for c in cases if c.regime == "single-answer")
    assert q1.query == "What port did I say the dev server uses?"
    assert all(c.query not in mem_texts for c in cases)


def test_build_substrate_is_deterministic_for_a_seed():
    a_mems, a_cases = build_substrate(_cases(), seed=7)
    b_mems, b_cases = build_substrate(_cases(), seed=7)
    assert a_mems == b_mems
    assert [c.query for c in a_cases] == [c.query for c in b_cases]


# ── preload: propose→commit, returns the maps, skips refusals ──────────────────
def test_preload_returns_mem_source_and_mem_text_maps():
    mems, _cases_out = build_substrate(_cases(), seed=0)
    b = _RecordingBackend()
    pre = preload(b, mems, approver="orchestrator")
    assert isinstance(pre, Preloaded)
    assert set(pre.mem_source) == set(pre.mem_text)          # same id space in both maps
    assert len(pre.mem_source) == len(mems)                  # nothing refused ⇒ all preloaded
    # every (mem_id → source_id, mem_id → text) pair traces back to an input memory
    for mid, src in pre.mem_source.items():
        assert (src, pre.mem_text[mid]) in mems


def test_preload_passes_the_approver_to_commit():
    b = _RecordingBackend()
    preload(b, [("s1", "redis runs on port 6379")], approver="the-human")
    assert all(approver == "the-human" for _txt, approver in b.committed.values())


def test_preload_skips_refused_stages_and_commits_without_leaking_handles():
    mems = [("s1", "kept fact one"), ("s2", "refused at stage"), ("s3", "refused at commit")]
    b = _RecordingBackend(refuse_stage={"refused at stage"}, refuse_commit={"refused at commit"})
    pre = preload(b, mems, approver="orchestrator")
    assert set(pre.mem_text.values()) == {"kept fact one"}   # only the un-refused fact preloaded
    assert "" not in pre.mem_source and "" not in pre.mem_text


# ── assert_model_parity: MODEL must match; dims may differ (geometry asymmetry) ─
def _cfg(model: str, d: int = 768):
    return SimpleNamespace(embedding=SimpleNamespace(model=model), geometry=SimpleNamespace(d=d))


def test_model_parity_passes_on_same_model_even_with_different_dims():
    # Hivemind truncates to 768 while mem0 keeps native 1024 — the disclosed asymmetry, NOT a drift
    assert_model_parity(_cfg(EMBEDDER_MODEL, d=768))         # must NOT raise


def test_model_parity_fails_fast_on_model_drift():
    with pytest.raises(ValueError, match="equal-footing|model"):
        assert_model_parity(_cfg("some/other-embedder", d=768))
