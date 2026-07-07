"""The orchestrator-in-loop seam: a ``GatePolicy`` decides approve/deny for each subagent
proposal, and the driver (B5) wires subagents → propose → gate → commit over a dataset.

Three policies (B5) bracket the curation value floor↔ceiling:
    AllowAllGate         — approve everything (the no-curation floor).
    LLMOrchestratorGate  — a Claude-subscription LLM judges keep/drop (the realistic arm).
    OracleEvidenceGate   — approve iff the fact derives from a gold-evidence session (the
                           perfect-curation ceiling; a labelled control, never a product number).

Only the LLM policy makes an LLM call, and it does so through the single
``ClaudeSubscriptionLLM`` seam (B5) — no raw API. Every decision is logged for replay so a
re-run reproduces bit-for-bit despite the LLM. This module holds only the contract; the
policies and ``run_ingestion``/``run_queries`` driver land in B5.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, runtime_checkable

from hive.research.bench.backends import MemoryBackend, Proposal, RecallObs
from hive.research.bench.dataset import Session
from hive.research.bench.llm import LLM


@dataclass(frozen=True)
class GateContext:
    """What a gate may see about a proposal beyond its text. ``prior`` is what the shared
    store already returns for this fact (so a gate can drop a redundant capture); ``seat`` is
    the proposing subagent. Evidence labels are NOT here — the Oracle gate owns its own label
    map so no other policy can peek at gold (anti-reward-hacking)."""
    prior: RecallObs
    seat: str


@runtime_checkable
class GatePolicy(Protocol):
    """Approve (True) or deny (False) one proposal. Pure w.r.t. the store — it only reads the
    proposal + context; the driver performs the commit on True. Deterministic policies must be
    reproducible; the LLM policy reproduces via its decision log."""

    def decide(self, proposal: Proposal, ctx: GateContext) -> bool:
        ...


# ── the three gate policies (floor ↔ ceiling) ──────────────────────────────────

class AllowAllGate:
    """The no-curation FLOOR: approve every proposal. Isolates the value the gate adds — any
    arm above this is the gate earning its keep."""

    def decide(self, proposal: Proposal, ctx: GateContext) -> bool:
        return True


class OracleEvidenceGate:
    """The perfect-curation CEILING: approve iff the fact derives from a gold-evidence session.
    It peeks at the answer key (``evidence`` = the current question's evidence-session ids), so it
    is a labelled control reported as an upper bound — NEVER a product number."""

    def __init__(self, evidence) -> None:
        self._evidence = frozenset(str(s) for s in evidence)

    def decide(self, proposal: Proposal, ctx: GateContext) -> bool:
        return proposal.source_id in self._evidence


# Frozen, versioned templates — the gate prompt is a fixed contract, never tuned on reported
# questions (anti-reward-hacking). Bump the version to change behavior.
_GATE_PROMPT_VERSION = "gate.v1"
_GATE_SYSTEM = (
    "You are the curator of a shared team memory. Approve a candidate fact only if it is durable, "
    "non-trivial, and likely to help a teammate later; reject transient, vague, or redundant ones. "
    "Reply with exactly one word: KEEP or DROP.")
_GATE_PROMPT = (
    "Candidate fact proposed by {seat}:\n{text}\n\n"
    "Shared memory already {prior}. Keep this fact in shared memory? Answer KEEP or DROP.")


class LLMOrchestratorGate:
    """The realistic ARM: a Claude-subscription LLM judges KEEP/DROP through the single LLM seam.
    FAIL-SAFE to DROP on an ambiguous verdict — only an unmistakable KEEP approves, so an unclear
    reply can never pollute shared memory. Every decision is appended to ``log`` (if given) for
    audit + replay alongside the LLM's own call log."""

    version = _GATE_PROMPT_VERSION

    def __init__(self, llm: LLM, *, log: Optional[list] = None) -> None:
        self._llm = llm
        self._log = log

    def decide(self, proposal: Proposal, ctx: GateContext) -> bool:
        prior = "has a closely related memory" if not ctx.prior.abstained else "has nothing similar"
        prompt = _GATE_PROMPT.format(seat=ctx.seat, text=proposal.text, prior=prior)
        response = self._llm.complete(prompt, system=_GATE_SYSTEM)
        u = response.upper()
        decision = ("KEEP" in u) and ("DROP" not in u)
        if self._log is not None:
            self._log.append({"proposal_id": proposal.proposal_id, "seat": ctx.seat,
                              "text": proposal.text, "response": response, "decision": decision})
        return decision


# ── fact extraction (subagent distillation, through the single LLM seam) ────────

_EXTRACT_VERSION = "extract.v1"
_EXTRACT_SYSTEM = (
    "You extract durable, atomic facts from a conversation for a shared team memory. Output one "
    "self-contained fact per line — no numbering, no bullets, no commentary. Skip pleasantries and "
    "anything not worth remembering later. If nothing is worth keeping, output nothing.")
_EXTRACT_PROMPT = "Conversation (session {sid}, {date}):\n{transcript}\n\nFacts:"

_BULLET_RX = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def _clean_fact(line: str) -> str:
    return _BULLET_RX.sub("", line).strip()


def extract_facts(llm: LLM, session: Session, *, max_facts: int = 8) -> list[str]:
    """A subagent distills a session into ≤``max_facts`` atomic facts via the single LLM seam.
    Deterministic given the LLM (whose cache + replay-log make re-runs free and bit-stable). The
    prompt is frozen + versioned — never tuned on reported questions (anti-reward-hacking)."""
    transcript = "\n".join(f"{t.role}: {t.content}" for t in session.turns)
    prompt = _EXTRACT_PROMPT.format(sid=session.session_id, date=session.date, transcript=transcript)
    reply = llm.complete(prompt, system=_EXTRACT_SYSTEM)
    facts = [_clean_fact(line) for line in reply.splitlines()]
    return [f for f in facts if f][:max_facts]


# ── the orchestrator-in-loop ingestion driver ──────────────────────────────────

@dataclass(frozen=True)
class IngestionTrace:
    """What a run needs from ingestion: which committed memory came from which source session (so
    gold-relevance is computed without the backend ever seeing gold), plus the proposed/approved
    counts that expose how aggressively the gate curated."""
    mem_source: dict[str, str]      # committed mem_id → source session_id
    proposed: int
    approved: int


def run_ingestion(backend: MemoryBackend, gate: "GatePolicy",
                  sessions_facts: Sequence[tuple[str, Sequence[str]]],
                  seats: Sequence[str]) -> IngestionTrace:
    """Drive read→propose→gate→commit over pre-extracted facts. Each session is handled by one
    subagent seat (round-robin); for every candidate the subagent RECALLs first (the read the
    topology requires, and the gate's redundancy signal), then proposes; the orchestrator gate
    decides; an approved proposal is committed and its mem_id mapped to its source session. The
    order is fixed, so reproducibility comes from the loop itself — no seed needed."""
    if not seats:
        raise ValueError("run_ingestion needs ≥1 subagent seat")
    mem_source: dict[str, str] = {}
    proposed = approved = 0
    for i, (session_id, facts) in enumerate(sessions_facts):
        seat = seats[i % len(seats)]
        for fact in facts:
            prior = backend.recall(seat, fact)
            proposal = backend.propose(seat, fact, source_id=session_id)
            proposed += 1
            if not proposal.proposal_id:        # backend refused to STAGE it (e.g. secret scan)
                continue
            if gate.decide(proposal, GateContext(prior=prior, seat=seat)):
                mem_id = backend.commit(proposal, approver="orchestrator")
                if not mem_id:                  # write refused at COMMIT time ⇒ never stored
                    continue
                mem_source[mem_id] = session_id
                approved += 1
    return IngestionTrace(mem_source=mem_source, proposed=proposed, approved=approved)


def run_queries(backend: MemoryBackend, questions: Sequence[str], *, seat: str) -> list[RecallObs]:
    """Ask the already-ingested store each question under one reader seat — one RecallObs each, in
    order, ready for scoring."""
    return [backend.recall(seat, q) for q in questions]
