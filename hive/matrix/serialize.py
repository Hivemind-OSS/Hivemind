"""graph.json read/write — the persistence-format decision, hidden here.

The on-disk shape is ``{nodes, links}`` (networkx ``node_link_data`` with
``edges="links"``), so ``build_merge`` can round-trip a prior graph by reading it
back directly as JSON. Clustering fields (``community`` / ``community_name`` /
``norm_label``) are not emitted — matrix has no clustering layer.

``to_json`` restores the true edge direction from the ``_src``/``_tgt`` attrs the
assembler stashes, so a directed ``calls`` edge survives even an undirected
backing graph. ``load`` forces a directed read so stored caller→callee direction
survives the round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import networkx as nx
from networkx.readwrite import json_graph

from .model import Graph


def _backing(graph: Graph | nx.Graph[Any]) -> nx.Graph[Any]:
    return graph.nx if isinstance(graph, Graph) else graph


def to_json(graph: Graph | nx.Graph[Any], output_path: str | Path) -> None:
    """Write the graph to ``output_path`` as ``{nodes, links, ...}`` JSON."""
    g = _backing(graph)
    try:
        data = json_graph.node_link_data(g, edges="links")
    except TypeError:  # pragma: no cover - older networkx without the kwarg
        data = json_graph.node_link_data(g)
    for link in data["links"]:
        # Restore original edge direction. Undirected NetworkX storage may
        # canonicalize endpoint order, flipping `calls` and other directional
        # edges; the assembler stashes the true endpoints in _src/_tgt for
        # exactly this purpose.
        true_src = link.pop("_src", None)
        true_tgt = link.pop("_tgt", None)
        if true_src is not None and true_tgt is not None:
            link["source"] = true_src
            link["target"] = true_tgt
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(path: str | Path) -> Graph:
    """Read ``graph.json`` back into a typed :class:`Graph` (directed).

    Forces a directed read so stored caller→callee direction survives, and
    normalizes an ``"edges"``-keyed file to the ``"links"`` networkx expects.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = {**raw, "directed": True}
    if "links" not in raw and "edges" in raw:
        raw = dict(raw, links=raw["edges"])
    try:
        g = json_graph.node_link_graph(raw, edges="links")
    except TypeError:  # pragma: no cover - older networkx without the kwarg
        g = json_graph.node_link_graph(raw)
    if not isinstance(g, nx.DiGraph):  # pragma: no cover - directed=True forces DiGraph
        g = nx.DiGraph(g)
    return Graph(g)
