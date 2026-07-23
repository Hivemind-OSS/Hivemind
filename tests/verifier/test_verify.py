"""Contracts over the verify() orchestrator — composition, fail-closed.

The unit tier drives the REAL matrix.blast_radius over small hand-built
nx.DiGraphs (no fake engine — the engine seam has no second implementation)
and injects spawn doubles at the one declared seam (the real spawn path is the
conformance tier's job). The headline invariant: not_run/errored NEVER read as
pass — per group, per language, per class, and in final assembly.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import networkx as nx
import pytest

from hive.verifier.registry import (
    REGISTRY,
    REGISTRY_VERSION,
    LangRecipe,
    ToolRecipe,
    all_test_globs,
)
from hive.verifier.result import LangOutcome, VerifyResult
from hive.verifier.spawn import SpawnResult
from hive.verifier.touched import TouchedFile, TouchedSet
from hive.verifier.verify import VerifyOptions, verify

# The package top level re-exports the verify FUNCTION under the same name as
# its module, so the module object (the monkeypatch target for the engine
# seam) must come from the import system, not attribute lookup.
verify_mod = importlib.import_module("hive.verifier.verify")

# --- report fixtures (real-shaped, parsed by the real ingesters) --------------------

_PYRIGHT_GREEN = json.dumps({"generalDiagnostics": []}).encode()
_JUNIT_GREEN = (
    b'<?xml version="1.0"?><testsuites><testsuite name="s">'
    b'<testcase classname="t" name="test_ok"/></testsuite></testsuites>'
)
_JUNIT_RED = (
    b'<?xml version="1.0"?><testsuites><testsuite name="s">'
    b'<testcase classname="t" name="test_bad"><failure message="boom"/></testcase>'
    b"</testsuite></testsuites>"
)
_TAP_GREEN = b"1..1\nok 1 works\n"

_PROBE_TAILS = ("--version", "version")


def _ok(argv, stdout=b"", stderr=b"", exit_code=0, truncated=False) -> SpawnResult:
    return SpawnResult(
        argv=tuple(argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
        truncated=truncated,
        duration_s=0.01,
    )


def _timed_out(argv) -> SpawnResult:
    return SpawnResult(
        argv=tuple(argv),
        exit_code=None,
        stdout=b"",
        stderr=b"",
        timed_out=True,
        truncated=False,
        duration_s=0.01,
    )


def _write_report(argv: tuple[str, ...], flag: str, content: bytes) -> None:
    Path(argv[argv.index(flag) + 1]).write_bytes(content)


def _default_recipe(argv: tuple[str, ...], cwd: Path) -> SpawnResult:
    tool = argv[0]
    if tool == "pyright":
        return _ok(argv, stdout=_PYRIGHT_GREEN)
    if tool == "pytest":
        _write_report(argv, "--junitxml", _JUNIT_GREEN)
        return _ok(argv)
    if tool == "gotestsum":
        _write_report(argv, "--junitfile", _JUNIT_GREEN)
        return _ok(argv)
    if tool == "go":
        return _ok(argv)
    if tool == "pg_prove":
        return _ok(argv, stdout=_TAP_GREEN)
    raise AssertionError(f"unexpected spawn: {argv}")


class ScriptedSpawn:
    """Injected spawn double: probes answered generically, recipe argvs routed
    to per-tool overrides (default: green), every call recorded."""

    def __init__(self, recipe_overrides=None, probe_fail: frozenset[str] = frozenset()):
        self.calls: list[SimpleNamespace] = []
        self._overrides = dict(recipe_overrides or {})
        self._probe_fail = set(probe_fail)

    def __call__(self, argv, *, cwd, timeout_s, extra_env=(), **kwargs) -> SpawnResult:
        argv = tuple(str(part) for part in argv)
        self.calls.append(
            SimpleNamespace(
                argv=argv,
                cwd=Path(cwd),
                timeout_s=timeout_s,
                extra_env=tuple(extra_env),
            )
        )
        if argv[-1] in _PROBE_TAILS:
            if argv[0] in self._probe_fail:
                return _ok(argv, stderr=b"command not found", exit_code=127)
            return _ok(argv, stdout=f"{argv[0]} 1.0".encode())
        if argv[0] in self._overrides:
            return self._overrides[argv[0]](argv, Path(cwd))
        return _default_recipe(argv, Path(cwd))

    def recipe_calls(self, tool: str | None = None) -> list[SimpleNamespace]:
        calls = [c for c in self.calls if c.argv[-1] not in _PROBE_TAILS]
        return [c for c in calls if tool is None or c.argv[0] == tool]


# --- repo + graph builders ---------------------------------------------------------


def _mkrepo(tmp_path: Path, *paths: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    return repo


def _graph(touched: str, test_file: str | None, lang: str = "python") -> nx.DiGraph:
    """touched file --contains--> symbol <--calls-- test symbol (in test_file)."""
    g = nx.DiGraph()
    g.add_node(touched, label=touched, file_type="file", source_file=touched)
    g.add_node("sym.foo", label="foo()", file_type=lang, source_file=touched)
    g.add_edge(touched, "sym.foo", relation="contains")
    if test_file is not None:
        g.add_node(
            "sym.test_foo", label="test_foo()", file_type=lang, source_file=test_file
        )
        g.add_edge("sym.test_foo", "sym.foo", relation="calls")
    return g


_PY_FILES = ("pyproject.toml", "src/app.py", "tests/test_app.py")
_PY_TOUCHED = TouchedSet((TouchedFile("src/app.py", frozenset({3})),))


def _py_verify(tmp_path: Path, spawn: ScriptedSpawn, **opt_kwargs) -> VerifyResult:
    repo = _mkrepo(tmp_path, *_PY_FILES)
    return verify(
        repo,
        _PY_TOUCHED,
        base_sha="base0000",
        head_sha="head1111",
        engine=_graph("src/app.py", "tests/test_app.py"),
        opts=VerifyOptions(**opt_kwargs) if opt_kwargs else VerifyOptions(),
        spawn=spawn,
    )


# --- the happy path ------------------------------------------------------------------


def test_happy_path_python_typecheck_and_scoped_tests_pass(tmp_path: Path) -> None:
    spawn = ScriptedSpawn()
    result = _py_verify(tmp_path, spawn)

    assert result.typecheck.state == "passed"
    assert result.typecheck.ran is True
    assert result.typecheck.mode is None
    assert result.typecheck.runners == ("pyright",)
    assert result.typecheck.report_format == "pyright"
    assert result.typecheck.per_lang == (
        LangOutcome(lang="python", state="passed", reason=None),
    )

    assert result.tests.state == "passed"
    assert result.tests.mode == "scoped"
    assert result.tests.runners == ("pytest",)
    assert result.tests.passed == 1

    assert result.affected.tests == ("tests/test_app.py",)
    assert result.affected.unsound is True
    assert "sym.test_foo" in result.affected.symbols
    assert result.adequacy is None


def test_scoped_argv_carries_affected_test_files_from_the_config_dir(
    tmp_path: Path,
) -> None:
    spawn = ScriptedSpawn()
    repo = _mkrepo(tmp_path, *_PY_FILES)
    verify(
        repo,
        _PY_TOUCHED,
        base_sha="b",
        head_sha="h",
        engine=_graph("src/app.py", "tests/test_app.py"),
        spawn=spawn,
    )
    (pyright_call,) = spawn.recipe_calls("pyright")
    assert pyright_call.argv == ("pyright", "--outputjson", "src/app.py")
    assert pyright_call.cwd == repo
    (pytest_call,) = spawn.recipe_calls("pytest")
    assert pytest_call.argv[0:2] == ("pytest", "--junitxml")
    assert pytest_call.argv[2].endswith("report.xml")
    assert pytest_call.argv[3:] == ("tests/test_app.py",)
    assert pytest_call.cwd == repo


def test_authoritative_elides_files_and_records_mode(tmp_path: Path) -> None:
    spawn = ScriptedSpawn()
    result = _py_verify(tmp_path, spawn, authoritative=True)
    assert result.tests.state == "passed"
    assert result.tests.mode == "authoritative"
    (pytest_call,) = spawn.recipe_calls("pytest")
    assert pytest_call.argv == ("pytest", "--junitxml", pytest_call.argv[2])
    assert "tests/test_app.py" not in pytest_call.argv


# --- the engine seam (real blast_radius) ----------------------------------------------


def test_blast_radius_receives_the_registry_glob_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import matrix

    seen: list[object] = []

    def spy(graph, seed, **kwargs):
        seen.append(kwargs.get("test_globs"))
        return matrix.blast_radius(graph, seed, **kwargs)

    monkeypatch.setattr(verify_mod, "_blast_radius", spy)
    result = _py_verify(tmp_path, ScriptedSpawn())
    assert result.tests.state == "passed"
    assert seen, "blast_radius was never consulted"
    assert all(globs == all_test_globs() for globs in seen)


def test_pgtap_test_is_reached_only_via_the_union_globs(tmp_path: Path) -> None:
    # t/*.sql (pgTAP layout) is invisible to the engine's default globs: this
    # scoped run only finds its affected test because the registry union rides
    # the kwarg.
    repo = _mkrepo(tmp_path, "db/schema.sql", "t/schema.sql")
    spawn = ScriptedSpawn()
    result = verify(
        repo,
        TouchedSet((TouchedFile("db/schema.sql", frozenset({1})),)),
        base_sha="b",
        head_sha="h",
        engine=_graph("db/schema.sql", "t/schema.sql", lang="sql"),
        spawn=spawn,
    )
    assert result.affected.tests == ("t/schema.sql",)
    assert result.tests.state == "passed"
    (pg_prove_call,) = spawn.recipe_calls("pg_prove")
    assert pg_prove_call.argv == ("pg_prove", "--verbose", "t/schema.sql")


def test_zero_affected_tests_is_not_run_lower_bound_never_passed(
    tmp_path: Path,
) -> None:
    repo = _mkrepo(tmp_path, *_PY_FILES)
    result = verify(
        repo,
        _PY_TOUCHED,
        base_sha="b",
        head_sha="h",
        engine=_graph("src/app.py", test_file=None),
        spawn=ScriptedSpawn(),
    )
    assert result.tests.state == "not_run"
    assert result.tests.ran is False
    assert result.tests.reason == "no affected tests in blast radius (lower bound)"
    assert result.typecheck.state == "passed"  # class 1 needs no engine


def test_engine_raise_lands_tests_errored_typecheck_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(graph, seed, **kwargs):
        raise RuntimeError("graph exploded")

    monkeypatch.setattr(verify_mod, "_blast_radius", boom)
    result = _py_verify(tmp_path, ScriptedSpawn())
    assert result.tests.state == "errored"
    assert result.tests.reason is not None
    assert result.tests.reason.startswith("engine blast_radius failed:")
    assert "graph exploded" in result.tests.reason
    assert result.typecheck.state == "passed"
    assert result.affected.tests == ()


# --- probe gating -------------------------------------------------------------------


def test_probe_fail_abstains_that_language_and_others_proceed(tmp_path: Path) -> None:
    repo = _mkrepo(tmp_path, "pyproject.toml", "src/app.py", "mod/go.mod", "mod/a.go")
    spawn = ScriptedSpawn(probe_fail=frozenset({"pyright"}))
    result = verify(
        repo,
        TouchedSet(
            (
                TouchedFile("src/app.py", frozenset({1})),
                TouchedFile("mod/a.go", frozenset({1})),
            )
        ),
        base_sha="b",
        head_sha="h",
        engine=nx.DiGraph(),
        spawn=spawn,
    )
    by_lang = {o.lang: o for o in result.typecheck.per_lang}
    assert by_lang["python"].state == "not_run"
    assert by_lang["python"].reason is not None
    assert "pyright" in by_lang["python"].reason
    assert "not installed" in by_lang["python"].reason
    assert by_lang["go"].state == "passed"
    assert result.typecheck.state == "passed"  # go proceeded; python abstained
    assert "python" in result.summary  # the summary names the abstained language


def test_probe_gate_never_spawns_an_absent_tool(tmp_path: Path) -> None:
    repo = _mkrepo(tmp_path, "pyproject.toml", "src/app.py")
    spawn = ScriptedSpawn(probe_fail=frozenset({"pyright"}))
    result = verify(
        repo,
        _PY_TOUCHED,
        base_sha="b",
        head_sha="h",
        engine=nx.DiGraph(),
        spawn=spawn,
    )
    assert result.typecheck.state == "not_run"
    assert spawn.recipe_calls("pyright") == [], (
        "a recipe spawn of an absent tool is the wrong failure shape"
    )


# --- per-group fail-closed shapes ---------------------------------------------------


def test_spawn_timeout_is_errored_never_passed(tmp_path: Path) -> None:
    spawn = ScriptedSpawn(
        recipe_overrides={"pyright": lambda argv, cwd: _timed_out(argv)}
    )
    result = _py_verify(tmp_path, spawn)
    assert result.typecheck.state == "errored"
    assert result.typecheck.reason is not None
    assert "timed out" in result.typecheck.reason
    assert result.tests.state == "passed"


def test_report_file_is_minted_and_a_red_report_reads_failed(tmp_path: Path) -> None:
    def red_pytest(argv, cwd):
        _write_report(argv, "--junitxml", _JUNIT_RED)
        return _ok(argv, exit_code=1)

    spawn = ScriptedSpawn(recipe_overrides={"pytest": red_pytest})
    result = _py_verify(tmp_path, spawn)
    assert result.tests.state == "failed"
    assert result.tests.failed == 1
    assert any("test_bad" in d.code for d in result.tests.diagnostics)


def test_absent_report_file_is_errored_not_a_crash(tmp_path: Path) -> None:
    spawn = ScriptedSpawn(recipe_overrides={"pytest": lambda argv, cwd: _ok(argv)})
    result = _py_verify(tmp_path, spawn)
    assert result.tests.state == "errored"
    assert result.tests.reason is not None
    assert "report" in result.tests.reason
    assert result.typecheck.state == "passed"


def test_truncated_stdout_report_is_errored(tmp_path: Path) -> None:
    spawn = ScriptedSpawn(
        recipe_overrides={
            "pyright": lambda argv, cwd: _ok(
                argv, stdout=_PYRIGHT_GREEN, truncated=True
            )
        }
    )
    result = _py_verify(tmp_path, spawn)
    assert result.typecheck.state == "errored"
    assert result.typecheck.reason is not None
    assert "truncated" in result.typecheck.reason


def test_truncated_streams_do_not_invalidate_a_file_report(tmp_path: Path) -> None:
    def noisy_pytest(argv, cwd):
        _write_report(argv, "--junitxml", _JUNIT_GREEN)
        return _ok(argv, truncated=True)  # the FILE is the record, not the stream

    spawn = ScriptedSpawn(recipe_overrides={"pytest": noisy_pytest})
    result = _py_verify(tmp_path, spawn)
    assert result.tests.state == "passed"


def test_one_group_internal_raise_lands_that_class_errored_other_intact(
    tmp_path: Path,
) -> None:
    def exploding(argv, cwd):
        raise RuntimeError("group blew up")

    spawn = ScriptedSpawn(recipe_overrides={"pyright": exploding})
    result = _py_verify(tmp_path, spawn)
    assert result.typecheck.state == "errored"
    assert result.typecheck.reason is not None
    assert "group blew up" in result.typecheck.reason
    assert result.tests.state == "passed"  # the other class still completed


# --- the polyglot merge (D4) ----------------------------------------------------------


def test_polyglot_merge_names_recipeless_language_and_summary_reports_it(
    tmp_path: Path,
) -> None:
    # No shipped language is class-1 recipeless, so an injected typecheck=None
    # row supplies the case: it must abstain (not_run, "no typecheck recipe")
    # without dragging the passing python member down.
    recipeless = LangRecipe(
        lang="conf",
        extensions=(".conf",),
        test_globs=(),
        root_markers=(),
        typecheck=None,
        test=None,
    )
    repo = _mkrepo(tmp_path, "pyproject.toml", "src/app.py", "etc/app.conf")
    result = verify(
        repo,
        TouchedSet(
            (
                TouchedFile("src/app.py", frozenset({1})),
                TouchedFile("etc/app.conf", frozenset({1})),
            )
        ),
        base_sha="b",
        head_sha="h",
        engine=nx.DiGraph(),
        spawn=ScriptedSpawn(),
        opts=VerifyOptions(registry={"python": REGISTRY["python"], "conf": recipeless}),
    )
    by_lang = {o.lang: o for o in result.typecheck.per_lang}
    assert by_lang["python"].state == "passed"
    assert by_lang["conf"].state == "not_run"
    assert by_lang["conf"].reason is not None
    assert "no typecheck recipe" in by_lang["conf"].reason
    assert result.typecheck.state == "passed"
    assert "conf" in result.summary


def test_errored_member_is_never_shadowed_by_a_passed_one(tmp_path: Path) -> None:
    # THE fail-closed assembly invariant: one language erroring drags the
    # merged class to errored no matter how green its siblings are.
    repo = _mkrepo(tmp_path, "pyproject.toml", "src/app.py", "mod/go.mod", "mod/a.go")
    spawn = ScriptedSpawn(recipe_overrides={"go": lambda argv, cwd: _timed_out(argv)})
    result = verify(
        repo,
        TouchedSet(
            (
                TouchedFile("src/app.py", frozenset({1})),
                TouchedFile("mod/a.go", frozenset({1})),
            )
        ),
        base_sha="b",
        head_sha="h",
        engine=nx.DiGraph(),
        spawn=spawn,
    )
    by_lang = {o.lang: o for o in result.typecheck.per_lang}
    assert by_lang["python"].state == "passed"
    assert by_lang["go"].state == "errored"
    assert result.typecheck.state == "errored"


# --- registry-row realities the orchestrator must tolerate -----------------------------


def test_go_rows_without_files_placeholder_run_per_config_dir(tmp_path: Path) -> None:
    repo = _mkrepo(tmp_path, "mod/go.mod", "mod/pkg/a.go", "mod/pkg/a_test.go")
    spawn = ScriptedSpawn()
    result = verify(
        repo,
        TouchedSet((TouchedFile("mod/pkg/a.go", frozenset({1})),)),
        base_sha="b",
        head_sha="h",
        engine=_graph("mod/pkg/a.go", "mod/pkg/a_test.go", lang="go"),
        spawn=spawn,
    )
    assert result.typecheck.state == "passed"
    assert result.tests.state == "passed"
    (vet_call,) = spawn.recipe_calls("go")
    assert vet_call.argv == ("go", "vet", "./...")  # no {files}: tolerated as-is
    assert vet_call.cwd == repo / "mod"  # nearest go.mod dir, not the repo root
    (gotestsum_call,) = spawn.recipe_calls("gotestsum")
    assert gotestsum_call.argv[-1] == "./..."
    assert "a_test.go" not in " ".join(gotestsum_call.argv)
    assert gotestsum_call.cwd == repo / "mod"


def test_injected_registry_rows_drive_recipes_env_and_timeout(tmp_path: Path) -> None:
    row = LangRecipe(
        lang="python",
        extensions=(".py",),
        test_globs=("test_*.py",),
        root_markers=(),
        typecheck=ToolRecipe(
            argv=("faketool", "{files}"),
            report_format="native-exit",
            available_probe=("faketool", "--version"),
            extra_env=(("HIVE_FAKE", "1"),),
            timeout_s=7,
        ),
        test=None,
    )
    repo = _mkrepo(tmp_path, "src/app.py", "tests/test_app.py")
    spawn = ScriptedSpawn(recipe_overrides={"faketool": lambda argv, cwd: _ok(argv)})
    result = verify(
        repo,
        _PY_TOUCHED,
        base_sha="b",
        head_sha="h",
        engine=_graph("src/app.py", "tests/test_app.py"),
        opts=VerifyOptions(registry={"python": row}),
        spawn=spawn,
    )
    (call,) = spawn.recipe_calls("faketool")
    assert call.extra_env == (("HIVE_FAKE", "1"),)
    assert call.timeout_s == 7
    assert result.typecheck.state == "passed"
    # The injected row has no test recipe: the affected test abstains honestly.
    assert result.tests.state == "not_run"
    by_lang = {o.lang: o for o in result.tests.per_lang}
    assert "no test recipe" in (by_lang["python"].reason or "")


# --- options, arguments, and honest emptiness ------------------------------------------


def test_disabled_classes_abstain_with_reasons(tmp_path: Path) -> None:
    spawn = ScriptedSpawn()
    result = _py_verify(tmp_path, spawn, run_typecheck=False)
    assert result.typecheck.state == "not_run"
    assert result.typecheck.reason == "typecheck disabled by options"
    assert result.tests.state == "passed"

    spawn2 = ScriptedSpawn()
    result2 = _py_verify(tmp_path, spawn2, run_tests=False)
    assert result2.tests.state == "not_run"
    assert result2.tests.reason == "tests disabled by options"
    assert result2.typecheck.state == "passed"
    assert spawn2.recipe_calls("pytest") == []


def test_untouched_languages_yield_an_honest_empty_typecheck(tmp_path: Path) -> None:
    repo = _mkrepo(tmp_path, "README.md")
    result = verify(
        repo,
        TouchedSet((TouchedFile("README.md", frozenset({1})),)),
        base_sha="b",
        head_sha="h",
        engine=nx.DiGraph(),
        spawn=ScriptedSpawn(),
    )
    assert result.typecheck.state == "not_run"
    assert result.typecheck.reason == "no typecheckable touched files"
    assert result.tests.state == "not_run"


def test_invalid_arguments_raise(tmp_path: Path) -> None:
    spawn = ScriptedSpawn()
    with pytest.raises(ValueError):
        verify(
            tmp_path / "nope",
            _PY_TOUCHED,
            base_sha="b",
            head_sha="h",
            engine=nx.DiGraph(),
            spawn=spawn,
        )
    repo = _mkrepo(tmp_path, *_PY_FILES)
    with pytest.raises(ValueError):
        verify(
            repo,
            _PY_TOUCHED,
            base_sha="",
            head_sha="h",
            engine=nx.DiGraph(),
            spawn=spawn,
        )
    with pytest.raises(ValueError):
        verify(
            repo,
            _PY_TOUCHED,
            base_sha="b",
            head_sha="",
            engine=nx.DiGraph(),
            spawn=spawn,
        )
    with pytest.raises(ValueError):
        VerifyOptions(depth=0)


def test_adequacy_is_none_even_when_requested(tmp_path: Path) -> None:
    result = _py_verify(tmp_path, ScriptedSpawn(), run_adequacy=True)
    assert result.adequacy is None  # v1: no row carries an adequacy recipe


# --- the stamp and the summary ---------------------------------------------------------


def test_stamp_carries_shas_invoked_tool_versions_and_registry_version(
    tmp_path: Path,
) -> None:
    result = _py_verify(tmp_path, ScriptedSpawn())
    stamp = result.tool_version
    assert stamp.base_sha == "base0000"
    assert stamp.head_sha == "head1111"
    assert stamp.registry_version == REGISTRY_VERSION
    assert stamp.tool_versions == (
        ("pyright", "pyright 1.0"),
        ("pytest", "pytest 1.0"),
    )


def test_stamp_excludes_probed_but_never_invoked_tools(tmp_path: Path) -> None:
    spawn = ScriptedSpawn(probe_fail=frozenset({"pyright"}))
    result = _py_verify(tmp_path, spawn)
    tools = dict(result.tool_version.tool_versions)
    assert "pyright" not in tools  # probed absent -> nothing invoked -> not stamped
    assert "pytest" in tools


def test_summary_is_deterministic(tmp_path: Path) -> None:
    first = _py_verify(tmp_path / "a", ScriptedSpawn())
    second = _py_verify(tmp_path / "b", ScriptedSpawn())
    assert first.summary == second.summary
    assert "typecheck" in first.summary
    assert "tests" in first.summary
