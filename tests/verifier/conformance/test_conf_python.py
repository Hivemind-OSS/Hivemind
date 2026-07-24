"""python row against real pyright + pytest — plus the zero-doubles e2e legs.

The e2e legs are this tier's deepest proof: the graph comes from the real
matrix extractor over the fixture repo, so engine + registry + rootfind +
spawn + ingest + model compose with no double anywhere in the loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.verifier.conformance._harness import FIXTURES, conf_verify
from hive.verifier.registry import REGISTRY_VERSION
from hive.verifier.touched import TouchedFile, TouchedSet
from hive.verifier.verify import verify

GREEN = FIXTURES / "python" / "green"
RED = FIXTURES / "python" / "red"


@pytest.mark.requires_tool("pyright")
def test_python_typecheck_green() -> None:
    result = conf_verify(GREEN, "calc.py", run_tests=False)
    assert result.typecheck.state == "passed"
    assert result.typecheck.ran is True
    assert result.typecheck.runners == ("pyright",)
    assert result.typecheck.report_format == "pyright"


@pytest.mark.requires_tool("pyright")
def test_python_typecheck_red() -> None:
    result = conf_verify(RED, "calc.py", run_tests=False)
    assert result.typecheck.state == "failed"
    assert result.typecheck.failed >= 1
    assert any(
        diag.severity == "error" and diag.file.endswith("calc.py")
        for diag in result.typecheck.diagnostics
    )


@pytest.mark.xfail(
    reason="BUG-016: the verifier spawns host pytest against the fixture repo; under pytest 9.x the "
    "fixture's bare pyproject loses rootdir pinning, so the spawned run loads the harness conftest and "
    "errors before producing a report (state 'errored'). Tracked in the hivemind bug registry; "
    "documented, not fixed here.",
    strict=False,
)
@pytest.mark.requires_tool("pytest")
def test_python_tests_green() -> None:
    result = conf_verify(GREEN, "calc.py", "test_calc.py", run_typecheck=False)
    assert result.tests.state == "passed"
    assert result.tests.mode == "scoped"
    assert result.tests.passed >= 1
    assert result.tests.runners == ("pytest",)


@pytest.mark.xfail(
    reason="BUG-016: the verifier spawns host pytest against the fixture repo; under pytest 9.x the "
    "fixture's bare pyproject loses rootdir pinning, so the spawned run loads the harness conftest and "
    "errors before producing a report (state 'errored'). Tracked in the hivemind bug registry; "
    "documented, not fixed here.",
    strict=False,
)
@pytest.mark.requires_tool("pytest")
def test_python_tests_red_names_the_failing_test() -> None:
    result = conf_verify(RED, "calc.py", "test_calc.py", run_typecheck=False)
    assert result.tests.state == "failed"
    assert result.tests.failed >= 1
    assert any("test_add_returns_sum" in diag.code for diag in result.tests.diagnostics)


@pytest.mark.requires_tool("pyright", "pytest")
def test_python_e2e_zero_doubles_green(matrix_scratch: Path) -> None:
    import hive.matrix as matrix

    result = verify(
        GREEN,
        TouchedSet((TouchedFile("calc.py", frozenset({2})),)),
        base_sha="conformance-base",
        head_sha="conformance-head",
        engine=matrix.build_graph(GREEN),
    )
    assert result.typecheck.state == "passed"
    assert result.tests.state == "passed"
    assert result.affected.tests == ("test_calc.py",)
    assert result.affected.unsound is True
    assert result.tool_version.registry_version == REGISTRY_VERSION
    assert dict(result.tool_version.tool_versions).keys() == {"pyright", "pytest"}


@pytest.mark.requires_tool("pyright", "pytest")
def test_python_e2e_zero_doubles_red(matrix_scratch: Path) -> None:
    import hive.matrix as matrix

    result = verify(
        RED,
        TouchedSet((TouchedFile("calc.py", frozenset({2})),)),
        base_sha="conformance-base",
        head_sha="conformance-head",
        engine=matrix.build_graph(RED),
    )
    assert result.typecheck.state == "failed"
    assert result.tests.state == "failed"
    assert result.tests.failed >= 1
