"""Frozen value carriers for the pure domain core.

PURE: stdlib + math only. The purity gate (tests/test_purity.py) forbids
sqlite3 | torch | subprocess | os | git | time imports anywhere in hive/domain/.
numpy is permitted but deliberately not imported here (these carriers are scalar).
"""
from __future__ import annotations

import hashlib
import math
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
    always coexists as a second row). Defaults are FAIL-SAFE: a construction site
    that forgets to wire them under-claims (quarantined/epoch-0), never over-claims."""
    episode_id: int
    text: str
    sim: float
    trust: str = QUARANTINED
    ts: int = 0


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
class RecallResult:
    """The frozen result of a recall. ``hits`` is non-empty IFF ``CONFIDENT``;
    ``trace_id`` is ALWAYS present (hit AND abstain) — the move-#6 join key.

    ``drafts`` is the self-quarantine resurfacing side-channel — a seat's own
    unpromoted quarantined captures, labeled. It is deliberately UNCONSTRAINED by
    ``state`` (it may ride ABSTAIN/EMPTY/CONFIDENT alike): the never-hallucinate
    biconditional below binds ``hits`` ONLY, so a populated ``drafts`` on an
    abstained result is by design, never a resurrected trusted answer."""
    state: RecallState
    trace_id: str
    hits: tuple[RecallHit, ...]
    entropy_norm: float                    # H/ln(N_eff) ∈ [0,1]; 0.0 on EMPTY_NO_DATA
    top_margin: float
    drafts: tuple["RecallDraft", ...] = ()

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


# ── dormant credit carrier ───────────────────────────────────────────────────
# The producer that fed these (associate→settle→clawback→drain) was removed; only
# SettledExposure survives — the unit the utility store + PredictionBiasMonitor read
# back (observed-not-applied in Phase 1). Nothing constructs it at runtime now.

@dataclass(frozen=True, slots=True)
class SettledExposure:
    """One settled task-outcome joined with the eids it exposed — the unit the
    PredictionBiasMonitor scores (guardrail-3 / readiness instrument, A6).
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

    def __post_init__(self) -> None:
        if self.content_hash != content_hash(self.text):
            raise ValueError("content_hash does not bind text (hash≠sha256(text))")
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


# ── onboarding carriers (M07 hive_init handshake; M06 depends on the port) ────

# The marker delimiters. The block is located + hashed by these bounds so the
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
    its own ``block_version``, or a hash that does not bind that body — is
    UNCONSTRUCTABLE, so the recorded link cannot lie about what was installed."""
    rendered_text: str
    block_version: int
    block_hash: bytes                       # sha256(rendered_text)

    def __post_init__(self) -> None:
        if RULES_BLOCK_START not in self.rendered_text or \
                RULES_BLOCK_END not in self.rendered_text:
            raise ValueError("rules block missing start/end markers")
        if rules_block_version_marker(self.block_version) not in self.rendered_text:
            raise ValueError(
                f"rules block missing version embed for block_version={self.block_version}")
        if self.block_hash != hashlib.sha256(self.rendered_text.encode("utf-8")).digest():
            raise ValueError("block_hash does not bind rendered_text")


# ── runtime-agnostic delivery: capability tiers + hook manifest/profile/recipe ──
# Tier 0 = MCP tools (universal); Tier 1 = rules-file contract (default); Tier 2 = native
# hooks (claude-code). A new IDE is a new HarnessProfile ROW, never a code change.
TIER_MCP, TIER_RULES, TIER_HOOKS = 0, 1, 2


@dataclass(frozen=True, slots=True)
class HookSpec:
    """One abstract, harness-INDEPENDENT behavioral hook the system wants installed (the
    'what'). ``event`` ∈ {task-start, turn-end, correction, commit}; ``action`` ∈
    {recall, capture, write}; ``directive`` is the NL instruction projected onto each host."""
    event: str
    action: str
    directive: str


@dataclass(frozen=True, slots=True)
class HookManifest:
    """The VERSIONED, harness-independent set of behavioral hooks. Projected onto a
    concrete host by HarnessRecipe; the ``generic`` profile returns it verbatim (as JSON)
    + an NL playbook so an un-profiled agent can self-apply it. ``manifest_version`` is the
    single source both the link record and the hive_health onboarding hint report."""
    manifest_version: int
    hooks: tuple[HookSpec, ...]

    def __post_init__(self) -> None:
        if not self.hooks:
            raise ValueError("HookManifest must declare at least one hook")


@dataclass(frozen=True, slots=True)
class HarnessProfile:
    """One DATA row per IDE — a new host is a new row, not code. ``max_tier`` caps
    materialization: a host with no hook mechanism can NEVER be handed OS hook files.
    Non-Claude ``mcp_config_target`` paths are best-effort (verify at build time)."""
    harness: str
    rules_file_candidates: tuple[str, ...]
    mcp_config_target: str
    hook_mechanism: Optional[str]               # None ⇒ no native hooks ⇒ max_tier ≤ 1
    max_tier: int

    def __post_init__(self) -> None:
        # max_tier==2 iff a hook mechanism exists — keeps the row from claiming hooks it
        # can't deliver (or hiding hooks it can). The HarnessRecipe invariant leans on this.
        if (self.max_tier >= TIER_HOOKS) != bool(self.hook_mechanism):
            raise ValueError(
                "HarnessProfile invariant: max_tier>=2 iff a hook_mechanism is set "
                f"(harness={self.harness!r}, max_tier={self.max_tier}, "
                f"hook_mechanism={self.hook_mechanism!r})")


@dataclass(frozen=True, slots=True)
class HookFile:
    """A concrete file the agent materializes at Tier-2 (path relative to the repo root,
    its content, and the mechanism that consumes it). Written in chunk 6 (verify gate)."""
    path: str
    content: str
    mechanism: str


@dataclass(frozen=True, slots=True)
class HarnessRecipe:
    """A HookManifest PROJECTED onto one harness at its resolved tier. INVARIANT (the
    mutation pin, enforced at construction so a violation is UNCONSTRUCTABLE): a Tier-≤1
    host emits ZERO ``hook_files`` (no OS hooks below Tier 2) and a Tier-2 host emits ≥1.
    ``rules_addendum`` is the Tier-≥1 capture directive; ``playbook`` is the NL self-apply
    fallback (always present — the ``generic`` deliverable alongside the manifest JSON)."""
    harness: str
    resolved_tier: int
    manifest_version: int
    rules_addendum: str
    playbook: str
    hook_files: tuple[HookFile, ...] = ()

    def __post_init__(self) -> None:
        if self.resolved_tier < TIER_HOOKS and self.hook_files:
            raise ValueError(
                "no OS hooks below Tier 2: a Tier-≤1 harness must emit zero hook_files "
                f"(harness={self.harness!r}, resolved_tier={self.resolved_tier})")
        if self.resolved_tier >= TIER_HOOKS and not self.hook_files:
            raise ValueError(
                f"a Tier-2 recipe must materialize at least one hook file (harness={self.harness!r})")


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """The typed result of hive_init phase-1 (zero writes). ``expected_confirm_hash`` is
    the phase-2 confirmation key; ``manifest``/``recipe`` carry the hook manifest and
    its per-harness projection. Frozen so the plan cannot drift between render and confirm."""
    rules_file: str
    harness: str
    rules_block: RulesBlock
    expected_confirm_hash: bytes            # == rules_block.block_hash
    manifest: HookManifest
    recipe: HarnessRecipe

    def __post_init__(self) -> None:
        if self.expected_confirm_hash != self.rules_block.block_hash:
            raise ValueError("expected_confirm_hash must equal rules_block.block_hash")
        if self.recipe.harness != self.harness:
            raise ValueError(
                f"recipe.harness ({self.recipe.harness!r}) must match plan.harness ({self.harness!r})")
        if self.recipe.manifest_version != self.manifest.manifest_version:
            raise ValueError(
                "recipe must project the plan's manifest version "
                f"({self.recipe.manifest_version} != {self.manifest.manifest_version})")


# ── utility posterior (Beta-Bernoulli) ───────────────────────────────────────

# Pinned in Cluster D2: uniform prior Beta(1,1),
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
        from ever moving ranking — an invariant)."""
        z = _Z_FOR_CI.get(round(ci_level, 2), 1.645)
        return abs(self.mean() - 0.5) > z * self._sd()
