"""build_merge: replace-changed / prune-deleted / preserve-untouched / idempotent.

Drives build_merge directly with hand-built extraction chunks (no extractor
needed) and a serialized prior graph, so the merge semantics are tested in
isolation from extraction.
"""

from __future__ import annotations

from pathlib import Path

from hive.matrix import assemble, serialize
from hive.matrix.model import Graph


def _chunk(source_file: str, *symbols: str) -> dict:
    """A minimal extraction chunk: a file node + one code node per symbol, with a
    contains edge from the file to each symbol."""
    file_id = source_file.replace(".", "_").replace("/", "_")
    nodes = [
        {
            "id": file_id,
            "label": source_file,
            "file_type": "code",
            "source_file": source_file,
            "_origin": "ast",
        }
    ]
    edges = []
    for sym in symbols:
        sid = f"{file_id}_{sym}"
        nodes.append(
            {
                "id": sid,
                "label": f"{sym}()",
                "file_type": "code",
                "source_file": source_file,
                "source_location": "1:0",
                "_origin": "ast",
            }
        )
        edges.append(
            {
                "source": file_id,
                "target": sid,
                "relation": "contains",
                "confidence": "EXTRACTED",
                "source_file": source_file,
            }
        )
    return {"nodes": nodes, "edges": edges}


def _labels(g) -> set[str]:
    g = g if isinstance(g, Graph) else Graph(g)
    return {n.label for n in g.nodes()}


def _write_base(graph_path: Path, *chunks: dict) -> None:
    g = assemble.build(list(chunks), directed=True)
    serialize.to_json(g, graph_path)


def test_changed_file_replaces_its_nodes_no_stale_accumulation(tmp_path):
    gp = tmp_path / "graph.json"
    _write_base(gp, _chunk("a.py", "alpha", "beta"))

    # Re-extract a.py with beta removed and a new node delta.
    merged = assemble.build_merge(
        [_chunk("a.py", "alpha", "delta")],
        graph_path=gp,
        root=tmp_path,
    )
    labels = _labels(merged)
    assert "alpha()" in labels
    assert "delta()" in labels
    assert "beta()" not in labels  # stale node gone, no accumulation


def test_deleted_file_pruned_via_prune_sources(tmp_path):
    gp = tmp_path / "graph.json"
    _write_base(gp, _chunk("a.py", "alpha"), _chunk("b.py", "gamma"))

    # b.py deleted: pass an empty new-chunks list and prune b.py.
    merged = assemble.build_merge(
        [],
        graph_path=gp,
        prune_sources=["b.py"],
        root=tmp_path,
    )
    labels = _labels(merged)
    assert "alpha()" in labels  # a.py untouched, survives
    assert "gamma()" not in labels  # b.py pruned


def test_untouched_files_survive(tmp_path):
    gp = tmp_path / "graph.json"
    _write_base(gp, _chunk("a.py", "alpha"), _chunk("b.py", "gamma"))

    # Only a.py re-extracted; b.py is neither in new_chunks nor prune_sources.
    merged = assemble.build_merge(
        [_chunk("a.py", "alpha")], graph_path=gp, root=tmp_path
    )
    assert "gamma()" in _labels(merged)  # b.py preserved unchanged


def test_idempotent_on_repeated_calls(tmp_path):
    gp = tmp_path / "graph.json"
    _write_base(gp, _chunk("a.py", "alpha", "beta"))

    first = assemble.build_merge(
        [_chunk("a.py", "alpha", "beta")], graph_path=gp, root=tmp_path
    )
    serialize.to_json(first, gp)
    n1, e1 = first.number_of_nodes(), first.number_of_edges()

    second = assemble.build_merge(
        [_chunk("a.py", "alpha", "beta")], graph_path=gp, root=tmp_path
    )
    assert (second.number_of_nodes(), second.number_of_edges()) == (n1, e1)


def test_new_file_added_without_disturbing_others(tmp_path):
    gp = tmp_path / "graph.json"
    _write_base(gp, _chunk("a.py", "alpha"))

    merged = assemble.build_merge(
        [_chunk("c.py", "delta")], graph_path=gp, root=tmp_path
    )
    labels = _labels(merged)
    assert "alpha()" in labels  # original preserved
    assert "delta()" in labels  # new file added
