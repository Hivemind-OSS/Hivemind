"""Domain error taxonomy. Pure (stdlib only)."""
from __future__ import annotations


class HiveError(Exception):
    """Base for all hive domain errors."""


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


class GeometryError(HiveError):
    """A value vector's dim does not match the live geometry (the model's native dim)."""
