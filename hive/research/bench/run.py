"""B6 — the benchmark runner: the per-case eval loop, preflight, provenance report, and CLI.

This chunk (B6a) is the analytical core, ``evaluate_arm`` — one arm (backend × gate) over the
dataset. For each question it builds a FRESH store (each LongMemEval question has its own haystack),
has subagents LLM-extract facts per session, drives the orchestrator-in-loop ingestion, asks the
question, and collects two scored streams:

  * retrieval — ANSWERABLE questions only, each paired with its gold-relevant memory ids;
  * abstention — EVERY question paired with ``is_unanswerable`` (both classes, for AUROC).

The fidelity rule that keeps the headline honest: an answerable question whose evidence facts were
never committed (the gate or extractor dropped them) has EMPTY gold. That is a real retrieval
failure, so it is scored as a guaranteed MISS via an unretrievable sentinel id — never silently
dropped the way a genuinely unanswerable question is. Skipping it would flatter an over-aggressive
gate by hiding the questions it curated away.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from hive.research.bench.backends import MemoryBackend, RecallObs
from hive.research.bench.dataset import Case, gold_relevant
from hive.research.bench.llm import LLM
from hive.research.bench.orchestrator import (
    GatePolicy, extract_facts, run_ingestion, run_queries,
)


@dataclass(frozen=True)
class ArmObservations:
    """One arm's raw observations, ready for ``scoring``. ``retrieval`` is answerable-only with
    non-empty gold (a sentinel stands in for lost evidence ⇒ a counted miss); ``abstention`` is
    every question with its unanswerable label."""
    retrieval: list[tuple[RecallObs, set[str]]]
    abstention: list[tuple[RecallObs, bool]]


def evaluate_arm(backend: MemoryBackend, gate: GatePolicy, cases: Sequence[Case], *,
                 llm: LLM, seats: Sequence[str], reader_seat: str = "reader",
                 max_facts: int = 8) -> ArmObservations:
    """Run one (backend, gate) arm over ``cases`` — a fresh store per question."""
    retrieval: list[tuple[RecallObs, set[str]]] = []
    abstention: list[tuple[RecallObs, bool]] = []
    for case in cases:
        backend.reset()
        sessions_facts = [(s.session_id, extract_facts(llm, s, max_facts=max_facts))
                          for s in case.sessions]
        trace = run_ingestion(backend, gate, sessions_facts, seats)
        obs = run_queries(backend, [case.question.text], seat=reader_seat)[0]
        abstention.append((obs, case.question.is_unanswerable))
        if not case.question.is_unanswerable:
            gold = gold_relevant(case, trace.mem_source)
            if not gold:
                # evidence was proposed but never committed ⇒ unretrievable ⇒ a counted miss,
                # NOT a skip (skipping would hide the questions an aggressive gate curated away).
                gold = {f"__unretrievable__:{case.question.question_id}"}
            retrieval.append((obs, gold))
    return ArmObservations(retrieval=retrieval, abstention=abstention)
