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
from typing import Protocol, runtime_checkable

from hive.research.bench.backends import Proposal, RecallObs


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
