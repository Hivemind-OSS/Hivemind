"""The backend-swap contract: one ``MemoryBackend`` Protocol both Hivemind and mem0
satisfy, so the identical orchestrator/subagent loop scores both on equal footing.

The surface is deliberately the orchestrator-in-loop workflow, NOT a bare key-value store:

    recall(seat, query)            — a subagent READS shared memory before proposing.
    propose(seat, text, source_id) — a subagent WRITES a candidate fact (genuine write;
                                     Hivemind → quarantined capture, mem0 → a held proposal).
    commit(proposal, approver)     — the orchestrator APPROVES it into the servable store
                                     (Hivemind → hive_write, which post-B0 establishes the
                                     quarantined capture IN PLACE; mem0 → add). DENY = never commit.
    reset()                        — drop all state for the next arm/seed.

``source_id`` threads the originating session/turn through ``propose`` so the harness can map
``mem_id → source_session`` for gold-relevance scoring without the backend knowing about gold.
Implementations live in B3 (``HivemindBackend``) and B4 (``Mem0Backend``); the Protocol is
``runtime_checkable`` so conformance is asserted, not assumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RecallObs:
    """One backend-agnostic recall observation — everything scoring needs, nothing it doesn't.

    ``ranked_ids``  servable memory ids, best-first (Hivemind ``episode_id`` / mem0 id, as str).
    ``top_score``   the top hit's native similarity (0.0 when none); mem0's abstention proxy.
    ``confidence``  native abstention signal in [0,1], HIGHER = more confident = less likely to
                    abstain. Hivemind: ``top_cos`` (the top absolute cosine). mem0: ``top_score``. The
                    cross-backend asymmetry is intentional and disclosed — this is "each
                    system's native abstention separability", NOT a calibration claim.
    ``abstained``   the system returned no usable answer (Hivemind gate fired / mem0 empty).
    """
    query: str
    ranked_ids: tuple[str, ...]
    top_score: float
    confidence: float
    abstained: bool

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        if self.abstained and self.ranked_ids:
            raise ValueError("an abstained observation cannot carry ranked_ids")


@dataclass(frozen=True)
class Proposal:
    """A subagent's staged candidate fact, awaiting the orchestrator's approve/deny.

    ``proposal_id`` is a backend-local handle (Hivemind: the quarantined ``episode_id`` as
    str; mem0: a held-buffer key). ``source_id`` is the session/turn the fact derives from —
    carried so ``commit`` can return a ``mem_id`` the harness maps back to gold evidence."""
    proposal_id: str
    seat: str
    text: str
    source_id: str


@runtime_checkable
class MemoryBackend(Protocol):
    """Read / subagent-write / orchestrator-approve / teardown. Both products satisfy it; the
    deny path is simply "never ``commit`` the proposal" (no method — cheap and uniform)."""

    def recall(self, seat: str, query: str) -> RecallObs:
        """Servable memories for ``query`` under seat identity ``seat`` (a subagent read)."""
        ...

    def propose(self, seat: str, text: str, *, source_id: str) -> Proposal:
        """A subagent writes a candidate fact (un-served until the orchestrator commits it)."""
        ...

    def commit(self, proposal: Proposal, *, approver: str) -> str:
        """The orchestrator approves a proposal into the servable store; returns its ``mem_id``."""
        ...

    def reset(self) -> None:
        """Drop every memory + proposal so the next arm/seed starts from an empty store."""
        ...
