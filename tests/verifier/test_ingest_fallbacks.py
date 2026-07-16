"""Contract tests for the typed per-tool fallbacks and the INGESTERS dispatch.

The load-bearing properties: for the text/stream shapes (tsc, go, cargo) a
nonzero exit whose output yields no parsed diagnostic RAISES — the tool failed
for a reason the parser could not read, and inventing counts would be
fail-open — and native-exit is the floor that never parses and never lies.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hive.verifier.ingest import (
    INGESTERS,
    ReportParseError,
    ingest_cargo,
    ingest_gotext,
    ingest_junit,
    ingest_native_exit,
    ingest_pyright,
    ingest_sarif,
    ingest_tap,
    ingest_tsc,
)
from hive.verifier.result import Ingested, classify

# --- tsc text ---------------------------------------------------------------

TSC_RED = (
    b"src/app.ts(10,5): error TS2322: Type 'string' is not assignable to type 'number'.\n"
    b"src/lib/util.ts(3,1): error TS2551: Property 'foo' does not exist on type 'Bar'.\n"
)


class TestTsc:
    def test_tsc_green_exit_zero(self) -> None:
        ingested = ingest_tsc(b"\nFiles: 42\n", exit_code=0)
        assert (ingested.passed, ingested.failed) == (1, 0)
        assert classify(ingested)[0] == "passed"

    def test_tsc_green_empty_output_exit_zero(self) -> None:
        # tsc --noEmit prints nothing on success.
        assert ingest_tsc(b"", exit_code=0).passed == 1

    def test_tsc_red_diagnostics_parsed(self) -> None:
        ingested = ingest_tsc(TSC_RED, exit_code=2)
        assert (ingested.passed, ingested.failed) == (0, 2)
        first = ingested.diagnostics[0]
        assert first.file == "src/app.ts"
        assert (first.line, first.col) == (10, 5)
        assert first.code == "TS2322"
        assert first.severity == "error"

    def test_tsc_malformed_raises(self) -> None:
        # Nonzero exit with nothing parseable: the failure reason is unreadable.
        with pytest.raises(ReportParseError):
            ingest_tsc(b"Segmentation fault\n", exit_code=139)

    def test_tsc_error_lines_trusted_over_exit_zero(self) -> None:
        assert ingest_tsc(TSC_RED, exit_code=0).failed == 2


# --- pyright --outputjson ----------------------------------------------------


def pyright_doc(*diagnostics: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "version": "1.1.403",
            "generalDiagnostics": list(diagnostics),
            "summary": {"filesAnalyzed": 3, "errorCount": 0, "warningCount": 0},
        }
    ).encode()


PYRIGHT_ERROR = {
    "file": "/repo/src/app.py",
    "severity": "error",
    "message": 'Argument of type "str" cannot be assigned to parameter "n" of type "int"',
    "range": {"start": {"line": 13, "character": 4}, "end": {"line": 13, "character": 9}},
    "rule": "reportArgumentType",
}
PYRIGHT_WARNING = {
    "file": "/repo/src/app.py",
    "severity": "warning",
    "message": 'Import "json" is not accessed',
    "range": {"start": {"line": 0, "character": 7}, "end": {"line": 0, "character": 11}},
    "rule": "reportUnusedImport",
}
PYRIGHT_INFO = {
    "file": "/repo/src/app.py",
    "severity": "information",
    "message": "Analysis note",
    "range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 1}},
}


class TestPyright:
    def test_pyright_green(self) -> None:
        ingested = ingest_pyright(pyright_doc(), exit_code=0)
        assert (ingested.passed, ingested.failed) == (1, 0)
        assert classify(ingested)[0] == "passed"

    def test_pyright_red_mixed_severities(self) -> None:
        ingested = ingest_pyright(
            pyright_doc(PYRIGHT_ERROR, PYRIGHT_WARNING, PYRIGHT_INFO), exit_code=1
        )
        assert (ingested.passed, ingested.failed) == (0, 1)
        # information entries are not evidence; error + warning are carried.
        assert len(ingested.diagnostics) == 2
        error = next(d for d in ingested.diagnostics if d.severity == "error")
        # pyright ranges are 0-based; diagnostics are 1-based.
        assert (error.line, error.col) == (14, 5)
        assert error.code == "reportArgumentType"
        assert error.file == "/repo/src/app.py"

    def test_pyright_malformed_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_pyright(b'{"generalDiagnostics": [', exit_code=1)

    def test_pyright_empty_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_pyright(b"", exit_code=0)

    def test_pyright_missing_general_diagnostics_raises(self) -> None:
        with pytest.raises(ReportParseError, match="generalDiagnostics"):
            ingest_pyright(b'{"summary": {"errorCount": 0}}', exit_code=0)

    def test_pyright_top_level_not_object_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_pyright(b"[]", exit_code=0)

    def test_pyright_non_object_entry_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_pyright(b'{"generalDiagnostics": ["oops"]}', exit_code=0)


# --- go vet / go build text ---------------------------------------------------


class TestGotext:
    def test_gotext_green_empty_exit_zero(self) -> None:
        # go build prints nothing on success.
        ingested = ingest_gotext(b"", exit_code=0)
        assert (ingested.passed, ingested.failed) == (1, 0)
        assert classify(ingested)[0] == "passed"

    def test_gotext_red_build_diagnostic(self) -> None:
        raw = b"# example.com/widget\n./widget.go:7:6: undefined: Frob\n"
        ingested = ingest_gotext(raw, exit_code=1)
        assert (ingested.passed, ingested.failed) == (0, 1)
        diagnostic = ingested.diagnostics[0]
        assert diagnostic.file == "./widget.go"
        assert (diagnostic.line, diagnostic.col) == (7, 6)
        assert "undefined: Frob" in diagnostic.message

    def test_gotext_vet_prefix_and_missing_column(self) -> None:
        ingested = ingest_gotext(b"vet: ./main.go:12: unreachable code\n", exit_code=1)
        assert ingested.failed == 1
        assert (ingested.diagnostics[0].line, ingested.diagnostics[0].col) == (12, 0)

    def test_gotext_malformed_raises(self) -> None:
        # A toolchain failure with no file:line diagnostics is unreadable here.
        with pytest.raises(ReportParseError):
            ingest_gotext(b"go: cannot find main module\n", exit_code=1)


# --- cargo check --message-format=json -----------------------------------------


def cargo_lines(*objs: dict[str, Any]) -> bytes:
    return b"\n".join(json.dumps(o).encode() for o in objs) + b"\n"


CARGO_ARTIFACT = {"reason": "compiler-artifact", "package_id": "widget 0.1.0"}
CARGO_FINISH_OK = {"reason": "build-finished", "success": True}
CARGO_ERROR = {
    "reason": "compiler-message",
    "package_id": "widget 0.1.0",
    "message": {
        "level": "error",
        "message": "mismatched types",
        "code": {"code": "E0308"},
        "spans": [
            {"file_name": "src/lib.rs", "line_start": 7, "column_start": 20, "is_primary": True}
        ],
    },
}
CARGO_ABORT = {
    "reason": "compiler-message",
    "message": {"level": "error", "message": "aborting due to 1 previous error", "code": None, "spans": []},
}
CARGO_WARNING = {
    "reason": "compiler-message",
    "message": {
        "level": "warning",
        "message": "unused variable: `x`",
        "code": {"code": "unused_variables"},
        "spans": [
            {"file_name": "src/lib.rs", "line_start": 2, "column_start": 9, "is_primary": True}
        ],
    },
}


class TestCargo:
    def test_cargo_green(self) -> None:
        ingested = ingest_cargo(cargo_lines(CARGO_ARTIFACT, CARGO_FINISH_OK), exit_code=0)
        assert (ingested.passed, ingested.failed) == (1, 0)
        assert classify(ingested)[0] == "passed"

    def test_cargo_red_counts_spans_and_code(self) -> None:
        raw = cargo_lines(
            CARGO_ERROR, CARGO_ABORT, {"reason": "build-finished", "success": False}
        )
        ingested = ingest_cargo(raw, exit_code=101)
        assert (ingested.passed, ingested.failed) == (0, 2)
        primary = ingested.diagnostics[0]
        assert primary.file == "src/lib.rs"
        assert (primary.line, primary.col) == (7, 20)
        assert primary.code == "E0308"
        assert primary.severity == "error"

    def test_cargo_warning_only_passes(self) -> None:
        ingested = ingest_cargo(cargo_lines(CARGO_WARNING, CARGO_FINISH_OK), exit_code=0)
        assert (ingested.passed, ingested.failed) == (1, 0)
        assert [d.severity for d in ingested.diagnostics] == ["warning"]

    def test_cargo_malformed_raises(self) -> None:
        # A truncated JSON-lines stream: the last line no longer parses. A
        # naive per-line scan that skips bad lines would read the prefix green.
        truncated = cargo_lines(CARGO_ARTIFACT) + b'{"reason":"compiler-mess'
        with pytest.raises(ReportParseError):
            ingest_cargo(truncated, exit_code=101)

    def test_cargo_empty_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_cargo(b"", exit_code=0)

    def test_cargo_nonzero_exit_with_no_errors_raises(self) -> None:
        # cargo failed but no error message survived parsing: unreadable failure.
        with pytest.raises(ReportParseError):
            ingest_cargo(cargo_lines(CARGO_ARTIFACT), exit_code=101)

    def test_cargo_non_object_line_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_cargo(b"42\n", exit_code=0)


# --- native-exit (the floor) ----------------------------------------------------


class TestNativeExit:
    def test_native_exit_zero_passes(self) -> None:
        ingested = ingest_native_exit(b"", exit_code=0)
        assert (ingested.passed, ingested.failed) == (1, 0)
        assert ingested.diagnostics == ()
        assert classify(ingested)[0] == "passed"

    def test_native_exit_nonzero_fails_with_tail(self) -> None:
        raw = b"main.c:3:1: error: unknown type name 'flaot'\n1 error generated.\n"
        ingested = ingest_native_exit(raw, exit_code=1)
        assert (ingested.passed, ingested.failed) == (0, 1)
        diagnostic = ingested.diagnostics[0]
        assert diagnostic.severity == "error"
        assert diagnostic.code == "native-exit"
        assert "unknown type name" in diagnostic.message
        assert "exit code 1" in diagnostic.message
        assert classify(ingested)[0] == "failed"

    def test_native_exit_nonzero_empty_output_still_fails(self) -> None:
        ingested = ingest_native_exit(b"", exit_code=2)
        assert ingested.failed == 1
        assert "exit code 2" in ingested.diagnostics[0].message

    def test_native_exit_tail_is_bounded_and_keeps_the_end(self) -> None:
        raw = b"A" * 10_000 + b"THE-END"
        message = ingest_native_exit(raw, exit_code=1).diagnostics[0].message
        assert message.endswith("THE-END")
        assert len(message) < 2_200


# --- the dispatch ----------------------------------------------------------------


class TestDispatch:
    def test_dispatch_covers_all_formats_exactly(self) -> None:
        assert set(INGESTERS) == {
            "junit",
            "tap",
            "sarif",
            "tsc",
            "pyright",
            "gotest",
            "cargo",
            "native-exit",
        }

    def test_dispatch_maps_each_format_to_its_ingester(self) -> None:
        assert INGESTERS["junit"] is ingest_junit
        assert INGESTERS["tap"] is ingest_tap
        assert INGESTERS["sarif"] is ingest_sarif
        assert INGESTERS["tsc"] is ingest_tsc
        assert INGESTERS["pyright"] is ingest_pyright
        assert INGESTERS["gotest"] is ingest_gotext
        assert INGESTERS["cargo"] is ingest_cargo
        assert INGESTERS["native-exit"] is ingest_native_exit

    def test_dispatch_uniform_signature(self) -> None:
        # Every ingester is callable the one way the orchestrator calls them.
        for ingester in INGESTERS.values():
            result = None
            try:
                result = ingester(b"", exit_code=0)
            except ReportParseError:
                continue  # refusing empty input is a valid answer
            assert isinstance(result, Ingested)

    def test_report_parse_error_is_an_exception(self) -> None:
        assert issubclass(ReportParseError, Exception)
