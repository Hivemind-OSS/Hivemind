"""Machine-readable data contract: frozen dataclasses + the Verdict literal.

These types ARE the contract; the JSON wire shape is derived from them at the
serialization seam, never duplicated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# The three verdict strings are also the exact JSON wire values, so they are
# modeled as a Literal rather than an enum (no second name for the same fact).
Verdict = Literal["current", "stale", "unverifiable"]

# Closed set of machine-stable reason codes a consumer may branch on. Kept here
# as the single source so resolution/verdict use identical spellings.
REASON_OK = "ok"
REASON_FILE_MISSING = "file_missing"
REASON_PATH_OUTSIDE_REPO = "path_outside_repo"
REASON_PARSE_ERROR = "parse_error"
REASON_SYMBOL_MISSING = "symbol_missing"
REASON_NO_SYMBOL_REQUESTED = "no_symbol_requested"
REASON_NO_ANCHORS = "no_anchors"
# Anchor points at a file whose language the resolver cannot parse.
REASON_UNSUPPORTED_LANGUAGE = "unsupported_language"
# Symbol name is present in the file but not a resolvable callable (a re-export,
# wildcard, data binding, nested/conditional def, or maybe-inherited method).
# Routes to unverifiable: presence combdrift cannot positively resolve is never stale.
REASON_SYMBOL_INDIRECT = "symbol_indirect"
# A recorded interface fingerprint provably broke a previously-valid call shape.
# The one new stale trigger this increment adds.
REASON_SIGNATURE_CHANGED = "signature_changed"
# The recorded fingerprint's version is unreadable (a future format), or the
# symbol's shape is ambiguous (overloaded). Routes to unverifiable, never stale.
REASON_FINGERPRINT_VERSION_MISMATCH = "fingerprint_version_mismatch"
# The symbol resolves but NO fingerprint was recorded for it, so its call shape was
# never compared. Existence is proven; the shape is unknown. Routes to unverifiable:
# reporting `ok` here would be a positive claim about a comparison that never ran.
REASON_NO_FINGERPRINT = "no_fingerprint"


@dataclass(frozen=True, slots=True)
class Anchor:
    path: str  # repo-relative path to a Python file
    symbol: str | None = None  # module-level function/class, or "Class.method"
    # Opaque, combdrift-minted interface token, honored only alongside a symbol; a
    # fingerprint with no symbol is inert and never changes a verdict.
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class Record:
    id: str
    claim_text: str
    anchors: tuple[Anchor, ...] = ()
    recorded_at_commit: str | None = None  # informational only; never acted on


@dataclass(frozen=True, slots=True)
class FingerprintDelta:
    """Structured interface-drift direction between two minted tokens.

    Surfaced on a drift result so a consumer reads the breaking/additive
    direction as a typed field rather than scraping the reason string.
    `old_token` is None when no baseline token was recorded.
    """

    old_token: str | None
    new_token: str
    breaking: bool


@dataclass(frozen=True, slots=True)
class AnchorResult:
    path: str
    symbol: str | None
    found: bool
    location: str | None  # e.g. "pkg/auth.py:42" when found, else None
    reason: str  # machine-stable code + detail, e.g. "symbol_missing: refresh"
    # Set only when a recorded fingerprint was compared (a breaking or additive
    # drift); None otherwise, so the JSON wire is unchanged unless populated.
    delta: "FingerprintDelta | None" = None


@dataclass(frozen=True, slots=True)
class RecordVerdict:
    id: str
    verdict: Verdict
    anchors: tuple[AnchorResult, ...]


@dataclass(frozen=True, slots=True)
class RunSummary:
    current: int
    stale: int
    unverifiable: int
