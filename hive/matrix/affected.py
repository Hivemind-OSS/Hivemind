"""C3 internal: reverse-reachability over the dependency edges.

Seed resolution + BFS. The public blast-radius surface (file expansion,
bucketing, the always-unsound lower bound) is in ``blastradius.py``; this module
is the pure graph walk it builds on.
"""

from __future__ import annotations

import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

import networkx as nx

from .model import DEFAULT_AFFECTED_RELATIONS


@dataclass(frozen=True)
class AffectedHit:
    node_id: str
    depth: int
    via_relation: str


def _normalize_label(label: str) -> str:
    return unicodedata.normalize("NFC", label).casefold()


def _bare_name(label: str) -> str:
    """Lowercased label with the callable decoration (trailing "()") removed."""
    label = _normalize_label(label)
    return label[:-2] if label.endswith("()") else label


def resolve_seed(graph: nx.Graph[Any], query: str) -> str | None:
    if query in graph:
        return query
    query_lower = _normalize_label(query)
    exact_label_matches = [
        str(node_id)
        for node_id, data in graph.nodes(data=True)
        if _normalize_label(str(data.get("label", ""))) == query_lower
    ]
    if len(exact_label_matches) == 1:
        return exact_label_matches[0]
    # Callable labels are decorated ("name()"), so a bare "name" query falls
    # through exact matching and then ties with any "name*" sibling in the
    # contains pass. Match on the undecorated name before giving up.
    query_bare = _bare_name(query_lower)
    bare_name_matches = [
        str(node_id)
        for node_id, data in graph.nodes(data=True)
        if _bare_name(str(data.get("label", ""))) == query_bare
    ]
    if len(bare_name_matches) == 1:
        return bare_name_matches[0]
    exact_source_matches = [
        str(node_id)
        for node_id, data in graph.nodes(data=True)
        if _normalize_label(str(data.get("source_file", ""))) == query_lower
    ]
    if len(exact_source_matches) == 1:
        return exact_source_matches[0]
    contains_matches = [
        str(node_id)
        for node_id, data in graph.nodes(data=True)
        if query_lower in _normalize_label(str(data.get("label", "")))
    ]
    if len(contains_matches) == 1:
        return contains_matches[0]
    return None


def affected_nodes(
    graph: nx.Graph[Any],
    seed: str,
    *,
    relations: Iterable[str] = DEFAULT_AFFECTED_RELATIONS,
    depth: int = 2,
) -> list[AffectedHit]:
    relation_set = set(relations)
    seen = {seed}
    queue: deque[tuple[str, int]] = deque([(seed, 0)])
    hits: list[AffectedHit] = []

    while queue:
        current, current_depth = queue.popleft()
        if current_depth >= depth:
            continue
        if hasattr(graph, "in_edges"):
            incoming = graph.in_edges(current, data=True)
        else:
            incoming = (
                (source, target, data)
                for source, target, data in graph.edges(data=True)
                if target == current
            )
        for source, _target, data in incoming:
            relation = str(data.get("relation", ""))
            if relation not in relation_set:
                continue
            source = str(source)
            if source in seen:
                continue
            seen.add(source)
            hit = AffectedHit(source, current_depth + 1, relation)
            hits.append(hit)
            queue.append((source, current_depth + 1))

    return hits
