"""Assemble contract: matrix.build_from_json graph shape.

The structural contract is that assembling an extraction with ``directed=True``
yields a directed graph whose non-rationale nodes and edges are preserved.
Direction-bookkeeping attrs (``_src``/``_tgt``) and the confidence/colour
metadata are not structural.
"""

from __future__ import annotations

import networkx as nx

from tests.matrix.fixtures_manifest import lang_paths

import hive.matrix as matrix
from hive.matrix import assemble


def test_build_from_json_assembles_extraction(tmp_path):
    """A built graph contains every non-rationale node from the extraction and is
    directed by default."""
    paths = lang_paths("python")
    extraction = matrix.extract(paths)
    g = assemble.build_from_json(extraction, root=paths[0].parent)
    assert isinstance(g, nx.DiGraph)  # directed default
    assert g.number_of_nodes() > 0
    assert g.number_of_edges() > 0


# ── Cross-OS path-separator device-invariance (mixed-OS fleet reproducibility) ────
#
# The shared store needs graph identity — and every fingerprint built from source_file —
# byte-identical across a fleet on one engine version, including Windows checkouts whose
# raw paths use backslashes. The normalization that guarantees this is scattered; these
# pin it against a silent regression from a future refactor.


def _win_chunk() -> dict:
    """A fresh extraction chunk whose source_files use Windows backslash separators — the raw
    form a Windows checkout produces. Fresh each call because the build_* paths normalize the
    node dicts in place."""
    return {
        "nodes": [
            {
                "id": "mod",
                "label": "mod.py",
                "source_file": "src\\pkg\\mod.py",
                "file_type": "code",
                "_origin": "ast",
            },
            {
                "id": "mod_fn",
                "label": "fn()",
                "source_file": "src\\pkg\\mod.py",
                "file_type": "code",
                "_origin": "ast",
            },
        ],
        "edges": [
            {
                "source": "mod",
                "target": "mod_fn",
                "relation": "contains",
                "source_file": "src\\pkg\\mod.py",
            },
        ],
    }


def test_norm_source_file_posixizes_backslashes():
    """A Windows-style source_file normalizes to posix, so the fp seam never sees a separator."""
    assert assemble._norm_source_file("src\\pkg\\mod.py") == "src/pkg/mod.py"


def test_build_paths_posixize_windows_source_files(tmp_path):
    """The backslash never survives assembly on EITHER path: the scratch build_from_json and the
    incremental build_merge both emit posix source_files, so a mixed-OS fleet's hashes agree."""
    for g in (
        assemble.build_from_json(_win_chunk(), directed=True),
        assemble.build_merge(
            [_win_chunk()], graph_path=tmp_path / "absent.json", directed=True
        ),
    ):
        for _, d in g.nodes(data=True):
            assert "\\" not in (d.get("source_file") or "")
        for _, _, d in g.edges(data=True):
            assert "\\" not in (d.get("source_file") or "")
