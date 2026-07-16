"""The fail-closed verification result model.

Safe direction: the answer path fails CLOSED — ``not_run`` and ``errored`` are
abstentions that can never read as a pass. Every carrier proves its own invariants
at construction (guards G1-G7), so readers assume validity and never re-check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RunState = Literal["passed", "failed", "not_run", "errored"]

_RAN_STATES = ("passed", "failed")
_ABSTAIN_STATES = ("not_run", "errored")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One tool-reported finding, located in the target repo."""

    file: str
    line: int
    col: int
    code: str
    message: str
    severity: Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class Ingested:
    """Counts + diagnostics from a report that PARSED.

    Constructable only from a successful parse — ingesters raise instead of
    returning empty counts — so "unparsed but green" cannot be represented.
    """

    passed: int
    failed: int
    errored: int
    skipped: int
    diagnostics: tuple[Diagnostic, ...]

    def __post_init__(self) -> None:
        for name in ("passed", "failed", "errored", "skipped"):
            if getattr(self, name) < 0:
                raise ValueError(f"Ingested.{name} must be >= 0")


_ZERO_CASES_REASON = "report parsed but zero cases ran"


def classify(ingested: Ingested) -> tuple[RunState, str | None]:
    """Single owner of the Ingested -> RunState derivation.

    Zero executed cases -> not_run (a parsed-but-vacuous report is never a pass);
    any failed or errored case -> failed; otherwise passed. The raise -> errored
    mapping belongs to the orchestrator, not here.
    """
    if ingested.passed + ingested.failed + ingested.errored == 0:
        return "not_run", _ZERO_CASES_REASON
    if ingested.failed > 0 or ingested.errored > 0:
        return "failed", None
    return "passed", None


@dataclass(frozen=True, slots=True)
class LangOutcome:
    """Per-language detail riding a merged ClassResult (the distinct truth channel)."""

    lang: str
    state: RunState
    reason: str | None


@dataclass(frozen=True, slots=True)
class ClassResult:
    """One evidence class (typecheck or tests), merged across languages, fail-closed."""

    state: RunState
    ran: bool
    passed: int
    failed: int
    errored: int
    diagnostics: tuple[Diagnostic, ...]
    runners: tuple[str, ...]
    reason: str | None
    report_format: str | None
    per_lang: tuple[LangOutcome, ...] = ()
    mode: Literal["scoped", "authoritative"] | None = None

    def __post_init__(self) -> None:
        # G6 — counts are sane before anything else reasons over them.
        if self.passed < 0 or self.failed < 0 or self.errored < 0:
            raise ValueError("G6: counts must be >= 0")
        # G1 — ran is derived truth, never an independent claim.
        if self.ran != (self.state in _RAN_STATES):
            raise ValueError("G1: ran must equal (state in {'passed', 'failed'})")
        # G3 — an abstention always says why.
        if self.state in _ABSTAIN_STATES and not (
            isinstance(self.reason, str) and self.reason
        ):
            raise ValueError("G3: not_run/errored require a non-empty reason")
        # G4 — a failed state is backed by at least one failing or erroring case.
        if self.state == "failed" and self.failed + self.errored < 1:
            raise ValueError("G4: failed requires failed + errored >= 1")
        if self.state == "passed":
            # G2 — no vacuous green: a pass has real passes and zero failures.
            if self.failed != 0 or self.errored != 0 or self.passed < 1:
                raise ValueError(
                    "G2: passed requires failed == 0, errored == 0 and passed >= 1"
                )
            # G5 — a pass cannot carry an error-severity diagnostic.
            if any(d.severity == "error" for d in self.diagnostics):
                raise ValueError("G5: passed forbids error-severity diagnostics")
            # G7 — the polyglot merge is fail-closed: no failed/errored member may
            # hide under a passed top, and a non-empty merge needs a real pass.
            if any(o.state not in ("passed", "not_run") for o in self.per_lang):
                raise ValueError(
                    "G7: passed requires every per_lang state in {'passed', 'not_run'}"
                )
            if self.per_lang and not any(o.state == "passed" for o in self.per_lang):
                raise ValueError(
                    "G7: passed with per_lang detail requires >= 1 passed language"
                )


@dataclass(frozen=True, slots=True)
class AdequacyResult:
    """Suite-strength measurement — the honest class-2 caveat carrier.

    v1 produces none (no registry row carries an adequacy recipe): "suite strength
    unmeasured" stays explicit rather than implied.
    """

    kind: Literal["mutation", "coverage"]
    value: float
    detail: str

    def __post_init__(self) -> None:
        # Note: comparisons with NaN are False, so a NaN value also raises here.
        if not 0.0 <= self.value <= 1.0:
            raise ValueError("AdequacyResult.value must be within [0.0, 1.0]")


@dataclass(frozen=True, slots=True)
class Affected:
    """The blast-radius projection consumed from the matrix engine."""

    files: tuple[str, ...]
    symbols: tuple[str, ...]
    tests: tuple[str, ...]
    unsound: bool = True  # mirrors the engine: the radius is always a lower bound


@dataclass(frozen=True, slots=True)
class VerifierToolVersion:
    """SHA-bound provenance: which tools, at which versions, produced this evidence."""

    head_sha: str
    base_sha: str
    tool_versions: tuple[tuple[str, str], ...]
    registry_version: str


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """The complete evidence bundle for one change (classes 1-2)."""

    typecheck: ClassResult
    tests: ClassResult
    adequacy: AdequacyResult | None
    affected: Affected
    tool_version: VerifierToolVersion
    summary: str
