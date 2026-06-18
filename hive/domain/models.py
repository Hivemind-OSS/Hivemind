"""Frozen value carriers for the pure domain core.

PURE: stdlib + math only. The purity gate (tests/test_purity.py) forbids
sqlite3 | torch | subprocess | os | git | time imports anywhere in hive/domain/.
numpy is permitted but deliberately not imported here (these carriers are scalar).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

import numpy as np

from hive.domain.lifecycle import ESTABLISHED, PROVISIONAL, QUARANTINED, TRUST_STATES


def content_hash(text: str) -> str:
    """sha256(text) hex — the dedup/PK key that binds a memory to its text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── recall-side carriers ─────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Scored:
    """A gate-passed recall candidate. ``weight`` is the immutable capture
    salience — the surfacer's base multiplier (NEVER ``sim``/``alpha``) [A1].

    Note (P1.5): the per-episode ``recall_margin`` the contract lists is NOT a
    field here — it is a pipeline-computed quantity (its true owner per D1), a
    parallel list zipped with the hits at exposure time. Keeping it off ``Scored``
    avoids a vacuous default on the surfacer's input and keeps ownership honest."""
    episode_id: int
    weight: float
    sim: float


# RecallState / RecallHit / RecallResult — the recall surface.
# abstain-no-resurrect is made STRUCTURAL on RecallResult: a non-CONFIDENT result
# carrying hits is UNCONSTRUCTABLE (__post_init__ raises), so no fallback can
# repopulate a refused query.
RecallState = str  # Literal["CONFIDENT", "ABSTAIN", "EMPTY_NO_DATA"] at the boundary

CONFIDENT = "CONFIDENT"
ABSTAIN = "ABSTAIN"
EMPTY_NO_DATA = "EMPTY_NO_DATA"


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One surfaced memory handed to the agent (framed under ``reference_context``).

    ``trust`` + ``ts`` label every hit so a consumer can discount provisional
    content and order coexisting versions (immutable rows + dedup mean an "update"
    always coexists as a second row). ``polarity`` (do|dont|neutral) is a third
    carried-not-interpreted label so a recalled prohibition is never pattern-matched
    as a ``do``. Defaults are FAIL-SAFE: a construction site that forgets to wire
    them under-claims (quarantined/epoch-0/neutral), never over-claims."""
    episode_id: int
    text: str
    sim: float
    trust: str = QUARANTINED
    ts: int = 0
    polarity: str = "neutral"


@dataclass(frozen=True, slots=True)
class RecallDraft:
    """One UNPROMOTED quarantined capture handed back to its OWN writer as a labeled
    draft (the self-quarantine resurfacing channel) — strictly separate from the
    trusted ``RecallHit``. A DISTINCT type so it can never be merged into the
    never-hallucinate ``hits`` channel; ``trust`` defaults to QUARANTINED (it always
    rides on the quarantined channel) and ``ts`` orders coexisting captures."""
    episode_id: int
    text: str
    sim: float
    trust: str = QUARANTINED
    ts: int = 0


@dataclass(frozen=True, slots=True)
class RecallAssoc:
    """One co-accessed neighbor of a served hit — the associative-recall channel.
    *Related*, NOT an *answer*: a memory frequently served TOGETHER with a trusted
    hit, surfaced on a SEPARATE channel so a co-access-popular but dense-irrelevant
    row never enters the ranked ``hits``. A DISTINCT type from ``RecallHit`` /
    ``RecallDraft`` so it can NEVER be merged into the trusted channel. ``weight`` is
    the co-access edge weight (co-occurrence count); ``trust``/``ts`` label the
    neighbor row (fail-safe defaults: under-claim, never over-claim)."""
    episode_id: int
    text: str
    weight: float
    trust: str = QUARANTINED
    ts: int = 0


@dataclass(frozen=True, slots=True)
class RecallResult:
    """The frozen result of a recall. ``hits`` is non-empty IFF ``CONFIDENT``;
    ``trace_id`` is ALWAYS present (hit AND abstain) — the move-#6 join key.

    ``drafts`` is the self-quarantine resurfacing side-channel — a seat's own
    unpromoted quarantined captures, labeled. It is deliberately UNCONSTRAINED by
    ``state`` (it may ride ABSTAIN/EMPTY/CONFIDENT alike): the never-hallucinate
    biconditional below binds ``hits`` ONLY, so a populated ``drafts`` on an
    abstained result is by design, never a resurrected trusted answer.

    ``associations`` is the co-access neighbor channel — *related* memories, never
    *answers*. Like ``drafts`` it is unconstrained by the biconditional (which binds
    ``hits`` only); in practice it is populated on CONFIDENT only (neighbors attach to
    hits), so ``empty()``/``abstain()`` keep it absent."""
    state: RecallState
    trace_id: str
    hits: tuple[RecallHit, ...]
    entropy_norm: float                    # H/ln(N_eff) ∈ [0,1]; 0.0 on EMPTY_NO_DATA
    top_margin: float
    drafts: tuple["RecallDraft", ...] = ()
    associations: tuple["RecallAssoc", ...] = ()

    @classmethod
    def empty(cls, trace_id: str, drafts: tuple["RecallDraft", ...] = ()) -> "RecallResult":
        return cls(EMPTY_NO_DATA, trace_id, (), 0.0, 0.0, drafts)

    @classmethod
    def abstain(cls, trace_id: str, h_norm: float, margin: float,
                drafts: tuple["RecallDraft", ...] = ()) -> "RecallResult":
        return cls(ABSTAIN, trace_id, (), h_norm, margin, drafts)

    def __post_init__(self) -> None:
        if self.state not in (CONFIDENT, ABSTAIN, EMPTY_NO_DATA):
            raise ValueError(f"bad recall state {self.state!r}")
        # the CONFIDENT<->has-hits biconditional, BOTH directions structural:
        #  - only CONFIDENT may carry hits (abstain-no-resurrect), AND
        #  - CONFIDENT must carry ≥1 hit (no empty-confident — the reverse resurrect).
        if self.state != CONFIDENT and self.hits:
            raise ValueError("only CONFIDENT may carry hits (abstain-no-resurrect)")
        if self.state == CONFIDENT and not self.hits:
            raise ValueError("CONFIDENT must carry ≥1 hit (CONFIDENT<->has-hits biconditional)")
        if not (0.0 <= self.entropy_norm <= 1.0):
            raise ValueError("entropy_norm must be in [0,1]")


# ── episode (the recall substrate) ───────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Episode:
    """A captured insight. Self-asserting: an inconsistent Episode is
    UNCONSTRUCTABLE — content_hash binds text, value is unit-norm float32[d] (or
    None while pending), and the trust/status/approver invariants hold.

    v2 (trust lifecycle): ``status`` now reads as *materialized* (scanned +
    embedded + blob complete); ``trust`` carries trust. The old biconditional
    ``approved ⇔ approved_by`` relaxes to implications so the autonomous-capture
    shape (approved + no approver + quarantined) is constructable while a lying
    row still is not."""
    id: int
    tenant_id: str
    text: str
    weight: float
    ts: int
    source: str
    tags: str
    content_hash: str
    status: str                        # 'pending' | 'approved'  (materialization)
    proposed_by: str
    value: Optional["np.ndarray"] = None
    approved_by: Optional[str] = None
    approved_ts: Optional[int] = None
    version: int = 0
    trust: str = QUARANTINED           # fail-safe default: unserved until vouched/promoted
    superseded_by: Optional[int] = None   # latest applied successor (None = live)
    last_active_ts: int = 0            # liveness clock: capture/write, promotion, exposure
    polarity: str = "neutral"         # do|dont|neutral — carried-not-interpreted consumer label

    def __post_init__(self) -> None:
        if self.content_hash != content_hash(self.text):
            raise ValueError("content_hash does not bind text (hash≠sha256(text))")
        if self.polarity not in ("do", "dont", "neutral"):
            raise ValueError(f"bad polarity {self.polarity!r}")
        if self.status not in ("pending", "approved"):
            raise ValueError(f"bad status {self.status!r}")
        if self.trust not in TRUST_STATES:
            raise ValueError(f"bad trust {self.trust!r}")
        if self.approved_by is not None and self.status != "approved":
            raise ValueError("invariant: approved_by set requires status='approved'")
        if self.trust in (ESTABLISHED, PROVISIONAL) and self.status != "approved":
            raise ValueError(
                "invariant: a servable-trust episode must be status='approved'")
        if self.superseded_by is not None and self.trust != "deprecated":
            raise ValueError(
                "invariant: superseded_by requires trust='deprecated' "
                "(a live row cannot point at a successor)")
        if self.value is not None:
            v = self.value
            if getattr(v, "dtype", None) != np.float32:
                raise ValueError("value must be float32")
            if v.ndim != 1:
                raise ValueError("value must be 1-D")
