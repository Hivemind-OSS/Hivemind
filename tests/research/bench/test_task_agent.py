"""C3: the agent-loop task driver. ``inject_scaffold`` is the ONE frozen task prompt — byte-identical
across arms except the served block. ``run_task`` recalls → injects served content → completes →
returns a TaskObs whose per-task input_tokens is THIS task's spend only (the cross-arm delta isolates
served content), with an answerable-abstain counted as a failure (honesty guard 6). Fully offline:
fakes for the backend and the LLM, no model, no network."""
from __future__ import annotations

import pytest

from hive.research.bench.backends import MemoryBackend, Proposal, RecallObs
from hive.research.bench.substrate import TaskCase
from hive.research.bench.task_agent import (
    ArmTokenObs, TaskObs, inject_scaffold, run_arm, run_task,
)

_SINGLE = TaskCase(query="What port did I say the dev server uses?", answerable=True,
                   gold_source_ids=frozenset({"s1"}), regime="single-answer", expected="8080")
_NOREL = TaskCase(query="What did I say about my pet dragon?", answerable=False,
                  gold_source_ids=frozenset(), regime="no-relevant", expected=None)


# ── doubles ────────────────────────────────────────────────────────────────────
class _Backend:
    """A recall-only fake: returns a fixed ranked set (abstains when empty). propose/commit/reset
    are unused by the agent loop but present so it conforms to MemoryBackend."""
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


class _LenLLM:
    """An LLM whose usage tracks PROMPT LENGTH — so served content shows up in input_tokens
    exactly as the real ``claude -p`` does. ``answer_for(prompt) -> str`` scripts the reply."""
    def __init__(self, answer_for=None) -> None:
        self._answer_for = answer_for or (lambda prompt: "")
        self._total = 0
        self._n = 0

    def complete(self, prompt: str, *, system=None) -> str:
        self._total += len(prompt)
        self._n += 1
        return self._answer_for(prompt)

    def usage_totals(self) -> dict:
        return {"input_tokens": self._total, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "total_cost_usd": 0.0, "n_live_calls": self._n}


def test_backend_double_conforms():
    assert isinstance(_Backend(), MemoryBackend)


# ── inject_scaffold: byte-identical across arms except the served block ─────────
def test_inject_scaffold_differs_only_in_the_served_block():
    empty = inject_scaffold("Q?", [])
    full = inject_scaffold("Q?", ["alpha", "beta"])
    # the ONLY difference is the served lines spliced into the fixed notes section
    assert full == empty.replace("retrieved from memory:\n", "retrieved from memory:\n- alpha\n- beta\n")
    # the fixed question + answer instruction is present and identical in both
    assert "Question: Q?" in empty and "Question: Q?" in full


def test_inject_scaffold_is_a_pure_function_of_query_and_served():
    assert inject_scaffold("X", ["a"]) == inject_scaffold("X", ["a"])     # deterministic, no hidden input


# ── run_task: serve / abstain / counted failure / token attribution ────────────
def test_run_task_serves_resolved_text_when_not_abstaining():
    served_text = {"m1": "the dev server runs on port 8080", "m2": "green tea fact"}
    obs = run_task(_Backend(ranked=("m1", "m2")), _LenLLM(), _SINGLE,
                   seat="reader", served_text=served_text)
    assert isinstance(obs, TaskObs)
    assert obs.served_text_count == 2 and obs.regime == "single-answer"
    assert obs.abstained_on_answerable is False


def test_run_task_abstain_serves_zero():
    obs = run_task(_Backend(ranked=()), _LenLLM(), _NOREL, seat="reader", served_text={})
    assert obs.served_text_count == 0 and obs.abstained_on_answerable is False  # not answerable


def test_answerable_abstain_is_a_counted_failure_even_if_the_guess_is_right():
    # the agent guesses "8080" from nothing, but the gate served nothing on an ANSWERABLE query —
    # under-serving a needed fact is a failure (honesty guard 6 / AC4), regardless of the guess.
    obs = run_task(_Backend(ranked=()), _LenLLM(lambda p: "8080"), _SINGLE,
                   seat="reader", served_text={})
    assert obs.abstained_on_answerable is True
    assert obs.answer_match is True            # raw match succeeded...
    assert obs.success is False                # ...but the scored success is penalized


def test_answerable_success_when_served_and_answer_matches():
    served_text = {"m1": "the dev server runs on port 8080"}
    obs = run_task(_Backend(ranked=("m1",)), _LenLLM(lambda p: "It is 8080."), _SINGLE,
                   seat="reader", served_text=served_text)
    assert obs.success is True and obs.abstained_on_answerable is False


def test_no_relevant_success_is_a_correct_refusal():
    obs = run_task(_Backend(ranked=("m1",)), _LenLLM(lambda p: "I don't know."), _NOREL,
                   seat="reader", served_text={"m1": "irrelevant junk"})
    assert obs.success is True                  # correctly declined on an unanswerable query


def test_per_task_input_tokens_is_that_tasks_scaffold_only():
    # two distinct queries; _LenLLM makes input_tokens == prompt length, so each task's delta must
    # equal ITS scaffold, never the cumulative running total (which would over-attribute later tasks).
    c2 = TaskCase(query="A completely different question entirely?", answerable=False,
                  gold_source_ids=frozenset(), regime="no-relevant", expected=None)
    cases = [_NOREL, c2]
    arm = run_arm(_Backend(ranked=()), _LenLLM(), cases, seat="reader", served_text={})
    for c, t in zip(cases, arm.tasks):
        assert t.input_tokens == len(inject_scaffold(c.query, []))


def test_served_content_increases_per_arm_input_tokens():
    served_text = {"m1": "alpha fact one", "m2": "beta fact two"}
    served = run_arm(_Backend(ranked=("m1", "m2")), _LenLLM(), [_SINGLE],
                     seat="reader", served_text=served_text, arm="served")
    abstain = run_arm(_Backend(ranked=()), _LenLLM(), [_SINGLE],
                      seat="reader", served_text=served_text, arm="abstain")
    assert isinstance(served, ArmTokenObs)
    assert served.usage_total["input_tokens"] > abstain.usage_total["input_tokens"]
    # the difference is exactly the served block (the cross-arm injected-content delta)
    delta = served.usage_total["input_tokens"] - abstain.usage_total["input_tokens"]
    assert delta == len("- alpha fact one\n- beta fact two\n")


def test_run_arm_collects_one_obs_per_case():
    arm = run_arm(_Backend(ranked=()), _LenLLM(), [_SINGLE, _NOREL], seat="reader", served_text={})
    assert len(arm.tasks) == 2 and arm.usage_total["n_live_calls"] == 2
