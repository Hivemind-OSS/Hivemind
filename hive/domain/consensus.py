"""The pure suspect-consensus detector — DETECTION ONLY, never resolution.

A demand-promoted (provisional) memory was promoted because ``demand_m`` window misses
matched it. But ``k`` raw asks are NOT ``k`` independent votes: if the asks were near-copies
of one another (one agent re-querying, or correlated LLM phrasings), the *effective*
independent-vote count ``n_eff`` is far below ``k``. The promote audit stamps the
``demand_independence`` it computed (``rho_bar``, ``n_eff``, ``k``); this module reads that
stamp and flags a provisional whose effective independence was THIN — the
confidently-wrong-but-popular failure mode (Kim et al. ICML 2025: ~60% of LLM errors are
correlated, so a popular promotion can be a correlated-error artifact, not corroboration).

It asserts NOTHING about truth. It renders a human worklist of provisionals to re-examine —
the resolution rail stays the human ``hive_supersede`` / ``hive_write(replaces=)`` path
(Law 3, THEORY §10 O7). It mirrors ``conflict.py``: a pure, total detector emitting a frozen,
self-asserting note carrying ids + numbers ONLY (Law 4 — no memory text).

``martingale_warning`` is the sharpened clause: a provisional that is BOTH thin AND has no
settled win is "popular but uncorroborated" — the sharper signal. The settled-win source
(read via the ``SettledWinReader`` port) is the UNION of ``outcome_helped`` (a self-report
logged via ``hive_outcome`` — trusted-solo: a hostile fleet could self-report to suppress its
own flag, moot under the trusted-solo stance and the worklist is detection-only) and
``outcome_verified_helped`` (the SHA-bound census corroboration — non-forgeable by the
memory's writer). Either marks ``has_settled_win=True``, so a thin-but-corroborated promotion
drops to ``martingale_warning=False`` while a thin-uncorroborated one stays True. The clause
is structural either way — the detector never changes, only the wired source does.

PURE: stdlib ``math`` only. The purity gate (tests/test_purity.py) forbids
sqlite3 | torch | subprocess | os | git | time imports anywhere in hive/domain/. The detector
is total and never raises: an unflaggable item (``k == 0``) is SKIPPED, never coined a flag.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Sequence

_log = logging.getLogger("hive.consensus")


@dataclass(frozen=True, slots=True)
class SuspectConsensusItem:
    """One provisional's suspect-consensus projection: the stamped demand-independence
    (``rho_bar``, ``n_eff``, ``k``) read from the newest ``promote`` audit, plus
    ``has_settled_win`` (True iff an ``outcome_helped`` audit corroborates it, via
    ``SettledWinReader``) and the ``anchor`` (carried for the app layer only; the NOTE never
    echoes it — ids + numbers, Law 4). Built by the app layer from ``promotion_provenance`` +
    ``settled_wins`` + ``scan_servable_labeled``."""

    episode_id: int
    rho_bar: float
    n_eff: float
    k: int
    has_settled_win: bool
    anchor: str = ""


@dataclass(frozen=True, slots=True)
class SuspectConsensusNote:
    """One flagged provisional: thin effective-independence demand. Frozen + self-asserting —
    ``rho_bar`` finite in [-1, 1], ``n_eff`` finite >= 0, ``k`` >= 0. Carries ids + numbers +
    the warning bool ONLY (no memory text, no anchor, no ``has_settled_win`` — Law 4). A
    detection note, never a verdict: a human re-examines the promotion via ``hive_supersede``."""

    episode_id: int
    rho_bar: float
    n_eff: float
    k: int
    martingale_warning: bool

    def __post_init__(self) -> None:
        if not (math.isfinite(self.rho_bar) and -1.0 <= self.rho_bar <= 1.0):
            raise ValueError(f"rho_bar must be finite in [-1, 1] (got {self.rho_bar})")
        if not (math.isfinite(self.n_eff) and self.n_eff >= 0.0):
            raise ValueError(f"n_eff must be finite and >= 0 (got {self.n_eff})")
        if self.k < 0:
            raise ValueError(f"k must be >= 0 (got {self.k})")


def detect_suspect_consensus(
    items: Sequence[SuspectConsensusItem], *, n_eff_frac_max: float, top_n: int = 10
) -> tuple[SuspectConsensusNote, ...]:
    """Flag each provisional whose effective votes are THIN — ``n_eff / k < n_eff_frac_max``
    (k>=1; ``k == 0`` is not flaggable and is SKIPPED). ``martingale_warning`` = thin AND NOT
    ``has_settled_win`` (popular-but-uncorroborated). Output is deterministic — sorted by
    (``n_eff / k`` asc, ``episode_id``) so the most-correlated promotion leads — and capped at
    ``top_n``. Detection ONLY (THEORY §10 O7): it asserts nothing about truth, retires nothing.
    PURE, total, never raises; a degenerate guard (non-finite ``n_eff_frac_max`` / ``top_n`` < 1)
    → ().  // O(n log n) time."""
    if not math.isfinite(n_eff_frac_max) or top_n < 1:
        return ()
    scored: list[tuple[float, SuspectConsensusNote]] = []
    for it in items:
        k = int(it.k)
        if k < 1:
            continue  # k==0 ⇒ not flaggable (no votes to weigh)
        frac = float(it.n_eff) / float(k)
        if not (frac < n_eff_frac_max):  # strict: at-or-above the floor is fine
            continue
        warning = not bool(
            it.has_settled_win
        )  # thin already true here ⇒ thin AND not win
        scored.append(
            (
                frac,
                SuspectConsensusNote(
                    episode_id=int(it.episode_id),
                    rho_bar=float(it.rho_bar),
                    n_eff=float(it.n_eff),
                    k=k,
                    martingale_warning=warning,
                ),
            )
        )
    scored.sort(key=lambda t: (t[0], t[1].episode_id))
    return tuple(note for _frac, note in scored[:top_n])
