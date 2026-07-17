"""typescript row against real tsc (text fallback) + vitest (junit file)."""

from __future__ import annotations

import pytest

from tests.verifier.conformance._harness import FIXTURES, conf_verify

GREEN = FIXTURES / "typescript" / "green"
RED = FIXTURES / "typescript" / "red"


def test_typescript_typecheck_green(ts_tools: None) -> None:
    result = conf_verify(GREEN, "calc.ts", run_tests=False)
    assert result.typecheck.state == "passed"
    assert result.typecheck.runners == ("tsc",)
    assert result.typecheck.report_format == "tsc"


def test_typescript_typecheck_red(ts_tools: None) -> None:
    result = conf_verify(RED, "calc.ts", run_tests=False)
    assert result.typecheck.state == "failed"
    assert result.typecheck.failed >= 1
    assert any(
        diag.code.startswith("TS") and diag.file.endswith("calc.ts")
        for diag in result.typecheck.diagnostics
    )


def test_typescript_tests_green(ts_tools: None) -> None:
    result = conf_verify(GREEN, "calc.ts", "calc.test.ts", run_typecheck=False)
    assert result.tests.state == "passed"
    assert result.tests.passed >= 1
    assert result.tests.runners == ("vitest",)


def test_typescript_tests_red_names_the_failing_test(ts_tools: None) -> None:
    result = conf_verify(RED, "calc.ts", "calc.test.ts", run_typecheck=False)
    assert result.tests.state == "failed"
    assert result.tests.failed >= 1
    assert any("add sums" in diag.code for diag in result.tests.diagnostics)
