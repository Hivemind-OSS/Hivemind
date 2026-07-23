"""The single-source-root path-dialect contract (BUG-038).

matrix infers its extraction root as the corpus' common ancestor, so on a
checkout whose parsed source ALL lives under one top-level directory the graph
spells node ``source_file`` package-relative (``mod.py``) while the diff — and
every receipt consumer, and every episode anchor — speaks repo-root-relative
(``pkg/mod.py``). The census must bridge that dialect at its own seam exactly
like hive-edge bridges it for anchors (BUG-035): receipts spell subjects
repo-root-relative on ANY layout, and a multi-rooted corpus (offset "") stays
byte-identical to the pre-bridge build.

Hermetic-REAL tier, driven through the REAL front door: a two-branch
single-source-root git repo built by ``hive.census.cli.main`` in-process; the
multi-root byte-inertia legs ride the session engine fixtures from conftest.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from hive.census import cli
from hive.census.engines import GraphPair, attribute_symbols, node_change, node_subject
from hive.census.join import regression_join
from hive.census.receipt import build_receipt
from hive.census.execution import coerce_execution


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


_BASE_MOD = "def fn(a, b):\n    return a + b\n\n\ndef other(x):\n    return fn(x, 1)\n"

_HEAD_MOD = "def fn(a):\n    return a\n\n\ndef other(x):\n    return fn(x)\n"

_TEST_MOD = "from pkg.mod import fn\n\n\ndef test_fn():\n    assert fn(1, 2) == 3\n"


@pytest.fixture(scope="session")
def single_root_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """ALL parsed source under pkg/ (no second top-level code entry), on two
    real branches: the head narrows fn (breaking) while other + a test call it."""
    repo = tmp_path_factory.mktemp("single-root") / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "mainline")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text(_BASE_MOD)
    (repo / "pkg" / "test_mod.py").write_text(_TEST_MOD)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "feat-x")
    (repo / "pkg" / "mod.py").write_text(_HEAD_MOD)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "narrow fn")
    return repo


@pytest.fixture(scope="session")
def single_root_receipt(
    single_root_repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> dict:
    """One REAL ``cli.main`` build over the two branches, decoded to the receipt."""
    out = tmp_path_factory.mktemp("single-root-out") / "receipt.json"
    rc = cli.main(
        [
            "build",
            "--repo",
            str(single_root_repo),
            "--base",
            "mainline",
            "--head",
            "feat-x",
            "--out",
            str(out),
            "--propagate",
        ]
    )
    assert rc == cli.EX_OK
    envelope = json.loads(out.read_text(encoding="utf-8"))
    statement = json.loads(base64.b64decode(envelope["payload"]))
    return statement["predicate"]


def _lines_of(receipt: dict, cls: str) -> list[dict]:
    return [line for line in receipt["lines"] if line["class"] == cls]


class TestSingleRootReceiptSpellsRepoRelativeSubjects:
    """The BUG-038 contract: the receipt's evidence lines exist AND their
    subjects ride the repo-root-relative dialect episode anchors join on."""

    def test_existence_and_contract_lines_spell_repo_relative_subjects(
        self, single_root_receipt: dict
    ) -> None:
        contract = {
            line["subject"]: line for line in _lines_of(single_root_receipt, "contract")
        }
        assert "pkg/mod.py::fn" in contract, sorted(contract)
        assert contract["pkg/mod.py::fn"]["detail"]["drift"] == "breaking"
        existence = {
            line["subject"] for line in _lines_of(single_root_receipt, "existence")
        }
        assert "pkg/mod.py::fn" in existence
        # The flattened graph dialect never leaks into a subject.
        flattened = {s for s in existence | set(contract) if s.startswith("mod.py::")}
        assert not flattened, flattened

    def test_node_class_decides_every_touched_symbol(
        self, single_root_receipt: dict
    ) -> None:
        (node,) = [
            entry
            for entry in single_root_receipt["precision"]
            if entry["class"] == "node"
        ]
        assert node["total"] >= 2  # fn + other, at least
        assert node["decided"] == node["total"] > 0  # never the 0/0 silent death

    def test_regression_line_seeds_and_spells_repo_relative(
        self, single_root_receipt: dict
    ) -> None:
        regressions = {
            line["subject"]: line
            for line in _lines_of(single_root_receipt, "regression")
        }
        assert "pkg/mod.py::fn" in regressions, sorted(regressions)
        finding = regressions["pkg/mod.py::fn"]
        assert finding["tag"] == "bounded-estimate"
        assert finding["detail"]["seed"] is not None

    def test_propagation_neighbours_spell_repo_relative(
        self, single_root_receipt: dict
    ) -> None:
        block = single_root_receipt.get("propagation")
        assert block, "breaking drift with callers must propagate"
        for entry in block:
            assert entry["seed"].startswith("pkg/"), entry["seed"]
            for neighbour in entry["neighbors"]:
                assert neighbour.startswith("pkg/"), neighbour


class TestAsymmetricRootsBridgePerSide:
    """The masking flip from the bug report, as a per-side contract: the head
    adds a root-level file, so the HEAD corpus is multi-rooted (offset "")
    while the BASE stays single-rooted (offset "pkg") — each side's index must
    ride ITS OWN offset (a shared or swapped offset red-flags here)."""

    def test_removed_base_symbol_and_head_subjects_both_repo_relative(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "mainline")
        (repo / "pkg").mkdir()
        (repo / "pkg" / "mod.py").write_text(
            _BASE_MOD + "\n\ndef doomed():\n    return 9\n"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        _git(repo, "checkout", "-qb", "feat-x")
        (repo / "pkg" / "mod.py").write_text(_HEAD_MOD)
        (repo / "main.py").write_text(
            "from pkg.mod import fn\n\n\ndef entry():\n    return fn(1)\n"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "narrow fn, drop doomed, add root entry")

        out = tmp_path / "receipt.json"
        rc = cli.main(
            [
                "build",
                "--repo",
                str(repo),
                "--base",
                "mainline",
                "--head",
                "feat-x",
                "--out",
                str(out),
            ]
        )
        assert rc == cli.EX_OK
        envelope = json.loads(out.read_text(encoding="utf-8"))
        receipt = json.loads(base64.b64decode(envelope["payload"]))["predicate"]

        existence = {line["subject"]: line for line in _lines_of(receipt, "existence")}
        # base-side attribution (removed_spans against the single-rooted BASE
        # graph): the deleted symbol is spelled repo-relative.
        assert "pkg/mod.py::doomed" in existence, sorted(existence)
        doomed = existence["pkg/mod.py::doomed"]["detail"]
        assert (doomed["existed_before"], doomed["exists_after"]) == (True, False)
        # head-side attribution (multi-rooted HEAD graph, offset "").
        assert "pkg/mod.py::fn" in existence
        assert "main.py::entry" in existence
        assert not {s for s in existence if s.startswith("mod.py::")}


class TestMultiRootStaysByteIdentical:
    """Offset "" on any multi-rooted corpus, and the offset seam is byte-inert
    there: the receipt built through the offset-aware pipeline equals the one
    built with the bridge forced off — the FULL lines list, not a spot check."""

    def test_multi_root_offsets_are_empty(self, graph_pair: GraphPair) -> None:
        assert graph_pair.base_offset == ""
        assert graph_pair.head_offset == ""

    def test_full_lines_list_identical_with_bridge_forced_off(
        self, engine_change, graph_pair: GraphPair
    ) -> None:
        stripped = GraphPair(
            base=graph_pair.base,
            head=graph_pair.head,
            base_stamp=graph_pair.base_stamp,
            head_stamp=graph_pair.head_stamp,
            base_offset="",
            head_offset="",
        )

        def _doc(pair: GraphPair) -> dict:
            touched = attribute_symbols(engine_change.touched, pair)
            verdict = node_change(engine_change, touched)
            findings = regression_join(verdict, pair.base, depth=2)
            return build_receipt(
                touched=engine_change.touched,
                verdict=verdict,
                findings=findings,
                execution=coerce_execution(None),
                verifier_stamp=None,
                graphs=pair,
                precision_budget=0.9,
                depth=2,
            )

        real, forced_off = _doc(graph_pair), _doc(stripped)
        assert real["lines"] == forced_off["lines"]
        assert real["precision"] == forced_off["precision"]
        subjects = {line["subject"] for line in real["lines"]}
        assert "lib.py::greet" in subjects  # non-vacuous: real evidence rode


class TestOffsetFailsOpen:
    """An unreadable manifest ⇒ offset "" ⇒ exactly today's behavior."""

    def test_unreadable_manifest_degrades_to_empty_offset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import matrix.paths
        from hive.census import engines

        monkeypatch.setattr(
            matrix.paths,
            "default_manifest",
            lambda: str(tmp_path / "absent" / "manifest.json"),
        )
        assert engines._graph_offset(tmp_path) == ""

    def test_node_subject_without_offset_spells_the_graph_dialect(
        self, graph_pair: GraphPair
    ) -> None:
        head_nx = graph_pair.head.nx
        subjects = {node_subject(head_nx, node_id) for node_id in head_nx.nodes()} - {
            None
        }
        assert any(s.startswith("lib.py::") for s in subjects)
