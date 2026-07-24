"""Contract tests for the fail-closed verification-result coercion.

The coercion consumes the verification CONTRACT shape, not the package: the
stubs here are plain frozen dataclasses mirroring the contract's fields plus
additive extras (per_lang, diagnostics, mode) that the real result carries and
the census must tolerate. One test round-trips the REAL installed
hive.verifier result through the same coercion to prove the seam holds against
the real shape, and the touched-set projection is exercised against the real
verifier input type — never a redefinition of it. The live-producer tier at
the bottom runs `run_verifier` against the real package over a real
two-commit repo: head-graph test discovery, head-tree execution, SHA/registry
binding, mode plumbing, and the full result→coercion round trip — no fakes.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from hive.census import ChangedFile, ChangeSet, ExecutionClassLine, coerce_execution
from hive.census.diff import Change, open_change
from hive.census.execution import coerce_verifier_stamp, verifier_touched_set

_BASE_SHA = "a" * 40
_HEAD_SHA = "b" * 40


@dataclass(frozen=True)
class _StubClass:
    """A contract-shaped class result plus additive fields to tolerate."""

    state: object = "passed"
    passed: object = 0
    failed: object = 0
    errored: object = 0
    reason: str | None = None
    runners: object = ()
    per_lang: tuple = ()  # additive channel on the real result
    diagnostics: tuple = ()  # additive
    mode: str | None = None  # additive


@dataclass(frozen=True)
class _StubResult:
    typecheck: object | None = None
    tests: object | None = None
    summary: str = "stub"  # additive
    affected: object | None = None  # additive


def test_producer_absent_yields_not_run_unverified() -> None:
    typecheck, tests = coerce_execution(None)
    for line, cls in ((typecheck, "typecheck"), (tests, "tests")):
        assert line.cls == cls
        assert line.state == "not_run"
        assert line.tag == "unverified"
        assert line.reason == "producer-absent"
        assert (line.passed, line.failed, line.errored) == (0, 0, 0)
        assert line.runners == ()


def test_conforming_stub_round_trips_counts_and_runners() -> None:
    result = _StubResult(
        typecheck=_StubClass(state="passed", passed=3, runners=("mypy",)),
        tests=_StubClass(
            state="failed",
            passed=12,
            failed=2,
            errored=1,
            reason="2 failing cases",
            runners=("pytest", "go test"),
            per_lang=("python", "go"),
        ),
    )
    typecheck, tests = coerce_execution(result)
    assert typecheck.state == "passed"
    assert typecheck.tag == "bounded-estimate"
    assert typecheck.passed == 3
    assert typecheck.runners == ("mypy",)
    assert tests.state == "failed"
    assert tests.tag == "bounded-estimate"
    assert (tests.passed, tests.failed, tests.errored) == (12, 2, 1)
    assert tests.runners == ("pytest", "go test")
    assert tests.reason == "2 failing cases"


@pytest.mark.parametrize("raw", ["skipped", "", None, 5, ["passed"]])
def test_non_contract_states_all_coerce_to_errored(raw: object) -> None:
    result = _StubResult(
        typecheck=_StubClass(state=raw),
        tests=_StubClass(state="passed", passed=1),
    )
    typecheck, tests = coerce_execution(result)
    assert typecheck.state == "errored"
    assert typecheck.tag == "unverified"
    assert typecheck.reason == f"unrecognized-state:{raw}"
    assert tests.state == "passed"


def test_missing_class_object_coerces_to_errored_never_raises() -> None:
    class OnlyTypecheck:
        typecheck = _StubClass(state="passed", passed=1)
        # no `tests` attribute at all

    typecheck, tests = coerce_execution(OnlyTypecheck())
    assert typecheck.state == "passed"
    assert tests.state == "errored"
    assert tests.tag == "unverified"
    assert tests.reason == "missing-class"

    # An attribute present but None coerces identically.
    _, none_tests = coerce_execution(_StubResult(typecheck=_StubClass(), tests=None))
    assert none_tests.state == "errored"
    assert none_tests.reason == "missing-class"


def test_authoritative_flips_decided_tags_only() -> None:
    result = _StubResult(
        typecheck=_StubClass(state="passed", passed=2),
        tests=_StubClass(state="not_run", reason="no runner matched"),
    )
    typecheck, tests = coerce_execution(result, authoritative=True)
    assert typecheck.tag == "machine-checked"
    assert tests.state == "not_run"
    assert tests.tag == "unverified"  # an abstention is never upgraded
    assert tests.reason == "no runner matched"


def test_malformed_counts_and_runners_degrade_without_raising() -> None:
    result = _StubResult(
        typecheck=_StubClass(state="passed", passed="3", runners="mypy"),
        tests=_StubClass(state="passed", passed=1, failed=-2, runners=5),
    )
    typecheck, tests = coerce_execution(result)
    assert typecheck.passed == 3
    assert typecheck.runners == ("mypy",)  # a bare string is one runner, not chars
    assert tests.failed == 0  # a nonsense count under-claims to zero
    assert tests.runners == ()


def test_undecided_state_cannot_carry_deciding_tag() -> None:
    for state, tag in (("not_run", "machine-checked"), ("errored", "bounded-estimate")):
        with pytest.raises(ValueError):
            ExecutionClassLine(
                cls="tests",
                state=state,
                passed=0,
                failed=0,
                errored=0,
                tag=tag,
                reason="x",
                runners=(),
            )


def test_carrier_rejects_unknown_state_unknown_tag_negative_counts() -> None:
    good = dict(cls="tests", passed=0, failed=0, errored=0, reason="", runners=())
    with pytest.raises(ValueError):
        ExecutionClassLine(state="skipped", tag="unverified", **good)
    with pytest.raises(ValueError):
        ExecutionClassLine(state="passed", tag="gold-star", **good)
    with pytest.raises(ValueError):
        ExecutionClassLine(
            cls="tests",
            state="passed",
            passed=-1,
            failed=0,
            errored=0,
            tag="bounded-estimate",
            reason="",
            runners=(),
        )


def test_real_verifier_result_coerces_through_the_contract() -> None:
    from hive.verifier import ClassResult, VerifierToolVersion, VerifyResult
    from hive.verifier.result import Affected, LangOutcome

    result = VerifyResult(
        typecheck=ClassResult(
            state="passed",
            ran=True,
            passed=1,
            failed=0,
            errored=0,
            diagnostics=(),
            runners=("mypy",),
            reason=None,
            report_format="mypy-json",
            per_lang=(LangOutcome(lang="python", state="passed", reason=None),),
            mode="scoped",
        ),
        tests=ClassResult(
            state="not_run",
            ran=False,
            passed=0,
            failed=0,
            errored=0,
            diagnostics=(),
            runners=(),
            reason="no runner matched",
            report_format=None,
        ),
        adequacy=None,
        affected=Affected(files=(), symbols=(), tests=()),
        tool_version=VerifierToolVersion(
            head_sha=_HEAD_SHA,
            base_sha=_BASE_SHA,
            tool_versions=(("mypy", "1.8.0"),),
            registry_version="r1",
        ),
        summary="typecheck passed; tests not run",
    )
    typecheck, tests = coerce_execution(result)
    assert typecheck.state == "passed"
    assert typecheck.tag == "bounded-estimate"
    assert typecheck.runners == ("mypy",)
    assert tests.state == "not_run"
    assert tests.tag == "unverified"
    assert tests.reason == "no runner matched"

    assert coerce_verifier_stamp(result) == {
        "head_sha": _HEAD_SHA,
        "base_sha": _BASE_SHA,
        "tool_versions": [["mypy", "1.8.0"]],
        "registry_version": "r1",
    }


def test_stamp_missing_fields_omit_the_block() -> None:
    assert coerce_verifier_stamp(None) is None

    @dataclass(frozen=True)
    class _NoRegistry:  # registry_version absent entirely
        head_sha: str = _HEAD_SHA
        base_sha: str = _BASE_SHA
        tool_versions: tuple = ()

    @dataclass(frozen=True)
    class _Result:
        tool_version: object | None = None

    assert coerce_verifier_stamp(_Result()) is None
    assert coerce_verifier_stamp(_Result(tool_version=_NoRegistry())) is None

    @dataclass(frozen=True)
    class _Complete:
        head_sha: str = _HEAD_SHA
        base_sha: str = _BASE_SHA
        tool_versions: tuple = ()  # empty is present, not missing
        registry_version: str = "r1"

    assert coerce_verifier_stamp(_Result(tool_version=_Complete())) == {
        "head_sha": _HEAD_SHA,
        "base_sha": _BASE_SHA,
        "tool_versions": [],
        "registry_version": "r1",
    }


def _change_set(*files: ChangedFile) -> ChangeSet:
    return ChangeSet(base_sha=_BASE_SHA, head_sha=_HEAD_SHA, files=tuple(files))


def test_projection_unions_head_side_added_lines() -> None:
    from hive.verifier import TouchedFile, TouchedSet

    touched = _change_set(
        ChangedFile(
            path="pkg/mod.py",
            status="modified",
            added_spans=((3, 5), (9, 9), (4, 6)),
            removed_spans=((2, 2),),
        ),
    )
    projected = verifier_touched_set(touched)
    assert isinstance(projected, TouchedSet)
    (one,) = projected.files
    assert isinstance(one, TouchedFile)
    assert one.path == "pkg/mod.py"
    assert one.lines == frozenset({3, 4, 5, 6, 9})


def test_projection_excludes_deleted_and_binary_keeps_renames_and_adds() -> None:
    touched = _change_set(
        ChangedFile(
            path="gone.py", status="deleted", added_spans=(), removed_spans=((1, 10),)
        ),
        ChangedFile(path="img.png", status="binary", added_spans=(), removed_spans=()),
        ChangedFile(
            path="new.py", status="added", added_spans=((1, 4),), removed_spans=()
        ),
        ChangedFile(
            path="moved.py",
            status="renamed",
            added_spans=(),
            removed_spans=(),
            old_path="old.py",
        ),
    )
    projected = verifier_touched_set(touched)
    assert [f.path for f in projected.files] == [
        "moved.py",
        "new.py",
    ]  # sorted, filtered
    by_path = {f.path: f.lines for f in projected.files}
    assert by_path["new.py"] == frozenset({1, 2, 3, 4})
    assert by_path["moved.py"] == frozenset()


# --- live producer tier: run_verifier against the REAL package, no fakes ---

_V_BASE_LIB = "def item():\n    return 1\n"
_V_HEAD_LIB = "def item():\n    return 2\n"
_V_HELPER = "def helper_value():\n    return 3\n"
_V_TEST_HELPER = (
    "from helper import helper_value\n"
    "\n"
    "\n"
    "def test_helper():\n"
    "    assert helper_value()\n"
)


def _verifier_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(os.environ),
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


@pytest.fixture()
def verifier_change(tmp_path: Path):
    """Two-commit repo: head modifies lib.py and ADDS helper.py + its test.

    The added-at-head test file is the discriminating probe: it exists only
    in the head graph (base-graph discovery finds nothing) and only in the
    head worktree (a base-tree spawn cannot run it).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _verifier_git(repo, "init", "-q")
    (repo / "lib.py").write_text(_V_BASE_LIB)
    _verifier_git(repo, "add", "-A")
    _verifier_git(repo, "commit", "-qm", "base")
    (repo / "lib.py").write_text(_V_HEAD_LIB)
    (repo / "helper.py").write_text(_V_HELPER)
    (repo / "test_helper.py").write_text(_V_TEST_HELPER)
    _verifier_git(repo, "add", "-A")
    _verifier_git(repo, "commit", "-qm", "head")
    with open_change(repo, "HEAD~1", "HEAD") as change:
        yield change


@pytest.fixture()
def verifier_graphs(verifier_change: Change, matrix_scratch):
    from hive.census.engines import build_graphs

    return build_graphs(verifier_change)


@pytest.fixture()
def scoped_result(verifier_change: Change, verifier_graphs):
    from hive.census.execution import run_verifier

    return run_verifier(verifier_change, verifier_graphs, depth=2, authoritative=False)


def test_run_verifier_discovers_head_only_test_and_decides(scoped_result) -> None:
    # Head-graph discovery: the test file added at head is reachable only
    # from the head graph, and runnable only inside the head worktree.
    assert any(path.endswith("test_helper.py") for path in scoped_result.affected.tests)
    assert scoped_result.tests.state == "passed"
    assert "pytest" in scoped_result.tests.runners
    assert scoped_result.tests.passed >= 1


def test_run_verifier_binds_shas_and_registry_version(
    verifier_change: Change, scoped_result
) -> None:
    import hive.verifier

    tool_version = scoped_result.tool_version
    assert tool_version.base_sha == verifier_change.touched.base_sha
    assert tool_version.head_sha == verifier_change.touched.head_sha
    assert tool_version.registry_version == hive.verifier.REGISTRY_VERSION


def test_authoritative_mode_rides_the_options(
    verifier_change: Change, verifier_graphs, scoped_result
) -> None:
    from hive.census.execution import run_verifier

    assert scoped_result.tests.mode == "scoped"
    authoritative = run_verifier(
        verifier_change, verifier_graphs, depth=2, authoritative=True
    )
    assert authoritative.tests.mode == "authoritative"


def test_live_result_round_trips_through_coercion(scoped_result) -> None:
    typecheck, tests = coerce_execution(scoped_result)
    # Construction succeeding IS the invariant proof (__post_init__); pin the
    # tag/state coherence on top of it.
    for line in (typecheck, tests):
        if line.state in {"passed", "failed"}:
            assert line.tag == "bounded-estimate"
        else:
            assert line.tag == "unverified"
            assert line.reason
    assert tests.state == "passed"
    assert tests.passed >= 1
    assert "pytest" in tests.runners


def test_run_verifier_bridges_the_head_offset_into_the_options(
    monkeypatch: pytest.MonkeyPatch, verifier_change: Change, verifier_graphs
) -> None:
    # The BUG-039 seam: the head graph speaks the extraction-root dialect, so
    # run_verifier must hand the verifier the HEAD side's offset (head worktree
    # + head graph + head offset, never split) for its own seed/path bridge.
    import dataclasses

    import hive.verifier

    from hive.census.execution import run_verifier

    seen: dict[str, object] = {}

    def spy_verify(root, touched, *, base_sha, head_sha, engine, opts, **kwargs):
        seen["opts"] = opts
        return object()

    monkeypatch.setattr(hive.verifier, "verify", spy_verify)
    bridged = dataclasses.replace(verifier_graphs, head_offset="pkg")
    run_verifier(verifier_change, bridged, depth=2, authoritative=False)
    opts = seen["opts"]
    assert opts.root_offset == "pkg"
    assert opts.depth == 2
