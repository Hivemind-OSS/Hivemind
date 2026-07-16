"""Contract tests for the CLI wiring: argv, atomic write.

Receipts are emitted unsigned unconditionally — there is no signing key or config
gate. The atomic-write pin forces a failure mid-serialization and asserts the
target path never materializes, partial or otherwise.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hive.census import cli
from hive.census.diff import DiffError


def _init_repo(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir()
    env = {**os.environ}
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, env=env)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "one"], check=True, env=env
    )
    return repo


class TestArgvContract:
    def test_defaults(self) -> None:
        args = cli.build_parser().parse_args(
            ["build", "--repo", "r", "--base", "b", "--head", "h"]
        )
        assert args.command == "build"
        assert args.out == Path("receipt.json")
        assert args.depth == 2
        assert args.precision_budget == 0.9
        assert args.authoritative is False
        assert args.hive_url is None
        assert args.memory_cap == 10
        assert args.propagate is False

    def test_authoritative_flag(self) -> None:
        args = cli.build_parser().parse_args(
            ["build", "--repo", "r", "--base", "b", "--head", "h", "--authoritative"]
        )
        assert args.authoritative is True

    def test_bad_precision_budget_refused(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(
                [
                    "build",
                    "--repo", "r",
                    "--base", "b",
                    "--head", "h",
                    "--precision-budget", "almost",
                ]
            )

    def test_missing_subcommand_refused(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])

    def test_console_entry_matches_parser_prog(self) -> None:
        assert cli.build_parser().prog == "hive-census"


class TestErrorMapping:
    def test_unknown_ref_maps_to_ex_software(
        self, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path)
        rc = cli.main(
            [
                "build",
                "--repo", str(repo),
                "--base", "no-such-ref",
                "--head", "HEAD",
                "--out", str(tmp_path / "envelope.json"),
            ]
        )
        assert rc == cli.EX_SOFTWARE
        err = capsys.readouterr().err
        assert err.startswith("hive-census: ")
        assert not (tmp_path / "envelope.json").exists()


class TestVerifierWiring:
    """Argv → producer-call plumbing, via the cli-module recorder seam.

    The recorder returns a contract-shaped decided stub so the receipt's
    execution tags expose which authoritative value reached the coercion;
    real-producer conformance lives in test_execution's live tier.
    """

    @staticmethod
    def _decided_stub():
        from types import SimpleNamespace

        return SimpleNamespace(
            typecheck=SimpleNamespace(
                state="passed", passed=1, failed=0, errored=0,
                reason=None, runners=("stubcheck",),
            ),
            tests=SimpleNamespace(
                state="failed", passed=0, failed=1, errored=0,
                reason=None, runners=("stubtest",),
            ),
            tool_version=None,
        )

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        extra_argv: list[str],
    ) -> tuple[list[dict], dict]:
        import base64
        import json

        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("two\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "two"], check=True)

        calls: list[dict] = []

        def _recorder(change, graphs, *, depth, authoritative):
            calls.append({"depth": depth, "authoritative": authoritative})
            return self._decided_stub()

        monkeypatch.setattr(cli, "run_verifier", _recorder)
        out = tmp_path / "envelope.json"
        rc = cli.main(
            [
                "build",
                "--repo", str(repo),
                "--base", "HEAD~1",
                "--head", "HEAD",
                "--out", str(out),
                *extra_argv,
            ]
        )
        assert rc == cli.EX_OK
        envelope = json.loads(out.read_text())
        statement = json.loads(base64.b64decode(envelope["payload"]))
        return calls, statement["predicate"]

    def test_scoped_default_calls_once_and_tags_bounded_estimate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls, receipt = self._run(monkeypatch, tmp_path, [])
        assert calls == [{"depth": 2, "authoritative": False}]
        execution = {
            line["class"]: line
            for line in receipt["lines"]
            if line["class"] in ("typecheck", "tests")
        }
        assert execution["typecheck"]["tag"] == "bounded-estimate"
        assert execution["tests"]["tag"] == "bounded-estimate"
        # A None tool_version under-claims: no verifier provenance block.
        assert "verifier" not in receipt["provenance"]

    def test_authoritative_flag_reaches_call_and_tags_machine_checked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls, receipt = self._run(
            monkeypatch, tmp_path, ["--authoritative", "--depth", "3"]
        )
        assert calls == [{"depth": 3, "authoritative": True}]
        execution = {
            line["class"]: line
            for line in receipt["lines"]
            if line["class"] in ("typecheck", "tests")
        }
        assert execution["typecheck"]["tag"] == "machine-checked"
        assert execution["tests"]["tag"] == "machine-checked"


class TestMemoryWiring:
    """--hive-url wiring: subjects out of the verdict, context into the receipt.

    run_verifier is stubbed (live producer conformance lives elsewhere) while
    the diff/graph/drift pipeline runs REAL over a one-symbol py change, so
    the recorded subjects prove the verdict -> order -> fetch path; the
    fail-open case runs the real client against a dead loopback port.
    """

    def _repo_with_py_change(self, tmp_path: Path) -> Path:
        repo = _init_repo(tmp_path)
        (repo / "lib.py").write_text(
            "def greet(name, punct):\n    return name + punct\n"
        )
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "lib"], check=True)
        (repo / "lib.py").write_text("def greet(name):\n    return name\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "narrow"], check=True)
        return repo

    def _build(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        extra_argv: list[str],
        fetch=None,
    ) -> dict:
        import base64
        import json

        repo = self._repo_with_py_change(tmp_path)
        monkeypatch.setattr(
            cli, "run_verifier", lambda *a, **k: TestVerifierWiring._decided_stub()
        )
        if fetch is not None:
            monkeypatch.setattr(cli, "fetch_institutional_context", fetch)
        out = tmp_path / "envelope.json"
        rc = cli.main(
            [
                "build",
                "--repo", str(repo),
                "--base", "HEAD~1",
                "--head", "HEAD",
                "--out", str(out),
                *extra_argv,
            ]
        )
        assert rc == cli.EX_OK
        envelope = json.loads(out.read_text())
        return json.loads(base64.b64decode(envelope["payload"]))["predicate"]

    def test_hive_url_fetches_ordered_subjects_and_context_rides(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from hive.census.memory import MemoryContext

        calls: list[dict] = []

        def recorder(url, subjects, *, cap_subjects=8, cap_total=10, timeout_s=5.0):
            calls.append(
                {"url": url, "subjects": list(subjects), "cap_total": cap_total}
            )
            return [
                MemoryContext(
                    episode_id=9, trust="established", polarity="dont",
                    kind="gotcha", anchor="lib.py::greet", ts=1, sim=0.8,
                    text="a served lesson",
                )
            ]

        receipt = self._build(
            monkeypatch,
            tmp_path,
            ["--hive-url", "http://127.0.0.1:8765/mcp", "--memory-cap", "3"],
            fetch=recorder,
        )
        assert calls == [
            {
                "url": "http://127.0.0.1:8765/mcp",
                "subjects": [("lib.py", "greet")],
                "cap_total": 3,
            }
        ]
        assert [entry["tag"] for entry in receipt["context"]] == [
            "institutional-memory"
        ]
        assert receipt["context"][0]["episode_id"] == 9
        assert "institutional-memory context: 1" in capsys.readouterr().out

    def test_without_hive_url_memory_is_never_touched_and_no_context(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def forbidden(*args, **kwargs):
            raise AssertionError("memory fetch fired without --hive-url")

        receipt = self._build(monkeypatch, tmp_path, [], fetch=forbidden)
        assert "context" not in receipt

    def test_unreachable_hive_fails_open_and_builds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        receipt = self._build(
            monkeypatch, tmp_path, ["--hive-url", f"http://127.0.0.1:{port}/mcp"]
        )
        assert "context" not in receipt
        assert "institutional memory unavailable" in capsys.readouterr().err


class TestPropagationWiring:
    """--propagate wiring: default OFF is byte-inert (the block builder is never
    touched and no propagation key rides), ON threads findings + graphs into the
    builder and the entries into the receipt."""

    def _build(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        extra_argv: list[str],
        block=None,
    ) -> dict:
        import base64
        import json

        repo = TestMemoryWiring._repo_with_py_change(TestMemoryWiring(), tmp_path)
        monkeypatch.setattr(
            cli, "run_verifier", lambda *a, **k: TestVerifierWiring._decided_stub()
        )
        if block is not None:
            monkeypatch.setattr(cli, "propagation_block", block)
        out = tmp_path / "envelope.json"
        rc = cli.main(
            [
                "build",
                "--repo", str(repo),
                "--base", "HEAD~1",
                "--head", "HEAD",
                "--out", str(out),
                *extra_argv,
            ]
        )
        assert rc == cli.EX_OK
        envelope = json.loads(out.read_text())
        return json.loads(base64.b64decode(envelope["payload"]))["predicate"]

    def test_propagate_flag_threads_findings_into_the_builder_and_receipt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        calls: list[dict] = []
        entries = [{"seed": "lib.py::greet", "drift": "breaking", "depth": 2,
                    "neighbors": ["app.py::caller"]}]

        def recorder(findings, graphs, **kwargs):
            calls.append({"n_findings": len(findings), "graphs": graphs is not None})
            return entries

        receipt = self._build(monkeypatch, tmp_path, ["--propagate"], block=recorder)
        assert len(calls) == 1 and calls[0]["graphs"] is True
        assert receipt["propagation"] == entries
        assert "propagation: 1 seeds" in capsys.readouterr().out

    def test_without_propagate_builder_never_touched_and_no_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def forbidden(*args, **kwargs):
            raise AssertionError("propagation_block fired without --propagate")

        receipt = self._build(monkeypatch, tmp_path, [], block=forbidden)
        assert "propagation" not in receipt              # byte-inert without the flag

    def test_propagate_with_no_resolvable_neighbours_emits_no_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # the real block builder over the one-symbol change: a breaking seed with
        # no callers/tests resolves zero neighbours -> empty block -> no key.
        receipt = self._build(monkeypatch, tmp_path, ["--propagate"])
        assert "propagation" not in receipt


class TestBuildOnlySurface:
    """The moved-in census exposes `build` only: the standalone launch verbs
    (init/upgrade — agent-side repo wiring) retired with the server-side move."""

    def test_init_verb_is_gone(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["init", "--hive-url", "http://h/mcp"])

    def test_upgrade_verb_is_gone(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["upgrade"])

    def test_build_is_the_only_subcommand(self) -> None:
        subparser_actions = [
            action for action in cli.build_parser()._actions
            if hasattr(action, "choices") and action.choices is not None
        ]
        assert len(subparser_actions) == 1
        assert list(subparser_actions[0].choices) == ["build"]


class TestAtomicWrite:
    def test_failed_serialization_leaves_no_out_file(self, tmp_path: Path) -> None:
        out = tmp_path / "envelope.json"
        with pytest.raises(TypeError):
            cli._write_atomic(out, {"fine": 1, "poison": object()})
        assert not out.exists()
        assert list(tmp_path.iterdir()) == []  # no orphaned temp file either

    def test_write_lands_complete_document(self, tmp_path: Path) -> None:
        out = tmp_path / "envelope.json"
        cli._write_atomic(out, {"payload": "x", "signatures": []})
        import json

        assert json.loads(out.read_text()) == {"payload": "x", "signatures": []}

    def test_pipeline_failure_before_write_leaves_no_out_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path)
        (repo / "a.txt").write_text("two\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "two"], check=True)

        def _explode(*args: object, **kwargs: object):
            raise DiffError("forced failure inside the pipeline")

        monkeypatch.setattr(cli, "build_graphs", _explode)
        out = tmp_path / "envelope.json"
        rc = cli.main(
            [
                "build",
                "--repo", str(repo),
                "--base", "HEAD~1",
                "--head", "HEAD",
                "--out", str(out),
            ]
        )
        assert rc == cli.EX_SOFTWARE
        assert not out.exists()


class TestRefStampWiring:
    """`census build` resolves the measured checkout's branch INSIDE the build (no new
    CLI flag — the checkout IS the measured line) and stamps it as provenance.ref;
    a detached HEAD builds the identical ref-less receipt (fail-open)."""

    def _build_predicate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, detach: bool
    ) -> dict:
        import base64
        import json

        repo = TestMemoryWiring._repo_with_py_change(TestMemoryWiring(), tmp_path)
        subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "feat-x"], check=True)
        if detach:
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"],
                           check=True)
        monkeypatch.setattr(
            cli, "run_verifier", lambda *a, **k: TestVerifierWiring._decided_stub()
        )
        out = tmp_path / "envelope.json"
        rc = cli.main(
            [
                "build",
                "--repo", str(repo),
                "--base", "HEAD~1",
                "--head", "HEAD",
                "--out", str(out),
            ]
        )
        assert rc == cli.EX_OK
        envelope = json.loads(out.read_text())
        return json.loads(base64.b64decode(envelope["payload"]))["predicate"]

    def test_receipt_carries_measured_ref(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hive.census.schema import validate_receipt

        receipt = self._build_predicate(monkeypatch, tmp_path, detach=False)
        assert receipt["provenance"]["ref"] == "feat-x"
        validate_receipt(receipt)                    # the stamped receipt is schema-valid

    def test_receipt_detached_head_omits_ref(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        receipt = self._build_predicate(monkeypatch, tmp_path, detach=True)
        assert "ref" not in receipt["provenance"]    # fail-open: no branch, no stamp


class TestBuildFlagOverrides:
    """`--ref` overrides the measured line verbatim (the server-side sync builder
    evaluates candidates from a detached mirror worktree and names
    refs/pull/N/head itself); `--repo-id` records the repository identity
    verbatim as provenance.repo. Both absent => the receipt is byte-identical
    to today's (the omit-when-absent markers in receipt.py)."""

    def _predicate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra_argv: list[str]
    ) -> dict:
        import base64
        import json

        repo = TestMemoryWiring._repo_with_py_change(TestMemoryWiring(), tmp_path)
        subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "feat-x"], check=True)
        monkeypatch.setattr(
            cli, "run_verifier", lambda *a, **k: TestVerifierWiring._decided_stub()
        )
        out = tmp_path / "envelope.json"
        rc = cli.main(
            [
                "build",
                "--repo", str(repo),
                "--base", "HEAD~1",
                "--head", "HEAD",
                "--out", str(out),
                *extra_argv,
            ]
        )
        assert rc == cli.EX_OK
        envelope = json.loads(out.read_text())
        return json.loads(base64.b64decode(envelope["payload"]))["predicate"]

    def test_ref_flag_wins_over_the_checkout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        receipt = self._predicate(
            monkeypatch, tmp_path, ["--ref", "refs/pull/7/head"]
        )
        assert receipt["provenance"]["ref"] == "refs/pull/7/head"  # never "feat-x"

    def test_repo_id_recorded_verbatim_and_schema_valid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from hive.census.schema import validate_receipt

        receipt = self._predicate(
            monkeypatch, tmp_path, ["--repo-id", "https://example.test/org/repo.git"]
        )
        assert receipt["provenance"]["repo"] == "https://example.test/org/repo.git"
        validate_receipt(receipt)               # the stamped receipt is schema-valid

    def test_absent_flags_keep_todays_receipt_shape(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        receipt = self._predicate(monkeypatch, tmp_path, [])
        assert "repo" not in receipt["provenance"]   # no flag, no key — legacy bytes
        assert receipt["provenance"]["ref"] == "feat-x"  # the checkout stays the line
