"""SHA + verifier-generation stamp bound onto a change verdict.

A change verdict is only trustworthy against the tree it was computed on. This
stamp records the base/head commit SHAs and both code generations — the package
semver and the fingerprint token-format version — so a consumer detects a stale
receipt by comparison (head_sha != the PR head, or fingerprint_version != the
current generation) rather than trusting an old verdict silently. The verifier
stamps; it never decides freshness. Pure: no IO, no git, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass

from hive.combdrift.fingerprint import FINGERPRINT_VERSION as FINGERPRINT_VERSION

# The hive-edge workspace lockstep version — every workspace member ships the one
# workspace version, bumped together per release, and the workspace guard test enforces
# equality across the version sites (including pyproject's copy of this value).
# Deliberately a pure literal, never read from install metadata, so the stamp stays
# pure and total regardless of install state.
COMBDRIFT_VERSION = "0.9.0"


@dataclass(frozen=True, slots=True)
class VerifierVersion:
    combdrift_version: str  # package semver of the verifier that computed the verdict
    fingerprint_version: (
        str  # the token-format generation (== fingerprint.FINGERPRINT_VERSION)
    )
    head_sha: str  # the change's head commit
    base_sha: str  # the change's base commit


def verifier_version(*, base_sha: str, head_sha: str) -> VerifierVersion:
    """Stamp the current verifier generation onto a change's base/head SHAs.

    SHAs are keyword-only to prevent a silent base/head swap at the call site.
    """
    return VerifierVersion(
        combdrift_version=COMBDRIFT_VERSION,
        fingerprint_version=FINGERPRINT_VERSION,
        head_sha=head_sha,
        base_sha=base_sha,
    )
