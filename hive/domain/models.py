"""Frozen value carriers for the pure domain core.

PURE: stdlib + math only. The purity gate (tests/test_purity.py) forbids
sqlite3 | torch | subprocess | os | git | time imports anywhere in hive/domain/.
numpy is permitted but deliberately not imported here (these carriers are scalar).
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


def content_hash(text: str) -> str:
    """sha256(text) hex — the fetch key + dedup key that binds a memory to its text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── recall-side carriers ─────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AgentContext:
    """The agent-declared context that pins the live query's family_scope [A2].

    Built with the SAME three-axis grammar the producer derives at link time
    (git-remote | language | coarse-workflow) so query-side and credit-side
    families are byte-comparable.
    """
    repo_remote: str = ""
    language: str = ""
    workflow: str = "general"


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


# RecallState / RecallHit / RecallResult — the recall surface (02-CONTRACTS §2.1).
# abstain-no-resurrect is made STRUCTURAL on RecallResult: a non-CONFIDENT result
# carrying hits is UNCONSTRUCTABLE (__post_init__ raises), so no fallback can
# repopulate a refused query.
RecallState = str  # Literal["CONFIDENT", "ABSTAIN", "EMPTY_NO_DATA"] at the boundary

CONFIDENT = "CONFIDENT"
ABSTAIN = "ABSTAIN"
EMPTY_NO_DATA = "EMPTY_NO_DATA"


@dataclass(frozen=True, slots=True)
class RecallHit:
    """One surfaced memory handed to the agent (framed under ``reference_context``)."""
    episode_id: int
    text: str
    sim: float


@dataclass(frozen=True, slots=True)
class RecallResult:
    """The frozen result of a recall. ``hits`` is non-empty IFF ``CONFIDENT``;
    ``trace_id`` is ALWAYS present (hit AND abstain) — the §11 move-#6 join key."""
    state: RecallState
    trace_id: str
    hits: tuple[RecallHit, ...]
    entropy_norm: float                    # H/ln(N_eff) ∈ [0,1]; 0.0 on EMPTY_NO_DATA
    top_margin: float

    @classmethod
    def empty(cls, trace_id: str) -> "RecallResult":
        return cls(EMPTY_NO_DATA, trace_id, (), 0.0, 0.0)

    @classmethod
    def abstain(cls, trace_id: str, h_norm: float, margin: float) -> "RecallResult":
        return cls(ABSTAIN, trace_id, (), h_norm, margin)

    def __post_init__(self) -> None:
        if self.state not in (CONFIDENT, ABSTAIN, EMPTY_NO_DATA):
            raise ValueError(f"bad recall state {self.state!r}")
        # §2.1 biconditional, BOTH directions structural:
        #  - only CONFIDENT may carry hits (abstain-no-resurrect), AND
        #  - CONFIDENT must carry ≥1 hit (no empty-confident — the reverse resurrect).
        if self.state != CONFIDENT and self.hits:
            raise ValueError("only CONFIDENT may carry hits (abstain-no-resurrect)")
        if self.state == CONFIDENT and not self.hits:
            raise ValueError("CONFIDENT must carry ≥1 hit (§2.1 biconditional)")
        if not (0.0 <= self.entropy_norm <= 1.0):
            raise ValueError("entropy_norm must be in [0,1]")


# ── producer-side carriers (§11 trace↔outcome join) ──────────────────────────

@dataclass(frozen=True, slots=True)
class WindowTrace:
    """One recent confident recall in the association window (id + when)."""
    trace_id: str
    ts: int


@dataclass(frozen=True, slots=True)
class RecallWindow:
    """The per-agent set of recall traces issued since the last stamped commit."""
    items: tuple[WindowTrace, ...] = ()


@dataclass(frozen=True, slots=True)
class CommitFact:
    """A normalized git fact yielded by an OutcomeSource. The producer never
    touches git directly; all I/O is sealed behind the OutcomeSource port [D6].

    ``kind`` ∈ {"commit","merge","revert","bugfix"}. ``touched_blame`` is the set
    of original-introduced line numbers this commit modifies (pre-attached by the
    source via git-blame) — the blame-overlap signal for delayed clawback.
    """
    sha: str
    ts: int
    kind: str = "commit"
    message: str = ""
    repo_remote: str = ""                   # the watched repo's remote (source pre-attaches it)
    files_touched: tuple[str, ...] = ()
    introduced_lines: frozenset[int] = field(default_factory=frozenset)  # lines THIS commit added
    touched_blame: frozenset[int] = field(default_factory=frozenset)     # original lines a bugfix modifies
    reverts: Optional[str] = None          # the SHA this commit reverts, if kind == "revert"
    trailer_traces: tuple[str, ...] = ()   # Hive-Trace: <T1> <T2> … override set


@dataclass(frozen=True, slots=True)
class SourcePoll:
    """The result of OutcomeSource.poll() for one watched repo."""
    repo_remote: str
    language: str = ""
    commits: tuple[CommitFact, ...] = ()
    ok: bool = True
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class OutcomeRow:
    """A row of the task_outcomes state machine (provisional → settled_pos|clawed_back)."""
    task_ref: str
    trace_id: str
    family_scope: str
    state: str                              # provisional | settled_pos | clawed_back
    reward: float
    merge_ts: int
    settle_at: int
    introduced_lines: frozenset[int] = field(default_factory=frozenset)
    files_touched: tuple[str, ...] = ()
    repo: str = ""


@dataclass(frozen=True, slots=True)
class JoinerEmit:
    """A settled/clawed reward the joiner emits; drained into credit deltas."""
    task_ref: str
    trace_id: str
    family_scope: str
    reward_sign: int                        # +1 settle, −1 clawback
    magnitude: float
    ts: int
    source_agent: str = ""


@dataclass(frozen=True, slots=True)
class SettledOutcome:
    """The attributor's input. ``reward_sign`` MUST be ±1 — a CI-green ~0 event is
    structurally unconstructable as credit (verifiable-credit-only made structural)."""
    family_scope: str
    reward_sign: int
    magnitude: float
    source_agent: str = ""

    def __post_init__(self) -> None:
        if self.reward_sign not in (-1, 1):
            raise ValueError(
                f"reward_sign must be ±1 (verifiable-credit-only); got {self.reward_sign!r}")
        if self.magnitude < 0:
            raise ValueError(f"magnitude must be ≥0; got {self.magnitude!r}")


@dataclass(frozen=True, slots=True)
class SettledExposure:
    """One settled task-outcome joined with the eids it exposed — the unit the
    PredictionBiasMonitor scores (guardrail-3 / §12 readiness instrument, A6).
    ``reward_sign`` is ±1 (settled_pos → +1, clawed_back → −1); realized maps +1→1,
    −1→0 "like the mean". ``exposed`` is the recalled eids credited at recall time;
    ``settle_ts`` is when reality landed (the window key). An unconstrainable
    reward_sign (a CI-green ~0 event) is not a settled credit signal."""
    family_scope: str
    reward_sign: int
    exposed: tuple[int, ...]
    settle_ts: int

    def __post_init__(self) -> None:
        if self.reward_sign not in (-1, 1):
            raise ValueError(
                f"reward_sign must be ±1 (verifiable-credit-only); got {self.reward_sign!r}")


@dataclass(frozen=True, slots=True)
class ProducerTick:
    """Typed audit record of one OutcomeProducer.step() — also the §12 Phase-2
    readiness signal. step() NEVER raises; per-repo/policy failures land in errors."""
    associated: int = 0
    settled: int = 0
    clawed_back: int = 0
    drained: int = 0
    stamp_hits: int = 0
    window_assoc: int = 0
    errors: int = 0
    poll_commits: int = 0


# ── episode (the recall substrate) ───────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Episode:
    """A captured insight. Self-asserting: an inconsistent Episode is
    UNCONSTRUCTABLE — content_hash binds text, value is unit-norm float32[d] (or
    None while pending), and approved ⇔ approved_by set (never-hallucinate /
    approved-only made structural)."""
    id: int
    tenant_id: str
    text: str
    weight: float
    ts: int
    source: str
    tags: str
    content_hash: str
    status: str                        # 'pending' | 'approved'
    proposed_by: str
    value: Optional["np.ndarray"] = None
    approved_by: Optional[str] = None
    approved_ts: Optional[int] = None
    version: int = 0

    def __post_init__(self) -> None:
        if self.content_hash != content_hash(self.text):
            raise ValueError("content_hash does not bind text (hash≠sha256(text))")
        if self.status not in ("pending", "approved"):
            raise ValueError(f"bad status {self.status!r}")
        if (self.status == "approved") != (self.approved_by is not None):
            raise ValueError("invariant: status=='approved' iff approved_by is set")
        if self.value is not None:
            v = self.value
            if getattr(v, "dtype", None) != np.float32:
                raise ValueError("value must be float32")
            if v.ndim != 1:
                raise ValueError("value must be 1-D")


# ── onboarding carriers (M07 hive_init handshake; M06 depends on the port) ────

# The §6.4 marker delimiters. The block is located + hashed by these bounds so the
# phase-2 confirm hashes EXACTLY what the agent installed. Defined here (the carrier
# that enforces them) so there is one source of truth for both the renderer (M07
# onboard.py) and the conformance-checking fakes.
RULES_BLOCK_START = "<!-- hive-init:start -->"
RULES_BLOCK_END = "<!-- hive-init:end -->"


def rules_block_version_marker(version: int) -> str:
    """The embedded version line a conformant block must carry (so a v1 block can
    never be confirmed as a v2 block — the marker is part of the hashed body)."""
    return f"<!-- hive-init:version={int(version)} -->"


@dataclass(frozen=True, slots=True)
class RulesBlock:
    """The marker-delimited rules-file block hive_init renders. Self-asserting: an
    ill-formed block — missing the start/end markers, missing the version embed for
    its own ``block_version``, a ``trailer_key`` that does not appear in the body, or
    a hash that does not bind that body — is UNCONSTRUCTABLE, so the recorded link
    cannot lie about what was installed. ``trailer_key`` is ALWAYS sourced from
    ``producer.stamp_trailer`` (never literal); requiring it to appear in the body
    kills the §11/§6.1#6 CONFIG_DRIFT silent-join-failure at construction time — a
    hard-coded template literal that diverges from ``trailer_key`` cannot be built."""
    rendered_text: str
    trailer_key: str
    block_version: int
    block_hash: bytes                       # sha256(rendered_text)

    def __post_init__(self) -> None:
        if RULES_BLOCK_START not in self.rendered_text or \
                RULES_BLOCK_END not in self.rendered_text:
            raise ValueError("rules block missing start/end markers")
        if rules_block_version_marker(self.block_version) not in self.rendered_text:
            raise ValueError(
                f"rules block missing version embed for block_version={self.block_version}")
        if self.trailer_key not in self.rendered_text:
            raise ValueError(
                "trailer_key must appear in rendered_text (single-source guard — a "
                "template literal that diverges from producer.stamp_trailer is rejected)")
        if self.block_hash != hashlib.sha256(self.rendered_text.encode("utf-8")).digest():
            raise ValueError("block_hash does not bind rendered_text")


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """The typed result of hive_init phase-1 (zero writes). ``expected_confirm_hash``
    is the phase-2 confirmation key; ``watch_warning`` is non-blocking (a repo not yet
    in producer.watch_repos still links). Frozen so the plan cannot drift between
    render and confirm."""
    rules_file: str
    harness: str
    rules_block: RulesBlock
    expected_confirm_hash: bytes            # == rules_block.block_hash
    watch_warning: Optional[str] = None

    def __post_init__(self) -> None:
        if self.expected_confirm_hash != self.rules_block.block_hash:
            raise ValueError("expected_confirm_hash must equal rules_block.block_hash")


# ── utility posterior (Beta-Bernoulli) ───────────────────────────────────────

# Pinned in docs/08-RESOLUTIONS.md Cluster D2: uniform prior Beta(1,1),
# normal-approx CI, confident iff the (1 - 2·(1-ci_level)) interval excludes 0.5.
PRIOR_A: float = 1.0
PRIOR_B: float = 1.0
_Z_FOR_CI = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}


@dataclass(frozen=True, slots=True)
class UtilityPosterior:
    """A Beta-Bernoulli utility posterior keyed (episode_id, family_scope).
    Separate from and never entangled with episode.weight (guardrail-4)."""
    episode_id: int
    family_scope: str
    wins: float = 0.0
    losses: float = 0.0
    n_sources: int = 0
    version: int = 1
    isolation: bool = False

    def mean(self) -> float:
        """Beta posterior mean = a/(a+b) with priors folded (∈ (0,1); fresh ⇒ 0.5)."""
        a = PRIOR_A + self.wins
        b = PRIOR_B + self.losses
        return a / (a + b)

    def _sd(self) -> float:
        a = PRIOR_A + self.wins
        b = PRIOR_B + self.losses
        u = a / (a + b)
        return math.sqrt(u * (1.0 - u) / (a + b + 1.0))

    def ci_excludes_half(self, ci_level: float = 0.90) -> bool:
        """True iff the normal-approx CI excludes the no-signal point u₀=0.5 [D2].
        confident ⇔ |mean − 0.5| > Z·sd  (the gate that keeps un-confident utility
        from ever moving ranking, spec §4.7 invariant)."""
        z = _Z_FOR_CI.get(round(ci_level, 2), 1.645)
        return abs(self.mean() - 0.5) > z * self._sd()
