"""cpp row against real clang++ -fsyntax-only (native-exit) + ctest (junit file).

Same family as the c fixture: the red class-1 fault lives outside the test
target (bad.cpp) so the test binary builds and class 2 fails at run time.
"""

from __future__ import annotations

import pytest

from tests.verifier.conformance._harness import FIXTURES, conf_verify, ensure_cmake_build

GREEN = FIXTURES / "cpp" / "green"
RED = FIXTURES / "cpp" / "red"


@pytest.mark.requires_tool("clang++")
def test_cpp_typecheck_green() -> None:
    result = conf_verify(GREEN, "calc.cpp", run_tests=False)
    assert result.typecheck.state == "passed"
    assert result.typecheck.runners == ("clang++",)
    assert result.typecheck.report_format == "native-exit"


@pytest.mark.requires_tool("clang++")
def test_cpp_typecheck_red() -> None:
    result = conf_verify(RED, "bad.cpp", run_tests=False)
    assert result.typecheck.state == "failed"
    assert result.typecheck.failed >= 1
    (diag,) = result.typecheck.diagnostics
    assert diag.code == "native-exit"
    assert "error" in diag.message


@pytest.mark.requires_tool("clang++", "ctest")
def test_cpp_tests_green() -> None:
    ensure_cmake_build(GREEN)
    result = conf_verify(GREEN, "calc.cpp", "test_calc.cpp", run_typecheck=False)
    assert result.tests.state == "passed"
    assert result.tests.passed >= 1
    assert result.tests.runners == ("ctest",)


@pytest.mark.requires_tool("clang++", "ctest")
def test_cpp_tests_red_names_the_failing_case() -> None:
    ensure_cmake_build(RED)
    result = conf_verify(RED, "calc.cpp", "test_calc.cpp", run_typecheck=False)
    assert result.tests.state == "failed"
    assert result.tests.failed >= 1
    assert any("calc_adds" in diag.code for diag in result.tests.diagnostics)
