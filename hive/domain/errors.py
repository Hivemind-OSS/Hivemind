"""Domain error taxonomy. Pure (stdlib only)."""
from __future__ import annotations


class HiveError(Exception):
    """Base for all hive domain errors."""


class AbstainNoData(HiveError):
    """Recall has no confident answer (never-hallucinate). Carries no hits."""


class SecretRefused(HiveError):
    """A write was refused because the deterministic scan found a raw credential.

    Carries the fired rule NAMES + count only (never the matched bytes) so the MCP
    layer can render the ``refused`` envelope's ``scan`` report without re-scanning.
    Secret-safety: ``rules`` is a list of labels (``"aws_akia"``…); no value is ever
    attached. // O(1)."""

    def __init__(self, message: str, *, rules: "list[str] | None" = None,
                 n_findings: int = 0) -> None:
        super().__init__(message)
        self.rules: list[str] = list(rules or ())
        self.n_findings: int = int(n_findings)


class NotApproved(HiveError):
    """An operation requires an approved episode; the row is still pending."""


class GeometryError(HiveError):
    """A value vector's dim/W_version does not match the live geometry."""


class HeadCodecError(HiveError):
    """A projection-head byte payload is malformed or version-incompatible."""


class CASConflictError(HiveError):
    """Optimistic single-writer CAS lost the race (cas_version mismatch)."""


class ReembedError(HiveError):
    """A re-embed migration could not round-trip the store."""


class TierViolation(HiveError):
    """A config field was changed at a reload tier that forbids it."""


class SqliteBusyExhausted(HiveError):
    """BEGIN IMMEDIATE retries were exhausted under contention."""
