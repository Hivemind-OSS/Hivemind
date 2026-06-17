"""B5c: the orchestrator-in-loop driver. extract_facts is the subagent's LLM distillation of a
session (through the single seam); run_ingestion is the deterministic read→propose→gate→commit
loop that builds the shared store and records mem_id→source_session for gold scoring; run_queries
asks the store. The loop is tested over a faithful in-memory backend (propose holds, commit serves)
with the real gate policies — no model, no network."""
from __future__ import annotations

import pytest

from hive.research.bench.backends import Proposal, RecallObs
from hive.research.bench.dataset import Case, Question, Session, Turn, gold_relevant
from hive.research.bench.llm import FakeLLM
from hive.research.bench.orchestrator import (
    AllowAllGate, GateContext, IngestionTrace, OracleEvidenceGate,
    extract_facts, run_ingestion, run_queries,
)


# ── a faithful, model-free MemoryBackend (propose HOLDS, commit SERVES) ─────────
class _FakeBackend:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._held: dict[str, str] = {}
        self._n = 0
        self.recalls: list[tuple[str, str]] = []

    def recall(self, seat: str, query: str) -> RecallObs:
        self.recalls.append((seat, query))
        q = set(query.lower().split())
        hits = [mid for mid, txt in self._store.items() if q & set(txt.lower().split())]
        if not hits:
            return RecallObs(query=query, ranked_ids=(), top_score=0.0, confidence=0.0, abstained=True)
        return RecallObs(query=query, ranked_ids=tuple(hits), top_score=0.5, confidence=0.5, abstained=False)

    def propose(self, seat: str, text: str, *, source_id: str) -> Proposal:
        self._n += 1
        pid = str(self._n)
        self._held[pid] = text
        return Proposal(proposal_id=pid, seat=seat, text=text, source_id=source_id)

    def commit(self, proposal: Proposal, *, approver: str) -> str:
        mid = f"m{proposal.proposal_id}"
        self._store[mid] = proposal.text
        self._held.pop(proposal.proposal_id, None)
        return mid

    def reset(self) -> None:
        self._store.clear(); self._held.clear(); self._n = 0


_FACTS = [("s1", ["a durable fact from s1", "another fact from s1"]), ("s2", ["a durable fact from s2"])]


def _session(sid="s7", *turns) -> Session:
    turns = turns or (Turn("user", "we use redis for caching", False),
                      Turn("assistant", "noted", False))
    return Session(session_id=sid, date="2023/05/01", turns=tuple(turns))


# ── extract_facts (subagent distillation through the seam) ──────────────────────
def test_extract_parses_lines_strips_bullets_and_caps():
    llm = FakeLLM(lambda p, s: "1. the db is postgres\n- the port is 5432\n\n* TLS is required\n")
    assert extract_facts(llm, _session(), max_facts=2) == ["the db is postgres", "the port is 5432"]


def test_extract_drops_blank_lines():
    llm = FakeLLM(lambda p, s: "fact a\n\n   \n\nfact b")
    assert extract_facts(llm, _session()) == ["fact a", "fact b"]


def test_extract_sends_transcript_and_session_id_with_a_system_prompt():
    seen = []
    llm = FakeLLM(lambda p, s: seen.append((p, s)) or "f")
    extract_facts(llm, _session("s7", Turn("user", "the queue is rabbitmq", False)))
    prompt, system = seen[0]
    assert "the queue is rabbitmq" in prompt and "s7" in prompt and system


def test_extract_empty_reply_yields_no_facts():
    assert extract_facts(FakeLLM(), _session()) == []


# ── run_ingestion (read → propose → gate → commit) ─────────────────────────────
def test_allow_all_commits_every_fact_and_maps_each_to_its_source():
    b = _FakeBackend()
    tr = run_ingestion(b, AllowAllGate(), _FACTS, ["sub-a"])
    assert isinstance(tr, IngestionTrace)
    assert tr.proposed == 3 and tr.approved == 3 and len(tr.mem_source) == 3
    assert set(tr.mem_source.values()) == {"s1", "s2"}
    assert b.recall("sub-a", "a durable fact from s1").abstained is False    # committed ⇒ servable


def test_oracle_commits_only_evidence_sourced_facts():
    b = _FakeBackend()
    tr = run_ingestion(b, OracleEvidenceGate({"s1"}), _FACTS, ["sub-a"])
    assert tr.proposed == 3 and tr.approved == 2
    assert set(tr.mem_source.values()) == {"s1"}                             # s2 facts never committed


def test_denied_facts_absent_from_trace_and_unservable():
    class _DropAll:
        def decide(self, p, ctx): return False
    b = _FakeBackend()
    tr = run_ingestion(b, _DropAll(), _FACTS, ["sub-a"])
    assert tr.approved == 0 and tr.mem_source == {}
    assert b.recall("sub-a", "a durable fact from s1").abstained is True


def test_round_robin_assigns_a_seat_per_session():
    seen = []
    class _Rec:
        def decide(self, p, ctx): seen.append(ctx.seat); return True
    four = [("s1", ["f"]), ("s2", ["f"]), ("s3", ["f"]), ("s4", ["f"])]
    run_ingestion(_FakeBackend(), _Rec(), four, ["sub-a", "sub-b"])
    assert seen == ["sub-a", "sub-b", "sub-a", "sub-b"]


def test_subagent_reads_before_it_proposes():
    b = _FakeBackend()
    run_ingestion(b, AllowAllGate(), [("s1", ["xyzzy plugh"])], ["sub-a"])
    assert ("sub-a", "xyzzy plugh") in b.recalls            # a recall preceded the proposal


def test_no_seats_raises():
    with pytest.raises(ValueError, match="seat"):
        run_ingestion(_FakeBackend(), AllowAllGate(), _FACTS, [])


def test_trace_feeds_gold_relevant():
    b = _FakeBackend()
    tr = run_ingestion(b, AllowAllGate(), _FACTS, ["sub-a"])
    case = Case(question=Question(question_id="q", question_type="t", text="?", answer="a",
                                  date="d", is_unanswerable=False,
                                  evidence_session_ids=frozenset({"s1"})),
                sessions=())
    gold = gold_relevant(case, tr.mem_source)
    assert gold == {mid for mid, sid in tr.mem_source.items() if sid == "s1"} and len(gold) == 2


# ── run_queries ─────────────────────────────────────────────────────────────────
def test_run_queries_returns_one_obs_per_question():
    b = _FakeBackend()
    run_ingestion(b, AllowAllGate(), [("s1", ["redis listens on 6379"])], ["sub-a"])
    obs = run_queries(b, ["what port does redis use", "wholly unrelated zzz"], seat="reader")
    assert len(obs) == 2 and obs[0].abstained is False and obs[1].abstained is True
    assert ("reader", "what port does redis use") in b.recalls
