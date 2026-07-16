"""TAP stream ingestion (node --test, prove / pg_prove).

A line state machine over the TAP subset: optional version line, one plan,
unindented ``ok`` / ``not ok`` test points with SKIP/TODO directives, and
``Bail out!``. Comments, YAML blocks, and banner noise are tolerated; indented
(subtest) lines are noise too — only top-level points count, so nested harness
output is never double-counted. A point must carry a body (number, description,
or directive): the prove harness prints a BARE ``ok`` as its per-file verdict
(pg_prove --verbose, conformance-proven), and counting it would break the plan.
Ignoring bare verdicts stays fail-closed — with a plan, a miscount still raises
the mismatch; without one, verdict-only streams raise as no-point streams.
Fail-closed: a bailed-out run is not a result, and a plan/point mismatch means
a truncated stream — both raise rather than return partial counts.
"""

from __future__ import annotations

import re

from hive.verifier.ingest import ReportParseError
from hive.verifier.result import Diagnostic, Ingested

_TEST_POINT = re.compile(r"^(not ok|ok)[ \t]+(\S.*)$")
_PLAN = re.compile(r"^1\.\.(\d+)\b")
_BAIL_OUT = re.compile(r"^bail out!", re.IGNORECASE)
_DIRECTIVE = re.compile(r"#\s*(skip|todo)\b", re.IGNORECASE)


def ingest_tap(raw: bytes, *, exit_code: int) -> Ingested:
    text = raw.decode("utf-8", errors="replace")
    plan: int | None = None
    points = passed = failed = skipped = 0
    diagnostics: list[Diagnostic] = []

    for line in text.splitlines():
        if _BAIL_OUT.match(line):
            raise ReportParseError(f"tap: run aborted: {line.strip()}")
        point = _TEST_POINT.match(line)
        if point:
            points += 1
            verdict, rest = point.group(1), point.group(2)
            if _DIRECTIVE.search(rest):
                # SKIP and TODO points assert nothing about the code — a TODO
                # "not ok" is an expected failure, never a failure count.
                skipped += 1
            elif verdict == "ok":
                passed += 1
            else:
                failed += 1
                diagnostics.append(
                    Diagnostic(
                        file="",
                        line=0,
                        col=0,
                        code="tap",
                        message=line.strip(),
                        severity="error",
                    )
                )
            continue
        plan_match = _PLAN.match(line)
        if plan_match:
            plan = int(plan_match.group(1))

    if plan is None and points == 0:
        raise ReportParseError("tap: no plan and no test points — not a TAP stream")
    if plan is not None and points != plan:
        raise ReportParseError(
            f"tap: plan declares {plan} test(s) but {points} test point(s) present "
            "(truncated or corrupted stream)"
        )
    return Ingested(
        passed=passed,
        failed=failed,
        errored=0,
        skipped=skipped,
        diagnostics=tuple(diagnostics),
    )
