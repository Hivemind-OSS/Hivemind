"""c row against real clang -fsyntax-only (native-exit) + ctest (junit file).

The ctest row presupposes a configured+built tree — that build is the target
repo's own responsibility, performed here as fixture setup. The red variant
keeps its class-1 fault in a file outside the test target (bad.c) so the test
binary still builds and class 2 can fail at RUN time with a named case.
"""

from __future__ import annotations

import pytest

from tests.verifier.conformance._harness import FIXTURES, conf_verify, ensure_cmake_build

GREEN = FIXTURES / "c" / "green"
RED = FIXTURES / "c" / "red"


@pytest.mark.requires_tool("clang")
def test_c_typecheck_green() -> None:
    result = conf_verify(GREEN, "calc.c", run_tests=False)
    assert result.typecheck.state == "passed"
    assert result.typecheck.runners == ("clang",)
    assert result.typecheck.report_format == "native-exit"


@pytest.mark.requires_tool("clang")
def test_c_typecheck_red() -> None:
    result = conf_verify(RED, "bad.c", run_tests=False)
    assert result.typecheck.state == "failed"
    assert result.typecheck.failed >= 1
    (diag,) = result.typecheck.diagnostics
    assert diag.code == "native-exit"
    assert "error" in diag.message


@pytest.mark.requires_tool("clang", "ctest")
def test_c_tests_green() -> None:
    ensure_cmake_build(GREEN)
    result = conf_verify(GREEN, "calc.c", "test_calc.c", run_typecheck=False)
    assert result.tests.state == "passed"
    assert result.tests.passed >= 1
    assert result.tests.runners == ("ctest",)


@pytest.mark.requires_tool("clang", "ctest")
def test_c_tests_red_names_the_failing_case() -> None:
    ensure_cmake_build(RED)
    result = conf_verify(RED, "calc.c", "test_calc.c", run_typecheck=False)
    assert result.tests.state == "failed"
    assert result.tests.failed >= 1
    assert any("calc_adds" in diag.code for diag in result.tests.diagnostics)
