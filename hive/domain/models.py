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

from hive.domain.kinds import DEFAULT_KIND, KIND_NAMES
from hive.domain.lifecycle import ESTABLISHED, PROVISIONAL, QUARANTINED, TRUST_STATES


def content_hash(text: str) -> str:
    """sha256(text) hex — the dedup/PK key that binds a memory to its text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── repo scope / code binding (schema v3) ────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AnchorRef:
    """One ``(repo, anchor)`` code binding of a memory — a row of the v3
    ``episode_anchors`` table projected into the carrier layer. ``repo`` is a
    registered repo NAME (never a URL/path); ``anchor`` is the code location the
    memory binds to (``path`` or ``path::Symbol``); ``fp_meta`` carries the
    per-anchor fingerprint tokens (serialized, carried-not-interpreted — the
    meta-envelope law).

    Both ``repo`` and ``anchor`` are required non-empty: a repo-scope-only
    membership is NOT an AnchorRef — it is carried by ``Episode.repos`` alone
    (the store encodes it as an ``anchor=''`` row, which the carrier projection
    folds into ``repos`` rather than surfacing as a degenerate anchor)."""
    repo: str
    anchor: str
    fp_meta: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.repo, str) or not self.repo:
            raise ValueError("AnchorRef.repo must be a non-empty repo name")
        if not isinstance(self.anchor, str) or not self.anchor:
            raise ValueError(
                "AnchorRef.anchor must be non-empty (scope-only membership "
                "rides Episode.repos, never a degenerate anchor)")
        if not isinstance(self.fp_meta, str):
            raise ValueError("AnchorRef.fp_meta must be a str (serialized carrier)")


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
    as a ``do``. ``repos`` / ``anchors`` carry the memory's repo partition and code
    bindings (v3 §3.4): ``repos == ()`` reads as a GENERAL memory, ``anchors == ()``
    as general / scope-only. Defaults are FAIL-SAFE: a construction site that
    forgets to wire them under-claims (quarantined/epoch-0/neutral/general), never
    over-claims."""
    episode_id: int
    text: str
    sim: float
    trust: str = QUARANTINED
    ts: int = 0
    polarity: str = "neutral"
    kind: str = DEFAULT_KIND
    repos: tuple[str, ...] = ()        # the repo partition; () = general
    anchors: tuple[AnchorRef, ...] = ()  # code bindings; () = general / scope-only
    meta: str = ""                     # serialized opaque map (meta.py); carried, never parsed


@dataclass(frozen=True, slots=True)
class RecallResult:
    """The frozen result of a recall. ``hits`` is non-empty IFF ``CONFIDENT``;
    ``trace_id`` is ALWAYS present (hit AND abstain) — the move-#6 join key.

    ``conflicts`` is the recall-time conflict carrier: ids-only ``ConflictNote``s detected
    over the PRE-select servable field (so a near-dup the decorrelated serve dropped is still
    surfaced for human resolution — a side-channel, never content). Empty on every non-CONFIDENT
    result and whenever conflict detection is off."""
    state: RecallState
    trace_id: str
    hits: tuple[RecallHit, ...]
    top_cos: float                         # max abs cosine ∈ [-1,1]; 0.0 on EMPTY_NO_DATA
    conflicts: tuple = ()                  # ids-only ConflictNotes over the pre-select field

    @classmethod
    def empty(cls, trace_id: str) -> "RecallResult":
        return cls(EMPTY_NO_DATA, trace_id, (), 0.0)

    @classmethod
    def abstain(cls, trace_id: str, top_cos: float) -> "RecallResult":
        return cls(ABSTAIN, trace_id, (), top_cos)

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
        # a non-CONFIDENT result surfaces no conflict carrier (a conflict needs ≥2 co-present
        # servable hits, only possible on the CONFIDENT path).
        if self.state != CONFIDENT and self.conflicts:
            raise ValueError("only CONFIDENT may carry conflicts (no co-present field otherwise)")
        if not (-1.0 <= self.top_cos <= 1.0):
            raise ValueError("top_cos must be in [-1,1]")


# ── episode (the recall substrate) ───────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Episode:
    """A captured insight. Self-asserting: an inconsistent Episode is
    UNCONSTRUCTABLE — content_hash binds text, value is unit-norm float32[d] (or
    None while pending), and the trust/status/scope invariants hold.

    v3 (repo-partitioned memory): ``status`` reads as *materialized* (scanned +
    embedded + blob complete); ``trust`` carries trust — there is no approver
    concept (the human vouch is deleted; ``established`` means outcome-verified
    on the canonical line). ``repos``/``anchors`` carry the per-memory repo
    partition and code bindings (the v3 ``episode_anchors`` projection):
    ``repos`` is the FULL scope — the sorted, deduplicated union of every
    anchor's repo and any declared scope-only membership — and every anchor's
    repo must appear in it (an episode claiming an anchor outside its own scope
    is unconstructable)."""
    id: int
    tenant_id: str
    text: str
    weight: float
    ts: int
    content_hash: str
    status: str                        # 'pending' | 'approved'  (materialization)
    proposed_by: str
    value: Optional["np.ndarray"] = None
    version: int = 0
    trust: str = QUARANTINED           # fail-safe default: unserved until promoted
    superseded_by: Optional[int] = None   # latest applied successor (None = live)
    last_active_ts: int = 0            # liveness clock: capture/write, promotion, exposure
    polarity: str = "neutral"         # do|dont|neutral — carried-not-interpreted consumer label
    kind: str = DEFAULT_KIND          # category label (kinds.py); carried-not-interpreted, never embedded
    meta: str = ""                    # serialized opaque map — grammar lives at the boundary
                                      # (meta.normalize_meta); carried-not-interpreted, never embedded
    repos: tuple[str, ...] = ()       # sorted unique repo scope; () = general (fail-safe default)
    anchors: tuple[AnchorRef, ...] = ()  # code bindings; every anchor repo ∈ repos

    def __post_init__(self) -> None:
        if self.content_hash != content_hash(self.text):
            raise ValueError("content_hash does not bind text (hash≠sha256(text))")
        if not isinstance(self.meta, str):
            raise ValueError("meta must be a str (the serialized carrier form)")
        if self.polarity not in ("do", "dont", "neutral"):
            raise ValueError(f"bad polarity {self.polarity!r}")
        if self.kind not in KIND_NAMES:
            raise ValueError(f"bad kind {self.kind!r}")
        if self.status not in ("pending", "approved"):
            raise ValueError(f"bad status {self.status!r}")
        if self.trust not in TRUST_STATES:
            raise ValueError(f"bad trust {self.trust!r}")
        if self.trust in (ESTABLISHED, PROVISIONAL) and self.status != "approved":
            raise ValueError(
                "invariant: a servable-trust episode must be status='approved'")
        if self.superseded_by is not None and self.trust != "deprecated":
            raise ValueError(
                "invariant: superseded_by requires trust='deprecated' "
                "(a live row cannot point at a successor)")
        # v3 scope invariants: repos is the sorted unique union of anchor repos and
        # declared scope — a scope that under- or over-states its anchors, or an
        # unsorted/duplicated encoding, is unconstructable (one canonical form).
        if not all(isinstance(r, str) and r for r in self.repos):
            raise ValueError("repos entries must be non-empty repo names")
        if list(self.repos) != sorted(set(self.repos)):
            raise ValueError("repos must be sorted and duplicate-free (canonical form)")
        if not all(isinstance(a, AnchorRef) for a in self.anchors):
            raise ValueError("anchors entries must be AnchorRef carriers")
        anchor_repos = {a.repo for a in self.anchors}
        if not anchor_repos.issubset(set(self.repos)):
            raise ValueError(
                "invariant: repos == sorted(anchor repos ∪ declared scope) — "
                "an anchor's repo must appear in the episode's repo scope")
        if self.value is not None:
            v = self.value
            if getattr(v, "dtype", None) != np.float32:
                raise ValueError("value must be float32")
            if v.ndim != 1:
                raise ValueError("value must be 1-D")
