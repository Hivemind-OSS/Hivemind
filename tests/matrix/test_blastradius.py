"""C3 public surface: file expansion, caller/dependent/test bucketing, unsoundness."""

from __future__ import annotations

import networkx as nx

from hive.matrix.blastradius import BlastRadius, blast_radius
from hive.matrix.model import Graph


def _graph() -> Graph:
    g = nx.DiGraph()
    g.add_node("pkg_a", label="a.py", source_file="a.py", file_type="file")
    g.add_node("a_foo", label="foo()", source_file="a.py", file_type="function")
    g.add_node("a_helper", label="helper()", source_file="a.py", file_type="function")
    g.add_edge("pkg_a", "a_foo", relation="contains")
    g.add_edge("pkg_a", "a_helper", relation="contains")
    g.add_node("b_bar", label="bar()", source_file="b.py", file_type="function")
    g.add_edge("b_bar", "a_foo", relation="calls")  # a caller
    g.add_node("c_mod", label="c.py", source_file="c.py", file_type="file")
    g.add_edge("c_mod", "a_foo", relation="uses")  # an import-inferred dependent
    g.add_node(
        "t_test_foo", label="test_foo()", source_file="test_a.py", file_type="function"
    )
    g.add_edge("t_test_foo", "a_foo", relation="calls")  # a test caller
    return Graph(g)


def test_callers_and_always_unsound():
    br = blast_radius(_graph(), "foo", depth=1)
    assert isinstance(br, BlastRadius)
    assert br.unsound is True
    assert {h.node_id for h in br.callers} == {"b_bar", "t_test_foo"}


def test_dependents_via_uses_are_weak_tagged():
    br = blast_radius(_graph(), "foo", depth=1)
    assert {h.node_id for h in br.dependents} == {"c_mod"}
    assert all(h.via_relation == "uses" for h in br.dependents)


def test_tests_bucket():
    br = blast_radius(_graph(), "foo", depth=1)
    assert {h.node_id for h in br.tests} == {"t_test_foo"}


def test_file_seed_expands_to_symbols():
    br = blast_radius(_graph(), "a.py", depth=1)
    assert "b_bar" in {h.node_id for h in br.callers}


def test_seed_not_found_is_empty_lower_bound():
    br = blast_radius(_graph(), "does_not_exist")
    assert br.callers == [] and br.dependents == [] and br.tests == []
    assert br.unsound is True


def test_accepts_raw_digraph():
    br = blast_radius(_graph().nx, "foo", depth=1)
    assert {h.node_id for h in br.callers} == {"b_bar", "t_test_foo"}
