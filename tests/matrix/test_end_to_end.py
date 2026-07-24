"""Cross-component end-to-end: build_graph → blast_radius → version_stamp.

The incremental add/modify/delete path is covered by test_update; this pins the
full pipeline wiring through the public surface and the graph.json contract
(shape, no community, root-relative paths) on a real git repo.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess

import pytest

import hive.matrix as matrix
from hive.matrix.version import version_stamp


@pytest.fixture()
def git_repo(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("MATRIX_OUT", str(out))
    import hive.matrix.paths

    importlib.reload(hive.matrix.paths)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text(
        "def foo():\n    return 1\n\ndef bar():\n    return foo()\n", encoding="utf-8"
    )
    (src / "b.py").write_text(
        "from a import foo\n\ndef baz():\n    return foo()\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(src), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "init"], check=True)
    return src, out


def test_build_blast_version_pipeline(git_repo):
    src, _out = git_repo
    graph = matrix.build_graph(src)
    assert len(graph) >= 4

    radius = matrix.blast_radius(graph, "foo", depth=2)
    assert {h.node_id for h in radius.callers} >= {"a_bar", "b_baz"}
    assert radius.unsound is True

    stamp = version_stamp(graph, src)
    assert len(stamp.graph_sha256) == 64
    assert len(stamp.commit_sha) == 40
    assert stamp.node_count == len(graph)
    assert stamp.engine_version == matrix.__version__


def test_graph_json_contract(git_repo):
    src, out = git_repo
    matrix.build_graph(src)
    data = json.loads((out / "graph.json").read_text())
    assert "nodes" in data and "links" in data
    assert not any(
        "community" in n for n in data["nodes"]
    )  # clustering excluded (§7 delta)
    assert all(
        not os.path.isabs(n.get("source_file", "")) for n in data["nodes"]
    )  # root-relative


def test_version_stamp_stable_across_rebuild(git_repo):
    src, _out = git_repo
    g1 = matrix.build_graph(src)
    g2 = matrix.build_graph(src)
    assert version_stamp(g1, src).graph_sha256 == version_stamp(g2, src).graph_sha256
