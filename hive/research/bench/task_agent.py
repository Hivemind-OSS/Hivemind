"""C3 — the agent-loop task driver for the abstain-token benchmark.

Per task: recall → inject the served content into ONE frozen task-prompt scaffold → the agent
answers via the LLM seam → return a frozen ``TaskObs``. The scaffold (``task.v1``) is byte-identical
across all arms EXCEPT the served block, so the real ``claude -p`` ``input_tokens`` delta ACROSS
arms is precisely the injected-content delta — actual agent spend, not a proxy. The per-task spend
is measured as the usage_totals() delta around that single ``complete``, so each task's tokens are
its own (never a cumulative running total bleeding onto later tasks, never another arm's).

Honesty (guard 6 / AC4): abstaining on an ANSWERABLE query is under-serving a needed fact — it is a
counted task FAILURE, never a saving. ``TaskObs.success`` enforces it structurally: an
answerable-abstain can never read as success even if the model happens to guess the answer.

Dev-time only (under ``hive.research``). No kernel change — it drives the shipped recall + LLM seams
read-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from hive.research.bench.backends import MemoryBackend
from hive.research.bench.llm import LLM, zero_usage
from hive.research.bench.substrate import TaskCase

# Frozen, versioned templates — never tuned on reported questions (anti-reward-hacking). Bump the
# version to change behavior. The notes header is FIXED; only the served block varies across arms.
_TASK_PROMPT_VERSION = "task.v1"
_SUCCESS_VERSION = "success.v1"

# A correct decline on an unanswerable query (the model was given no answering note). Frozen set.
_REFUSAL_MARKERS = (
    "i don't know", "i dont know", "no information", "not available", "no relevant",
    "cannot find", "couldn't find", "could not find", "don't have", "do not have",
    "unable to", "no record", "isn't anything", "nothing about",
)


def inject_scaffold(query: str, served: Sequence[str]) -> str:
    """The ONE task-prompt template (``task.v1``). The fixed prefix/suffix is constant across arms;
    ONLY ``served_block`` (the recall content, empty when nothing was served) varies — so the
    cross-arm ``input_tokens`` delta isolates served content exactly."""
    served_block = "".join(f"- {s}\n" for s in served)            # "" when nothing served
    return (
        "You are answering a question using your team's shared memory.\n"
        "Notes retrieved from memory:\n"
        f"{served_block}"
        f"\nQuestion: {query}\n"
        "Answer concisely using ONLY the notes above. "
        "If the notes do not contain the answer, reply exactly: I don't know.\n"
    )


def _normalize(s: str) -> str:
    return " ".join(s.lower().split())


def _score_answer(case: TaskCase, answer: str) -> bool:
    """Deterministic success (``success.v1``): an answerable task succeeds iff the gold answer
    appears in the reply; an unanswerable task succeeds iff the agent correctly DECLINES (a refusal
    marker) rather than fabricating from injected junk."""
    norm = _normalize(answer)
    if case.expected is None:
        return any(m in norm for m in _REFUSAL_MARKERS)
    return _normalize(case.expected) in norm


@dataclass(frozen=True)
class TaskObs:
    """One task's outcome. ``input_tokens`` is THIS task's spend (the usage delta around its single
    ``complete``). ``answer_match`` is the raw deterministic match; ``abstained_on_answerable`` marks
    the under-serve. ``success`` is the SCORED success — answer match AND not an answerable-abstain —
    so a saving can never be bought by refusing a fact the task needed."""
    query: str
    regime: str
    served_text_count: int
    input_tokens: int
    answer_match: bool
    abstained_on_answerable: bool

    def __post_init__(self) -> None:
        if self.input_tokens < 0:
            raise ValueError(f"input_tokens cannot be negative, got {self.input_tokens}")
        if self.served_text_count < 0:
            raise ValueError(f"served_text_count cannot be negative, got {self.served_text_count}")
        if self.abstained_on_answerable and self.served_text_count:
            raise ValueError("an abstained task cannot have served content")

    @property
    def success(self) -> bool:
        return self.answer_match and not self.abstained_on_answerable


def _usage(llm: LLM) -> dict:
    """The LLM's cumulative usage snapshot — degrades to zeros for a double lacking the method."""
    return getattr(llm, "usage_totals", zero_usage)()


def run_task(backend: MemoryBackend, llm: LLM, case: TaskCase, *,
             seat: str, served_text: dict[str, str]) -> TaskObs:
    """Recall → resolve served ids to their text via the arm's serving map → inject → complete.
    Per-task ``input_tokens`` is the usage delta around the one ``complete`` (its own spend only)."""
    obs = backend.recall(seat, case.query)
    served = [served_text[mid] for mid in obs.ranked_ids if mid in served_text]
    scaffold = inject_scaffold(case.query, served)
    before = _usage(llm)["input_tokens"]
    answer = llm.complete(scaffold)
    after = _usage(llm)["input_tokens"]
    return TaskObs(
        query=case.query, regime=case.regime, served_text_count=len(served),
        input_tokens=after - before,
        answer_match=_score_answer(case, answer),
        abstained_on_answerable=(case.answerable and obs.abstained))


@dataclass(frozen=True)
class ArmTokenObs:
    """One arm's per-task observations + the arm-total usage (the usage_totals() delta over the
    whole arm — input/output/cache tokens, cost, and calls actually spent this run)."""
    arm: str
    tasks: tuple[TaskObs, ...]
    usage_total: dict


def run_arm(backend: MemoryBackend, llm: LLM, cases: Sequence[TaskCase], *,
            seat: str, served_text: dict[str, str], arm: str = "arm") -> ArmTokenObs:
    """Run every task through one arm; snapshot the LLM usage around the whole arm for the arm total
    (the per-task ``input_tokens`` sum equals it by construction, since usage accumulates)."""
    before = _usage(llm)
    tasks = tuple(run_task(backend, llm, c, seat=seat, served_text=served_text) for c in cases)
    after = _usage(llm)
    usage_total = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    return ArmTokenObs(arm=arm, tasks=tasks, usage_total=usage_total)
