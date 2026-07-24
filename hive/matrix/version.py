"""C5: the version stamp — a deterministic identity for a built graph.

Identity (``graph_sha256``) hashes a *canonical projection*: nodes and edges
sorted, only the load-bearing fields, volatile fields (``built_at``, insertion
order, indentation) excluded. Storage format is a separate decision, so the same
structural graph always hashes the same regardless of how it was serialized. The
only subprocess in the whole engine lives here: reading the repo's git HEAD.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from .gitenv import clean_git_env
from .model import Graph

# ENGINE_VERSION: frozen provenance literal, matching hive.matrix.__version__.
# Was importlib.metadata.version("matrix"), which silently degrades to "0+unknown"
# now that matrix is a first-party hive subpackage, not a separately-installed dist.
ENGINE_VERSION = "0.9.0"

# Load-bearing fields that define graph identity. Anything not listed (degree,
# community, built_at, …) is excluded so identity is structural, not incidental.
_NODE_FIELDS = ("label", "source_file", "source_location", "file_type")
_EDGE_FIELDS = ("relation", "source_file", "source_location")


@dataclass(frozen=True)
class ModelVersion:
    graph_sha256: str
    commit_sha: str
    engine_version: str
    built_at: str
    node_count: int
    edge_count: int


def _backing(graph: Graph | nx.DiGraph[Any]) -> nx.DiGraph[Any]:
    return graph.nx if isinstance(graph, Graph) else graph


def canonical_json(graph: Graph | nx.DiGraph[Any]) -> str:
    """Deterministic, storage-independent projection of the graph for hashing."""
    g = _backing(graph)
    nodes = []
    for nid, data in g.nodes(data=True):
        node = {"id": str(nid)}
        for f in _NODE_FIELDS:
            value = data.get(f)
            if value is not None:
                node[f] = value
        nodes.append(node)
    nodes.sort(key=lambda n: n["id"])

    edges = []
    for source, target, data in g.edges(data=True):
        edge = {"source": str(source), "target": str(target)}
        for f in _EDGE_FIELDS:
            value = data.get(f)
            if value is not None:
                edge[f] = value
        edges.append(edge)
    edges.sort(
        key=lambda e: (
            e["source"],
            e["target"],
            e.get("relation", ""),
            str(e.get("source_location") or ""),
        )
    )

    return json.dumps(
        {"nodes": nodes, "edges": edges},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def graph_sha256(graph: Graph | nx.DiGraph[Any]) -> str:
    return hashlib.sha256(canonical_json(graph).encode("utf-8")).hexdigest()


def _commit_sha(repo_root: Path) -> str:
    # clean_git_env: an inherited GIT_DIR would silently retarget this child
    # inside a git hook subprocess (BUG-034) — the shared owner strips it.
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            env=clean_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "unknown"


def version_stamp(graph: Graph | nx.DiGraph[Any], repo_root: Path) -> ModelVersion:
    g = _backing(graph)
    return ModelVersion(
        graph_sha256=graph_sha256(graph),
        commit_sha=_commit_sha(Path(repo_root)),
        engine_version=ENGINE_VERSION,
        built_at=datetime.now(timezone.utc).isoformat(),
        node_count=g.number_of_nodes(),
        edge_count=g.number_of_edges(),
    )
