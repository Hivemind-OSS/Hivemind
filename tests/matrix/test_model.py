"""C1 contract: typed model surface + the relation taxonomy."""

from __future__ import annotations

import dataclasses

import networkx as nx
import pytest

from hive.matrix.model import (
    CONTAINMENT_RELATIONS,
    DEFAULT_AFFECTED_RELATIONS,
    DEPENDENCY_RELATIONS,
    Edge,
    Graph,
    Node,
)


def _sample_graph() -> Graph:
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
    g.add_node("a_py", label="a.py", source_file="a.py", file_type="file")
    g.add_edge(
        "a_bar", "a_foo", relation="calls", source_file="a.py", source_location="L6"
    )
    g.add_edge(
        "a_py", "a_foo", relation="contains", source_file="a.py", source_location=None
    )
    return Graph(g)


def test_node_dto_at_egress():
    n = _sample_graph().node("a_foo")
    assert isinstance(n, Node)
    assert (n.id, n.label, n.source_file, n.source_location) == (
        "a_foo",
        "foo()",
        "a.py",
        "L1",
    )
    assert n.kind == "function"  # mapped from file_type


def test_node_missing_returns_none():
    assert _sample_graph().node("nope") is None


def test_in_edges_typed_and_complete():
    ins = _sample_graph().in_edges("a_foo")
    assert all(isinstance(e, Edge) for e in ins)
    assert {e.relation for e in ins} == {"calls", "contains"}


def test_out_edges_directional():
    outs = _sample_graph().out_edges("a_bar")
    assert [e.target for e in outs] == ["a_foo"]
    assert outs[0].relation == "calls"


def test_edges_of_absent_node_empty():
    G = _sample_graph()
    assert G.in_edges("nope") == [] and G.out_edges("nope") == []


def test_dtos_are_frozen():
    n = Node(id="x", label="x", source_file="x.py")
    with pytest.raises(dataclasses.FrozenInstanceError):
        n.id = "y"  # type: ignore[misc]


def test_relation_taxonomy():
    assert DEFAULT_AFFECTED_RELATIONS == (
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
    assert DEPENDENCY_RELATIONS == frozenset(DEFAULT_AFFECTED_RELATIONS) | {
        "depends_on",
        "defines",
    }
    assert CONTAINMENT_RELATIONS == frozenset({"contains", "method", "defines"})
    # the affected set is a strict subset of the dependency set
    assert frozenset(DEFAULT_AFFECTED_RELATIONS) < DEPENDENCY_RELATIONS
