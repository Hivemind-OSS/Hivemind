"""B4 — Mem0Backend: the MemoryBackend over mem0, the competitor, behind a narrow Mem0Client
swap seam so tests run model-free (FakeMem0Client) and the real arm runs offline.

The orchestrator-in-loop workflow maps onto mem0 (which has NO two-phase write / quarantine):

    propose → HOLD the candidate in a local buffer; nothing is written to mem0 yet, because an
              un-approved subagent fact must not be servable. (Hivemind quarantines; mem0 can't,
              so we hold it outside the store — same observable: un-served until commit.)
    commit  → ``add(infer=False)`` under ONE shared ``user_id`` (mirrors Hivemind's shared store);
              ``infer=False`` keeps it a RAW store — no LLM consolidation — so the number isolates
              the store, not mem0's native fact-extraction. Returns the real mem0 id (the gold key).
    recall  → ``search`` top-k. mem0 has no honest-abstention gate: it returns top-k whenever the
              store is non-empty, so it abstains ONLY on an empty store. ``confidence`` is the
              clamped top score (mem0's native proxy, disclosed in ``RecallObs`` — an embedder-
              dependent separability signal, NOT a calibration claim).
    reset   → drop every memory for the shared user.

Embedder parity: both sides use the SAME base model ``Qwen/Qwen3-Embedding-0.6B`` at its native
1024 dim — Hivemind no longer truncates, so the geometry is identical on both sides. The store is
the only difference under test (the plan's ISOLATED contract).
"""
from __future__ import annotations

import itertools
import tempfile
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from hive.research.bench.backends import Proposal, RecallObs

_SHARED_USER = "shared"


@dataclass(frozen=True)
class Mem0Hit:
    """One mem0 search hit, narrowed to what scoring needs: the memory id and its native score."""
    id: str
    score: float


@runtime_checkable
class Mem0Client(Protocol):
    """The minimal mem0 surface the backend drives — the swap seam. ``FakeMem0Client`` (tests) and
    ``RealMem0Client`` (the offline run) both satisfy it; ``runtime_checkable`` so it's asserted."""

    def add(self, text: str) -> str:
        """Write one memory under the shared user_id; return its id."""
        ...

    def search(self, query: str, *, limit: int) -> list[Mem0Hit]:
        """Best-first hits for ``query`` (≤ ``limit``); empty only when the store is empty."""
        ...

    def reset(self) -> None:
        """Drop every memory for the shared user."""
        ...


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


class Mem0Backend:
    """Orchestrator-in-loop over mem0. ``top_k`` matches Hivemind's recall ``top_n`` so both
    backends are scored over the same top-k window (fair hit@k/recall@k)."""

    def __init__(self, client: Mem0Client, *, top_k: int = 10) -> None:
        self._client = client
        self._top_k = int(top_k)
        self._held: dict[str, str] = {}          # proposal_id → text, awaiting the orchestrator
        self._ids = itertools.count(1)

    def recall(self, seat: str, query: str) -> RecallObs:
        # seat is ignored: mem0 is a flat shared store (single user_id), like Hivemind's shared
        # store seen across seats — the comparison is store-vs-store, not identity-vs-identity.
        hits = self._client.search(query, limit=self._top_k)
        if not hits:
            return RecallObs(query=query, ranked_ids=(), top_score=0.0,
                             confidence=0.0, abstained=True)
        top = float(hits[0].score)
        return RecallObs(query=query, ranked_ids=tuple(h.id for h in hits),
                         top_score=top, confidence=_clamp01(top), abstained=False)

    def propose(self, seat: str, text: str, *, source_id: str) -> Proposal:
        pid = str(next(self._ids))
        self._held[pid] = text                   # held OUT of mem0 until the orchestrator commits
        return Proposal(proposal_id=pid, seat=seat, text=text, source_id=source_id)

    def commit(self, proposal: Proposal, *, approver: str) -> str:
        mid = self._client.add(proposal.text)    # approver has no mem0 analog — recorded by caller
        self._held.pop(proposal.proposal_id, None)
        return str(mid)

    def reset(self) -> None:
        self._client.reset()
        self._held.clear()
        self._ids = itertools.count(1)


class RealMem0Client:
    """The shipping mem0, configured for a fully OFFLINE, zero-API run: a local HuggingFace ST
    embedder (pinned to Hivemind's base model, native 1024), a local file-backed Qdrant, and an UNUSED
    lmstudio LLM slot (keyless, localhost — mem0 builds an LLM eagerly but ``infer=False`` never
    calls it). mem0 is imported lazily so the model-free tests need none of it installed."""

    def __init__(self, *, model: str, dims: int, user_id: str = _SHARED_USER,
                 path: Optional[str] = None, collection: str = "bench") -> None:
        from mem0 import Memory  # noqa: PLC0415 — heavy, dev-time-only optional dep
        self._uid = user_id
        store_path = path or tempfile.mkdtemp(prefix="mem0_bench_")
        self._mem = Memory.from_config({
            "embedder": {"provider": "huggingface",
                         "config": {"model": model, "model_kwargs": {"device": "cpu"},
                                    "embedding_dims": dims}},
            "vector_store": {"provider": "qdrant",
                             "config": {"collection_name": collection, "path": store_path,
                                        "embedding_model_dims": dims, "on_disk": False}},
            "llm": {"provider": "lmstudio"},
        })

    def add(self, text: str) -> str:
        res = self._mem.add(text, user_id=self._uid, infer=False)["results"]
        if not res:
            raise RuntimeError(f"mem0 add(infer=False) returned no memory for {text!r}")
        return str(res[0]["id"])

    def search(self, query: str, *, limit: int) -> list[Mem0Hit]:
        # mem0's search kwarg is ``top_k`` (default 20), NOT ``limit`` — a ``limit=`` is swallowed by
        # its ``**kwargs`` and silently ignored, widening the scored window past the requested k.
        # ``threshold=0.0`` disables mem0's native 0.1 similarity floor so the store serves an
        # unconditional top-k (coverage ~1.0, abstains only when empty) — the comparison contract.
        res = self._mem.search(query, filters={"user_id": self._uid},
                               top_k=limit, threshold=0.0)["results"]
        return [Mem0Hit(id=str(h["id"]), score=float(h["score"])) for h in res]

    def reset(self) -> None:
        self._mem.delete_all(user_id=self._uid)
