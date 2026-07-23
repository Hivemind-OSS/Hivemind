"""Engine adapters: the only census module that knows the engine APIs.

matrix (the edge engine: code graph + blast radius) and combdrift (the node
engine: symbol existence + contract drift) are consumed here and in the join,
nowhere else, so an engine-surface change lands in one place. Every engine
import is lazy — deferred into the function that needs it — because matrix
reads the MATRIX_OUT env var exactly once, at import time: the scratch pin in
`ensure_scratch_matrix_out` must win that race, and a module-level import here
would silently lose it for every caller.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hive.census.diff import Change, ChangeSet

if TYPE_CHECKING:  # annotation targets only; runtime imports stay lazy
    import combdrift
    import matrix
    import matrix.model


class EngineError(Exception):
    """An engine violated an expectation the receipt depends on; fail fast."""


# Process-wide scratch pin: MATRIX_OUT is import-time state in matrix, so the
# pin is process state here, not per-call state.
_scratch: Path | None = None


def ensure_scratch_matrix_out() -> Path:
    """Pin MATRIX_OUT to a per-process scratch dir before matrix can read it.

    Idempotent: the first call creates the scratch dir and sets the env var;
    later calls return the same dir. Setting the env var after matrix has
    imported is a silent no-op (the value is read once, at import time), so a
    too-late first call refuses loudly instead of littering the CWD, and a
    matrix that pinned some other directory is refused the same way.
    """
    global _scratch
    if _scratch is None:
        if "matrix" in sys.modules:
            raise EngineError(
                "matrix was imported before MATRIX_OUT was pinned; the env var "
                "is read once at matrix import time, so a late pin would be "
                "silently ignored and graph artifacts would land outside the "
                "scratch dir"
            )
        scratch = Path(tempfile.mkdtemp(prefix="hive-census-matrix-out-"))
        os.environ["MATRIX_OUT"] = str(scratch)
        _scratch = scratch
        return scratch
    paths_module = sys.modules.get("matrix.paths")
    if paths_module is not None:
        pinned = Path(str(paths_module.MATRIX_OUT))
        if pinned != _scratch and _scratch not in pinned.parents:
            raise EngineError(
                f"matrix pinned MATRIX_OUT to {pinned}, outside the census "
                f"scratch {_scratch}; graph artifacts would leak outside the "
                "per-run dir"
            )
    return _scratch


@dataclass(frozen=True, slots=True)
class GraphPair:
    """Both sides' graphs, version stamps, and path-dialect offsets, in memory only.

    The second build_graph run overwrites {MATRIX_OUT}/graph.json, so after
    the head build the on-disk artifact no longer describes the base side;
    nothing may ever re-read the pair from disk. The offsets are captured the
    same way — per side, right after each build, before the next build
    overwrites the manifest they derive from: matrix roots node ``source_file``
    at the corpus common ancestor, so on a single-source-root checkout the
    graph dialect drops that prefix (``pkg/mod.py`` -> ``mod.py``) while the
    diff stays repo-root-relative; the offset (e.g. ``"pkg"``, or ``""`` on any
    multi-rooted corpus) is what re-roots graph paths at the census seam
    (BUG-038; the hive-edge anchor twin is BUG-035).
    """

    base: matrix.model.Graph
    head: matrix.model.Graph
    base_stamp: matrix.ModelVersion
    head_stamp: matrix.ModelVersion
    base_offset: str = ""
    head_offset: str = ""


def _graph_offset(tree: Path) -> str:
    """The path-dialect offset between ``tree`` and the graph just built for it,
    derived from the manifest THAT build persisted (the exact corpus the engine
    inferred its root from) via matrix's own ``root_offset`` — the single owner
    of the derivation, so the census bridge can never drift from the engine.
    Must be read before the next build overwrites the manifest. Fail-open: an
    unreadable or foreign manifest ⇒ "" — the pre-bridge repo-dialect assumption.
    """
    try:
        import matrix
        import matrix.paths

        keys = json.loads(
            Path(matrix.paths.default_manifest()).read_text(encoding="utf-8")
        )
        offset: str = matrix.root_offset(tree, [Path(key) for key in keys])
        return offset
    except Exception:
        return ""


def build_graphs(change: Change) -> GraphPair:
    """Build and stamp both sides' graphs from the provisioned worktrees.

    Each stamp's commit_sha comes from the worktree's checked-out HEAD; a
    mismatch against the change's SHAs means the worktree is not what the
    diff described, and the receipt must not be built from it. Each side's
    path-dialect offset is captured immediately after its build — the second
    build overwrites the manifest the first offset derives from.
    """
    from matrix import build_graph, version_stamp

    base = build_graph(change.base_tree)
    base_offset = _graph_offset(change.base_tree)
    base_stamp = version_stamp(base, change.base_tree)
    head = build_graph(change.head_tree)
    head_offset = _graph_offset(change.head_tree)
    head_stamp = version_stamp(head, change.head_tree)
    for side, stamp, expected in (
        ("base", base_stamp, change.touched.base_sha),
        ("head", head_stamp, change.touched.head_sha),
    ):
        if stamp.commit_sha != expected:
            raise EngineError(
                f"{side} graph was stamped at commit {stamp.commit_sha}, but "
                f"the change names {expected}; the worktree does not match "
                "the diff"
            )
    return GraphPair(
        base=base,
        head=head,
        base_stamp=base_stamp,
        head_stamp=head_stamp,
        base_offset=base_offset,
        head_offset=head_offset,
    )


_START_LINE_RE = re.compile(r"L(\d+)")

# Relations meaning "this scope declares that symbol" — walked upward once to
# reconstruct a method's Class.method spelling.
_SYMBOL_PARENT_RELATIONS = frozenset({"contains", "defines", "method"})


def _is_file_node(data: dict[str, Any]) -> bool:
    # Built graphs mark no node with file_type="file" (every node is "code");
    # a file's own node is recognizable only as label == basename(source_file).
    # Reading one as a symbol would absorb whole-file spans and poison the
    # symbol spelling handed to combdrift.
    label = str(data.get("label") or "")
    source_file = str(data.get("source_file") or "")
    return bool(source_file) and label == Path(source_file).name


def _bare_name(label: str) -> str:
    # Callable labels are decorated ("greet()"), method labels additionally
    # dot-prefixed (".wave()"); the combdrift spelling wants neither.
    if label.endswith("()"):
        label = label[:-2]
    return label.lstrip(".")


def _repo_relative(source_file: str, offset: str) -> str:
    """Re-root one graph-dialect path into the repo-root-relative dialect the
    diff, the receipt subjects, and every episode anchor speak. The offset is
    the extraction-root prefix matrix dropped ("" whenever the dialects already
    agree — any multi-rooted corpus — which keeps this an exact identity there).
    """
    # marker: returning source_file verbatim when offset is set (joining the
    # graph dialect raw) is a mutation — BUG-038's exact silent death.
    return f"{offset}/{source_file}" if offset else source_file


def _symbol_index(
    g_nx: Any, *, offset: str = ""
) -> dict[str, list[tuple[int, str, str]]]:
    """repo-relative source_file -> [(start_line, node_id, label)], by start line.

    Keys ride the REPO dialect (``offset`` re-roots the graph's spelling) so the
    diff's paths join them verbatim on any layout. File nodes and nodes without
    a parseable start line are excluded. A single unreadable node is skipped
    rather than poisoning every file's index.
    """
    by_file: dict[str, list[tuple[int, str, str]]] = {}
    for node_id, data in g_nx.nodes(data=True):
        try:
            if _is_file_node(data):
                continue
            source_file = str(data.get("source_file") or "")
            match = _START_LINE_RE.match(str(data.get("source_location") or ""))
            if not source_file or match is None:
                continue
            by_file.setdefault(_repo_relative(source_file, offset), []).append(
                (int(match.group(1)), str(node_id), str(data.get("label") or node_id))
            )
        except Exception:
            continue
    for entries in by_file.values():
        entries.sort()
    return by_file


def _qualified_symbol(g_nx: Any, node_id: str, label: str) -> str:
    """The combdrift spelling: a top-level name, or Class.method.

    A declaring parent that is neither the file's own node nor
    callable-decorated is a class scope; its name prefixes the member's.
    """
    name = _bare_name(label)
    for parent_id in sorted(g_nx.predecessors(node_id), key=str):
        relation = str(
            (g_nx.get_edge_data(parent_id, node_id) or {}).get("relation") or ""
        )
        if relation not in _SYMBOL_PARENT_RELATIONS:
            continue
        parent = g_nx.nodes[parent_id]
        if _is_file_node(parent):
            continue
        parent_label = str(parent.get("label") or "")
        if parent_label and not parent_label.endswith("()"):
            return f"{_bare_name(parent_label)}.{name}"
    return name


def node_subject(g_nx: Any, node_id: str, *, offset: str = "") -> str | None:
    """The ``path::Symbol`` receipt spelling of one graph node, or None.

    ``offset`` re-roots the graph's source_file dialect to repo-root-relative
    (the dialect episode anchors join on); "" — any multi-rooted corpus — is an
    exact identity. None for a node id this graph does not hold (e.g. a
    base-side symbol that no longer exists at head), a file's own node (a file
    is not a symbol), a node without a source file, or a node whose attributes
    are unreadable — consumers under-claim rather than hand the kernel a
    spelling it would then join episode anchors on.
    """
    try:
        if node_id not in g_nx:
            return None
        data = g_nx.nodes[node_id]
        if _is_file_node(data):
            return None
        source_file = str(data.get("source_file") or "")
        if not source_file:
            return None
        label = str(data.get("label") or "") or str(node_id)
        path = _repo_relative(source_file, offset)
        return f"{path}::{_qualified_symbol(g_nx, node_id, label)}"
    except Exception:
        return None


def _nodes_in_span(
    entries: list[tuple[int, str, str]], span: tuple[int, int]
) -> list[tuple[str, str]]:
    """The union of the enclosing node and every node starting within the span.

    Enclosing = the same-file node with the greatest start line <= the span
    start. Whole-file spans begin at line 1, before the first symbol node, so
    enclosing-only attribution finds nothing there; the starting-within half
    is what attributes added and deleted files.
    """
    start, end = span
    enclosing: tuple[str, str] | None = None
    inside: list[tuple[str, str]] = []
    for node_start, node_id, label in entries:
        if node_start <= start:
            enclosing = (node_id, label)
        elif node_start <= end:
            inside.append((node_id, label))
        else:
            break
    return ([enclosing] if enclosing is not None else []) + inside


def attribute_symbols(
    touched: ChangeSet, graphs: GraphPair
) -> tuple[tuple[str, str | None], ...]:
    """Map changed line spans to touched (path, symbol) pairs, per side.

    removed_spans attribute against the base graph (a deleted symbol exists
    only there), added_spans against the head graph. Each side's index is
    keyed through its own path-dialect offset, so the diff's repo-relative
    paths join it verbatim on any layout (BUG-038). A span with no candidate
    node degrades to the file's (path, None) entry — under-claimed, never
    guessed — and one pathological file degrades to its own file-level entry
    without aborting the others.
    """
    base_index = _symbol_index(graphs.base.nx, offset=graphs.base_offset)
    head_index = _symbol_index(graphs.head.nx, offset=graphs.head_offset)
    pairs: set[tuple[str, str | None]] = set()
    for changed in touched.files:
        try:
            base_path = changed.old_path or changed.path
            sides = (
                (base_index.get(base_path, []), graphs.base.nx, changed.removed_spans),
                (head_index.get(changed.path, []), graphs.head.nx, changed.added_spans),
            )
            for entries, g_nx, spans in sides:
                for span in spans:
                    found = _nodes_in_span(entries, span)
                    if not found:
                        pairs.add((changed.path, None))
                    for node_id, label in found:
                        pairs.add(
                            (changed.path, _qualified_symbol(g_nx, node_id, label))
                        )
        except Exception:
            pairs.add((changed.path, None))
    return tuple(
        sorted(pairs, key=lambda pair: (pair[0], pair[1] is not None, pair[1] or ""))
    )


def node_change(
    change: Change, touched_symbols: tuple[tuple[str, str | None], ...]
) -> combdrift.ChangeVerdict:
    """Classify each touched symbol's existence + drift between the two trees.

    Thin pass-through: combdrift does no git, so it receives the provisioned
    worktrees and the resolved SHAs; (path, None) file-level entries produce
    no SymbolChange. Reason strings come back with machine-stable prefixes
    plus ": <detail>" suffixes and are never rewritten here.
    """
    import combdrift

    return combdrift.verify_change(
        str(change.base_tree),
        str(change.head_tree),
        touched_symbols,
        base_sha=change.touched.base_sha,
        head_sha=change.touched.head_sha,
    )
