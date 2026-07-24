"""C1: the typed node/edge model and the relation taxonomy.

The frozen ``Node``/``Edge`` dataclasses are the public *egress* shape. Internally
the graph is an ``nx.DiGraph`` of plain dicts (the persisted ``graph.json`` form,
keyed by ``file_type``, ``source_file``, ``relation``, …); DTOs are materialized
only when handed out, never threaded
through extraction or assembly. That keeps persistence-format, identity-hash, and
public-DTO as three separately-hidden decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import networkx as nx

# The structural relation strings. These are the edges reverse-reachability
# walks for the blast radius.
DEFAULT_AFFECTED_RELATIONS: tuple[str, ...] = (
    "calls",
    "references",
    "imports",
    "imports_from",
    "re_exports",
    "inherits",
    "extends",
    "implements",
    "uses",
    "mixes_in",
    "embeds",
)

# Relations a blast-radius walk follows over *incoming* edges: the affected set
# plus explicit dependency/definition edges.
DEPENDENCY_RELATIONS: frozenset[str] = frozenset(DEFAULT_AFFECTED_RELATIONS) | {
    "depends_on",
    "defines",
}

# Relations meaning "this scope contains/defines that symbol" — used to expand a
# file seed into its symbols before walking dependencies.
CONTAINMENT_RELATIONS: frozenset[str] = frozenset({"contains", "method", "defines"})


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    source_file: str
    source_location: str | None = None
    kind: str = "code"


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    relation: str
    source_file: str
    source_location: str | None = None


def _node_dto(nid: str, data: dict[str, Any]) -> Node:
    return Node(
        id=str(nid),
        label=str(data.get("label") or nid),
        source_file=str(data.get("source_file") or ""),
        source_location=data.get("source_location"),
        # The persisted graph names the node category "file_type"; expose it as
        # the cleaner "kind" at egress.
        kind=str(data.get("file_type") or data.get("kind") or "code"),
    )


def _edge_dto(source: str, target: str, data: dict[str, Any]) -> Edge:
    return Edge(
        source=str(source),
        target=str(target),
        relation=str(data.get("relation") or ""),
        source_file=str(data.get("source_file") or ""),
        source_location=data.get("source_location"),
    )


class Graph:
    """A directed code-structure graph — a thin typed facade over ``nx.DiGraph``.

    The underlying DiGraph holds dict node/edge data (the ``graph.json`` shape).
    ``Node``/``Edge`` DTOs are materialized only at these accessors.
    """

    __slots__ = ("_g",)

    def __init__(self, digraph: nx.DiGraph[Any] | None = None) -> None:
        self._g = digraph if digraph is not None else nx.DiGraph()

    @property
    def nx(self) -> nx.DiGraph[Any]:
        """The backing directed graph (for builders/serializers; not for callers)."""
        return self._g

    def __len__(self) -> int:
        return self._g.number_of_nodes()

    def __contains__(self, nid: str) -> bool:
        return nid in self._g

    def node(self, nid: str) -> Node | None:
        if nid not in self._g:
            return None
        return _node_dto(nid, self._g.nodes[nid])

    def nodes(self) -> list[Node]:
        return [_node_dto(nid, data) for nid, data in self._g.nodes(data=True)]

    def edges(self) -> list[Edge]:
        return [_edge_dto(s, t, data) for s, t, data in self._g.edges(data=True)]

    def in_edges(self, nid: str) -> list[Edge]:
        if nid not in self._g:
            return []
        return [_edge_dto(s, t, d) for s, t, d in self._g.in_edges(nid, data=True)]

    def out_edges(self, nid: str) -> list[Edge]:
        if nid not in self._g:
            return []
        return [_edge_dto(s, t, d) for s, t, d in self._g.out_edges(nid, data=True)]
