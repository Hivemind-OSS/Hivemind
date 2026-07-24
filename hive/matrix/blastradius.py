"""C3 public: the blast-radius lower bound.

Answers "if this symbol or file changes, what could be affected?" by reverse
reachability over the dependency edges. The answer is always a **lower bound**:
static analysis cannot see dynamic dispatch, reflection, or framework callbacks
(a documented 40-61% dynamic miss), so every result carries ``unsound=True``.
Treat it as "at least these", never "only these".
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

import networkx as nx

from .affected import affected_nodes, resolve_seed
from .model import CONTAINMENT_RELATIONS, DEPENDENCY_RELATIONS, Graph

_DEFAULT_TEST_GLOBS: tuple[str, ...] = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.ts",
    "*.test.js",
)


@dataclass(frozen=True)
class Hit:
    node_id: str
    depth: int
    via_relation: str
    source_file: str
    source_location: str | None


@dataclass(frozen=True)
class BlastRadius:
    seed: str
    callers: list[Hit]
    dependents: list[Hit]
    tests: list[Hit]
    depth: int
    unsound: bool = True


def _is_test_file(source_file: str, globs: tuple[str, ...]) -> bool:
    if not source_file:
        return False
    name = source_file.rsplit("/", 1)[-1]
    return any(fnmatch(source_file, g) or fnmatch(name, g) for g in globs)


def _expand_if_file(g: nx.DiGraph[Any], seed: str) -> list[str]:
    """A file seed expands to the symbols it contains; a symbol seed is itself."""
    data = g.nodes[seed]
    if str(data.get("file_type") or "") != "file":
        return [seed]
    symbols = [
        str(t)
        for _s, t, d in g.out_edges(seed, data=True)
        if str(d.get("relation", "")) in CONTAINMENT_RELATIONS
    ]
    return symbols or [seed]


def _hit(g: nx.DiGraph[Any], node_id: str, depth: int, via_relation: str) -> Hit:
    data = g.nodes[node_id]
    return Hit(
        node_id=node_id,
        depth=depth,
        via_relation=via_relation,
        source_file=str(data.get("source_file") or ""),
        source_location=data.get("source_location"),
    )


def blast_radius(
    graph: Graph | nx.DiGraph[Any],
    symbol_or_file: str,
    *,
    depth: int = 2,
    test_globs: tuple[str, ...] = _DEFAULT_TEST_GLOBS,
) -> BlastRadius:
    """Reverse-reachability lower bound for a changed symbol or file.

    Resolves the seed (id → label → bare-name → source_file → substring; first
    unique wins). A file seed expands to its contained symbols. BFS walks
    *incoming* edges filtered to the dependency relations, depth-bounded. Hits
    arriving via ``calls`` are callers; other dependency relations are dependents;
    any hit in a test file is additionally listed under ``tests``. ``uses`` hits
    are weaker import-inferred evidence (identifiable by ``via_relation``).
    """
    g = graph.nx if isinstance(graph, Graph) else graph
    seed = resolve_seed(g, symbol_or_file)
    if seed is None:
        return BlastRadius(symbol_or_file, [], [], [], depth, unsound=True)

    # Keep the shallowest arrival for each affected node across all seeds.
    shallowest: dict[str, tuple[int, str]] = {}
    for s in _expand_if_file(g, seed):
        for ah in affected_nodes(g, s, relations=DEPENDENCY_RELATIONS, depth=depth):
            prev = shallowest.get(ah.node_id)
            if prev is None or ah.depth < prev[0]:
                shallowest[ah.node_id] = (ah.depth, ah.via_relation)

    callers: list[Hit] = []
    dependents: list[Hit] = []
    tests: list[Hit] = []
    for node_id, (d, via) in shallowest.items():
        hit = _hit(g, node_id, d, via)
        (callers if via == "calls" else dependents).append(hit)
        if _is_test_file(hit.source_file, test_globs):
            tests.append(hit)

    def key(h: Hit) -> tuple[int, str]:
        return (h.depth, h.node_id)

    return BlastRadius(
        seed=seed,
        callers=sorted(callers, key=key),
        dependents=sorted(dependents, key=key),
        tests=sorted(tests, key=key),
        depth=depth,
        unsound=True,
    )
