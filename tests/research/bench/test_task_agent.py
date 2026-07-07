"""C3 (+ BUG-004 fix): the agent-loop task driver. The token axis is a DETERMINISTIC count of the
served memory block (``served_tokens``) — not claude usage, which prompt caching + the Claude Code
base prompt make unrecoverable. ``run_task`` recalls → resolves served ids → counts the served block
→ completes (for the success axis). An answerable task succeeds only if the gold was actually served
AND the answer matches. Fully offline."""
from __future__ import annotations

import json

import pytest

from hive.research.bench.backends import MemoryBackend, Proposal, RecallObs
from hive.research.bench.llm import ClaudeSubscriptionLLM, FakeLLM, RunResult
from hive.research.bench.substrate import TaskCase
from hive.research.bench.task_agent import (
    ArmTokenObs, TaskObs, _score_answer, _served_block, count_tokens, inject_scaffold,
    run_arm, run_task, tokenizer_name,
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


def _llm(answer: str = ""):
    return FakeLLM(lambda prompt, system: answer)


def test_backend_double_conforms():
    assert isinstance(_Backend(), MemoryBackend)


# ── inject_scaffold: byte-identical across arms except the served block ─────────
def test_inject_scaffold_differs_only_in_the_served_block():
    empty = inject_scaffold("Q?", [])
    full = inject_scaffold("Q?", ["alpha", "beta"])
    assert full == empty.replace("retrieved from memory:\n", "retrieved from memory:\n- alpha\n- beta\n")
    assert "Question: Q?" in empty and "Question: Q?" in full


# ── the served-token counter (deterministic, tokenizer-stamped) ────────────────
def test_count_tokens_is_deterministic_positive_and_empty_is_zero():
    assert count_tokens("") == 0
    n = count_tokens("postgres listens on port 5432")
    assert n > 0 and count_tokens("postgres listens on port 5432") == n   # deterministic
    assert count_tokens("a b c d e") > count_tokens("a b")                # monotonic with content
    assert isinstance(tokenizer_name(), str) and tokenizer_name()         # a non-empty stamp


# ── success.v2 scorer ──────────────────────────────────────────────────────────
def test_success_v2_scores_a_composite_gold_answered_as_decomposed_facts():
    decomposed = "You use postgres for the main store, and the db listens on port 5432."
    assert _score_answer(_BROAD, decomposed) is True
    assert _score_answer(_BROAD, "I don't know.") is False
    assert _score_answer(_SINGLE, "It is 8080.") is True
    assert _score_answer(_SINGLE, "The port is 9090.") is False


def test_unanswerable_success_is_a_refusal():
    assert _score_answer(_NOREL, "I don't know.") is True
    assert _score_answer(_NOREL, "Your pet dragon is named Spark.") is False


# ── run_task: serve / abstain / gold-coverage / token count ────────────────────
def test_run_task_counts_the_served_block_and_detects_gold():
    served_text = {"m1": "the dev server runs on port 8080", "m2": "green tea fact"}
    mem_source = {"m1": "s1", "m2": "sX"}                          # m1 derives from the gold session s1
    obs = run_task(_Backend(ranked=("m1", "m2")), _llm("8080"), _SINGLE,
                   seat="reader", served_text=served_text, mem_source=mem_source)
    assert isinstance(obs, TaskObs)
    assert obs.served_text_count == 2 and obs.served_gold is True
    # the token axis is the DETERMINISTIC count of exactly the injected block
    assert obs.served_tokens == count_tokens(_served_block(["the dev server runs on port 8080",
                                                            "green tea fact"]))
    assert obs.served_tokens > 0


def test_run_task_abstain_serves_zero_tokens():
    obs = run_task(_Backend(ranked=()), _llm(), _NOREL, seat="reader", served_text={}, mem_source={})
    assert obs.served_text_count == 0 and obs.served_tokens == 0
    assert obs.abstained_on_answerable is False                    # not answerable


def test_answerable_abstain_is_a_counted_failure_even_if_the_guess_is_right():
    obs = run_task(_Backend(ranked=()), _llm("8080"), _SINGLE,
                   seat="reader", served_text={}, mem_source={})
    assert obs.abstained_on_answerable is True and obs.served_gold is False
    assert obs.answer_match is True and obs.success is False


def test_confident_serve_that_drops_the_gold_is_a_failure_even_if_the_guess_is_right():
    obs = run_task(_Backend(ranked=("m9",)), _llm("It is 8080."), _SINGLE,
                   seat="reader", served_text={"m9": "a plausible but non-answering note"},
                   mem_source={"m9": "sX"})                        # sX is NOT the gold session s1
    assert obs.served_text_count == 1 and obs.served_gold is False
    assert obs.abstained_on_answerable is False
    assert obs.answer_match is True and obs.success is False


def test_answerable_success_requires_gold_served_and_answer_matched():
    obs = run_task(_Backend(ranked=("m1",)), _llm("It is 8080."), _SINGLE,
                   seat="reader", served_text={"m1": "the dev server runs on port 8080"},
                   mem_source={"m1": "s1"})
    assert obs.served_gold is True and obs.success is True


def test_no_relevant_success_is_a_correct_refusal():
    obs = run_task(_Backend(ranked=("m1",)), _llm("I don't know."), _NOREL,
                   seat="reader", served_text={"m1": "irrelevant junk"}, mem_source={"m1": "sX"})
    assert obs.success is True and obs.served_gold is False


# ── TaskObs invariants ─────────────────────────────────────────────────────────
def test_taskobs_rejects_abstain_with_served_content():
    with pytest.raises(ValueError, match="abstained"):
        TaskObs(query="q", regime="single-answer", answerable=True, served_text_count=1,
                served_tokens=0, answer_match=True, served_gold=False, abstained_on_answerable=True)


def test_taskobs_rejects_served_tokens_without_served_content():
    with pytest.raises(ValueError, match="served_tokens"):
        TaskObs(query="q", regime="single-answer", answerable=True, served_text_count=0,
                served_tokens=5, answer_match=True, served_gold=False, abstained_on_answerable=False)


def test_taskobs_rejects_served_gold_without_served_content():
    with pytest.raises(ValueError, match="served_gold"):
        TaskObs(query="q", regime="single-answer", answerable=True, served_text_count=0,
                served_tokens=0, answer_match=True, served_gold=True, abstained_on_answerable=False)


# ── per-arm served-token aggregation (deterministic; the headline axis) ────────
def test_served_tokens_track_injected_volume_across_arms():
    served_text = {"m1": "alpha fact one", "m2": "beta fact two"}
    served = run_arm(_Backend(ranked=("m1", "m2")), _llm(), [_SINGLE], seat="reader",
                     served_text=served_text, mem_source={"m1": "s1"}, arm="served")
    abstain = run_arm(_Backend(ranked=()), _llm(), [_SINGLE], seat="reader",
                      served_text=served_text, mem_source={"m1": "s1"}, arm="abstain")
    assert isinstance(served, ArmTokenObs)
    sv = sum(t.served_tokens for t in served.tasks)
    ab = sum(t.served_tokens for t in abstain.tasks)
    assert sv == count_tokens(_served_block(["alpha fact one", "beta fact two"]))
    assert ab == 0 and sv > ab                                     # serving more ⇒ more tokens


def test_run_arm_collects_one_obs_per_case():
    arm = run_arm(_Backend(ranked=()), _llm(), [_SINGLE, _NOREL], seat="reader",
                  served_text={}, mem_source={})
    assert len(arm.tasks) == 2


# ── the cost DIAGNOSTIC (claude usage) still reproduces on a log replay (R1) ────
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


def test_arm_cost_diagnostic_reproduces_on_a_log_replay(tmp_path):
    # served_tokens is deterministic (no LLM), so the token axis is trivially replay-invariant; the
    # claude usage DIAGNOSTIC must also reproduce from the log rather than zeroing (the R1 fix).
    log = tmp_path / "arm.jsonl"
    cases = [_NOREL, TaskCase(query="a second, distinct question?", answerable=False,
                              gold_source_ids=frozenset(), regime="no-relevant", expected=None)]
    runner = _SeqRunner(RunResult(0, _usage_env("a", 11), ""), RunResult(0, _usage_env("b", 13), ""))
    cold = run_arm(_Backend(ranked=()), ClaudeSubscriptionLLM(runner=runner, log_path=log), cases,
                   seat="reader", served_text={}, mem_source={})
    boom = _SeqRunner(RunResult(1, "", "SHOULD NOT BE CALLED"))
    replay = run_arm(_Backend(ranked=()), ClaudeSubscriptionLLM(runner=boom, log_path=log), cases,
                     seat="reader", served_text={}, mem_source={})
    assert len(boom.calls) == 0
    assert replay.usage_total["input_tokens"] == cold.usage_total["input_tokens"] == 24
    assert [t.served_tokens for t in replay.tasks] == [t.served_tokens for t in cold.tasks]
