"""Contract tests for the fail-closed result model: guards G1-G7 and classify().

The load-bearing property: an illegal result state cannot be CONSTRUCTED, so no
reader ever needs to re-check it — and not_run/errored can never read as a pass.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import pytest

from hive.verifier.result import (
    AdequacyResult,
    Affected,
    ClassResult,
    Diagnostic,
    Ingested,
    LangOutcome,
    VerifierToolVersion,
    VerifyResult,
    classify,
)


def diag(severity: str = "error") -> Diagnostic:
    return Diagnostic(
        file="src/app.py", line=10, col=2, code="E1", message="boom", severity=severity
    )


def cr(**over: Any) -> ClassResult:
    """A valid passed ClassResult, overridable per test."""
    base: dict[str, Any] = dict(
        state="passed",
        ran=True,
        passed=3,
        failed=0,
        errored=0,
        diagnostics=(),
        runners=("pyright",),
        reason=None,
        report_format="pyright",
    )
    base.update(over)
    return ClassResult(**base)


def abstained(**over: Any) -> ClassResult:
    """A valid not_run ClassResult, overridable per test."""
    base: dict[str, Any] = dict(
        state="not_run",
        ran=False,
        passed=0,
        failed=0,
        errored=0,
        diagnostics=(),
        runners=(),
        reason="tool not installed",
        report_format=None,
    )
    base.update(over)
    return ClassResult(**base)


class TestClassResultGuards:
    def test_valid_passed_constructs(self) -> None:
        c = cr()
        assert c.state == "passed" and c.ran

    def test_valid_abstain_constructs(self) -> None:
        c = abstained()
        assert c.state == "not_run" and not c.ran and c.reason

    def test_ran_flag_mismatch_unconstructable(self) -> None:
        with pytest.raises(ValueError, match="G1"):
            cr(ran=False)
        with pytest.raises(ValueError, match="G1"):
            abstained(ran=True)

    def test_passed_with_failures_unconstructable(self) -> None:
        with pytest.raises(ValueError, match="G2"):
            cr(failed=1)

    def test_passed_with_errored_cases_unconstructable(self) -> None:
        with pytest.raises(ValueError, match="G2"):
            cr(errored=1)

    def test_vacuous_green_unconstructable(self) -> None:
        with pytest.raises(ValueError, match="G2"):
            cr(passed=0)

    def test_abstain_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="G3"):
            abstained(reason=None)
        with pytest.raises(ValueError, match="G3"):
            abstained(reason="")
        with pytest.raises(ValueError, match="G3"):
            abstained(state="errored", reason=None)

    def test_failed_requires_failure_counts(self) -> None:
        with pytest.raises(ValueError, match="G4"):
            cr(state="failed", passed=5, failed=0, errored=0)

    def test_failed_with_failures_constructs(self) -> None:
        c = cr(state="failed", failed=2)
        assert c.state == "failed" and c.ran

    def test_passed_with_error_diagnostic_unconstructable(self) -> None:
        with pytest.raises(ValueError, match="G5"):
            cr(diagnostics=(diag("error"),))

    def test_passed_with_warning_diagnostic_allowed(self) -> None:
        assert cr(diagnostics=(diag("warning"),)).state == "passed"

    def test_negative_counts_unconstructable(self) -> None:
        with pytest.raises(ValueError, match="G6"):
            cr(passed=-1)
        with pytest.raises(ValueError, match="G6"):
            cr(state="failed", failed=-2)
        with pytest.raises(ValueError, match="G6"):
            abstained(errored=-1)

    # --- G7: the polyglot merge is fail-closed -------------------------------

    def test_merge_fail_closed_errored_member_under_passed_top(self) -> None:
        with pytest.raises(ValueError, match="G7"):
            cr(
                per_lang=(
                    LangOutcome("python", "passed", None),
                    LangOutcome("go", "errored", "toolchain crashed"),
                )
            )

    def test_merge_fail_closed_failed_member_under_passed_top(self) -> None:
        with pytest.raises(ValueError, match="G7"):
            cr(
                per_lang=(
                    LangOutcome("python", "passed", None),
                    LangOutcome("go", "failed", None),
                )
            )

    def test_merge_fail_closed_requires_a_passing_member(self) -> None:
        with pytest.raises(ValueError, match="G7"):
            cr(per_lang=(LangOutcome("go", "not_run", "toolchain absent"),))

    def test_merge_passed_with_abstained_members_constructs(self) -> None:
        c = cr(
            per_lang=(
                LangOutcome("python", "passed", None),
                LangOutcome("go", "not_run", "toolchain absent"),
            )
        )
        assert c.state == "passed"

    def test_failed_top_may_carry_any_members(self) -> None:
        c = cr(
            state="failed",
            failed=1,
            per_lang=(
                LangOutcome("go", "errored", "boom"),
                LangOutcome("python", "failed", None),
            ),
        )
        assert c.state == "failed"

    def test_mode_rides(self) -> None:
        assert cr(mode="scoped").mode == "scoped"
        assert cr().mode is None

    def test_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            cr().state = "failed"  # type: ignore[misc]


class TestIngested:
    def test_valid_constructs(self) -> None:
        i = Ingested(
            passed=2, failed=1, errored=0, skipped=3, diagnostics=(diag("warning"),)
        )
        assert i.passed == 2 and len(i.diagnostics) == 1

    def test_negative_counts_rejected(self) -> None:
        for field in ("passed", "failed", "errored", "skipped"):
            kwargs: dict[str, Any] = dict(
                passed=0, failed=0, errored=0, skipped=0, diagnostics=()
            )
            kwargs[field] = -1
            with pytest.raises(ValueError, match=">= 0"):
                Ingested(**kwargs)


class TestClassify:
    def test_zero_cases_is_not_run(self) -> None:
        assert classify(Ingested(0, 0, 0, 0, ())) == (
            "not_run",
            "report parsed but zero cases ran",
        )
        # Skipped-only runs executed nothing either — still never a vacuous pass.
        assert classify(Ingested(0, 0, 0, 7, ())) == (
            "not_run",
            "report parsed but zero cases ran",
        )

    def test_failures_classify_failed(self) -> None:
        assert classify(Ingested(2, 1, 0, 0, ())) == ("failed", None)
        assert classify(Ingested(0, 0, 1, 0, ())) == ("failed", None)

    def test_clean_run_classifies_passed(self) -> None:
        assert classify(Ingested(5, 0, 0, 2, ())) == ("passed", None)
        assert classify(Ingested(1, 0, 0, 0, ())) == ("passed", None)


class TestCarriers:
    def test_adequacy_value_bounds(self) -> None:
        assert AdequacyResult(kind="mutation", value=0.0, detail="floor").value == 0.0
        assert AdequacyResult(kind="coverage", value=1.0, detail="ceiling").value == 1.0
        with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
            AdequacyResult(kind="mutation", value=1.1, detail="")
        with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
            AdequacyResult(kind="mutation", value=-0.1, detail="")
        with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
            AdequacyResult(kind="mutation", value=math.nan, detail="")

    def test_affected_defaults_unsound(self) -> None:
        a = Affected(files=("a.py",), symbols=("a.f",), tests=("tests/test_a.py",))
        assert a.unsound is True

    def test_verify_result_composes(self) -> None:
        result = VerifyResult(
            typecheck=cr(),
            tests=cr(state="failed", failed=1, mode="scoped"),
            adequacy=None,
            affected=Affected(files=(), symbols=(), tests=()),
            tool_version=VerifierToolVersion(
                head_sha="a" * 40,
                base_sha="b" * 40,
                tool_versions=(("pyright", "1.1.411"),),
                registry_version="r1",
            ),
            summary="typecheck passed; tests failed (1)",
        )
        assert result.typecheck.state == "passed"
        assert result.tests.mode == "scoped"
        assert result.adequacy is None
        assert result.tool_version.registry_version == "r1"
