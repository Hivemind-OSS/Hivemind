"""sql row: real sqlfluff SARIF (class 1), and both class-2 postures —
the degraded abstain path (no pgTAP runner) and the live pg_prove leg."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.verifier.conformance._harness import FIXTURES, conf_verify
from hive.verifier.evidence import tag_tests
from hive.verifier.registry import REGISTRY

GREEN = FIXTURES / "sql" / "green"
RED = FIXTURES / "sql" / "red"


@pytest.mark.requires_tool("sqlfluff")
def test_sql_typecheck_green() -> None:
    result = conf_verify(GREEN, "schema.sql", run_tests=False)
    assert result.typecheck.state == "passed"
    assert result.typecheck.runners == ("sqlfluff",)
    assert result.typecheck.report_format == "sarif"


@pytest.mark.requires_tool("sqlfluff")
def test_sql_typecheck_red() -> None:
    result = conf_verify(RED, "schema.sql", run_tests=False)
    assert result.typecheck.state == "failed"
    assert result.typecheck.failed >= 1
    assert any(
        diag.severity == "error" and diag.code == "PRS"
        for diag in result.typecheck.diagnostics
    )


def test_sql_tests_degraded_env_abstains_with_reason() -> None:
    """With no pgTAP runner installed, class 2 must abstain — not_run with the
    runner named — never pass. Pinned deterministically by pointing the row's
    probe at a binary that exists nowhere, which exercises the real
    probe-fail path in any environment, strict included."""
    row = REGISTRY["sql"]
    assert row.test is not None
    absent = replace(
        row.test, available_probe=("hive-verifier-conformance-absent-tool", "--version")
    )
    registry = {**REGISTRY, "sql": replace(row, test=absent)}
    result = conf_verify(
        GREEN, "schema.sql", "t/schema_test.sql", run_typecheck=False, registry=registry
    )
    assert result.tests.state == "not_run"
    assert result.tests.reason is not None
    assert "pg_prove not installed" in result.tests.reason
    assert tag_tests(result.tests) == "abstain"


def _live_registry(pg_env: tuple[tuple[str, str], ...]) -> dict:
    """The sql row with the operator's PG* connection env riding extra_env —
    the hermetic spawn is minimal by design, so connection settings must be
    delivered explicitly, never inherited."""
    row = REGISTRY["sql"]
    assert row.test is not None
    live = replace(row.test, extra_env=row.test.extra_env + pg_env)
    return {**REGISTRY, "sql": replace(row, test=live)}


@pytest.mark.requires_tool("pg_prove")
def test_sql_tests_live_green(pg_env: tuple[tuple[str, str], ...]) -> None:
    result = conf_verify(
        GREEN,
        "schema.sql",
        "t/schema_test.sql",
        run_typecheck=False,
        registry=_live_registry(pg_env),
    )
    assert result.tests.state == "passed"
    assert result.tests.passed >= 1
    assert result.tests.runners == ("pg_prove",)


@pytest.mark.requires_tool("pg_prove")
def test_sql_tests_live_red_names_the_failing_check(
    pg_env: tuple[tuple[str, str], ...],
) -> None:
    result = conf_verify(
        RED,
        "schema.sql",
        "t/schema_test.sql",
        run_typecheck=False,
        registry=_live_registry(pg_env),
    )
    assert result.tests.state == "failed"
    assert result.tests.failed >= 1
    assert any(
        "deliberately failing check" in diag.message
        for diag in result.tests.diagnostics
    )
