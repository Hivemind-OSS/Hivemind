"""matrix — a deterministic, AST-only code-structure engine.

Turns a repository into a directed graph of symbols and files
(``calls``/``imports``/``contains``/``inherits``/…), answers the blast-radius
lower-bound query by reverse reachability, re-derives itself incrementally per
commit via a content-hash manifest, and stamps every build with a version
identity so a stale graph is detectable.

A first-party server subpackage of the one ``hive`` dist (absorbed from the former
``hive-edge`` workspace) — the dependency-neighborhood / subgraph-fingerprint engine
behind the census ``matrix/subgraph_fp`` token and the verifier's affected-test set.
Detect-only and fail-open: an absent or stale graph degrades to a rebuild or a skipped
check, never a false verdict.

No LLM, no network, no API cost — a rebuildable cache, never a maintained source
of truth. The public surface is intentionally narrow; see ``docs/engines/matrix.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import networkx as nx

from . import gitenv
from .blastradius import BlastRadius, Hit, blast_radius
from .extract import collect_files, extract, root_offset
from .model import (
    CONTAINMENT_RELATIONS,
    DEPENDENCY_RELATIONS,
    Edge,
    Graph,
    Node,
)
from .version import ModelVersion, version_stamp

# Frozen provenance literal (D7): the engine's version identity, stamped into receipts via
# provenance["matrix"] and read by hive/domain/change_evidence.py:version_stamp (the fail-closed
# rider gate). Frozen at the absorption value — there is no longer a workspace-lockstep version
# (that machinery died when the engine became a first-party subpackage); version.py's
# ENGINE_VERSION mirrors this literal.
__version__ = "0.9.0"

__all__ = [
    "__version__",
    # git subprocess hygiene (the ONE owner of the repo-discovery denylist)
    "gitenv",
    # extraction
    "extract",
    "collect_files",
    "root_offset",
    # lifecycle
    "build_graph",
    "update",
    # model
    "Node",
    "Edge",
    "Graph",
    "DEPENDENCY_RELATIONS",
    "CONTAINMENT_RELATIONS",
    # blast radius
    "blast_radius",
    "BlastRadius",
    "Hit",
    # version stamp
    "version_stamp",
    "ModelVersion",
]


def _artifact_paths(out_dir: "Path | None") -> tuple[str, str]:
    """The (graph.json, manifest.json) paths for one build/update.

    ``out_dir=None`` defers to :mod:`matrix.paths`'s env-configured default —
    resolved lazily here, at call time, exactly as before. A non-None
    ``out_dir`` pins both artifacts under it, so two repos can keep separate
    caches in one process regardless of the import-time ``MATRIX_OUT`` value.
    """
    from .paths import default_graph_json, default_manifest

    if out_dir is None:
        return default_graph_json(), default_manifest()
    base = Path(out_dir)
    return str(base / "graph.json"), str(base / "manifest.json")


def build_graph(repo_root: Path, *, out_dir: "Path | None" = None) -> "Graph":
    """Derive the full graph for ``repo_root`` from scratch.

    Discovers every source file, extracts it, assembles a directed graph, writes
    ``graph.json`` and the content-hash manifest, and returns the typed
    :class:`~matrix.model.Graph`. The on-disk artifacts make the next
    :func:`update` incremental. They land under ``out_dir`` when given (keep it
    outside the repo so it is never re-scanned as source), else under the
    env-configured default (``matrix-out``).
    """
    from . import assemble, detect, serialize
    from .model import Graph

    graph_json, manifest = _artifact_paths(out_dir)
    repo_root = Path(repo_root)
    files = collect_files(repo_root)
    extraction = extract(files)
    digraph = assemble.build_from_json(extraction, directed=True, root=repo_root)
    serialize.to_json(digraph, graph_json)
    detect.save_manifest(files, manifest)
    return Graph(cast("nx.DiGraph[Any]", digraph))


def update(
    repo_root: Path, *, graph: "Graph | None" = None, out_dir: "Path | None" = None
) -> "Graph":
    """Incrementally re-derive the graph for ``repo_root`` since the last build.

    Reads the manifest, finds changed/deleted files, re-extracts a chunk CLOSED
    over imports, and merges it into the prior ``graph.json`` — a changed file's
    stale nodes/edges are replaced and a deleted file's are pruned. Writes the
    refreshed ``graph.json`` + manifest and returns the typed
    :class:`~matrix.model.Graph`. Falls back to a full :func:`build_graph` when
    no prior manifest exists. ``out_dir`` names the artifact directory to read
    AND write (threaded through the fallback and the full-rebuild escape);
    ``None`` keeps the env-configured default.

    Closure (BUG-025): extraction binds cross-file references against its
    chunk-local node table only, so the chunk holds the changed files, every
    persisted importer of them (re-MERGED, so their inbound bindings re-form),
    and — as binding context only — the transitive out-import closure read from
    disk; the merged contribution is filtered back to changed+importers so a
    context file never displaces its intact base contribution. The extraction
    root — matrix's inferred corpus common-ancestor, which on a single-source-root
    checkout is a SUBDIR of the scan root — is threaded to the import-closure, the
    chunk extraction, and the merge/prune alike, so chunk ids, importer matching,
    and pruned paths all speak the persisted graph's ``source_file`` dialect on ANY
    layout. When the closure exceeds half the corpus the incremental premise is
    gone and the always-correct :func:`build_graph` runs instead.

    ``graph`` is accepted for API symmetry; the merge always reads the persisted
    ``graph.json`` (the direction-preserving source of truth), so it is not used.
    """
    from . import assemble, closure, detect, serialize
    from .extract import _infer_root
    from .model import Graph

    graph_json, manifest = _artifact_paths(out_dir)
    repo_root = Path(repo_root)
    change = detect.detect_incremental(repo_root, manifest)
    if not detect.load_manifest(manifest):
        return build_graph(repo_root, out_dir=out_dir)

    changed = change["changed"]
    discovered = change["discovered"]
    # The extraction root — matrix's inferred corpus common-ancestor — is what the persisted
    # graph relativized its source_files against; on a single-source-root checkout it is a
    # SUBDIR of the scan root. Thread THIS (not repo_root) to the closure and the merge so
    # importer matching, import resolution, and prune all speak the persisted dialect. The
    # empty-repo guard keeps the degenerate all-deleted case byte-identical to before.
    root = _infer_root(discovered) if discovered else repo_root
    new_chunks = []
    if changed:
        merge_set, extraction_set = closure.merge_and_extraction_sets(
            changed, graph_json, root
        )
        if len(extraction_set) > len(discovered) / 2:
            return build_graph(repo_root, out_dir=out_dir)
        chunk = extract(sorted(extraction_set), root=root)
        new_chunks = [closure.filter_chunk_to_merge_set(chunk, merge_set, root)]
    prune = [str(p) for p in change["prune_sources"]]
    digraph = assemble.build_merge(
        new_chunks,
        graph_path=graph_json,
        prune_sources=prune,
        directed=True,
        root=root,
    )
    serialize.to_json(digraph, graph_json)
    detect.save_manifest(change["discovered"], manifest)
    return Graph(cast("nx.DiGraph[Any]", digraph))
