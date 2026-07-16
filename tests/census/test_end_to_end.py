"""Hermetic-REAL end-to-end: the CLI over a constructed repo, no fakes.

A two-commit tmp repo (a breaking signature narrowing, its caller, and a test
touching it) runs through the real console entry — via subprocess, so the
MATRIX_OUT scratch pin is exercised in a fresh interpreter and CWD litter is
observable. The emitted envelope must be explicitly unsigned, the embedded
receipt must validate against the published schema, and every produced line
class must ride the bundle with its fail-closed tags.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hive.census import cli
from hive.census.schema import validate_receipt

_BASE_LIB = (
    "def greet(name, punct):\n"
    '    return "hi " + name + punct\n'
)
_HEAD_LIB = (
    "def greet(name):\n"
    '    return "hi " + name\n'
)
_BASE_APP = (
    "from lib import greet\n"
    "\n"
    "\n"
    "def caller():\n"
    '    return greet("world", "!")\n'
)
_HEAD_APP = (
    "from lib import greet\n"
    "\n"
    "\n"
    "def caller():\n"
    '    return greet("world")\n'
)
_TEST_LIB = (
    "from lib import greet\n"
    "\n"
    "\n"
    "def test_greet():\n"
    '    assert greet("x", ".")\n'
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


@pytest.fixture(scope="module")
def e2e_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("e2e") / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "lib.py").write_text(_BASE_LIB)
    (repo / "app.py").write_text(_BASE_APP)
    (repo / "test_lib.py").write_text(_TEST_LIB)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "lib.py").write_text(_HEAD_LIB)
    (repo / "app.py").write_text(_HEAD_APP)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head")
    return repo


@pytest.fixture(scope="module")
def shas(e2e_repo: Path) -> tuple[str, str]:
    return (
        _git(e2e_repo, "rev-parse", "HEAD~1").strip(),
        _git(e2e_repo, "rev-parse", "HEAD").strip(),
    )


def _lines_of(receipt: dict, cls: str) -> list[dict]:
    return [line for line in receipt["lines"] if line["class"] == cls]


def _assert_full_bundle(receipt: dict, shas: tuple[str, str]) -> None:
    base_sha, head_sha = shas
    validate_receipt(receipt)

    contract = {line["subject"]: line for line in _lines_of(receipt, "contract")}
    breaking = contract["lib.py::greet"]
    assert breaking["detail"]["drift"] == "breaking"
    assert breaking["tag"] == "machine-checked"

    (regression,) = _lines_of(receipt, "regression")
    assert regression["subject"] == "lib.py::greet"
    assert regression["tag"] == "bounded-estimate"
    assert regression["detail"]["seed"] is not None
    assert any("caller" in hit for hit in regression["detail"]["callers"])
    assert any("test_greet" in hit for hit in regression["detail"]["tests"])

    execution = {line["class"]: line for line in receipt["lines"]}

    # Deterministic live signal: pytest is present wherever this suite runs,
    # and the head narrows greet while test_greet calls the OLD arity — the
    # affected test fails by construction, so an inert producer path cannot
    # hide behind a green suite.
    tests_line = execution["tests"]
    assert tests_line["detail"]["state"] == "failed"
    assert tests_line["detail"]["failed"] >= 1
    assert "pytest" in tests_line["detail"]["runners"]
    assert tests_line["tag"] == "bounded-estimate"
    assert tests_line["reason"] != "producer-absent"

    # pyright is not a census dependency: assert tag/state COHERENCE, never
    # a specific decision (the decided path is proven live by the in-situ
    # tier). Whatever happened, it was never producer absence.
    typecheck_line = execution["typecheck"]
    typecheck_state = typecheck_line["detail"]["state"]
    assert typecheck_state in {"passed", "failed", "not_run", "errored"}
    if typecheck_state in {"passed", "failed"}:
        assert typecheck_line["tag"] == "bounded-estimate"
    else:
        assert typecheck_line["tag"] == "unverified"
        assert typecheck_line["reason"]
    assert typecheck_line["reason"] != "producer-absent"

    (differential,) = _lines_of(receipt, "differential")
    assert differential["tag"] == "not-run"
    assert differential["reason"] == "no-producer"

    provenance = receipt["provenance"]
    assert provenance["base_sha"] == base_sha
    assert provenance["head_sha"] == head_sha
    verifier = provenance["verifier"]
    assert verifier["base_sha"] == base_sha
    assert verifier["head_sha"] == head_sha
    assert verifier["registry_version"]
    combdrift = provenance["combdrift"]
    assert combdrift["combdrift_version"]
    assert combdrift["base_sha"] == base_sha
    assert combdrift["head_sha"] == head_sha
    for side, sha in (("base", base_sha), ("head", head_sha)):
        stamp = provenance["matrix"][side]
        assert stamp["commit_sha"] == sha
        assert stamp["graph_sha256"]


@pytest.fixture(scope="module")
def run(
    e2e_repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    run_dir = tmp_path_factory.mktemp("e2e-run")
    out = run_dir / "envelope.json"
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "MATRIX_OUT"
    }
    proc = subprocess.run(
        [
            sys.executable, "-m", "hive.census.cli",
            "build",
            "--repo", str(e2e_repo),
            "--base", "HEAD~1",
            "--head", "HEAD",
            "--out", str(out),
        ],
        cwd=run_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc, run_dir, out


class TestUnsignedSubprocessRun:
    def test_exit_zero_and_summary(self, run) -> None:
        proc, _, out = run
        assert proc.returncode == 0, proc.stderr
        assert str(out) in proc.stdout
        for cls in ("node", "edge", "regression", "execution", "differential"):
            assert cls in proc.stdout

    def test_no_matrix_out_litter_in_cwd(self, run) -> None:
        # MATRIX_OUT defaults CWD-relative and is read once at matrix import:
        # only the scratch pin taken before any matrix import prevents litter.
        _, run_dir, _ = run
        assert not (run_dir / "matrix-out").exists()

    def test_envelope_is_explicitly_unsigned(self, run) -> None:
        _, _, out = run
        envelope = json.loads(out.read_text())
        assert envelope["unsigned"] is True
        assert envelope["signatures"] == []

    def test_embedded_receipt_is_the_full_bundle(self, run, shas) -> None:
        _, _, out = run
        envelope = json.loads(out.read_text())
        statement = json.loads(base64.b64decode(envelope["payload"]))
        _assert_full_bundle(statement["predicate"], shas)


class TestParsedOnce:
    def test_diff_parsed_exactly_once_across_the_pipeline(
        self, e2e_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole pipeline — engines, join, live verifier — runs off ONE
        diff parse and ONE `git diff` text fetch. Both names are module
        globals resolved at call time, so counting wrappers see every caller
        without production seams."""
        import hive.census.diff as diff_module

        parse_calls = 0
        diff_fetches = 0
        real_parse = diff_module.parse_unified_diff
        real_git = diff_module._git

        def counting_parse(text: str):
            nonlocal parse_calls
            parse_calls += 1
            return real_parse(text)

        def counting_git(repo: Path, *args: str):
            nonlocal diff_fetches
            if args and args[0] == "diff":
                diff_fetches += 1
            return real_git(repo, *args)

        monkeypatch.setattr(diff_module, "parse_unified_diff", counting_parse)
        monkeypatch.setattr(diff_module, "_git", counting_git)

        out = tmp_path / "envelope.json"
        rc = cli.main(
            [
                "build",
                "--repo", str(e2e_repo),
                "--base", "HEAD~1",
                "--head", "HEAD",
                "--out", str(out),
            ]
        )
        assert rc == cli.EX_OK
        assert out.exists()
        assert parse_calls == 1
        assert diff_fetches == 1
