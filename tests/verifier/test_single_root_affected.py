"""The single-source-root affected-test contract (BUG-039), in its real shape.

matrix roots its graph at the corpus' common ancestor, so on a checkout whose
parsed source ALL lives under one top-level directory the graph spells node
``source_file`` package-relative (``mod.py``) while the composer's touched
paths — and the worktree the spawn runs in — speak repo-root-relative
(``pkg/mod.py``). ``VerifyOptions.root_offset`` bridges that dialect, but the
bridge alone is not enough: the graph's test linkage is SYMBOL-level
(``test_fn -[calls]-> fn``) and a file seed is dead on every nested layout
(multi-root: unresolvable; single-root: a bare file node that package-dialect
imports never join), so ``_compute_affected`` seeds the radius from every
node the graph roots in the touched file, file-path seeding retained as the
fallback where the graph has no such index.

This tier is deliberately REAL end to end — the earlier scripted-double
revision of this file hid the failure it exists to catch. The fixtures spell
the import in the PACKAGE dialect a real single-root tree uses
(``from pkg.mod import fn``, package ``__init__.py``, root ``pyproject.toml``),
the graph comes from the real matrix extractor, pytest is really spawned (a
junit is earned, never fabricated), and the flagship case drives the real
``python -m hive.census.cli build`` subprocess so the decided RED receipt is
the one ``derive_pre_merge`` accepts — the exact leg the sync candidate
evaluation stands on.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hive.domain.change_evidence import derive_pre_merge
from hive.verifier.touched import TouchedFile, TouchedSet
from hive.verifier.verify import VerifyOptions, verify

_ZERO_TESTS_REASON = "no affected tests in blast radius (lower bound)"

# The BUG-038/BUG-039 repro pair: the head narrows fn (breaking) while an
# in-corpus test — importing through the PACKAGE path, as a real single-root
# tree does — still exercises the two-argument contract. mod.py carries a
# second symbol so the file's source_file index is never a single-node
# degenerate case.
_BASE_MOD = "def fn(a, b):\n    return a + b\n\n\ndef other(x):\n    return fn(x, 1)\n"

_HEAD_MOD = "def fn(a):\n    return a\n\n\ndef other(x):\n    return fn(x)\n"

_TEST_MOD = "from pkg.mod import fn\n\n\ndef test_fn():\n    assert fn(1, 2) == 3\n"

# A [tool.pytest.ini_options] table makes the fixture root the spawned run's
# own rootdir anchor (a bare [project] table is not one under pytest 9).
_PYPROJECT = (
    "[project]\n"
    'name = "bug039-fixture"\n'
    'version = "0.0.1"\n'
    "\n"
    "[tool.pytest.ini_options]\n"
    'testpaths = ["pkg"]\n'
)

_CALC = "def add(a, b):\n    return a + b\n"
_CALC_RED = "def add(a):\n    return a\n"
_TEST_CALC = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"

_PKG_TOUCHED = TouchedSet((TouchedFile("pkg/mod.py", frozenset({1})),))
_CALC_TOUCHED = TouchedSet((TouchedFile("calc.py", frozenset({1})),))


@pytest.fixture(scope="module")
def _venv_tools_on_path():
    """The real spawns need a real pytest: the suite's own interpreter env
    carries one, so its bin dir rides PATH first, deterministically — the
    hermetic spawn env inherits PATH and nothing else ambient."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(
            "PATH",
            f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        )
        yield


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=bug039",
            "-c",
            "user.email=bug039@fixture.invalid",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _run(repo: Path, graph, touched: TouchedSet, **opt_kwargs):
    """The real verify(): default hermetic spawn — pytest is really run."""
    return verify(
        repo,
        touched,
        base_sha="bug039-base",
        head_sha="bug039-head",
        engine=graph,
        opts=VerifyOptions(run_typecheck=False, **opt_kwargs),
    )


@pytest.fixture(scope="module")
def package_single_root(tmp_path_factory: pytest.TempPathFactory):
    """(repo, graph) for the real package-dialect single-root shape at HEAD:
    ALL parsed source under pkg/, the test importing ``from pkg.mod import fn``,
    a root pyproject.toml (config-dir marker, not parsed source)."""
    import matrix

    base = tmp_path_factory.mktemp("bug039-package")
    repo = _write_tree(
        base / "repo",
        {
            "pyproject.toml": _PYPROJECT,
            "pkg/__init__.py": "",
            "pkg/mod.py": _HEAD_MOD,
            "pkg/test_mod.py": _TEST_MOD,
        },
    )
    graph = matrix.build_graph(repo, out_dir=base / "matrix-out")
    # The repro's premise, pinned against engine drift: the graph really does
    # speak the pkg-rooted dialect (no "pkg/" spelling survives extraction),
    # and the test linkage it carries is the SYMBOL-level calls edge.
    g = graph.nx
    sources = {str(data.get("source_file")) for _n, data in g.nodes(data=True)}
    assert sources == {"__init__.py", "mod.py", "test_mod.py"}
    assert any(
        str(data.get("relation")) == "calls" and str(target) == "mod_fn"
        for _s, target, data in g.out_edges("test_mod_test_fn", data=True)
    )
    return repo, graph


@pytest.fixture(scope="module")
def package_result(package_single_root, _venv_tools_on_path):
    repo, graph = package_single_root
    return _run(repo, graph, _PKG_TOUCHED, root_offset="pkg")


@pytest.fixture(scope="module")
def multi_root(tmp_path_factory: pytest.TempPathFactory):
    """(repo, graph) for the multi-rooted dual — THE existing multi-root
    corpus, the byte-identity regression baseline: two top-level entries, so
    the extraction root IS the repo root (offset "") and dialects agree."""
    import matrix

    base = tmp_path_factory.mktemp("bug039-multi-root")
    repo = _write_tree(base / "repo", {"calc.py": _CALC, "test_calc.py": _TEST_CALC})
    graph = matrix.build_graph(repo, out_dir=base / "matrix-out")
    return repo, graph


@pytest.fixture(scope="module")
def multi_root_result(multi_root, _venv_tools_on_path):
    repo, graph = multi_root
    return _run(repo, graph, _CALC_TOUCHED)


class TestPackageSingleRootDecides:
    """Intent 5, sharpened: given a single-root candidate tree in the real
    package dialect with an in-corpus test exercising the touched symbol, a
    breaking narrowing yields DECIDED red execution evidence via a real
    pytest spawn — never the zero-affected abstention."""

    def test_symbol_seeded_discovery_re_roots_tests_to_the_repo_dialect(
        self, package_result
    ) -> None:
        # Discovery: the touched file's SYMBOLS seed the radius (the file
        # seed is dead in this dialect), and the symbol-level calls edge
        # names the test. Re-rooting: the spawnable path joins the worktree.
        assert package_result.affected.tests == ("pkg/test_mod.py",)
        assert "test_mod_test_fn" in package_result.affected.symbols
        assert package_result.affected.unsound is True

    def test_tests_line_decides_failed_on_the_warned_drift(
        self, package_result
    ) -> None:
        # THE contract: decided RED through a really-spawned pytest run — the
        # junit is earned by fn(1, 2) hitting the narrowed fn(a).
        assert package_result.tests.state == "failed"
        assert package_result.tests.ran is True
        assert package_result.tests.failed >= 1
        assert package_result.tests.runners == ("pytest",)
        assert package_result.tests.reason != _ZERO_TESTS_REASON

    def test_unbridged_package_layout_stays_starved_fail_open(
        self, package_single_root
    ) -> None:
        # The offset bridge stays load-bearing: without it the repo-dialect
        # path matches no source_file, the file-seed fallback is unresolvable,
        # and the class abstains honestly (not_run, never a false pass).
        repo, graph = package_single_root
        result = _run(repo, graph, _PKG_TOUCHED)
        assert result.affected.tests == ()
        assert result.tests.state == "not_run"
        assert result.tests.reason == _ZERO_TESTS_REASON


@pytest.fixture(scope="module")
def package_receipt(
    tmp_path_factory: pytest.TempPathFactory, _venv_tools_on_path
) -> dict:
    """One REAL ``python -m hive.census.cli build`` subprocess over a
    two-branch package-layout git repo (base: fn(a, b); head: narrowed fn(a))
    — the exact invocation shape the sync candidate leg uses, decoded to the
    receipt."""
    base = tmp_path_factory.mktemp("bug039-cli")
    repo = _write_tree(
        base / "repo",
        {
            "pyproject.toml": _PYPROJECT,
            "pkg/__init__.py": "",
            "pkg/mod.py": _BASE_MOD,
            "pkg/test_mod.py": _TEST_MOD,
        },
    )
    _git(repo, "init", "-q", "-b", "mainline")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "feat-x")
    (repo / "pkg" / "mod.py").write_text(_HEAD_MOD, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "narrow fn")

    out = base / "receipt.envelope.json"
    env = {key: value for key, value in os.environ.items() if key != "MATRIX_OUT"}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hive.census.cli",
            "build",
            "--repo",
            str(repo),
            "--base",
            "mainline",
            "--head",
            "feat-x",
            "--out",
            str(out),
        ],
        cwd=base,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    envelope = json.loads(out.read_text(encoding="utf-8"))
    return json.loads(base64.b64decode(envelope["payload"]))["predicate"]


class TestSingleRootReceiptDecidesEndToEnd:
    """The receipt built by the real front door carries a DECIDED tests line,
    and the server-side derivation accepts it — the pre-fix shape (not_run ⇒
    ReceiptRefused ⇒ no pre_merge row, ever) is dead."""

    def test_receipt_tests_line_is_decided_red(self, package_receipt: dict) -> None:
        (tests_line,) = [
            line for line in package_receipt["lines"] if line["class"] == "tests"
        ]
        assert tests_line["detail"]["state"] == "failed"
        assert tests_line["tag"] == "bounded-estimate"
        assert tests_line["detail"]["failed"] >= 1
        assert "pytest" in tests_line["detail"]["runners"]
        assert tests_line["reason"] != _ZERO_TESTS_REASON

    def test_derive_pre_merge_accepts_the_receipt(self, package_receipt: dict) -> None:
        # The sync candidate leg's own acceptance seam: decided execution
        # evidence derives a verdict instead of raising ReceiptRefused.
        verdict, tag = derive_pre_merge(package_receipt["lines"])
        assert verdict == "fail"
        assert tag == "bounded-estimate"


class TestMultiRootStaysByteIdentical:
    """Intent 5's second half: on the EXISTING multi-root corpus every
    receipt-visible field — discovery (affected files/tests, the spawn scope)
    and the execution line (state/counts/runners, all a receipt ever carries
    of the verifier) — is byte-identical to the pre-fix goldens captured from
    the file-seeded code with the same real spawn. ``affected.symbols`` is
    deliberately not pinned exactly: symbol-level seeding names the
    symbol-level hits it walks (a pure superset), and symbols never ride the
    receipt. The empty offset stays the default and a structural no-op."""

    def test_default_offset_is_empty(self) -> None:
        assert VerifyOptions().root_offset == ""

    def test_receipt_visible_surface_matches_the_pre_fix_goldens(
        self, multi_root_result
    ) -> None:
        result = multi_root_result
        assert result.affected.files == ("test_calc.py",)
        assert result.affected.tests == ("test_calc.py",)
        assert "test_calc" in result.affected.symbols  # the file-level hit survives
        assert result.tests.state == "passed"
        assert (result.tests.passed, result.tests.failed, result.tests.errored) == (
            1,
            0,
            0,
        )
        assert result.tests.runners == ("pytest",)
        assert result.tests.mode == "scoped"

    def test_explicit_empty_offset_is_identical_to_the_default(
        self, multi_root, multi_root_result, _venv_tools_on_path
    ) -> None:
        repo, graph = multi_root
        assert _run(repo, graph, _CALC_TOUCHED, root_offset="") == multi_root_result


class TestFlatLayoutStillDecides:
    """The working flat path (the e2e's paired control) must not regress: a
    breaking narrowing on a fully-flat tree still discovers the root-level
    test and decides RED through the real spawn."""

    def test_flat_breaking_change_decides_failed(
        self, tmp_path: Path, _venv_tools_on_path
    ) -> None:
        import matrix

        repo = _write_tree(
            tmp_path / "repo", {"calc.py": _CALC_RED, "test_calc.py": _TEST_CALC}
        )
        graph = matrix.build_graph(repo, out_dir=tmp_path / "matrix-out")
        result = _run(repo, graph, _CALC_TOUCHED)
        assert result.affected.tests == ("test_calc.py",)
        assert result.tests.state == "failed"
        assert result.tests.ran is True
        assert result.tests.failed >= 1
        assert result.tests.runners == ("pytest",)


class TestFilePathSeedFallback:
    """The seed of last resort: an engine that carries NO source_file index
    for the touched file (a degraded or foreign extractor) must still walk
    the radius from the file-path seed where it resolves — dropping the
    fallback blinds such graphs entirely."""

    def test_path_seed_still_walks_an_index_less_graph(self) -> None:
        import networkx as nx

        g = nx.DiGraph()  # node id IS the path; no node carries source_file
        g.add_node("calc.py", label="calc.py")
        g.add_node("sym.dependent", label="dependent()")
        g.add_edge("sym.dependent", "calc.py", relation="calls")
        result = verify(
            Path(__file__).parent,  # any real dir; nothing is spawned
            _CALC_TOUCHED,
            base_sha="bug039-base",
            head_sha="bug039-head",
            engine=g,
            opts=VerifyOptions(run_typecheck=False),
        )
        # Discovery survives via the path seed (hits carry no source_file, so
        # no test can be named — the observable effect is the symbol hit).
        assert "sym.dependent" in result.affected.symbols
        assert result.affected.tests == ()


class TestNestedMultiRootDecides:
    """The nested multi-root dual of the package fix: with offset "" the
    bridge is a no-op and ONLY the symbol seeds keep the radius live (the
    nested file path is unresolvable in this dialect too) — the root-level
    test importing ``from pkg.mod import fn`` is discovered and decides."""

    def test_nested_touched_file_decides_failed(
        self, tmp_path: Path, _venv_tools_on_path
    ) -> None:
        import matrix

        repo = _write_tree(
            tmp_path / "repo",
            {
                "pkg/mod.py": _HEAD_MOD,
                "test_mod.py": (
                    "from pkg.mod import fn\n"
                    "\n"
                    "\n"
                    "def test_fn():\n"
                    "    assert fn(1, 2) == 3\n"
                ),
            },
        )
        graph = matrix.build_graph(repo, out_dir=tmp_path / "matrix-out")
        result = _run(repo, graph, _PKG_TOUCHED)
        assert result.affected.tests == ("test_mod.py",)
        assert result.tests.state == "failed"
        assert result.tests.failed >= 1
