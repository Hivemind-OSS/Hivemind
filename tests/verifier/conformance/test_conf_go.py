"""go row against real go vet (text fallback) + gotestsum (junit file).

The go rows carry no {files}: both tools run the config-dir package tree
(./...), so these legs also prove the files-elided argv shape live.
"""

from __future__ import annotations

import pytest

from tests.verifier.conformance._harness import FIXTURES, conf_verify

GREEN = FIXTURES / "go" / "green"
RED = FIXTURES / "go" / "red"


@pytest.mark.requires_tool("go")
def test_go_typecheck_green() -> None:
    result = conf_verify(GREEN, "calc.go", run_tests=False)
    assert result.typecheck.state == "passed"
    assert result.typecheck.runners == ("go",)
    assert result.typecheck.report_format == "gotest"


@pytest.mark.requires_tool("go")
def test_go_typecheck_red() -> None:
    result = conf_verify(RED, "calc.go", run_tests=False)
    assert result.typecheck.state == "failed"
    assert result.typecheck.failed >= 1
    assert any(
        diag.file.endswith("calc.go") and "Sprintf" in diag.message
        for diag in result.typecheck.diagnostics
    )


@pytest.mark.requires_tool("gotestsum")
def test_go_tests_green() -> None:
    result = conf_verify(GREEN, "calc.go", "calc_test.go", run_typecheck=False)
    assert result.tests.state == "passed"
    assert result.tests.passed >= 1
    assert result.tests.runners == ("gotestsum",)


@pytest.mark.requires_tool("gotestsum")
def test_go_tests_red_names_the_failing_test() -> None:
    result = conf_verify(RED, "calc.go", "calc_test.go", run_typecheck=False)
    assert result.tests.state == "failed"
    assert result.tests.failed >= 1
    assert any("TestAdd" in diag.code for diag in result.tests.diagnostics)
