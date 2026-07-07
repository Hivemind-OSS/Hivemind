"""B1 — LongMemEval loader + gold mapping. The ONE place the dataset schema is encoded; a load
fails fast on schema drift (a missing field names itself) so a silently-wrong parse can never
feed the benchmark. Verified against ``xiaowu0162/longmemeval-cleaned`` (``longmemeval_s``):

  instance = {question_id, question_type, question, answer, question_date,
              haystack_session_ids[], haystack_dates[], haystack_sessions[][turn], answer_session_ids[]}
  turn     = {role, content, has_answer?}          # has_answer marks an evidence turn
  abstention question ⇔ question_id endswith "_abs" (no ground-truth evidence location)
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# every instance must carry these 9 top-level fields — a missing one is schema drift, not data.
_REQUIRED = (
    "question_id", "question_type", "question", "answer", "question_date",
    "haystack_session_ids", "haystack_dates", "haystack_sessions", "answer_session_ids",
)


@dataclass(frozen=True)
class Turn:
    role: str
    content: str
    has_answer: bool          # True ⇒ this turn carries required evidence (turn-level gold)


@dataclass(frozen=True)
class Session:
    session_id: str
    date: str
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class Question:
    question_id: str
    text: str
    answer: str
    date: str
    is_unanswerable: bool                 # question_id endswith "_abs"
    evidence_session_ids: frozenset[str]  # answer_session_ids (empty for abstention)


@dataclass(frozen=True)
class Case:
    question: Question
    sessions: tuple[Session, ...]         # the haystack, aligned to haystack_session_ids


def _parse_instance(inst: dict, idx: int) -> Case:
    for f in _REQUIRED:
        if f not in inst:
            raise ValueError(
                f"LongMemEval schema drift: instance {idx} "
                f"(question_id={inst.get('question_id', '?')!r}) missing field {f!r}")
    sids, sessions_raw, dates = (
        inst["haystack_session_ids"], inst["haystack_sessions"], inst["haystack_dates"])
    if not (len(sids) == len(sessions_raw) == len(dates)):
        raise ValueError(
            f"LongMemEval instance {idx}: haystack length mismatch "
            f"(ids={len(sids)} sessions={len(sessions_raw)} dates={len(dates)})")
    sessions = tuple(
        Session(session_id=str(sid), date=str(date),
                turns=tuple(Turn(role=str(t["role"]), content=str(t["content"]),
                                 has_answer=bool(t.get("has_answer", False)))
                            for t in sess))
        for sid, date, sess in zip(sids, dates, sessions_raw))
    qid = str(inst["question_id"])
    q = Question(
        question_id=qid,
        text=str(inst["question"]), answer=str(inst["answer"]),
        date=str(inst["question_date"]),
        is_unanswerable=qid.endswith("_abs"),
        evidence_session_ids=frozenset(str(s) for s in (inst["answer_session_ids"] or ())))
    return Case(question=q, sessions=sessions)


def load_longmemeval(path: str, *, n: Optional[int] = None, seed: int = 0) -> list[Case]:
    """Parse ``longmemeval_s(_cleaned).json`` into Cases. Fail-fast on a missing path or schema
    drift. ``n`` deterministically subsamples (seeded) for a cheaper run; ``None`` ⇒ all."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"LongMemEval dataset not found at {path!r} — set HIVE_BENCH_LME_PATH "
            "(longmemeval_s_cleaned.json from xiaowu0162/longmemeval-cleaned)")
    data = json.loads(p.read_text())
    if not isinstance(data, list):
        raise ValueError(f"LongMemEval file {path!r} is not a JSON list of instances")
    cases = [_parse_instance(inst, i) for i, inst in enumerate(data)]
    if n is not None and n < len(cases):
        cases = random.Random(seed).sample(cases, n)
    return cases


def gold_relevant(case: Case, mem_source: dict[str, str]) -> set[str]:
    """Gold-relevant memory ids for the question: every memory whose source session is an
    evidence session. ``mem_source`` maps mem_id → the session_id the fact was extracted from.
    Empty for an abstention question (no evidence) ⇒ it is scored by abstention, not retrieval."""
    ev = case.question.evidence_session_ids
    return {mid for mid, sid in mem_source.items() if sid in ev}


def dev_test_split(cases: list[Case], *, seed: int = 0,
                   dev_frac: float = 0.2) -> tuple[list[Case], list[Case]]:
    """Deterministic dev/test partition. Thresholds + gate prompts are tuned ONLY on dev;
    reported numbers come from test (anti-reward-hacking). Disjoint + exhaustive."""
    if not (0.0 < dev_frac < 1.0):
        raise ValueError(f"dev_frac must be in (0,1), got {dev_frac}")
    shuffled = list(cases)
    random.Random(seed).shuffle(shuffled)
    n_dev = max(1, int(len(shuffled) * dev_frac))
    return shuffled[:n_dev], shuffled[n_dev:]
