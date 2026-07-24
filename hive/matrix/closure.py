"""Import-closure computation for the incremental update (the BUG-025 fix).

``extract()`` binds cross-file references eagerly against its chunk-local node table and
silently drops what it cannot see, so re-extracting a changed file ALONE permanently loses the
cross-file edges that file owns — and an unchanged importer never re-binds into a changed
file's new symbols. ``update()`` therefore re-extracts a CLOSED chunk built here:

  merge set       — changed files ∪ every persisted importer of them: the files whose graph
                    contribution is REPLACED (their bindings must be re-made).
  extraction set  — merge set ∪ its transitive out-import closure read from DISK (the
                    persisted graph may be stale about a changed file's new imports): binding
                    context only, never displacing its own intact base contribution.

Python resolves through the shared ``_resolve_python_module_path`` seam (absolute + relative
imports); the JS family through ``_resolve_js_module_path`` (relative specifiers; bare
specifiers are external packages). Other languages contribute no disk closure — the caller's
fallback valve (a full rebuild) is the correctness ceiling. Fail-open throughout: an
unparseable file imports nothing; a missing graph.json degrades to merge set == changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .extract import _extract_all
from .extract.configs import _resolve_js_module_path
from .extract.resolve import (
    _js_module_specifier,
    _parse_js_tree,
    _parse_python_tree,
    _python_import_from_module,
    _python_imported_names,
    _resolve_python_module_path,
    _walk_js_tree,
    _walk_python_tree,
)
from .model import DEFAULT_AFFECTED_RELATIONS

_JS_SUFFIXES = {".js", ".jsx", ".mjs", ".ts", ".tsx"}

# Reference relations whose cross-file target is a sourceless stub the corpus rewire
# collapses onto the real table (SQL FK/view/trigger) — they populate no ``raw_calls``,
# so the referenced name is surfaced from the reference EDGE, not the call-name pass.
_REFERENCE_RELATIONS = frozenset({"references", "reads_from", "triggers"})


def _source_file_forms(path: Path, base: Path) -> set[str]:
    """Every string form a graph/chunk item's ``source_file`` may use for ``path``
    (raw, posix, and base-relative posix — mirrors build_merge's both-forms matching)."""
    forms = {str(path), path.as_posix()}
    try:
        forms.add(path.resolve().relative_to(Path(base).resolve()).as_posix())
    except ValueError:
        pass
    return forms


def _file_imports(path: Path, root: Path) -> set[Path]:
    """The repo files ``path`` imports, resolved on disk (absolute resolved Paths).

    ``root`` is the EXTRACTION root — the corpus common-ancestor the persisted graph
    relativized against — NOT necessarily the repo scan root. Absolute Python imports
    (rootless ``from util import helper`` or package-qualified ``from pkg.a import f``)
    resolve as ``root / <module>``, so on a single-source-root corpus (all parsed source
    under one subdir) they reach the right file only when ``root`` is that subdir; the raw
    scan root would resolve them to nonexistent siblings and drop the file from the closure."""
    out: set[Path] = set()
    if path.suffix == ".py":
        parsed = _parse_python_tree(path)
        if parsed is None:
            return out
        source, root_node = parsed
        for node in _walk_python_tree(root_node):
            if node.type == "import_from_statement":
                module = _python_import_from_module(node, source)
                if module is None:
                    continue
                level, module_name = module
                target = _resolve_python_module_path(module_name, path, root, level)
                if target is not None:
                    out.add(target.resolve())
            elif node.type == "import_statement":
                for name, _local in _python_imported_names(node, source):
                    target = _resolve_python_module_path(name, path, root, 0)
                    if target is not None:
                        out.add(target.resolve())
    elif path.suffix in _JS_SUFFIXES:
        parsed = _parse_js_tree(path)
        if parsed is None:
            return out
        source, root_node = parsed
        for node in _walk_js_tree(root_node):
            if node.type not in ("import_statement", "export_statement"):
                continue
            spec = _js_module_specifier(node, source)
            if spec is None:
                continue
            target = _resolve_js_module_path(spec, path.parent)
            if target is not None:
                out.add(Path(target).resolve())
    return out


def _norm_label(s: str) -> str:
    """The single normalization applied to BOTH persisted node labels and raw-call callees so a
    callee name (`f()` / `.f` / `f`) compares equal to the node that defines it. Mirrors the
    extractor's own ``global_label_to_nids`` key, so the name-closure below reproduces its
    bind/skip decision instead of guessing a second dialect."""
    return s.strip().strip("()").lstrip(".").lower()


def _current_call_names(merge_files: list[Path]) -> set[str]:
    """The normalized bare-call callee names the merge-set files CURRENTLY make — read from the
    extractor's own ``raw_calls`` (member calls excluded, matching the extractor's name pass).
    Language-agnostic: every extractor that supports cross-file calls emits ``raw_calls``, so the
    callee-defining file can be seeded without any per-language import resolution. Reads the files
    fresh from disk, so a call REMOVED by the edit yields no name -> no seed -> the edge correctly
    drops (this rebinds against current content, it never preserves a stale edge)."""
    names: set[str] = set()
    for res in _extract_all(sorted(merge_files)):
        for rc in res.get("raw_calls", []):
            if rc.get("is_member_call"):
                continue
            callee = rc.get("callee")
            if callee:
                names.add(_norm_label(str(callee)))
    return names


def _current_reference_names(merge_files: list[Path]) -> set[str]:
    """The normalized names of the CROSS-FILE tables the merge-set files CURRENTLY
    reference — surfaced from the extractor's own reference edges (references/reads_from/
    triggers) whose target is a SOURCELESS cross-file stub. SQL emits no ``raw_calls``
    (that field is call-graph specific), so the referenced table cannot be seeded through
    ``_current_call_names``; the name is read off the reference edge's stub target instead.
    Reads the files fresh from disk, so a reference REMOVED by the edit yields no name -> no
    seed -> the edge correctly drops (rebinds against current content, never a stale edge)."""
    names: set[str] = set()
    for res in _extract_all(sorted(merge_files)):
        stub_label: dict[str, str] = {
            n["id"]: str(n.get("label", ""))
            for n in res.get("nodes", [])
            if n.get("id") and not n.get("source_file")
        }
        for e in res.get("edges", []):
            if e.get("relation") not in _REFERENCE_RELATIONS:
                continue
            label = stub_label.get(e.get("target"))
            if label:
                names.add(_norm_label(label))
    return names


def merge_and_extraction_sets(
    changed: list[Path],
    graph_json: str | Path,
    root: Path,
) -> tuple[set[Path], set[Path]]:
    """The (merge set, extraction set) for one incremental update — absolute resolved Paths.

    The merge set is the changed files ∪ every persisted cross-file DEPENDENT of them (the inbound
    bindings that must re-form). The extraction set adds callee files as binding context only, from
    two sources: the transitive out-import closure read from disk, and — language-agnostically —
    the file that DEFINES each name the merge set CURRENTLY calls, resolved through a label index
    over the persisted graph. The name half re-forms the cross-file ``calls`` edges bound by NAME
    rather than by a resolvable import (Go/C/… same-package calls, and Python whose qualified
    import does not resolve against the inferred root) — the import-closure alone never pulls their
    callee file. A dependent or callee that no longer exists on disk (deleted in the same change)
    is skipped — prune handles it.

    ``root`` is the EXTRACTION root the persisted graph relativized its ``source_file`` values
    against (matrix's inferred corpus common-ancestor), NOT necessarily the repo scan root. It
    both builds the ``source_file`` dialect an importer edge is matched against and resolves the
    disk out-imports; on a single-source-root corpus the two roots differ, so the raw scan root
    would fail to match importers and resolve rootless/qualified imports to nonexistent paths —
    shrinking the closure and dropping cross-file edges at merge."""
    root = Path(root).resolve()
    merge: set[Path] = {Path(c).resolve() for c in changed}

    changed_forms: set[str] = set()
    for c in merge:
        changed_forms |= _source_file_forms(c, root)

    try:
        data = json.loads(Path(graph_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    nodes = data.get("nodes", [])
    links = data.get("links" if "links" in data else "edges", [])

    def _abs(sf: str) -> Path:
        """A persisted source_file string -> its absolute resolved Path in the root's dialect."""
        p = Path(sf)
        return (p if p.is_absolute() else root / p).resolve()

    # One pass over the persisted nodes yields both the id->source_file map the importer seed
    # matches against AND a normalized-label -> defining-files index. The index lets a caller's
    # CURRENT call name resolve to the file that defines it with no import at all — the
    # language-agnostic half of the closure, since Go/C/... bind cross-file calls by NAME.
    node_sf: dict[Any, Any] = {}
    label_to_files: dict[str, set[Path]] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        node_sf[n.get("id")] = n.get("source_file")
        sf = n.get("source_file")
        label = n.get("label")
        if sf and label:
            label_to_files.setdefault(_norm_label(str(label)), set()).add(_abs(str(sf)))

    # IN (dependent -> merge): re-merge a persisted cross-file dependent of a changed file so its
    # inbound binding re-forms. The relation set is the full cross-file dependency taxonomy, not
    # just imports, so a persisted name-caller (a `calls` edge, not an import) is re-merged too —
    # byte-identical on resolvable corpora, where a name-caller is already an importer.
    for e in links:
        if (
            not isinstance(e, dict)
            or e.get("relation") not in DEFAULT_AFFECTED_RELATIONS
        ):
            continue
        if node_sf.get(e.get("target")) not in changed_forms:
            continue
        src_sf = node_sf.get(e.get("source"))
        if not src_sf:
            continue
        importer = _abs(str(src_sf))
        if importer.is_file():
            merge.add(importer)

    extraction: set[Path] = set(merge)
    frontier = list(merge)
    while frontier:
        for target in _file_imports(frontier.pop(), root):
            if target not in extraction and target.is_file():
                extraction.add(target)
                frontier.append(target)

    # OUT (merge -> callee) name-closure: seed the file that DEFINES each name the merge set
    # currently calls, so a name-bound cross-file `calls` edge re-forms when the caller is
    # re-extracted WITH its callee present. Only an unambiguous (single-definition) name is
    # seeded — the extractor binds a name-only call only to a unique definition, so this both
    # reproduces its decision and bounds the set (a common name in many files never balloons it).
    # Import-mediated context stays covered by the disk closure above; this adds only the
    # calls-only, import-free callee files that closure cannot reach.
    for name in _current_call_names(sorted(merge)):
        files = label_to_files.get(name)
        if files and len(files) == 1:
            (target,) = tuple(files)
            if target.is_file() and target not in extraction:
                extraction.add(target)

    # OUT (merge -> referenced table) name-closure for reference edges that populate no
    # raw_calls (SQL references/reads_from/triggers): seed the file that DEFINES each
    # cross-file table the merge set CURRENTLY references, so the reference edge re-binds
    # when the referencer is re-extracted WITH the table present. Same unambiguous
    # (single-definition) guard as the call closure — matrix rebinds a cross-file
    # reference stub only to a unique definition (_rewire_unique_stub_nodes), so this
    # both reproduces that decision and bounds the set. The disk out-import closure
    # cannot reach these: SQL has no import to resolve.
    for name in _current_reference_names(sorted(merge)):
        files = label_to_files.get(name)
        if files and len(files) == 1:
            (target,) = tuple(files)
            if target.is_file() and target not in extraction:
                extraction.add(target)
    return merge, extraction


def filter_chunk_to_merge_set(
    chunk: dict[str, Any], merge_set: set[Path], root: Path
) -> dict[str, Any]:
    """Strip the context files' contribution from an extraction chunk. ``build_merge`` REPLACES
    the base per ``source_file`` present in the chunk's nodes, and a context file's intact base
    contribution must never be displaced by its re-read. An edge lacking a ``source_file`` is
    kept only when an endpoint is a merge-set node (assemble backfills its attribution from the
    endpoints the same way). ``root`` is the EXTRACTION root the chunk relativized against."""
    keep: set[str] = set()
    for p in merge_set:
        keep |= _source_file_forms(p, root)
    nodes = [n for n in chunk.get("nodes", []) if n.get("source_file") in keep]
    merge_ids = {n.get("id") for n in nodes}

    def _kept(e: dict[str, Any]) -> bool:
        sf = e.get("source_file")
        if sf:
            return sf in keep
        return e.get("source") in merge_ids or e.get("target") in merge_ids

    edges = [e for e in chunk.get("edges", []) if _kept(e)]
    return {**chunk, "nodes": nodes, "edges": edges}
