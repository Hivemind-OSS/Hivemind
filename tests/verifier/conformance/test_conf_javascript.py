"""javascript row against real eslint (SARIF) + node --test (TAP)."""

from __future__ import annotations

import pytest

from tests.verifier.conformance._harness import FIXTURES, conf_verify

GREEN = FIXTURES / "javascript" / "green"
RED = FIXTURES / "javascript" / "red"


def test_javascript_typecheck_green(js_tools: None) -> None:
    result = conf_verify(GREEN, "lib.js", run_tests=False)
    assert result.typecheck.state == "passed"
    assert result.typecheck.runners == ("eslint",)
    assert result.typecheck.report_format == "sarif"


def test_javascript_typecheck_red(js_tools: None) -> None:
    result = conf_verify(RED, "lib.js", run_tests=False)
    assert result.typecheck.state == "failed"
    assert result.typecheck.failed >= 1
    assert any(
        diag.severity == "error" and diag.code == "no-unused-vars"
        for diag in result.typecheck.diagnostics
    )


@pytest.mark.requires_tool("node")
def test_javascript_tests_green() -> None:
    result = conf_verify(GREEN, "lib.js", "lib.test.js", run_typecheck=False)
    assert result.tests.state == "passed"
    assert result.tests.passed >= 1
    assert result.tests.runners == ("node",)
    assert result.tests.report_format == "tap"


@pytest.mark.requires_tool("node")
def test_javascript_tests_red_names_the_failing_test() -> None:
    result = conf_verify(RED, "lib.js", "lib.test.js", run_typecheck=False)
    assert result.tests.state == "failed"
    assert result.tests.failed >= 1
    assert any("add sums" in diag.message for diag in result.tests.diagnostics)
