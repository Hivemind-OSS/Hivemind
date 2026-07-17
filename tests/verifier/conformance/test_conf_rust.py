"""rust row against real cargo check (JSON messages) + cargo test (native-exit).

A rust compile error necessarily reds both classes (the test binary cannot
build), so the red fixture proves class 2's failed state via the native-exit
floor rather than a named runtime failure — the named-failure path is proven
by the junit/tap languages.
"""

from __future__ import annotations

import pytest

from tests.verifier.conformance._harness import FIXTURES, conf_verify

GREEN = FIXTURES / "rust" / "green"
RED = FIXTURES / "rust" / "red"


@pytest.mark.requires_tool("cargo")
def test_rust_typecheck_green() -> None:
    result = conf_verify(GREEN, "src/lib.rs", run_tests=False)
    assert result.typecheck.state == "passed"
    assert result.typecheck.runners == ("cargo",)
    assert result.typecheck.report_format == "cargo"


@pytest.mark.requires_tool("cargo")
def test_rust_typecheck_red() -> None:
    result = conf_verify(RED, "src/lib.rs", run_tests=False)
    assert result.typecheck.state == "failed"
    assert result.typecheck.failed >= 1
    assert any(
        diag.severity == "error" and diag.file.endswith("lib.rs")
        for diag in result.typecheck.diagnostics
    )


@pytest.mark.requires_tool("cargo")
def test_rust_tests_green() -> None:
    result = conf_verify(GREEN, "src/lib.rs", "tests/calc_test.rs", run_typecheck=False)
    assert result.tests.state == "passed"
    assert result.tests.passed >= 1
    assert result.tests.report_format == "native-exit"


@pytest.mark.requires_tool("cargo")
def test_rust_tests_red() -> None:
    result = conf_verify(RED, "src/lib.rs", "tests/calc_test.rs", run_typecheck=False)
    assert result.tests.state == "failed"
    assert result.tests.failed >= 1
    (diag,) = result.tests.diagnostics
    assert diag.code == "native-exit"
    assert diag.message  # the compiler/test tail rides along
