"""C3 (+ review hardening): the agent-loop task driver. ``inject_scaffold`` is the ONE frozen task
prompt — byte-identical across arms except the served block. ``run_task`` recalls → resolves served
ids → injects → completes; per-task input_tokens is THIS task's served spend (measured from
served_usage_totals so a replay reproduces it). An answerable task succeeds only if the gold was
actually served AND the answer matches (no saving from under-serving). Fully offline."""
from __future__ import annotations

import json

import pytest

from hive.research.bench.backends import MemoryBackend, Proposal, RecallObs
from hive.research.bench.llm import ClaudeSubscriptionLLM, RunResult
from hive.research.bench.substrate import TaskCase
from hive.research.bench.task_agent import (
    ArmTokenObs, TaskObs, _score_answer, inject_scaffold, run_arm, run_task,
)

_SINGLE = TaskCase(query="What port did I say the dev server uses?", answerable=True,
                   gold_source_ids=frozenset({"s1"}), regime="single-answer", expected="8080")
_BROAD = TaskCase(query="What database and which port did I mention?", answerable=True,
                  gold_source_ids=frozenset({"s3", "s4"}), regime="broad-relevant",
                  expected="postgres on port 5432")
_NOREL = TaskCase(query="What did I say about my pet dragon?", answerable=False,
                  gold_source_ids=frozenset(), regime="no-relevant", expected=None)


# ── doubles ────────────────────────────────────────────────────────────────────
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


class _LenLLM:
    """An LLM whose served usage tracks PROMPT LENGTH — so served content shows up in input_tokens
    exactly as the real ``claude -p`` does. ``answer_for(prompt) -> str`` scripts the reply."""
    def __init__(self, answer_for=None) -> None:
        self._answer_for = answer_for or (lambda prompt: "")
        self._total = 0
        self._n = 0

    def complete(self, prompt: str, *, system=None) -> str:
        self._total += len(prompt)
        self._n += 1
        return self._answer_for(prompt)

    def served_usage_totals(self) -> dict:
        return {"input_tokens": self._total, "output_tokens": 0, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "total_cost_usd": 0.0, "n_served": self._n}


def test_backend_double_conforms():
    assert isinstance(_Backend(), MemoryBackend)


# ── inject_scaffold: byte-identical across arms except the served block ─────────
def test_inject_scaffold_differs_only_in_the_served_block():
    empty = inject_scaffold("Q?", [])
    full = inject_scaffold("Q?", ["alpha", "beta"])
    assert full == empty.replace("retrieved from memory:\n", "retrieved from memory:\n- alpha\n- beta\n")
    assert "Question: Q?" in empty and "Question: Q?" in full


def test_inject_scaffold_is_a_pure_function_of_query_and_served():
    assert inject_scaffold("X", ["a"]) == inject_scaffold("X", ["a"])


# ── success.v2 scorer: content-token set-containment ───────────────────────────
def test_success_v2_scores_a_composite_gold_answered_as_decomposed_facts():
    # the broad-relevant probe: a faithful decomposed answer must score, not only the contiguous gold
    decomposed = "You use postgres for the main store, and the db listens on port 5432."
    assert _score_answer(_BROAD, decomposed) is True
    assert _score_answer(_BROAD, "I don't know.") is False
    assert _score_answer(_SINGLE, "It is 8080.") is True          # punctuation-robust, single token
    assert _score_answer(_SINGLE, "The port is 9090.") is False


def test_unanswerable_success_is_a_refusal():
    assert _score_answer(_NOREL, "I don't know.") is True
    assert _score_answer(_NOREL, "Your pet dragon is named Spark.") is False


# ── run_task: serve / abstain / gold-coverage / token attribution ──────────────
def test_run_task_serves_resolved_text_and_detects_gold():
    served_text = {"m1": "the dev server runs on port 8080", "m2": "green tea fact"}
    mem_source = {"m1": "s1", "m2": "sX"}                          # m1 derives from the gold session s1
    obs = run_task(_Backend(ranked=("m1", "m2")), _LenLLM(), _SINGLE,
                   seat="reader", served_text=served_text, mem_source=mem_source)
    assert isinstance(obs, TaskObs)
    assert obs.served_text_count == 2 and obs.served_gold is True
    assert obs.abstained_on_answerable is False


def test_answerable_abstain_is_a_counted_failure_even_if_the_guess_is_right():
    obs = run_task(_Backend(ranked=()), _LenLLM(lambda p: "8080"), _SINGLE,
                   seat="reader", served_text={}, mem_source={})
    assert obs.abstained_on_answerable is True and obs.served_gold is False
    assert obs.answer_match is True and obs.success is False       # under-serve ⇒ not a success


def test_confident_serve_that_drops_the_gold_is_a_failure_even_if_the_guess_is_right():
    # the gate serves a hard negative but NOT the gold session ⇒ served_gold False ⇒ no success,
    # even though the model guessed the answer from parametric memory (honesty guard 6 / finding #10)
    obs = run_task(_Backend(ranked=("m9",)), _LenLLM(lambda p: "It is 8080."), _SINGLE,
                   seat="reader", served_text={"m9": "a plausible but non-answering note"},
                   mem_source={"m9": "sX"})                        # sX is NOT the gold session s1
    assert obs.served_text_count == 1 and obs.served_gold is False
    assert obs.abstained_on_answerable is False                    # it DID serve (not empty)
    assert obs.answer_match is True and obs.success is False


def test_answerable_success_requires_gold_served_and_answer_matched():
    obs = run_task(_Backend(ranked=("m1",)), _LenLLM(lambda p: "It is 8080."), _SINGLE,
                   seat="reader", served_text={"m1": "the dev server runs on port 8080"},
                   mem_source={"m1": "s1"})
    assert obs.served_gold is True and obs.success is True


def test_no_relevant_success_is_a_correct_refusal():
    obs = run_task(_Backend(ranked=("m1",)), _LenLLM(lambda p: "I don't know."), _NOREL,
                   seat="reader", served_text={"m1": "irrelevant junk"}, mem_source={"m1": "sX"})
    assert obs.success is True and obs.served_gold is False        # unanswerable ignores served_gold


# ── TaskObs invariants (finding #3) ────────────────────────────────────────────
def test_taskobs_rejects_abstain_with_served_content():
    with pytest.raises(ValueError, match="abstained"):
        TaskObs(query="q", regime="single-answer", answerable=True, served_text_count=1,
                input_tokens=0, answer_match=True, served_gold=False, abstained_on_answerable=True)


def test_taskobs_rejects_served_gold_without_served_content():
    with pytest.raises(ValueError, match="served_gold"):
        TaskObs(query="q", regime="single-answer", answerable=True, served_text_count=0,
                input_tokens=0, answer_match=True, served_gold=True, abstained_on_answerable=False)


def test_taskobs_rejects_negative_fields():
    with pytest.raises(ValueError, match="input_tokens"):
        TaskObs(query="q", regime="single-answer", answerable=True, served_text_count=0,
                input_tokens=-1, answer_match=False, served_gold=False, abstained_on_answerable=False)


# ── token attribution (served-usage delta; replay-safe) ────────────────────────
def test_per_task_input_tokens_is_that_tasks_scaffold_only():
    c2 = TaskCase(query="A completely different question entirely?", answerable=False,
                  gold_source_ids=frozenset(), regime="no-relevant", expected=None)
    cases = [_NOREL, c2]
    arm = run_arm(_Backend(ranked=()), _LenLLM(), cases, seat="reader", served_text={}, mem_source={})
    for c, t in zip(cases, arm.tasks):
        assert t.input_tokens == len(inject_scaffold(c.query, []))


def test_served_content_increases_per_arm_input_tokens():
    served_text = {"m1": "alpha fact one", "m2": "beta fact two"}
    served = run_arm(_Backend(ranked=("m1", "m2")), _LenLLM(), [_SINGLE], seat="reader",
                     served_text=served_text, mem_source={"m1": "s1"}, arm="served")
    abstain = run_arm(_Backend(ranked=()), _LenLLM(), [_SINGLE], seat="reader",
                      served_text=served_text, mem_source={"m1": "s1"}, arm="abstain")
    assert isinstance(served, ArmTokenObs)
    delta = served.usage_total["input_tokens"] - abstain.usage_total["input_tokens"]
    assert delta == len("- alpha fact one\n- beta fact two\n")


def test_run_arm_collects_one_obs_per_case():
    arm = run_arm(_Backend(ranked=()), _LenLLM(), [_SINGLE, _NOREL], seat="reader",
                  served_text={}, mem_source={})
    assert len(arm.tasks) == 2 and arm.usage_total["n_served"] == 2


# ── replay safety (finding #2): a --llm-log re-run reproduces the token axis, never zeroes it ──
def _usage_env(result: str, input_tokens: int) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result,
                       "usage": {"input_tokens": input_tokens, "output_tokens": 0,
                                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                       "total_cost_usd": 0.0})


class _SeqRunner:
    def __init__(self, *results) -> None:
        self.calls: list = []
        self._r = list(results)

    def __call__(self, argv):
        self.calls.append(argv)
        return self._r[min(len(self.calls) - 1, len(self._r) - 1)]


def test_run_arm_token_vector_reproduces_on_a_log_replay(tmp_path):
    log = tmp_path / "arm.jsonl"
    cases = [_NOREL,
             TaskCase(query="a second, distinct question?", answerable=False,
                      gold_source_ids=frozenset(), regime="no-relevant", expected=None)]
    runner = _SeqRunner(RunResult(0, _usage_env("a", 11), ""), RunResult(0, _usage_env("b", 13), ""))
    cold = run_arm(_Backend(ranked=()), ClaudeSubscriptionLLM(runner=runner, log_path=log), cases,
                   seat="reader", served_text={}, mem_source={})
    cold_vec = [t.input_tokens for t in cold.tasks]
    assert cold_vec == [11, 13] and len(runner.calls) == 2
    # replay over the SAME log with a runner that EXPLODES if called — the token vector must
    # reproduce from the log, not collapse to [0, 0] (the bug the served-usage axis fixes).
    boom = _SeqRunner(RunResult(1, "", "SHOULD NOT BE CALLED"))
    replay = run_arm(_Backend(ranked=()), ClaudeSubscriptionLLM(runner=boom, log_path=log), cases,
                     seat="reader", served_text={}, mem_source={})
    assert [t.input_tokens for t in replay.tasks] == cold_vec and len(boom.calls) == 0
    assert replay.usage_total["input_tokens"] == cold.usage_total["input_tokens"] == 24
