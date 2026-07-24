"""serialize: graph.json round-trip and the no-community shape contract.

to_json must write a ``{nodes, links}`` graph with no clustering fields, and
load must read it back into the same node/edge sets (direction preserved).
"""

from __future__ import annotations

import importlib
import json


from hive.matrix import assemble, serialize


def _build(tmp_path):
    """A small directed graph from a hand-built extraction (no extractor needed)."""
    extraction = {
        "nodes": [
            {
                "id": "a_py",
                "label": "a.py",
                "file_type": "code",
                "source_file": "a.py",
                "_origin": "ast",
            },
            {
                "id": "a_py_alpha",
                "label": "alpha()",
                "file_type": "code",
                "source_file": "a.py",
                "source_location": "1:0",
                "_origin": "ast",
            },
            {
                "id": "a_py_beta",
                "label": "beta()",
                "file_type": "code",
                "source_file": "a.py",
                "source_location": "4:0",
                "_origin": "ast",
            },
        ],
        "edges": [
            {
                "source": "a_py",
                "target": "a_py_alpha",
                "relation": "contains",
                "confidence": "EXTRACTED",
                "source_file": "a.py",
            },
            {
                "source": "a_py_alpha",
                "target": "a_py_beta",
                "relation": "calls",
                "confidence": "EXTRACTED",
                "source_file": "a.py",
            },
        ],
    }
    return assemble.build_from_json(extraction, directed=True)


def _node_ids(graph):
    return {n.id for n in graph.nodes()}


def _edge_keys(graph):
    return {(e.source, e.target, e.relation) for e in graph.edges()}


def test_round_trip_preserves_nodes_and_edges(tmp_path):
    g = _build(tmp_path)
    from hive.matrix.model import Graph

    g_wrapped = Graph(g)

    gp = tmp_path / "graph.json"
    serialize.to_json(g, gp)
    loaded = serialize.load(gp)

    assert _node_ids(loaded) == _node_ids(g_wrapped)
    assert _edge_keys(loaded) == _edge_keys(g_wrapped)


def test_round_trip_preserves_edge_direction(tmp_path):
    g = _build(tmp_path)
    gp = tmp_path / "graph.json"
    serialize.to_json(g, gp)
    loaded = serialize.load(gp)
    # alpha -> beta (calls), never the reverse.
    assert ("a_py_alpha", "a_py_beta", "calls") in _edge_keys(loaded)
    assert ("a_py_beta", "a_py_alpha", "calls") not in _edge_keys(loaded)


def test_graph_json_has_nodes_and_links_no_community(tmp_path):
    g = _build(tmp_path)
    gp = tmp_path / "graph.json"
    serialize.to_json(g, gp)
    data = json.loads(gp.read_text())

    assert "nodes" in data and "links" in data  # the build_merge round-trip shape
    assert "edges" not in data  # networkx links key, not edges
    for node in data["nodes"]:
        assert "community" not in node
        assert "community_name" not in node
        assert "norm_label" not in node


def test_build_graph_serialize_load_round_trip(tmp_path, monkeypatch):
    """End-to-end: build_graph writes graph.json; load reads the same graph back."""
    monkeypatch.setenv("MATRIX_OUT", str(tmp_path / "out"))
    import hive.matrix.paths

    importlib.reload(hive.matrix.paths)
    import hive.matrix as matrix

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text(
        "def alpha():\n    return beta()\n\ndef beta():\n    return 1\n",
        encoding="utf-8",
    )

    built = matrix.build_graph(src)
    loaded = serialize.load(matrix.paths.default_graph_json())
    assert _node_ids(loaded) == _node_ids(built)
    assert _edge_keys(loaded) == _edge_keys(built)

    data = json.loads((tmp_path / "out" / "graph.json").read_text())
    assert all("community" not in n for n in data["nodes"])
