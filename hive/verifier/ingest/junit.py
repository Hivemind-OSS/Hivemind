"""JUnit XML ingestion (pytest --junitxml, gotestsum --junitfile, ctest --output-junit).

Parsed with defusedxml so a hostile report cannot expand entities. Counts come
from ``<testcase>`` children wherever they exist; a case-less LEAF suite falls
back to its own count attributes (a crashed runner often leaves only
``tests=/failures=/errors=``), while suites with descendants never contribute
attributes — that would double-count their children. The document, not the
runner's exit code, is the record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from hive.verifier.ingest import ReportParseError
from hive.verifier.result import Diagnostic, Ingested

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

_ROOT_TAGS = ("testsuites", "testsuite")


def ingest_junit(raw: bytes, *, exit_code: int) -> Ingested:
    if not raw.strip():
        raise ReportParseError("junit: empty report")
    try:
        root = fromstring(raw)
    except (SyntaxError, ValueError, DefusedXmlException) as exc:
        raise ReportParseError(f"junit: malformed XML: {exc}") from exc
    if root.tag not in _ROOT_TAGS:
        raise ReportParseError(f"junit: root element <{root.tag}> is not a JUnit report")

    passed = failed = errored = skipped = 0
    diagnostics: list[Diagnostic] = []

    for case in root.iter("testcase"):
        error_node = case.find("error")
        failure_node = case.find("failure")
        if error_node is not None:
            errored += 1
            diagnostics.append(_case_diagnostic(case, error_node))
        elif failure_node is not None:
            failed += 1
            diagnostics.append(_case_diagnostic(case, failure_node))
        elif case.find("skipped") is not None:
            skipped += 1
        else:
            passed += 1

    for suite in root.iter("testsuite"):
        if suite.find(".//testcase") is not None or suite.find(".//testsuite") is not None:
            continue  # descendants own the counts; attrs here would double-count
        counts = {name: suite.get(name) for name in ("tests", "failures", "errors", "skipped")}
        if all(value is None for value in counts.values()):
            continue  # a bare empty suite contributes nothing
        try:
            total = int(counts["tests"] or 0)
            suite_failed = int(counts["failures"] or 0)
            suite_errored = int(counts["errors"] or 0)
            suite_skipped = int(counts["skipped"] or 0)
        except ValueError as exc:
            raise ReportParseError(f"junit: non-integer suite count attribute: {exc}") from exc
        failed += suite_failed
        errored += suite_errored
        skipped += suite_skipped
        passed += max(total - suite_failed - suite_errored - suite_skipped, 0)

    return Ingested(
        passed=passed,
        failed=failed,
        errored=errored,
        skipped=skipped,
        diagnostics=tuple(diagnostics),
    )


def _case_diagnostic(case: Element, node: Element) -> Diagnostic:
    name = case.get("name") or "<unnamed>"
    classname = case.get("classname") or ""
    message = node.get("message") or (node.text or "").strip() or node.tag
    try:
        line = int(case.get("line") or 0)
    except ValueError:
        line = 0  # a bad location attribute degrades the location, not the report
    return Diagnostic(
        file=case.get("file") or "",
        line=line,
        col=0,
        code=f"{classname}::{name}" if classname else name,
        message=message,
        severity="error",
    )
