"""C5 contract: canonical projection, deterministic hash, and the version stamp."""

from __future__ import annotations

import json
import subprocess

import networkx as nx

from hive.matrix.model import Graph
from hive.matrix.version import (
    ENGINE_VERSION,
    ModelVersion,
    canonical_json,
    graph_sha256,
    version_stamp,
)


def _graph(extra: bool = False) -> Graph:
    g = nx.DiGraph()
    g.add_node(
        "a_foo",
        label="foo()",
        source_file="a.py",
        source_location="L1",
        file_type="function",
    )
    g.add_node(
        "a_bar",
        label="bar()",
        source_file="a.py",
        source_location="L5",
        file_type="function",
    )
    g.add_edge(
        "a_bar", "a_foo", relation="calls", source_file="a.py", source_location="L6"
    )
    if extra:
        g.add_node("a_baz", label="baz()", source_file="a.py", file_type="function")
    return Graph(g)


def test_canonical_json_is_sorted_and_minimal():
    data = json.loads(canonical_json(_graph()))
    assert [n["id"] for n in data["nodes"]] == ["a_bar", "a_foo"]
    assert "built_at" not in canonical_json(_graph())


def test_canonical_json_ignores_insertion_order_and_volatile_fields():
    g1 = nx.DiGraph()
    g1.add_node("b", label="B")
    g1.add_node("a", label="A")
    g2 = nx.DiGraph()
    g2.add_node("a", label="A", degree=99, built_at="now")  # volatile junk
    g2.add_node("b", label="B")
    assert canonical_json(Graph(g1)) == canonical_json(Graph(g2))


def test_graph_sha256_is_deterministic():
    assert graph_sha256(_graph()) == graph_sha256(_graph())


def test_graph_sha256_changes_with_structure():
    assert graph_sha256(_graph()) != graph_sha256(_graph(extra=True))


def test_version_stamp_fields(tmp_path):
    vs = version_stamp(_graph(), tmp_path)
    assert isinstance(vs, ModelVersion)
    assert len(vs.graph_sha256) == 64
    assert vs.engine_version == ENGINE_VERSION
    assert (vs.node_count, vs.edge_count) == (2, 1)
    assert vs.built_at  # ISO 8601 timestamp
    assert vs.commit_sha == "unknown"  # tmp_path is not a git repo


def _init_commit(repo, message="init"):
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text(message)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_version_stamp_reads_git_head(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True
    )
    vs = version_stamp(_graph(), tmp_path)
    assert len(vs.commit_sha) == 40


def test_version_stamp_survives_a_polluted_git_dir_env(tmp_path, monkeypatch):
    """Reproduces the live post-merge hook failure: git populates a hook
    subprocess's env with GIT_DIR/GIT_WORK_TREE pointing at a DIFFERENT repo,
    which (pre-fix) silently overrides `git -C <repo_root>` targeting so the
    stamp reads the wrong repo's HEAD instead of the one named by repo_root."""
    target = tmp_path / "target"
    other = tmp_path / "other"
    expected = _init_commit(target, "target-head")
    other_head = _init_commit(other, "other-head")
    assert expected != other_head
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    vs = version_stamp(_graph(), target)
    assert vs.commit_sha == expected
