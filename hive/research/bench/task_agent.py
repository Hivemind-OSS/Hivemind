"""C3 — the agent-loop task driver for the abstain-token benchmark.

Per task: recall → inject the served content into ONE frozen task-prompt scaffold → the agent
answers via the LLM seam → return a frozen ``TaskObs``. The scaffold (``task.v1``) is byte-identical
across all arms EXCEPT the served block, so the real ``claude -p`` ``input_tokens`` delta ACROSS
arms is precisely the injected-content delta — actual agent spend, not a proxy. The per-task spend
is measured from ``served_usage_totals()`` (which advances on every served result — real call, cache
hit, OR log replay), so a ``--llm-log`` re-run reproduces the SAME per-task tokens instead of zeroing
them.

Honesty (guards 1/6 / AC4): a token saving may never come from under-serving a needed fact. Two
mechanisms enforce it structurally: ``abstained_on_answerable`` flags an answerable query the gate
served NOTHING for, and ``served_gold`` records whether the gold memory was actually among the served
ids. ``TaskObs.success`` for an answerable task requires BOTH a correct answer AND that the gold was
served — so neither an empty serve, a confident serve that DROPS the gold, nor a parametric-memory
guess can read as success.

Dev-time only (under ``hive.research``). No kernel change — it drives the shipped recall + LLM seams
read-only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from hive.research.bench.backends import MemoryBackend
from hive.research.bench.llm import LLM, zero_served_usage
from hive.research.bench.substrate import TaskCase

# Frozen, versioned templates — never tuned on reported questions (anti-reward-hacking). Bump the
# version to change behavior. The notes header is FIXED; only the served block varies across arms.
TASK_PROMPT_VERSION = "task.v1"
SUCCESS_VERSION = "success.v2"      # v2: content-token set-containment (composite gold answers score)

# A correct decline on an unanswerable query (the model was given no answering note). Frozen set.
_REFUSAL_MARKERS = (
    "i don't know", "i dont know", "no information", "not available", "no relevant",
    "cannot find", "couldn't find", "could not find", "don't have", "do not have",
    "unable to", "no record", "isn't anything", "nothing about",
)

# Function words dropped from gold/answer token sets so set-containment turns on the CONTENT terms,
# not on whether the model echoed an article or preposition.
_STOPWORDS = frozenset(
    "a an the of on in at to is are was were be and or for with by from it i my you your this that".split())
_TOKEN_RX = re.compile(r"[a-z0-9]+")


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


def _content_tokens(s: str) -> set[str]:
    return {t for t in _TOKEN_RX.findall(s.lower()) if t not in _STOPWORDS}


def _score_answer(case: TaskCase, answer: str) -> bool:
    """Deterministic answer match (``success.v2``). Answerable: every CONTENT token of the gold
    answer is present in the reply — set-containment, so a composite broad-relevant gold (e.g.
    "postgres on port 5432") scores when answered as decomposed facts, not only as that contiguous
    phrase. Unanswerable: the agent correctly DECLINES (a refusal marker) rather than fabricating."""
    if case.expected is None:
        norm = _normalize(answer)
        return any(m in norm for m in _REFUSAL_MARKERS)
    gold = _content_tokens(case.expected)
    if not gold:                                   # degenerate all-stopword gold ⇒ substring fallback
        return _normalize(case.expected) in _normalize(answer)
    return gold <= _content_tokens(answer)


@dataclass(frozen=True)
class TaskObs:
    """One task's outcome. ``input_tokens`` is THIS task's served spend. ``answer_match`` is the raw
    deterministic match; ``served_gold`` records whether a gold memory was actually served;
    ``abstained_on_answerable`` marks an empty serve on an answerable query. ``success`` is the SCORED
    success — for an answerable task it requires BOTH a correct answer AND that the gold was served,
    so a saving can never be bought by under-serving (empty serve, gold-dropping serve, or a guess)."""
    query: str
    regime: str
    answerable: bool
    served_text_count: int
    input_tokens: int
    answer_match: bool
    served_gold: bool
    abstained_on_answerable: bool

    def __post_init__(self) -> None:
        if self.input_tokens < 0:
            raise ValueError(f"input_tokens cannot be negative, got {self.input_tokens}")
        if self.served_text_count < 0:
            raise ValueError(f"served_text_count cannot be negative, got {self.served_text_count}")
        if self.abstained_on_answerable and self.served_text_count:
            raise ValueError("an abstained task cannot have served content")
        if self.served_gold and not self.served_text_count:
            raise ValueError("served_gold is impossible without served content")
        if self.abstained_on_answerable and self.served_gold:
            raise ValueError("an abstained task cannot have served the gold")

    @property
    def success(self) -> bool:
        if self.answerable:
            return self.answer_match and self.served_gold
        return self.answer_match


def _usage(llm: LLM) -> dict:
    """The LLM's cumulative SERVED-usage snapshot — degrades to zeros for a double lacking it."""
    fn = getattr(llm, "served_usage_totals", None)
    return fn() if fn is not None else zero_served_usage()


def run_task(backend: MemoryBackend, llm: LLM, case: TaskCase, *,
             seat: str, served_text: dict[str, str], mem_source: dict[str, str]) -> TaskObs:
    """Recall → resolve served ids to text via the arm's serving map → inject → complete. The arm's
    ``mem_source`` (mem_id → source session) lets the loop tell whether the GOLD memory was among the
    served ids. Per-task ``input_tokens`` is the served-usage delta around the one ``complete``."""
    obs = backend.recall(seat, case.query)
    served_ids = obs.ranked_ids
    served = [served_text[mid] for mid in served_ids if mid in served_text]
    gold_mem_ids = {mid for mid, src in mem_source.items() if src in case.gold_source_ids}
    served_gold = bool(set(served_ids) & gold_mem_ids)
    scaffold = inject_scaffold(case.query, served)
    before = _usage(llm)["input_tokens"]
    answer = llm.complete(scaffold)
    after = _usage(llm)["input_tokens"]
    return TaskObs(
        query=case.query, regime=case.regime, answerable=case.answerable,
        served_text_count=len(served), input_tokens=after - before,
        answer_match=_score_answer(case, answer), served_gold=served_gold,
        abstained_on_answerable=(case.answerable and obs.abstained))


@dataclass(frozen=True)
class ArmTokenObs:
    """One arm's per-task observations + the arm-total served usage (the ``served_usage_totals()``
    delta over the whole arm — input/output/cache tokens, cost, and ``n_served``)."""
    arm: str
    tasks: tuple[TaskObs, ...]
    usage_total: dict


def run_arm(backend: MemoryBackend, llm: LLM, cases: Sequence[TaskCase], *,
            seat: str, served_text: dict[str, str], mem_source: dict[str, str],
            arm: str = "arm") -> ArmTokenObs:
    """Run every task through one arm; snapshot the LLM served-usage around the whole arm for the arm
    total (the per-task ``input_tokens`` sum equals it by construction)."""
    before = _usage(llm)
    tasks = tuple(run_task(backend, llm, c, seat=seat, served_text=served_text, mem_source=mem_source)
                  for c in cases)
    after = _usage(llm)
    usage_total = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    return ArmTokenObs(arm=arm, tasks=tasks, usage_total=usage_total)
