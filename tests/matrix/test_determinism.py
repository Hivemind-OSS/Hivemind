"""Determinism + offline guarantees of the graph identity."""

from __future__ import annotations

import socket

import networkx as nx

from hive.matrix.model import Graph
from hive.matrix.version import graph_sha256, version_stamp


def _graph() -> Graph:
    g = nx.DiGraph()
    g.add_node("a_foo", label="foo()", source_file="a.py", file_type="function")
    g.add_node("a_bar", label="bar()", source_file="a.py", file_type="function")
    g.add_edge("a_bar", "a_foo", relation="calls")
    return Graph(g)


def test_same_graph_same_hash():
    assert graph_sha256(_graph()) == graph_sha256(_graph())


def test_built_at_volatile_but_identity_stable(tmp_path):
    v1 = version_stamp(_graph(), tmp_path)
    v2 = version_stamp(_graph(), tmp_path)
    assert v1.graph_sha256 == v2.graph_sha256


def test_hashing_opens_no_sockets():
    opened: list[int] = []
    original = socket.socket

    class _Guard(original):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            opened.append(1)
            super().__init__(*a, **k)

    socket.socket = _Guard  # type: ignore[misc]
    try:
        graph_sha256(_graph())
    finally:
        socket.socket = original  # type: ignore[misc]
    assert not opened


def test_graph_identity_is_line_ending_invariant(tmp_path):
    """CRLF vs LF (Windows git autocrlf) must not move the graph identity: the same source
    under either line ending hashes identically — source_location is line-based, so a mixed-OS
    fleet's graph fingerprints agree byte-for-byte."""
    import hive.matrix as matrix
    from hive.matrix import assemble

    src_lf = "def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n"
    f = tmp_path / "mod.py"
    f.write_bytes(src_lf.encode("utf-8"))
    g_lf = assemble.build_from_json(matrix.extract([f], root=tmp_path), root=tmp_path)
    f.write_bytes(src_lf.replace("\n", "\r\n").encode("utf-8"))
    g_crlf = assemble.build_from_json(matrix.extract([f], root=tmp_path), root=tmp_path)
    assert graph_sha256(g_lf) == graph_sha256(g_crlf)
