"""Contract tests for the SARIF ingester.

The load-bearing properties: the document spine (object -> runs list -> results
list) is shape-checked and raises when out of contract — coerce-before-index,
never a KeyError — and a truncated document (the vitest-footgun shape: a naive
tail-scan would still "find" results) always raises.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hive.verifier.ingest import ReportParseError, ingest_sarif
from hive.verifier.result import classify


def sarif(*runs: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": list(runs),
        }
    ).encode()


def sqlfluff_run(*results: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": {"driver": {"name": "sqlfluff", "version": "4.0.0"}},
        "results": list(results),
    }


def finding(level: str | None, rule: str = "CP01", line: int = 12, col: int = 8) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ruleId": rule,
        "message": {"text": "Keywords must be consistently upper case."},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": "models/query.sql"},
                    "region": {"startLine": line, "startColumn": col},
                }
            }
        ],
    }
    if level is not None:
        result["level"] = level
    return result


class TestSarifCounts:
    def test_sarif_green_clean_run(self) -> None:
        ingested = ingest_sarif(sarif(sqlfluff_run()), exit_code=0)
        assert (ingested.passed, ingested.failed, ingested.errored) == (1, 0, 0)
        assert ingested.diagnostics == ()
        assert classify(ingested)[0] == "passed"

    def test_sarif_red_error_result_with_location(self) -> None:
        raw = sarif(sqlfluff_run(finding("error"), finding("warning", rule="LT01", line=3, col=1)))
        ingested = ingest_sarif(raw, exit_code=1)
        assert (ingested.passed, ingested.failed) == (0, 1)
        assert classify(ingested)[0] == "failed"
        error = next(d for d in ingested.diagnostics if d.severity == "error")
        assert error.file == "models/query.sql"
        assert error.line == 12
        assert error.col == 8
        assert error.code == "CP01"
        assert "upper case" in error.message
        assert any(d.severity == "warning" for d in ingested.diagnostics)

    def test_sarif_warning_only_run_passes_with_advisory(self) -> None:
        # sqlfluff exits 1 on any finding; warnings alone are still a clean check.
        ingested = ingest_sarif(sarif(sqlfluff_run(finding("warning"))), exit_code=1)
        assert (ingested.passed, ingested.failed) == (1, 0)
        assert [d.severity for d in ingested.diagnostics] == ["warning"]

    def test_sarif_default_level_is_warning(self) -> None:
        ingested = ingest_sarif(sarif(sqlfluff_run(finding(None))), exit_code=0)
        assert ingested.failed == 0
        assert [d.severity for d in ingested.diagnostics] == ["warning"]

    def test_sarif_note_results_are_not_evidence(self) -> None:
        ingested = ingest_sarif(sarif(sqlfluff_run(finding("note"))), exit_code=0)
        assert ingested.failed == 0
        assert ingested.diagnostics == ()

    def test_sarif_multi_run_aggregation(self) -> None:
        raw = sarif(sqlfluff_run(), sqlfluff_run(finding("error")))
        ingested = ingest_sarif(raw, exit_code=1)
        assert ingested.failed == 1

    def test_sarif_zero_runs_is_not_run(self) -> None:
        # runs: [] means nothing was analyzed — never a clean pass.
        ingested = ingest_sarif(sarif(), exit_code=0)
        assert (ingested.passed, ingested.failed, ingested.errored, ingested.skipped) == (
            0,
            0,
            0,
            0,
        )
        assert classify(ingested)[0] == "not_run"


class TestSarifParseFailure:
    def test_sarif_malformed_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_sarif(b'{"version": "2.1.0", not json', exit_code=0)

    def test_sarif_empty_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_sarif(b"", exit_code=0)
        with pytest.raises(ReportParseError):
            ingest_sarif(b" \n ", exit_code=0)

    def test_sarif_truncated_vitest_footgun_raises(self) -> None:
        # The exact footgun shape: cut mid-document, a brace-scanner would still
        # "successfully" read zero-or-some results. The parse must refuse.
        full = sarif(sqlfluff_run(finding("error")))
        truncated = full[: full.index(b'"CP01"') + 4]
        with pytest.raises(ReportParseError):
            ingest_sarif(truncated, exit_code=1)

    def test_sarif_top_level_not_object_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_sarif(b'["runs"]', exit_code=0)

    def test_sarif_missing_runs_raises(self) -> None:
        with pytest.raises(ReportParseError, match="runs"):
            ingest_sarif(b'{"version": "2.1.0"}', exit_code=0)

    def test_sarif_runs_not_list_raises(self) -> None:
        with pytest.raises(ReportParseError, match="runs"):
            ingest_sarif(b'{"runs": {}}', exit_code=0)

    def test_sarif_run_entry_not_object_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_sarif(b'{"runs": [42]}', exit_code=0)

    def test_sarif_run_missing_results_raises(self) -> None:
        with pytest.raises(ReportParseError, match="results"):
            ingest_sarif(json.dumps({"runs": [{"tool": {}}]}).encode(), exit_code=0)

    def test_sarif_result_entry_not_object_raises(self) -> None:
        raw = json.dumps({"runs": [{"results": ["oops"]}]}).encode()
        with pytest.raises(ReportParseError):
            ingest_sarif(raw, exit_code=0)
