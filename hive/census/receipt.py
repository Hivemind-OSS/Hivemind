"""Full-bundle receipt assembly + write-boundary self-validation.

Pure composition over the frozen upstream carriers: node lines from the drift
verdict, edge and regression lines from the join findings, execution lines
from the coerced producer classes, the constant class-4 differential line
(no producer exists, so it is surfaced as not-run, never faked), the
provenance block binding every engine stamp to the change's SHAs, and the
per-class precision entries. Node-line tags are single-sourced from
combdrift's reason->tag policy — raw reason strings pass through untrimmed
(they carry ": <detail>" suffixes and the policy prefix-matches) and the
census never re-derives a tag. Institutional-memory context, when provided,
rides an optional top-level block OUTSIDE the lines — trust-labelled
reference that informs the reader and never joins an evidence class, never
gates, and is absent entirely when empty; propagation neighbourhoods (the
``--propagate`` blast-radius expansion) ride a second optional block under
the same rules. The last act before returning is schema validation: an
off-schema receipt is refused, never emitted.
"""

from __future__ import annotations

from functools import cache
from importlib import metadata
from typing import TYPE_CHECKING, Any

from hive.census.diff import ChangeSet
from hive.census.engines import GraphPair
from hive.census.execution import ExecutionClassLine
from hive.census.join import RegressionFinding
from hive.census.precision import assess_precision
from hive.census.schema import SCHEMA_VERSION, validate_receipt

if TYPE_CHECKING:  # annotation targets only; the runtime imports stay lazy
    from collections.abc import Sequence

    import hive.combdrift as combdrift

    from hive.census.memory import MemoryContext

PRECISION_MODEL = "v0-decided-fraction"

_DECIDED_DRIFTS = frozenset({"unchanged", "additive", "breaking", "removed"})
_DECIDED_STATES = frozenset({"passed", "failed"})

# Which line classes each precision class governs when stamping shown-not-gated.
_PRECISION_LINE_CLASSES: dict[str, tuple[str, ...]] = {
    "node": ("existence", "contract"),
    "edge": ("blast-radius",),
    "regression": ("regression",),
    "execution": ("typecheck", "tests"),
    "differential": ("differential",),
}


@cache
def _census_version() -> str:
    # The census ships inside the `hive` distribution; its version IS the
    # census version (the receipt key stays `hive_census_version` — wire format).
    try:
        return metadata.version("hive")
    except metadata.PackageNotFoundError:  # pragma: no cover - uninstalled tree only
        return "0+unknown"


def _subject(path: str, symbol: str) -> str:
    return f"{path}::{symbol}"


def _node_lines(verdict: combdrift.ChangeVerdict) -> list[dict[str, Any]]:
    from hive.combdrift import tag_for_reason

    lines: list[dict[str, Any]] = []
    for change in getattr(verdict, "symbols", ()) or ():
        path = str(getattr(change, "path", "") or "")
        symbol = str(getattr(change, "symbol", "") or "")
        reason = str(getattr(change, "reason", "") or "")
        # Raw reason in, tag out: the policy prefix-matches, so the suffix is
        # never trimmed here, and an unknown reason fails closed to unverified.
        tag = tag_for_reason(reason)
        subject = _subject(path, symbol)
        old_fp = getattr(change, "old_fingerprint", None)
        new_fp = getattr(change, "new_fingerprint", None)
        lines.append(
            {
                "class": "existence",
                "tag": tag,
                "subject": subject,
                "reason": reason,
                "detail": {
                    "existed_before": bool(getattr(change, "existed_before", False)),
                    "exists_after": bool(getattr(change, "exists_after", False)),
                },
            }
        )
        lines.append(
            {
                "class": "contract",
                "tag": tag,
                "subject": subject,
                "reason": reason,
                "detail": {
                    "drift": str(getattr(change, "drift", "") or ""),
                    "old_fingerprint": str(old_fp) if old_fp is not None else None,
                    "new_fingerprint": str(new_fp) if new_fp is not None else None,
                },
            }
        )
    return lines


def _blast_lines(findings: tuple[RegressionFinding, ...]) -> list[dict[str, Any]]:
    return [
        {
            "class": "blast-radius",
            "tag": finding.tag,
            "subject": _subject(finding.path, finding.symbol),
            "reason": finding.reason,
            "detail": {
                "seed": finding.seed,
                "depth": finding.depth,
                "unsound": True,  # the radius is always a lower bound
                "callers": list(finding.callers),
                "dependents": list(finding.dependents),
                "tests": list(finding.tests),
            },
        }
        for finding in findings
    ]


def _regression_lines(
    findings: tuple[RegressionFinding, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "class": "regression",
            "tag": finding.tag,
            "subject": _subject(finding.path, finding.symbol),
            "reason": finding.reason,
            "detail": {
                "drift": finding.drift,
                "seed": finding.seed,
                "depth": finding.depth,
                "callers": list(finding.callers),
                "dependents": list(finding.dependents),
                "tests": list(finding.tests),
            },
        }
        for finding in findings
    ]


def _execution_lines(
    execution: tuple[ExecutionClassLine, ExecutionClassLine],
) -> list[dict[str, Any]]:
    return [
        {
            "class": line.cls,
            "tag": line.tag,
            "subject": line.cls,
            "reason": line.reason,
            "detail": {
                "state": line.state,
                "passed": line.passed,
                "failed": line.failed,
                "errored": line.errored,
                "runners": list(line.runners),
            },
        }
        for line in execution
    ]


def _differential_line() -> dict[str, Any]:
    # Class 4 has no producer; the constant line surfaces that honestly.
    return {
        "class": "differential",
        "tag": "not-run",
        "subject": "differential",
        "reason": "no-producer",
        "detail": {},
    }


def _precision_counts(
    verdict: combdrift.ChangeVerdict,
    findings: tuple[RegressionFinding, ...],
    execution: tuple[ExecutionClassLine, ExecutionClassLine],
) -> dict[str, tuple[int, int]]:
    symbols = tuple(getattr(verdict, "symbols", ()) or ())
    node_decided = sum(
        1
        for change in symbols
        if str(getattr(change, "drift", "") or "") in _DECIDED_DRIFTS
    )
    seeded = sum(1 for finding in findings if finding.seed is not None)
    execution_decided = sum(1 for line in execution if line.state in _DECIDED_STATES)
    return {
        "node": (node_decided, len(symbols)),
        "edge": (seeded, len(findings)),
        "regression": (seeded, len(findings)),
        "execution": (execution_decided, len(execution)),
        "differential": (0, 0),
    }


def _verifier_version_block(verdict: combdrift.ChangeVerdict) -> dict[str, str | None]:
    stamp = getattr(verdict, "verifier_version", None)
    block: dict[str, str | None] = {}
    for field in ("combdrift_version", "fingerprint_version", "base_sha", "head_sha"):
        value = getattr(stamp, field, None)
        block[field] = str(value) if value is not None else None
    return block


def _stamp_block(stamp: object) -> dict[str, str | int]:
    return {
        "graph_sha256": str(getattr(stamp, "graph_sha256", "") or ""),
        "commit_sha": str(getattr(stamp, "commit_sha", "") or ""),
        "engine_version": str(getattr(stamp, "engine_version", "") or ""),
        "built_at": str(getattr(stamp, "built_at", "") or ""),
        "node_count": int(getattr(stamp, "node_count", 0) or 0),
        "edge_count": int(getattr(stamp, "edge_count", 0) or 0),
    }


def _context_block(
    context: Sequence[MemoryContext],
) -> list[dict[str, str | int | float]]:
    # Reference entries rendered whole, labels verbatim: the tag names what
    # they are, and nothing downstream mistakes them for evidence lines.
    return [
        {
            "tag": "institutional-memory",
            "episode_id": entry.episode_id,
            "trust": entry.trust,
            "polarity": entry.polarity,
            "kind": entry.kind,
            "anchor": entry.anchor,
            "ts": entry.ts,
            "sim": entry.sim,
            "text": entry.text,
        }
        for entry in context
    ]


def build_receipt(
    *,
    touched: ChangeSet,
    verdict: combdrift.ChangeVerdict,
    findings: tuple[RegressionFinding, ...],
    execution: tuple[ExecutionClassLine, ExecutionClassLine],
    verifier_stamp: dict[str, Any] | None,
    graphs: GraphPair,
    precision_budget: float,
    depth: int,
    context: Sequence[MemoryContext] = (),
    propagation: Sequence[dict[str, Any]] = (),
    ref: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Assemble the complete receipt document and refuse an off-schema one.

    Lines ride in a fixed order (node, blast-radius, regression, execution,
    differential); every line of a below-budget precision class is stamped
    ``"gating": false`` — shown, never gated. Institutional-memory context,
    when non-empty, rides the optional top-level ``context`` block outside
    the lines (informative labels only); an empty context emits NO key, so
    the context-free receipt stays byte-identical. Propagation entries (the
    blast-radius neighbourhoods of breaking/removed seeds, already rendered
    as ``path::Symbol``) ride the optional top-level ``propagation`` block
    the same way: outside the lines, never gating, absent entirely when
    empty. The returned document has already passed schema validation;
    ReceiptSchemaError propagates instead of a partial receipt. ``ref`` (the measured
    checkout's branch, resolved by the caller) rides ``provenance.ref`` only when
    truthy — a ref-less build stays byte-identical to the pre-stamp receipt. ``repo``
    (the repository identity the caller chose to record, verbatim) rides
    ``provenance.repo`` under the same omit-when-absent rule.
    """
    lines = (
        _node_lines(verdict)
        + _blast_lines(findings)
        + _regression_lines(findings)
        + _execution_lines(execution)
        + [_differential_line()]
    )
    entries = assess_precision(
        _precision_counts(verdict, findings, execution), budget=precision_budget
    )
    shown_not_gated = {
        line_cls
        for entry in entries
        if not entry.gating
        for line_cls in _PRECISION_LINE_CLASSES.get(entry.cls, ())
    }
    for line in lines:
        if line["class"] in shown_not_gated:
            line["gating"] = False
    provenance: dict[str, Any] = {
        "base_sha": touched.base_sha,
        "head_sha": touched.head_sha,
        # marker: writing `ref` or `repo` unconditionally (empty/None included) is a
        # mutation — legacy byte-identity requires omit-when-absent.
        **({"ref": ref} if ref else {}),
        **({"repo": repo} if repo else {}),
        "schema_version": SCHEMA_VERSION,
        "hive_census_version": _census_version(),
        "precision_model": PRECISION_MODEL,
        "blast_depth": depth,
        "combdrift": _verifier_version_block(verdict),
        "matrix": {
            "base": _stamp_block(graphs.base_stamp),
            "head": _stamp_block(graphs.head_stamp),
        },
    }
    if verifier_stamp is not None:
        provenance["verifier"] = dict(verifier_stamp)
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "provenance": provenance,
        "lines": lines,
        "precision": [
            {
                "class": entry.cls,
                "decided": entry.decided,
                "total": entry.total,
                "value": entry.value,
                "budget": entry.budget,
                "gating": entry.gating,
            }
            for entry in entries
        ],
    }
    if context:
        doc["context"] = _context_block(context)
    if propagation:
        doc["propagation"] = [dict(entry) for entry in propagation]
    validate_receipt(doc)
    return doc
