"""Contract tests for the JUnit XML ingester.

The load-bearing property: a report either parses into real counts or raises
``ReportParseError`` — a malformed, empty, or truncated document can never read
as green. defusedxml is pinned by the entity-expansion fixture: stdlib XML would
expand it and parse "successfully".
"""

from __future__ import annotations

import pytest

from hive.verifier.ingest import ReportParseError, ingest_junit
from hive.verifier.result import classify

GREEN = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="0" failures="0" skipped="1" tests="3" time="0.041">
    <testcase classname="tests.test_app" name="test_add" time="0.001" />
    <testcase classname="tests.test_app" name="test_sub" time="0.001" />
    <testcase classname="tests.test_app" name="test_windows_only" time="0.000">
      <skipped type="pytest.skip" message="requires windows" />
    </testcase>
  </testsuite>
</testsuites>
"""

RED = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="1" skipped="0" tests="3" time="0.2">
    <testcase classname="tests.test_app" name="test_ok" />
    <testcase classname="tests.test_app" name="test_broken" file="tests/test_app.py" line="14">
      <failure message="assert 2 == 3">def test_broken(): ...</failure>
    </testcase>
    <testcase classname="tests.test_app" name="test_crashes">
      <error type="RuntimeError" message="db down">traceback</error>
    </testcase>
  </testsuite>
</testsuites>
"""

# A runner that crashed after writing only suite-level attributes: the counts
# live in the attrs, and the errors="2" must not be dropped.
ATTRS_ONLY = (
    b'<testsuites><testsuite name="crashed-runner" tests="4" failures="1" '
    b'errors="2" skipped="1"/></testsuites>'
)

# A parent suite carrying aggregate attrs AND descendant cases: the cases are
# the truth; counting the parent attrs too would double-count.
NESTED = b"""<testsuites>
  <testsuite name="root" tests="2" failures="1">
    <testsuite name="inner">
      <testcase name="a" />
      <testcase name="b"><failure message="nope" /></testcase>
    </testsuite>
  </testsuite>
</testsuites>
"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE bomb [
<!ENTITY a "aaaaaaaaaa">
<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
]>
<testsuites><testsuite tests="1"><testcase name="&c;"/></testsuite></testsuites>
"""


class TestJunitCounts:
    def test_junit_green_counts_cases(self) -> None:
        ingested = ingest_junit(GREEN, exit_code=0)
        assert (
            ingested.passed,
            ingested.failed,
            ingested.errored,
            ingested.skipped,
        ) == (
            2,
            0,
            0,
            1,
        )
        assert not any(d.severity == "error" for d in ingested.diagnostics)
        assert classify(ingested)[0] == "passed"

    def test_junit_red_counts_failure_and_error(self) -> None:
        ingested = ingest_junit(RED, exit_code=1)
        assert (
            ingested.passed,
            ingested.failed,
            ingested.errored,
            ingested.skipped,
        ) == (
            1,
            1,
            1,
            0,
        )
        assert classify(ingested)[0] == "failed"
        errors = [d for d in ingested.diagnostics if d.severity == "error"]
        assert len(errors) == 2
        broken = next(d for d in errors if "test_broken" in d.code)
        assert broken.file == "tests/test_app.py"
        assert broken.line == 14
        assert "assert 2 == 3" in broken.message

    def test_junit_counts_suite_errors(self) -> None:
        # The attrs-only fallback: errors="2" from a case-less leaf suite counts.
        ingested = ingest_junit(ATTRS_ONLY, exit_code=1)
        assert ingested.errored == 2
        assert ingested.failed == 1
        assert ingested.skipped == 1
        assert ingested.passed == 0
        assert classify(ingested)[0] == "failed"

    def test_junit_nested_suites_count_once(self) -> None:
        ingested = ingest_junit(NESTED, exit_code=1)
        assert (ingested.passed, ingested.failed, ingested.errored) == (1, 1, 0)

    def test_junit_root_testsuite_accepted(self) -> None:
        # ctest --output-junit emits a bare <testsuite> root.
        raw = b'<testsuite name="ctest" tests="2"><testcase name="t1"/><testcase name="t2"/></testsuite>'
        ingested = ingest_junit(raw, exit_code=0)
        assert ingested.passed == 2

    def test_junit_multiple_failure_children_count_once(self) -> None:
        raw = (
            b'<testsuite><testcase name="t">'
            b'<failure message="a"/><failure message="b"/>'
            b"</testcase></testsuite>"
        )
        assert ingest_junit(raw, exit_code=1).failed == 1

    def test_junit_zero_case_document_is_not_run(self) -> None:
        ingested = ingest_junit(b"<testsuites></testsuites>", exit_code=0)
        assert (
            ingested.passed,
            ingested.failed,
            ingested.errored,
            ingested.skipped,
        ) == (
            0,
            0,
            0,
            0,
        )
        assert classify(ingested)[0] == "not_run"

    def test_junit_document_beats_exit_code(self) -> None:
        # The XML document is the record; the runner's exit code is not re-read.
        assert ingest_junit(GREEN, exit_code=2) == ingest_junit(GREEN, exit_code=0)


class TestJunitParseFailure:
    def test_junit_malformed_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_junit(b'<testsuites><testsuite name="x"', exit_code=0)

    def test_junit_empty_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_junit(b"", exit_code=0)
        with pytest.raises(ReportParseError):
            ingest_junit(b"   \n\t", exit_code=0)

    def test_junit_truncated_raises(self) -> None:
        # A naive tail-scan would find the first complete <testcase/> and read
        # green; the parse must refuse the whole document instead.
        truncated = (
            b'<testsuites><testsuite tests="2"><testcase name="a"/><testcase nam'
        )
        with pytest.raises(ReportParseError):
            ingest_junit(truncated, exit_code=0)

    def test_junit_non_xml_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_junit(b"vitest v1.6.0\n2 tests passed\n", exit_code=0)

    def test_junit_xml_but_not_junit_raises(self) -> None:
        with pytest.raises(ReportParseError, match="not a JUnit report"):
            ingest_junit(
                b"<html><body><p>502 Bad Gateway</p></body></html>", exit_code=0
            )

    def test_junit_billion_laughs_refused(self) -> None:
        # Pins defusedxml: stdlib ElementTree would expand the entities and
        # return a green one-test report.
        with pytest.raises(ReportParseError):
            ingest_junit(BILLION_LAUGHS, exit_code=0)
