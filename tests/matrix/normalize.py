"""Normalization for golden comparison.

Reduces an ``extract()`` result to its load-bearing, order-independent shape so
it can be compared against a committed golden snapshot, modulo the documented
intentional deltas:

- rationale nodes (``file_type == "rationale"``) and ``rationale_for`` edges are
  dropped — matrix never emits them;
- volatile / excluded fields (``_origin``, ``community``, ``norm_label``,
  ``confidence``/``confidence_score``/``weight``/``context``) are stripped;
- nodes/edges are reduced to sorted tuples of their structural fields.

The result is a pair of sorted lists (nodes, edges) of hashable tuples, suitable
for direct ``==`` comparison and for stable JSON golden snapshots.
"""

from __future__ import annotations


# Edge metadata that is provenance/confidence colour, not structure. Excluded so
# the comparison turns on topology + identity, not on a confidence label that
# either engine might score differently for the same edge.
_EDGE_VOLATILE = frozenset({"confidence", "confidence_score", "weight", "context"})

# Load-bearing node fields, in canonical order.
_NODE_FIELDS = ("id", "label", "file_type", "source_file", "source_location")
# Load-bearing edge fields, in canonical order.
_EDGE_FIELDS = ("source", "target", "relation", "source_file", "source_location")


def _is_rationale_node(n: dict) -> bool:
    return n.get("file_type") == "rationale"


def _is_rationale_edge(e: dict) -> bool:
    return e.get("relation") == "rationale_for"


def normalize_nodes(nodes: list[dict]) -> list[tuple]:
    out = []
    for n in nodes:
        if _is_rationale_node(n):
            continue
        vals = [_norm(n.get(f)) for f in _NODE_FIELDS]
        out.append(tuple(vals))
    return sorted(out)


def normalize_edges(edges: list[dict]) -> list[tuple]:
    out = []
    for e in edges:
        if _is_rationale_edge(e):
            continue
        vals = [_norm(e.get(f)) for f in _EDGE_FIELDS]
        out.append(tuple(vals))
    return sorted(out)


def _norm(v):
    """Coerce a field to a comparable scalar (None stays None, else str)."""
    if v is None:
        return None
    return str(v)


def normalize_for_golden(result: dict) -> dict:
    """JSON-serializable golden form: lists-of-lists, sorted, deltas stripped."""
    return {
        "nodes": [list(t) for t in normalize_nodes(result.get("nodes", []))],
        "edges": [list(t) for t in normalize_edges(result.get("edges", []))],
    }
