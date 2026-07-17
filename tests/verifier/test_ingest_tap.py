"""Contract tests for the TAP ingester.

The load-bearing properties: a bailed-out run is not a result (it raises), and a
plan/point mismatch means a truncated stream (it raises) — partial TAP can never
read as green. Banner noise, comments, and indented subtest output are tolerated
without ever being counted.
"""

from __future__ import annotations

import pytest

from hive.verifier.ingest import ReportParseError, ingest_tap
from hive.verifier.result import classify

GREEN_BATS = b"1..2\nok 1 addition works\nok 2 subtraction works\n"

GREEN_NODE = b"""TAP version 13
# Subtest: adds
ok 1 - adds
  ---
  duration_ms: 0.55
  ...
# Subtest: subtracts
ok 2 - subtracts
  ---
  duration_ms: 0.21
  ...
1..2
# tests 2
# pass 2
"""

RED = b"1..2\nok 1 works\nnot ok 2 - fails hard\n# expected 1 got 2\n"

DIRECTIVES = (
    b"1..4\n"
    b"ok 1 basic\n"
    b"ok 2 docker case # SKIP no docker daemon\n"
    b"not ok 3 flaky thing # TODO known race\n"
    b"ok 4 more\n"
)

# pg_prove --verbose: raw TAP interleaved with prove-harness lines, including a
# BARE "ok" per-file verdict after the points — noise, never a test point.
GREEN_PG_PROVE = b"""t/schema_test.sql ..
1..1
ok 1 - schema sanity holds
ok
All tests successful.
Files=1, Tests=1,  0 wallclock secs
Result: PASS
"""

RED_PG_PROVE = b"""t/schema_test.sql ..
1..1
not ok 1 - deliberately failing check
# Failed test 1: "deliberately failing check"
Failed 1/1 subtests

Test Summary Report
-------------------
t/schema_test.sql (Wstat: 0 Tests: 1 Failed: 1)
Result: FAIL
"""


class TestTapCounts:
    def test_tap_green_bats_stream(self) -> None:
        ingested = ingest_tap(GREEN_BATS, exit_code=0)
        assert (ingested.passed, ingested.failed, ingested.errored, ingested.skipped) == (
            2,
            0,
            0,
            0,
        )
        assert classify(ingested)[0] == "passed"

    def test_tap_green_node_stream_with_yaml_noise(self) -> None:
        # Version banner, subtest comments, indented YAML blocks, trailing plan:
        # only the two top-level points count.
        ingested = ingest_tap(GREEN_NODE, exit_code=0)
        assert (ingested.passed, ingested.failed, ingested.skipped) == (2, 0, 0)

    def test_tap_pg_prove_verbose_green_bare_ok_verdict_is_noise(self) -> None:
        # The prove harness prints a BARE "ok" as the per-file verdict; a bare
        # verdict is not a test point, so the plan (1) matches the points (1).
        ingested = ingest_tap(GREEN_PG_PROVE, exit_code=0)
        assert (ingested.passed, ingested.failed, ingested.skipped) == (1, 0, 0)
        assert classify(ingested)[0] == "passed"

    def test_tap_pg_prove_verbose_red_names_the_failing_check(self) -> None:
        ingested = ingest_tap(RED_PG_PROVE, exit_code=1)
        assert (ingested.passed, ingested.failed) == (0, 1)
        assert any(
            "deliberately failing check" in diag.message for diag in ingested.diagnostics
        )
        assert classify(ingested)[0] == "failed"

    def test_tap_red_counts_and_diagnostic(self) -> None:
        ingested = ingest_tap(RED, exit_code=1)
        assert (ingested.passed, ingested.failed) == (1, 1)
        assert classify(ingested)[0] == "failed"
        assert len(ingested.diagnostics) == 1
        assert ingested.diagnostics[0].severity == "error"
        assert "fails hard" in ingested.diagnostics[0].message

    def test_tap_skip_and_todo_directives_are_skipped(self) -> None:
        # SKIP and TODO points assert nothing about the code — including a
        # "not ok # TODO", which TAP defines as an expected failure.
        ingested = ingest_tap(DIRECTIVES, exit_code=0)
        assert (ingested.passed, ingested.failed, ingested.skipped) == (2, 0, 2)

    def test_tap_no_plan_with_results_tolerated(self) -> None:
        ingested = ingest_tap(b"# starting\nok 1 x\nnot ok 2 y\n", exit_code=1)
        assert (ingested.passed, ingested.failed) == (1, 1)

    def test_tap_ok_prefix_words_are_not_points(self) -> None:
        ingested = ingest_tap(b"okay then\n1..1\nok 1 fine\n", exit_code=0)
        assert (ingested.passed, ingested.failed) == (1, 0)

    def test_tap_zero_plan_zero_points_is_not_run(self) -> None:
        # "1..0" declares zero tests and delivers zero: a complete, vacuous
        # report — zero counts, which classify() refuses to call a pass.
        ingested = ingest_tap(b"1..0 # Skipped: no tests for this platform\n", exit_code=0)
        assert (ingested.passed, ingested.failed, ingested.errored, ingested.skipped) == (
            0,
            0,
            0,
            0,
        )
        assert classify(ingested)[0] == "not_run"


class TestTapParseFailure:
    def test_tap_bailout_raises(self) -> None:
        # An aborted run is not a result, even with green points before the bail.
        with pytest.raises(ReportParseError, match="abort"):
            ingest_tap(b"1..5\nok 1 boots\nBail out! database unreachable\n", exit_code=1)

    def test_tap_plan_only_no_results_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_tap(b"1..5\n", exit_code=0)

    def test_tap_plan_mismatch_truncated_raises(self) -> None:
        with pytest.raises(ReportParseError, match="truncated"):
            ingest_tap(b"1..5\nok 1\nok 2\n", exit_code=0)

    def test_tap_extra_points_beyond_plan_raise(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_tap(b"1..2\nok 1\nok 2\nok 3\n", exit_code=0)

    def test_tap_malformed_raises(self) -> None:
        # Pure banner noise: no plan, no test points — not a TAP stream.
        with pytest.raises(ReportParseError):
            ingest_tap(b"vitest v1.2.0\n\nRUN src/app.test.ts\n", exit_code=0)

    def test_tap_empty_raises(self) -> None:
        with pytest.raises(ReportParseError):
            ingest_tap(b"", exit_code=0)
        with pytest.raises(ReportParseError):
            ingest_tap(b"  \n\n", exit_code=0)
