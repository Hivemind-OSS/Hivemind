"""Contract tests for the propagation block: blast-radius neighbourhoods in the
receipt's public vocabulary.

The real-engine tier runs the session fixture repo through the actual pipeline
(diff -> graphs -> drift -> regression join) and pins that every neighbour is a
``path::Symbol`` string resolved against the HEAD graph's node table — the
engine's node-id scheme never leaks into the artifact. The duck-typed tier
drives the policy edges (drift filter, caps, dedup, unknown/file nodes,
determinism) with hand-built graphs and findings that the frozen
RegressionFinding carrier could never construct (e.g. an additive-drift seed).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _duck_graphs(head_nx) -> object:
    from hive.census.engines import GraphPair

    return GraphPair(base=None, head=SimpleNamespace(nx=head_nx), base_stamp=None,
                     head_stamp=None)


def _duck_finding(**overrides) -> SimpleNamespace:
    finding = dict(
        path="pkg/lib.py", symbol="greet", drift="breaking", seed="n_greet",
        callers=("n_caller",), dependents=(), tests=(), depth=2,
        tag="bounded-estimate", reason="blast-radius-lower-bound",
    )
    finding.update(overrides)
    return SimpleNamespace(**finding)


@pytest.fixture()
def head_nx(matrix_scratch):
    import networkx as nx

    g = nx.DiGraph()
    g.add_node("n_greet", label="greet()", source_file="pkg/lib.py")
    g.add_node("n_caller", label="caller()", source_file="pkg/app.py")
    g.add_node("n_test", label="test_greet()", source_file="tests/test_lib.py")
    g.add_node("n_file", label="app.py", source_file="pkg/app.py")  # a file's own node
    g.add_node("n_nofile", label="ghost()", source_file="")         # unreadable source
    return g


# ── the real-engine tier: head-resolved path::Symbol, never raw node ids ────────


def test_real_blast_neighbours_render_as_head_path_symbols(
    graph_pair, change_verdict
) -> None:
    from hive.census import regression_join
    from hive.census.propagation import propagation_block

    findings = regression_join(change_verdict, graph_pair.base, depth=2)
    entries = propagation_block(findings, graph_pair)
    assert entries                                       # the fixture change has seeds
    by_seed = {entry["seed"]: entry for entry in entries}
    greet = by_seed["lib.py::greet"]                     # the breaking seed
    assert greet["drift"] == "breaking" and greet["depth"] == 2
    assert "app.py::caller" in greet["neighbors"]        # its caller, head-resolved
    assert "test_lib.py::test_greet" in greet["neighbors"]
    head_ids = set(graph_pair.head.nx.nodes)
    for entry in entries:
        assert "::" in entry["seed"]
        assert entry["drift"] in ("breaking", "removed")
        assert entry["neighbors"] == sorted(entry["neighbors"])   # deterministic order
        for neighbor in entry["neighbors"]:
            assert "::" in neighbor                      # the public vocabulary…
            assert neighbor not in head_ids              # …never a raw engine node id


def test_real_output_is_deterministic(graph_pair, change_verdict) -> None:
    from hive.census import regression_join
    from hive.census.propagation import propagation_block

    findings = regression_join(change_verdict, graph_pair.base, depth=2)
    assert propagation_block(findings, graph_pair) == propagation_block(
        findings, graph_pair)


# ── the duck-typed tier: policy edges the frozen carrier cannot reach ───────────


def test_additive_drift_seeds_are_filtered(head_nx) -> None:
    from hive.census.propagation import propagation_block

    entries = propagation_block(
        (_duck_finding(drift="additive"), _duck_finding(drift="unchanged"),
         _duck_finding(drift="")),
        _duck_graphs(head_nx))
    assert entries == []                                 # only breaking/removed propagate


def test_unknown_file_and_sourceless_nodes_are_skipped(head_nx) -> None:
    from hive.census.propagation import propagation_block

    (entry,) = propagation_block(
        (_duck_finding(callers=("n_caller", "n_unknown", "n_file", "n_nofile"),
                       tests=("n_test",)),),
        _duck_graphs(head_nx))
    assert entry["neighbors"] == ["pkg/app.py::caller", "tests/test_lib.py::test_greet"]


def test_seed_itself_never_rides_as_its_own_neighbour(head_nx) -> None:
    from hive.census.propagation import propagation_block

    (entry,) = propagation_block(
        (_duck_finding(callers=("n_greet", "n_caller")),), _duck_graphs(head_nx))
    assert entry["neighbors"] == ["pkg/app.py::caller"]  # the seed subject is excluded


def test_neighbours_deduped_and_capped_sorted(head_nx) -> None:
    import networkx as nx

    from hive.census.propagation import propagation_block

    g = nx.DiGraph()
    ids = []
    for i in range(10):
        node_id = f"n_{i}"
        g.add_node(node_id, label=f"fn_{i:02d}()", source_file="pkg/many.py")
        ids.append(node_id)
    (entry,) = propagation_block(
        (_duck_finding(callers=tuple(ids), dependents=tuple(ids),
                       tests=("n_3", "n_4")),),
        _duck_graphs(g), cap_neighbors=4)
    assert len(entry["neighbors"]) == 4                  # deduped across the union, capped
    assert entry["neighbors"] == sorted(f"pkg/many.py::fn_{i:02d}" for i in range(4))


def test_abstained_or_neighbourless_findings_produce_no_entry(head_nx) -> None:
    from hive.census.propagation import propagation_block

    entries = propagation_block(
        (_duck_finding(seed=None, callers=(), dependents=(), tests=()),
         _duck_finding(callers=("n_unknown",))),        # nothing head-resolvable
        _duck_graphs(head_nx))
    assert entries == []


def test_headless_graph_pair_degrades_to_empty(head_nx) -> None:
    from hive.census.engines import GraphPair
    from hive.census.propagation import propagation_block

    graphs = GraphPair(base=None, head=None, base_stamp=None, head_stamp=None)
    assert propagation_block((_duck_finding(),), graphs) == []
