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

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from hive.research.bench.backends import Proposal, RecallObs
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
