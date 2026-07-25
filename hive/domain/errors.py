"""Domain error taxonomy. Pure (stdlib only)."""

from __future__ import annotations

from typing import Sequence

from hive.domain.secret_scan import SecretFinding


class HiveError(Exception):
    """Base for all hive domain errors."""


class SecretRefused(HiveError):
    """A write was refused because the deterministic scan found a raw credential.

    Carries the fired ``SecretFinding``s — rule NAME + char SPAN, never the matched
    bytes — so the MCP layer can render the ``refused`` envelope's ``scan`` report
    without re-scanning. The findings are the ONE input; ``rules`` / ``n_findings`` /
    ``spans`` are derived here so no call site can report a rule without its span.

    The SPAN is what makes a refusal actionable: it points the writer at the run that
    fired instead of leaving it to bisect its own memory text by hand (BUG-018).
    Secret-safety: labels + integer offsets only, and the message this builds is the
    envelope's ``reason``, so a client that surfaces only the reason still sees the
    spans. // O(f)."""

    def __init__(self, context: str, *, findings: Sequence[SecretFinding]) -> None:
        self.findings: tuple[SecretFinding, ...] = tuple(findings)
        self.rules: list[str] = [f.rule for f in self.findings]
        self.n_findings: int = len(self.findings)
        self.spans: list[list[int]] = [[f.span[0], f.span[1]] for f in self.findings]
        super().__init__(
            f"refused: {context} ({self.n_findings} finding(s), "
            f"rules={self.rules}, spans={self.spans})"
        )


class GeometryError(HiveError):
    """A value vector's dim does not match the live geometry (the model's native dim)."""
