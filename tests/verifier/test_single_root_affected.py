"""The single-source-root affected-test contract (BUG-039).

matrix roots its graph at the corpus' common ancestor, so on a checkout whose
parsed source ALL lives under one top-level directory the graph spells node
``source_file`` package-relative (``mod.py``) while the composer's touched
paths — and the worktree the spawn runs in — speak repo-root-relative
(``pkg/mod.py``). ``VerifyOptions.root_offset`` bridges that dialect at the
verifier's own seam: seeds are translated INTO the graph dialect and every
returned hit path is re-rooted BACK to the repo dialect, so an in-corpus test
exercising the touched symbol is discovered AND spawned by its real path — the
tests line finally DECIDES on such repos. The empty offset (the default) is a
structural no-op: multi-root behavior stays byte-identical.

The tier drives the REAL matrix graph over the BUG-038 repro tree (the graph
dialect is the engine's own truth, never hand-faked) with a spawn double at
the one declared seam, mirroring test_verify.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hive.verifier.spawn import SpawnResult
from hive.verifier.touched import TouchedFile, TouchedSet
from hive.verifier.verify import VerifyOptions, verify

_JUNIT_RED = (
    b'<?xml version="1.0"?><testsuites><testsuite name="s">'
    b'<testcase classname="t" name="test_fn"><failure message="fn narrowed"/>'
    b"</testcase></testsuite></testsuites>"
)

# The BUG-038 repro tree, head side: fn narrowed (breaking) while an in-corpus
# test still exercises the two-argument contract. The import is spelled in the
# corpus' own dialect (``from mod import fn`` — resolvable against the single
# extraction root), exactly the shape a single-source-root package uses.
_HEAD_MOD = (
    "def fn(a):\n"
    "    return a\n"
    "\n"
    "\n"
    "def other(x):\n"
    "    return fn(x)\n"
)

_TEST_MOD = (
    "from mod import fn\n"
    "\n"
    "\n"
    "def test_fn():\n"
    "    assert fn(1, 2) == 3\n"
)


class _ScriptedSpawn:
    """Spawn double for the one declared seam: probes answer present, pytest
    writes the repro's RED junit (the warned drift come true); calls recorded."""

    def __init__(self) -> None:
        self.recipe_calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, argv, *, cwd, timeout_s, extra_env=(), **kwargs) -> SpawnResult:
        argv = tuple(str(part) for part in argv)
        if argv[-1] in ("--version", "version"):
            return self._ok(argv, stdout=f"{argv[0]} 1.0".encode())
        self.recipe_calls.append((argv, Path(cwd)))
        assert argv[0] == "pytest", f"unexpected spawn: {argv}"
        Path(argv[argv.index("--junitxml") + 1]).write_bytes(_JUNIT_RED)
        return self._ok(argv, exit_code=1)

    @staticmethod
    def _ok(argv, stdout: bytes = b"", exit_code: int = 0) -> SpawnResult:
        return SpawnResult(
            argv=tuple(argv), exit_code=exit_code, stdout=stdout, stderr=b"",
            timed_out=False, truncated=False, duration_s=0.01,
        )


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def single_root(tmp_path_factory: pytest.TempPathFactory):
    """(repo, graph) for the single-root repro: ALL parsed source under pkg/,
    so the REAL engine roots the graph at pkg/ and spells paths without it."""
    import matrix

    base = tmp_path_factory.mktemp("bug039-single-root")
    repo = _write_tree(
        base / "repo", {"pkg/mod.py": _HEAD_MOD, "pkg/test_mod.py": _TEST_MOD}
    )
    graph = matrix.build_graph(repo, out_dir=base / "matrix-out")
    # The repro's premise, pinned against engine drift: the graph really does
    # speak the offset dialect (no "pkg/" spelling survives extraction).
    sources = {
        str(data.get("source_file")) for _n, data in graph.nx.nodes(data=True)
    }
    assert sources == {"mod.py", "test_mod.py"}
    return repo, graph


@pytest.fixture(scope="module")
def multi_root(tmp_path_factory: pytest.TempPathFactory):
    """(repo, graph) for the multi-rooted dual: two top-level entries, so the
    extraction root IS the repo root (offset "") and dialects agree."""
    import matrix

    base = tmp_path_factory.mktemp("bug039-multi-root")
    repo = _write_tree(
        base / "repo",
        {
            "calc.py": "def add(a, b):\n    return a + b\n",
            "test_calc.py": (
                "from calc import add\n\n\ndef test_add():\n"
                "    assert add(1, 2) == 3\n"
            ),
        },
    )
    graph = matrix.build_graph(repo, out_dir=base / "matrix-out")
    return repo, graph


_TOUCHED = TouchedSet((TouchedFile("pkg/mod.py", frozenset({1})),))


def _run(repo: Path, graph, touched: TouchedSet, spawn, **opt_kwargs):
    return verify(
        repo,
        touched,
        base_sha="bug039-base",
        head_sha="bug039-head",
        engine=graph,
        opts=VerifyOptions(run_typecheck=False, **opt_kwargs),
        spawn=spawn,
    )


class TestSingleRootTestsLineDecides:
    """Intent 5: given a single-root candidate tree with an in-corpus test
    exercising the touched symbol, the tests line DECIDES."""

    def test_affected_tests_are_discovered_and_re_rooted(self, single_root) -> None:
        repo, graph = single_root
        result = _run(repo, graph, _TOUCHED, _ScriptedSpawn(), root_offset="pkg")
        # Discovery: the repo-dialect seed resolved against the graph dialect.
        # Re-rooting: files AND tests come back in the repo dialect the
        # worktree spawn joins on — never the graph's stripped spelling.
        assert result.affected.tests == ("pkg/test_mod.py",)
        assert result.affected.files == ("pkg/test_mod.py",)
        assert "test_mod" in result.affected.symbols
        assert result.affected.unsound is True

    def test_tests_line_decides_failed_on_the_warned_drift(self, single_root) -> None:
        repo, graph = single_root
        spawn = _ScriptedSpawn()
        result = _run(repo, graph, _TOUCHED, spawn, root_offset="pkg")
        # THE contract: decided, never the zero-affected abstention.
        assert result.tests.state == "failed"
        assert result.tests.ran is True
        assert result.tests.failed == 1
        # The spawn received the worktree's real path from the repo root — the
        # second half of BUG-039 (a graph-dialect path would miss the tree).
        ((argv, cwd),) = spawn.recipe_calls
        assert argv[0] == "pytest"
        assert argv[-1] == "pkg/test_mod.py"
        assert cwd == repo

    def test_unbridged_single_root_stays_starved_fail_open(self, single_root) -> None:
        # The pre-bridge shape, pinned: without an offset the repo-dialect seed
        # never resolves, the radius unions empty, and the class abstains
        # honestly (not_run, never a false pass) with no spawn at all.
        repo, graph = single_root
        spawn = _ScriptedSpawn()
        result = _run(repo, graph, _TOUCHED, spawn)
        assert result.affected.tests == ()
        assert result.tests.state == "not_run"
        assert result.tests.reason == "no affected tests in blast radius (lower bound)"
        assert spawn.recipe_calls == []


class TestMultiRootStaysByteIdentical:
    """The offset-"" regression contract: the default offset is empty, and an
    empty offset is a structural no-op — multi-root results are byte-identical
    field for field, and the affected set carries the pre-bridge literals."""

    def test_default_offset_is_empty(self) -> None:
        assert VerifyOptions().root_offset == ""

    def test_multi_root_result_is_byte_identical_with_explicit_empty_offset(
        self, multi_root
    ) -> None:
        repo, graph = multi_root
        touched = TouchedSet((TouchedFile("calc.py", frozenset({1})),))
        result_default = _run(repo, graph, touched, _ScriptedSpawn())
        result_explicit = _run(repo, graph, touched, _ScriptedSpawn(), root_offset="")
        assert result_default == result_explicit
        # The pre-bridge literal pin (the conformance tier's own multi-root
        # expectation): found, spelled exactly as the graph already spoke.
        assert result_default.affected.tests == ("test_calc.py",)
        assert result_default.tests.state == "failed"
