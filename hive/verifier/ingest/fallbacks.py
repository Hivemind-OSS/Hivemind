"""Typed per-tool fallbacks for tools without a standard report format.

One parser per tool-native shape — tsc text, pyright --outputjson, go vet/build
text, cargo --message-format=json — plus the native-exit floor. Fail-closed like
every ingester: for the text/stream shapes, a nonzero exit whose output yields
no parsed diagnostic RAISES, because the tool failed for a reason this parser
could not read and inventing counts would be fail-open. Clean analyzer runs
count as one passed unit so a green check is never a vacuous zero-case report.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from hive.verifier.ingest import ReportParseError
from hive.verifier.result import Diagnostic, Ingested

# --- tsc --noEmit text -------------------------------------------------------

_TSC_DIAGNOSTIC = re.compile(r"^(.+?)\((\d+),(\d+)\): error (TS\d+): (.*)$", re.MULTILINE)


def ingest_tsc(raw: bytes, *, exit_code: int) -> Ingested:
    """``path(line,col): error TSnnnn: message`` lines; exit 0 with none is clean."""
    text = raw.decode("utf-8", errors="replace")
    diagnostics = tuple(
        Diagnostic(
            file=match.group(1),
            line=int(match.group(2)),
            col=int(match.group(3)),
            code=match.group(4),
            message=match.group(5).strip(),
            severity="error",
        )
        for match in _TSC_DIAGNOSTIC.finditer(text)
    )
    if diagnostics:
        return Ingested(
            passed=0, failed=len(diagnostics), errored=0, skipped=0, diagnostics=diagnostics
        )
    if exit_code != 0:
        raise ReportParseError(f"tsc: exit code {exit_code} but no diagnostics parsed")
    return Ingested(passed=1, failed=0, errored=0, skipped=0, diagnostics=())


# --- pyright --outputjson ------------------------------------------------------


def ingest_pyright(raw: bytes, *, exit_code: int) -> Ingested:
    """Counts from ``generalDiagnostics``; the JSON document is authoritative."""
    if not raw.strip():
        raise ReportParseError("pyright: empty report")
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReportParseError(f"pyright: malformed JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ReportParseError("pyright: top level is not an object")
    general = doc.get("generalDiagnostics")
    if not isinstance(general, list):
        raise ReportParseError("pyright: missing 'generalDiagnostics' list")

    error_count = 0
    diagnostics: list[Diagnostic] = []
    for entry in general:
        if not isinstance(entry, dict):
            raise ReportParseError("pyright: diagnostic entry is not an object")
        severity = entry.get("severity")
        if severity not in ("error", "warning"):
            continue  # "information" and friends are not evidence
        range_ = entry.get("range")
        start = range_.get("start") if isinstance(range_, dict) else None
        diagnostics.append(
            Diagnostic(
                file=str(entry.get("file") or ""),
                # pyright ranges are 0-based; diagnostics are 1-based.
                line=_as_int(start.get("line")) + 1 if isinstance(start, dict) else 0,
                col=_as_int(start.get("character")) + 1 if isinstance(start, dict) else 0,
                code=str(entry.get("rule") or ""),
                message=str(entry.get("message") or ""),
                severity="error" if severity == "error" else "warning",
            )
        )
        if severity == "error":
            error_count += 1
    if error_count:
        return Ingested(
            passed=0, failed=error_count, errored=0, skipped=0, diagnostics=tuple(diagnostics)
        )
    return Ingested(passed=1, failed=0, errored=0, skipped=0, diagnostics=tuple(diagnostics))


# --- go vet / go build text ------------------------------------------------------

_GO_DIAGNOSTIC = re.compile(r"^(?:vet: )?([^\s:]+\.go):(\d+)(?::(\d+))?: (.+)$", re.MULTILINE)


def ingest_gotext(raw: bytes, *, exit_code: int) -> Ingested:
    """``file.go:line[:col]: message`` lines; exit 0 with none (or no output) is clean."""
    text = raw.decode("utf-8", errors="replace")
    diagnostics = tuple(
        Diagnostic(
            file=match.group(1),
            line=int(match.group(2)),
            col=int(match.group(3) or 0),
            code="go",
            message=match.group(4).strip(),
            severity="error",
        )
        for match in _GO_DIAGNOSTIC.finditer(text)
    )
    if diagnostics:
        return Ingested(
            passed=0, failed=len(diagnostics), errored=0, skipped=0, diagnostics=diagnostics
        )
    if exit_code != 0:
        raise ReportParseError(f"go: exit code {exit_code} but no diagnostics parsed")
    return Ingested(passed=1, failed=0, errored=0, skipped=0, diagnostics=())


# --- cargo check --message-format=json ---------------------------------------------


def ingest_cargo(raw: bytes, *, exit_code: int) -> Ingested:
    """JSON-lines; ``compiler-message`` with level "error" fails the check.

    Any line that does not parse means a truncated or corrupted stream and
    raises — skipping bad lines would read the surviving prefix as green.
    """
    if not raw.strip():
        raise ReportParseError("cargo: empty report")
    error_count = 0
    diagnostics: list[Diagnostic] = []
    for index, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ReportParseError(f"cargo: malformed JSON on line {index}: {exc}") from exc
        if not isinstance(obj, dict):
            raise ReportParseError(f"cargo: line {index} is not a JSON object")
        if obj.get("reason") != "compiler-message":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            raise ReportParseError(f"cargo: compiler-message on line {index} has no message object")
        level = message.get("level")
        if level not in ("error", "warning"):
            continue
        severity: Literal["error", "warning"] = "error" if level == "error" else "warning"
        diagnostics.append(_cargo_diagnostic(message, severity))
        if level == "error":
            error_count += 1
    if error_count:
        return Ingested(
            passed=0, failed=error_count, errored=0, skipped=0, diagnostics=tuple(diagnostics)
        )
    if exit_code != 0:
        raise ReportParseError(f"cargo: exit code {exit_code} but no error messages parsed")
    return Ingested(passed=1, failed=0, errored=0, skipped=0, diagnostics=tuple(diagnostics))


def _cargo_diagnostic(
    message: dict[str, Any], severity: Literal["error", "warning"]
) -> Diagnostic:
    spans = message.get("spans")
    primary = None
    if isinstance(spans, list):
        primary = next(
            (span for span in spans if isinstance(span, dict) and span.get("is_primary")),
            None,
        )
    code = message.get("code")
    return Diagnostic(
        file=str(primary.get("file_name") or "") if primary else "",
        line=_as_int(primary.get("line_start")) if primary else 0,
        col=_as_int(primary.get("column_start")) if primary else 0,
        code=str(code.get("code") or "") if isinstance(code, dict) else "",
        message=str(message.get("message") or ""),
        severity=severity,
    )


# --- native-exit (the floor) ---------------------------------------------------------

_TAIL_CHARS = 2_000


def ingest_native_exit(raw: bytes, *, exit_code: int) -> Ingested:
    """The honest floor: exit 0 is one passed unit; nonzero is one failed unit
    carrying the tail of the tool's output. Never raises — there is no report
    to mis-parse, only an exit code to read."""
    if exit_code == 0:
        return Ingested(passed=1, failed=0, errored=0, skipped=0, diagnostics=())
    tail = raw.decode("utf-8", errors="replace")[-_TAIL_CHARS:].strip()
    message = f"exit code {exit_code}" + (f": {tail}" if tail else "")
    return Ingested(
        passed=0,
        failed=1,
        errored=0,
        skipped=0,
        diagnostics=(
            Diagnostic(
                file="", line=0, col=0, code="native-exit", message=message, severity="error"
            ),
        ),
    )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
