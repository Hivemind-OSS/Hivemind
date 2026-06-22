"""C2 — the preloaded-memory + recall-heavy task substrate for the abstain-token benchmark.

A SINGLE pooled store of REAL memory content (LongMemEval haystack turns — never synthetic text,
which is gameable) is loaded once; every task query then probes it, so an answerable query's gold
sits among hard negatives (the non-answering facts of every other case) and the gate must actually
discriminate. Three query regimes are derived and STAMPED so a per-regime report can show where a
token win really comes from:

  * single-answer  — exactly one gold-evidence session ⇒ a confident serve is expected.
  * broad-relevant — ≥2 comparably-relevant gold sessions for ONE single-intent query: the
                     shift-invariance over-abstention probe (the gate goes flat and can wrongly
                     suppress a genuinely answerable SET — see BENCHMARK.md §1 root-cause note).
  * no-relevant    — an ``_abs`` question: nothing relevant ⇒ the ONLY regime where abstaining
                     correctly saves tokens (mem0 dumps top-k junk; abstain-ON returns zero).

Queries are the independently-authored QUESTIONS, never lifted from a memory's own tokens
(deriving a query from stored text bakes in lexical overlap — a reward-hack). Equal footing is a
CHECKED invariant: ``assert_model_parity`` fails fast unless the Hivemind embedder MODEL equals
mem0's. Both sides now use the same native 1024-dim Qwen3 vectors (Hivemind no longer truncates),
so the geometry is identical and the store is the only difference under test.

Dev-time only (under ``hive.research`` — the purity fence forbids the runtime importing it). No
kernel change: it drives the shipped ``MemoryBackend`` propose→commit seam, read-only.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

from hive.research.bench.backends import MemoryBackend
from hive.research.bench.dataset import Case
from hive.research.bench.run import EMBEDDER_MODEL

_REGIMES = ("single-answer", "broad-relevant", "no-relevant")
_PRELOAD_SEAT = "preloader"


@dataclass(frozen=True)
class TaskCase:
    """One recall-heavy task — a frozen, self-asserting carrier (illegal states unconstructable).

    ``gold_source_ids`` holds the gold-evidence SESSION ids (the backend-independent gold
    reference; the per-arm preload maps these sources to concrete memory ids). It is EMPTY for
    no-relevant and has ≥2 entries for broad-relevant. ``expected`` is the deterministic answer
    key where the task admits one (answerable) and ``None`` for unanswerable.
    """
    query: str
    answerable: bool
    gold_source_ids: frozenset[str]
    regime: str
    expected: str | None

    def __post_init__(self) -> None:
        if self.regime not in _REGIMES:
            raise ValueError(f"regime must be one of {_REGIMES}, got {self.regime!r}")
        if self.answerable != (self.regime != "no-relevant"):
            raise ValueError(f"answerable={self.answerable} inconsistent with regime {self.regime!r}")
        if self.answerable and not self.gold_source_ids:
            raise ValueError("an answerable TaskCase must carry ≥1 gold source")
        if not self.answerable and self.gold_source_ids:
            raise ValueError("a no-relevant TaskCase must carry NO gold source")


@dataclass(frozen=True)
class Preloaded:
    """The result of establishing the memory pool into one backend's store. ``mem_source`` is the
    gold-mapping (mem_id → the session the fact came from); ``mem_text`` reconstructs the served
    content (mem_id → text) so the agent loop can inject exactly what recall surfaced. Refused
    writes never appear in either map (no empty handle leaks)."""
    mem_source: dict[str, str]
    mem_text: dict[str, str]


def build_substrate(cases: Sequence[Case], *, seed: int) -> tuple[list[tuple[str, str]], list[TaskCase]]:
    """Derive ``(memories, task_cases)`` from a LongMemEval slice. ``memories`` is the pooled
    ``(source_id, text)`` set (every case's haystack, so each query faces cross-case hard
    negatives); ``task_cases`` is one per question with its regime stamped. ``seed`` deterministically
    shuffles the pool so insertion-order tie-breaking never tracks case order."""
    memories: list[tuple[str, str]] = []
    task_cases: list[TaskCase] = []
    for case in cases:
        for session in case.sessions:
            for turn in session.turns:
                # user turns are the natural memory content; a has_answer turn (the gold evidence,
                # whichever role) is ALWAYS kept so an answerable query is genuinely answerable.
                if turn.role == "user" or turn.has_answer:
                    memories.append((session.session_id, turn.content))
        q = case.question
        gold = q.evidence_session_ids
        if q.is_unanswerable:
            regime, answerable, expected, gold = "no-relevant", False, None, frozenset()
        elif len(gold) >= 2:
            regime, answerable, expected = "broad-relevant", True, q.answer
        else:
            regime, answerable, expected = "single-answer", True, q.answer
        task_cases.append(TaskCase(query=q.text, answerable=answerable, gold_source_ids=gold,
                                   regime=regime, expected=expected))
    random.Random(seed).shuffle(memories)
    return memories, task_cases


def preload(backend: MemoryBackend, memories: Sequence[tuple[str, str]], *,
            approver: str, seat: str = _PRELOAD_SEAT) -> Preloaded:
    """Establish ``memories`` into ``backend`` via the shipped propose→commit seam (Hivemind:
    a quarantined capture that the orchestrator's ``hive_write`` establishes in place; mem0: a held
    proposal that ``add`` commits). A stage refused by the secret floor (empty ``proposal_id``) or
    a write refused at commit (empty handle) is SKIPPED — never leaked as an empty key (BUG-002
    class). Returns the gold + serving maps over what actually became servable."""
    mem_source: dict[str, str] = {}
    mem_text: dict[str, str] = {}
    for source_id, text in memories:
        proposal = backend.propose(seat, text, source_id=source_id)
        if not proposal.proposal_id:                 # refused at stage ⇒ never staged
            continue
        mem_id = backend.commit(proposal, approver=approver)
        if not mem_id:                               # refused at commit ⇒ never stored
            continue
        mem_source[mem_id] = source_id
        mem_text[mem_id] = text
    return Preloaded(mem_source=mem_source, mem_text=mem_text)


def assert_model_parity(cfg, *, mem0_model: str = EMBEDDER_MODEL) -> None:
    """Equal-footing belt: the Hivemind embedder MODEL must equal mem0's, else fail fast (a silent
    model drift would make the comparison meaningless). Both sides use the model's native dim
    (Hivemind no longer truncates), so the geometry is identical and the store is the only
    difference under test — only the MODEL is asserted (the dim follows from it)."""
    actual = cfg.embedding.model
    if actual != mem0_model:
        raise ValueError(
            f"equal-footing violation: Hivemind embedder model {actual!r} != mem0 model "
            f"{mem0_model!r} — the MODEL (and thus the native dim) must match")
