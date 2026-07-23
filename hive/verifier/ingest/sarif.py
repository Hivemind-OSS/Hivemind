"""SARIF ingestion (eslint -f sarif, sqlfluff --format sarif, and kin).

The document spine (top-level object -> ``runs`` list -> per-run ``results``
list) is shape-checked and raises when out of contract — coerce before index,
never a KeyError. Individual results are walked with defensive ``.get`` so a
sparse result degrades a field, never the report. ``error`` results fail the
check; ``warning`` (SARIF's default level) rides as advisory. A document with
zero runs analyzed nothing: zero counts, which downstream is not_run — never a
clean pass.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from hive.verifier.ingest import ReportParseError
from hive.verifier.result import Diagnostic, Ingested


def ingest_sarif(raw: bytes, *, exit_code: int) -> Ingested:
    if not raw.strip():
        raise ReportParseError("sarif: empty report")
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReportParseError(f"sarif: malformed JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ReportParseError("sarif: top level is not an object")
    runs = doc.get("runs")
    if not isinstance(runs, list):
        raise ReportParseError("sarif: missing or non-list 'runs'")

    analyzed = False
    error_count = 0
    diagnostics: list[Diagnostic] = []
    for run in runs:
        if not isinstance(run, dict):
            raise ReportParseError("sarif: run entry is not an object")
        results = run.get("results")
        if not isinstance(results, list):
            raise ReportParseError("sarif: run is missing its 'results' list")
        analyzed = True
        for result in results:
            if not isinstance(result, dict):
                raise ReportParseError("sarif: result entry is not an object")
            level = result.get("level", "warning")  # SARIF's default level
            if level not in ("error", "warning"):
                continue  # note/none results are informational, not evidence
            severity: Literal["error", "warning"] = (
                "error" if level == "error" else "warning"
            )
            diagnostics.append(_result_diagnostic(result, severity))
            if level == "error":
                error_count += 1

    if not analyzed:
        return Ingested(passed=0, failed=0, errored=0, skipped=0, diagnostics=())
    if error_count:
        return Ingested(
            passed=0,
            failed=error_count,
            errored=0,
            skipped=0,
            diagnostics=tuple(diagnostics),
        )
    # One analyzed unit, clean — the non-vacuous pass classify() requires.
    return Ingested(
        passed=1, failed=0, errored=0, skipped=0, diagnostics=tuple(diagnostics)
    )


def _result_diagnostic(
    result: dict[str, Any], severity: Literal["error", "warning"]
) -> Diagnostic:
    message = result.get("message")
    text = message.get("text") if isinstance(message, dict) else None
    locations = result.get("locations")
    location = locations[0] if isinstance(locations, list) and locations else None
    physical = location.get("physicalLocation") if isinstance(location, dict) else None
    artifact = physical.get("artifactLocation") if isinstance(physical, dict) else None
    region = physical.get("region") if isinstance(physical, dict) else None
    return Diagnostic(
        file=str(artifact.get("uri", "")) if isinstance(artifact, dict) else "",
        line=_as_int(region.get("startLine")) if isinstance(region, dict) else 0,
        col=_as_int(region.get("startColumn")) if isinstance(region, dict) else 0,
        code=str(result.get("ruleId") or ""),
        message=str(text or ""),
        severity=severity,
    )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
