"""The pure conflict/redundancy detector — mechanical surfacing, NEVER resolution.

Two memories that co-exist as *servable* rows have, by construction, no recorded
relation between them (a superseded row is deprecated → not servable → never seen).
This module turns the labels already on every Episode (``polarity``, ``anchor``, the
embedding) into one of two mechanical classes over a near-dup (cosine ≥ τ) scan:

  - OPPOSING polarity (do ↔ dont)  → ``contradiction`` (symmetric, no winner)
  - compatible polarity            → ``redundancy``    (+ a directional ``loser_hint``)

This is detection only (THEORY I1/I2, in-domain). It REFUSES automatic resolution
(THEORY §10 O7): the ``loser_hint`` is a HINT a human applies via ``hive_supersede`` /
``hive_write(replaces=)`` — it is never acted on here. Law 3 keeps retirement
human-vouched.

PURE: stdlib ``math`` + numpy only. The purity gate (tests/test_purity.py) forbids
sqlite3 | torch | subprocess | os | git | time imports anywhere in hive/domain/.
The detector is total and fail-closed: an undecidable (non-finite / shape-mismatch /
zero-norm) pair is SKIPPED, never counted as a conflict.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import numpy as np  # permitted in domain (compute, not I/O)

from hive.domain.errors import SecretRefused
from hive.domain.secret_scan import REDACT, REFUSE

_log = logging.getLogger("hive.conflict")

# the mechanical relation vocabulary (distinct from the advisory ``kind`` vocabulary
# {conflict, supersedes} on ConflictFlag — one documented mapping, THEORY decision 10).
CONTRADICTION = "contradiction"
REDUNDANCY = "redundancy"
_RELATIONS = (CONTRADICTION, REDUNDANCY)

# trust ranking for the redundancy loser hint: a human-vouched memory outranks a
# demand-promoted one (established > provisional); anything else under-ranks (0).
_TRUST_RANK = {"established": 2, "provisional": 1}


@dataclass(frozen=True, slots=True)
class ConflictItem:
    """One servable memory's conflict-relevant projection: the vector to compare and
    the carried labels that classify a near-dup pair. ``trust`` is established|provisional
    (only servable rows enter detection). Built by the app layer from the eps it already
    holds (recall belt) or from ``scan_servable_labeled`` (offline report)."""
    episode_id: int
    vector: "np.ndarray"
    polarity: str            # do | dont | neutral
    anchor: str
    ts: int
    trust: str               # established | provisional


@dataclass(frozen=True, slots=True)
class ConflictNote:
    """One detected relation between two co-present servable memories. Frozen +
    self-asserting: ``a_id < b_id`` canonical, ``relation`` in the vocabulary, ``cosine``
    finite in [-1, 1]. ``loser_hint`` is the directional retire-candidate for a
    ``redundancy`` (the lower-(trust, ts, id) id) and ``None`` for a symmetric
    ``contradiction`` — a HINT, never applied here."""
    a_id: int                # canonical: a_id < b_id
    b_id: int
    relation: str            # contradiction | redundancy
    cosine: float            # finite, in [-1, 1]
    anchor: str              # the shared anchor if both equal, else ""
    loser_hint: Optional[int]

    def __post_init__(self) -> None:
        if self.relation not in _RELATIONS:
            raise ValueError(f"bad relation {self.relation!r}")
        if not self.a_id < self.b_id:
            raise ValueError(f"ConflictNote ids must be canonical a_id<b_id "
                             f"(got {self.a_id}, {self.b_id})")
        if not (math.isfinite(self.cosine) and -1.0 <= self.cosine <= 1.0):
            raise ValueError(f"cosine must be finite in [-1, 1] (got {self.cosine})")


def _cosine(a, b) -> Optional[float]:
    """True cosine in float64, or None when UNDECIDABLE — non-finite components, shape
    mismatch, or a zero norm. None is the fail-closed signal: an undecidable similarity
    must never count as a conflict (mirrors lifecycle._cosine).  // O(d)."""
    if a is None or b is None:
        return None
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    if va.shape != vb.shape or va.ndim != 1:
        return None
    if not (np.all(np.isfinite(va)) and np.all(np.isfinite(vb))):
        return None
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na <= 0.0 or nb <= 0.0:
        return None
    return float(np.dot(va, vb) / (na * nb))


def _opposing(p: str, q: str) -> bool:
    """True iff the two polarities are a do↔dont prescription/prohibition clash. neutral
    is compatible with everything (no opposition)."""
    return (p == "do" and q == "dont") or (p == "dont" and q == "do")


def _loser_hint(x: ConflictItem, y: ConflictItem) -> int:
    """The redundancy retire-candidate: the LOWER-ranked of the pair, where rank is
    (trust, ts, id) — keep the higher-trust, then newer, then higher-id row; the other
    is the loser. Total + deterministic (ids differ ⇒ keys differ)."""
    kx = (_TRUST_RANK.get(x.trust, 0), x.ts, x.episode_id)
    ky = (_TRUST_RANK.get(y.trust, 0), y.ts, y.episode_id)
    return x.episode_id if kx < ky else y.episode_id


def suppression_targets(notes: Sequence[ConflictNote],
                        trust_by_id: dict[int, str]) -> frozenset[int]:
    """The episode ids a serve-time filter may PRUNE: for each detected note, the member
    whose ``_TRUST_RANK`` is STRICTLY lower than its partner's (a provisional loses to an
    established). A tie — equal trust, or two unknown-equal ranks — yields NO target: geometry
    cannot decide which co-present fact is true, so the strict-dominance rule refuses to
    coin-flip one away (that stays the human ``hive_supersede`` / worklist path). The target is
    read from ``trust_by_id`` ONLY, never the note's ``loser_hint`` (which breaks ts/id ties the
    filter must not act on). Pure, total, never raises.  // O(notes)."""
    drop: set[int] = set()
    for note in notes:
        ra = _TRUST_RANK.get(trust_by_id.get(note.a_id, ""), 0)
        rb = _TRUST_RANK.get(trust_by_id.get(note.b_id, ""), 0)
        if ra < rb:
            drop.add(note.a_id)
        elif rb < ra:
            drop.add(note.b_id)
        # ra == rb ⇒ undecidable by trust ⇒ no target (human/worklist path)
    return frozenset(drop)


def detect_conflicts(items: Sequence[ConflictItem], *, tau: float,
                     top_n: int = 10) -> tuple[ConflictNote, ...]:
    """All pairs i<j whose cosine ≥ ``tau``, classified by polarity: opposing (do↔dont)
    → contradiction; else → redundancy (+ a directional loser_hint). Pure cosine in
    float64; an undecidable pair (non-finite / shape / zero-norm) is SKIPPED (fail-closed,
    never a fabricated conflict). Output is deterministic — sorted by (cosine desc, a_id,
    b_id) — and capped at ``top_n``. Degenerate input (empty / single / all-below-τ) → ().
    Total, never raises.  // O(n²·d) time, O(pairs) space."""
    if not (math.isfinite(tau)) or top_n < 1:
        return ()
    scored: list[tuple[float, ConflictNote]] = []
    n = len(items)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = items[i], items[j]
            if a.episode_id == b.episode_id:
                continue                              # defensive: same row, no self-pair
            c = _cosine(a.vector, b.vector)
            if c is None:
                continue                              # undecidable ⇒ fail-closed skip
            c = max(-1.0, min(1.0, c))                # float-error clamp into [-1, 1]
            if c < tau:
                continue
            lo, hi = (a, b) if a.episode_id < b.episode_id else (b, a)
            if _opposing(a.polarity, b.polarity):
                relation, loser = CONTRADICTION, None
            else:
                relation, loser = REDUNDANCY, _loser_hint(lo, hi)
            anchor = lo.anchor if lo.anchor == hi.anchor else ""
            scored.append((c, ConflictNote(lo.episode_id, hi.episode_id, relation, c,
                                           anchor, loser)))
    scored.sort(key=lambda t: (-t[0], t[1].a_id, t[1].b_id))
    return tuple(note for _c, note in scored[:top_n])


# ── the advisory-flag domain service (Layer 2 — untrusted, never retires) ───────

@dataclass(frozen=True, slots=True)
class FlagResult:
    """The result of an advisory ``flag``. ``status`` is ``flagged`` (recorded or already
    present) or ``disabled`` (the feature is off — nothing written). ``recorded`` is True
    only when a NEW row landed (an idempotent re-flag is ``flagged`` + ``recorded=False``).
    ``kind`` echoes the advisory kind, or None when disabled."""
    status: str              # "flagged" | "disabled"
    recorded: bool
    kind: Optional[str]


class ConflictFlagService:
    """Records an UNTRUSTED advisory marker that two memories conflict (symmetric) or one
    supersedes the other (directional) — the semantic cases the mechanical labels can't reach
    (embedding-distant, different anchor). It NEVER retires anything: resolution stays the
    human ``hive_supersede`` / ``hive_write(replaces=)`` path (Law 3, THEORY §10 O7). It is
    structurally incapable of retirement — it holds only a ``ConflictFlagStore`` (write seam),
    an ``EpisodeReader`` (id-exists), a ``SecretScanner``, and a clock; no supersede/set_trust
    handle exists to call.

    Fail direction: validation rejects (raises) BEFORE any persistence, so an invalid flag
    leaves zero rows. Disabled ⇒ inert. PURE domain (ports + clock only)."""

    def __init__(self, *, flag_store, scanner, reader, now: Callable[[], int],
                 enabled: bool = False) -> None:
        self._flags = flag_store          # ConflictFlagStore (record_conflict_flag)
        self._scanner = scanner           # SecretScanner (scan the resolution text)
        self._reader = reader             # EpisodeReader (both ids must exist)
        self._now = now
        self.enabled = bool(enabled)

    def flag(self, *, kind: str, a_id: int, b_id: int, winner_id: Optional[int],
             resolution: str, proposed_by: str, request_id: str = "-") -> FlagResult:
        """Validate → scan resolution → canonicalize → record ONE advisory row. Raises
        ValueError on a self-pair / unknown episode / a ``supersedes`` without a winner in
        the pair (nothing written); raises SecretRefused if the resolution carries a
        credential (nothing written). ``conflict`` is symmetric (any winner is dropped to
        None). Idempotent on the canonical (a_id, b_id, kind)."""
        if not self.enabled:
            return FlagResult("disabled", recorded=False, kind=None)
        a_id, b_id = int(a_id), int(b_id)
        if a_id == b_id:
            raise ValueError("cannot flag an episode against itself")
        if kind not in ("conflict", "supersedes"):
            raise ValueError(f"bad flag kind {kind!r}")
        # both episodes must EXIST before anything is persisted (no half state)
        if self._reader.get_episode(a_id) is None or self._reader.get_episode(b_id) is None:
            raise ValueError(
                "both episodes must exist to flag a conflict — nothing recorded")
        if kind == "supersedes":
            if winner_id is None or int(winner_id) not in (a_id, b_id):
                raise ValueError(
                    "kind='supersedes' requires winner to be one of the two episodes")
            winner: Optional[int] = int(winner_id)
        else:                              # conflict is symmetric — there is no winner
            winner = None
        # scan the human/agent resolution text BEFORE persisting (refuse → 0 rows)
        verdict = self._scanner.scan(resolution or "")
        if verdict.action == REFUSE:
            _log.warning("conflict.flag_refused", extra={
                "event": "conflict.flag_refused",
                "rules": [f.rule for f in verdict.findings],
                "proposed_by": proposed_by, "request_id": request_id})
            raise SecretRefused(
                f"refused: credential in resolution "
                f"({len(verdict.findings)} finding(s))",
                rules=[f.rule for f in verdict.findings],
                n_findings=len(verdict.findings))
        stored = verdict.redacted_text if verdict.action == REDACT else (resolution or "")
        lo, hi = (a_id, b_id) if a_id < b_id else (b_id, a_id)   # winner stable under swap
        recorded = self._flags.record_conflict_flag(
            kind=kind, a_id=lo, b_id=hi, winner_id=winner, resolution=stored,
            proposed_by=proposed_by, ts=int(self._now()))
        _log.info("conflict.flagged", extra={
            "event": "conflict.flagged", "kind": kind, "a_id": lo, "b_id": hi,
            "recorded": recorded, "proposed_by": proposed_by, "request_id": request_id})
        return FlagResult("flagged", recorded=recorded, kind=kind)
