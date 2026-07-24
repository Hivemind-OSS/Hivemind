"""C3 reverse-reachability core: seed resolution + BFS."""

from __future__ import annotations

import networkx as nx

from hive.matrix.affected import affected_nodes, resolve_seed
from hive.matrix.model import DEPENDENCY_RELATIONS


def _graph() -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_node("a_foo", label="foo()", source_file="a.py")
    g.add_node("a_bar", label="bar()", source_file="a.py")
    g.add_node("b_baz", label="baz()", source_file="b.py")
    g.add_node("c_qux", label="qux()", source_file="c.py")
    g.add_edge("a_bar", "a_foo", relation="calls")  # bar calls foo
    g.add_edge("b_baz", "a_bar", relation="calls")  # baz calls bar
    g.add_edge("c_qux", "a_foo", relation="mentions")  # NOT a dependency relation
    return g


def test_resolve_seed_exact_id():
    assert resolve_seed(_graph(), "a_foo") == "a_foo"


def test_resolve_seed_by_label():
    assert resolve_seed(_graph(), "foo()") == "a_foo"


def test_resolve_seed_by_bare_name():
    assert resolve_seed(_graph(), "foo") == "a_foo"


def test_resolve_seed_no_match():
    assert resolve_seed(_graph(), "nope") is None


def test_affected_one_hop():
    hits = affected_nodes(_graph(), "a_foo", relations=DEPENDENCY_RELATIONS, depth=1)
    assert {h.node_id for h in hits} == {"a_bar"}
    assert hits[0].via_relation == "calls" and hits[0].depth == 1


def test_affected_two_hops():
    hits = affected_nodes(_graph(), "a_foo", relations=DEPENDENCY_RELATIONS, depth=2)
    assert {h.node_id for h in hits} == {"a_bar", "b_baz"}


def test_affected_filters_non_dependency_relation():
    hits = affected_nodes(_graph(), "a_foo", relations=DEPENDENCY_RELATIONS, depth=3)
    assert "c_qux" not in {h.node_id for h in hits}
