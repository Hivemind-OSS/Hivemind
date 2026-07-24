"""matrix.extract — AST-only structural extraction package.

Public surface: ``extract(paths)`` and ``collect_files(target)``. The package
splits the extractor into a support layer, the generic config-driven walker, the
language configs, the per-language extractors, the cross-file resolution pass,
and this orchestrator + file-discovery layer.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from .c_cpp import extract_c, extract_cpp
from .configs import _WORKSPACE_PACKAGE_CACHE
from .go import extract_go
from .javascript import extract_js
from .python import extract_python
from .resolve import (
    _augment_symbol_resolution_edges,
    _disambiguate_colliding_node_ids,
    _resolve_cross_file_imports,
    _resolve_python_member_calls,
    _rewire_unique_stub_nodes,
)
from .rust import extract_rust
from .sql import extract_sql
from .support import (
    _LANGUAGE_BUILTIN_GLOBALS,
    _check_tree_sitter_version,
    _file_node_id,
    _make_id,
    _raise_recursion_limit,
    _safe_extract,
)

__all__ = [
    "extract",
    "collect_files",
    "root_offset",
    "_DISPATCH",
    "_get_extractor",
    "_extract_all",
]


# ── .NET project files (.sln, .slnx, .csproj, .razor) ───────────────────────

# Config/manifest JSON filenames the structural extractor understands. Anything
# else (eval fixtures, datasets, GeoJSON, API dumps) is *data* and must NOT be
# AST-walked into per-key nodes — that floods the graph with orphan key-nodes
# and near-duplicate communities (#1224). Data JSON is left to the LLM semantic
# pass instead. Matched case-insensitively against the bare filename.
# Top-level keys that prove a JSON object is a config/manifest the extractor can
# draw *cross-file* edges from (deps, extends chains, schema refs).
# ── DM (BYOND DreamMaker) extractor ──────────────────────────────────────────
# DM identity is path-based (`/datum/object/proc/New()`), not block-based, so
# the generic class-body walker doesn't fit well.

# ── DMI (BYOND icon files) ────────────────────────────────────────────────────
# .dmi is a PNG with a tEXt/zTXt "Description" chunk containing BYOND state
# metadata. We want the icon state names (icon_state = "X" in DM code
# references them).

# ── DMM (BYOND map files) ─────────────────────────────────────────────────────
# A .dmm starts with a tile dictionary — each "key" = (type, type{var=val}, ...)
# names one or more types that compose a tile — then a grid. We only need the
# dictionary section: every type path referenced is a `uses` edge.

# ── Dispatch ──────────────────────────────────────────────────────────────────

_DISPATCH: dict[str, Any] = {
    ".py": extract_python,
    ".js": extract_js,
    ".jsx": extract_js,
    ".mjs": extract_js,
    ".ts": extract_js,
    ".tsx": extract_js,
    ".go": extract_go,
    ".rs": extract_rust,
    ".c": extract_c,
    ".h": extract_c,
    ".cpp": extract_cpp,
    ".cc": extract_cpp,
    ".cxx": extract_cpp,
    ".hpp": extract_cpp,
    ".sql": extract_sql,
}


def _get_extractor(path: Path) -> Any | None:
    """Return the correct extractor function for a file, or None if unsupported."""
    return _DISPATCH.get(path.suffix)


def _extract_all(paths: list[Path]) -> list[dict[str, Any]]:
    """Extract every path serially, fresh (no cache, no process pool).

    Returns one result dict per input path, index-aligned. Unsupported
    suffixes and crashes degrade to an empty ``{"nodes": [], "edges": []}``.
    """
    per_file: list[dict[str, Any]] = []
    for path in paths:
        extractor = _get_extractor(path)
        if extractor is None:
            per_file.append({"nodes": [], "edges": []})
            continue
        per_file.append(_safe_extract(extractor, path))
    return per_file


def _infer_root(paths: list[Path]) -> Path:
    """Infer the common root file-node IDs relativize against (the first diverging path
    segment), resolved. Shared by :func:`extract` and the incremental update: a chunk-subset
    extraction must pin the root the FULL corpus would infer, or its ids fork from the persisted
    graph's and every merge mints ghost duplicates (BUG-025 side-finding)."""
    try:
        if not paths:
            root = Path(".")
        elif len(paths) == 1:
            root = paths[0].parent
        else:
            min_parts = min(len(p.parts) for p in paths)
            common_len = 0
            for i in range(min_parts):
                if len({p.parts[i] for p in paths}) == 1:
                    common_len += 1
                else:
                    break
            root = Path(*paths[0].parts[:common_len]) if common_len else Path(".")
    except Exception:
        root = Path(".")
    return root.resolve()


def root_offset(base: Path | str, paths: list[Path]) -> str:
    """The POSIX path offset between ``base`` and the extraction root
    :func:`_infer_root` pins for ``paths`` (the corpus common ancestor every node
    ``source_file`` is relativized against). ``""`` whenever the dialects already
    agree — any multi-rooted corpus — else the base-relative prefix the graph
    dialect drops (a ``src/``-only checkout yields ``"src"``).

    The ONE owner of the offset derivation: a consumer that joins graph paths
    back to ``base``-relative paths (hive-edge anchors — BUG-035; hive-census
    receipt subjects — BUG-038) derives its bridge HERE, from the same inference
    the extraction ran, so bridge and engine cannot drift apart. Fail-open: an
    empty corpus, paths outside ``base``, or any fault ⇒ ``""`` — exactly the
    pre-bridge repo-dialect assumption.
    """
    # marker: returning "" unconditionally (severing the dialect bridge) is a mutation.
    try:
        if not paths:
            return ""
        root = _infer_root([Path(p) for p in paths])
        rel = root.relative_to(Path(os.path.realpath(base)))
        return rel.as_posix() if rel.parts else ""
    except Exception:
        return ""


def extract(paths: list[Path], *, root: Path | None = None) -> dict[str, Any]:
    """Extract AST nodes and edges from a list of code files.

    Two-pass process:
    1. Per-file structural extraction (classes, functions, imports), serial and
       cache-free — every file is re-extracted fresh, so the output is a pure
       function of the inputs.
    2. Cross-file resolution: turns file-level imports into class-level edges
       (DigestAuth --uses--> Response) and resolves cross-file calls.

    Args:
        paths: files to extract from.
        root: the root file-node IDs relativize against. ``None`` infers it from
            ``paths`` (the scratch-build behavior); the incremental update passes
            the full-corpus root explicitly so a chunk extraction mints the same
            ids the persisted graph carries.
    """
    paths = [Path(p) for p in paths]
    _check_tree_sitter_version()
    _raise_recursion_limit()
    # Workspace package manifests/globs can change between repeated extractions.
    _WORKSPACE_PACKAGE_CACHE.clear()

    root = _infer_root(paths) if root is None else Path(root).resolve()

    per_file = _extract_all(paths)

    all_nodes: list[dict[str, Any]] = []
    all_edges: list[dict[str, Any]] = []
    all_raw_calls: list[dict[str, Any]] = []
    for result in per_file:
        all_nodes.extend(result.get("nodes", []))
        all_edges.extend(result.get("edges", []))
        all_raw_calls.extend(result.get("raw_calls", []))

    _augment_symbol_resolution_edges(paths, all_nodes, all_edges, root)

    # Remap file node IDs from absolute-path-derived to the canonical
    # {parent_dir}_{stem} spec form so (a) graph.json edge endpoints are stable
    # across machines (#502) and (b) AST file nodes match the IDs semantic
    # subagents generate (#1033). Resolve before relativizing so paths passed in
    # relative form still anchor to the (resolved) root.
    id_remap: dict[str, str] = {}
    # Symbol node IDs embed the file stem as a prefix (_file_node_id of the path
    # the extractor saw). For a root-level file that stem picks up the absolute
    # parent directory name, so a symbol becomes <rootdir>_main_run while the
    # file node is correctly relativized to main and the canonical id is
    # main_run -- splitting the symbol into two disconnected ghosts. Relativize
    # the symbol prefix the same way, gated by source_file so two files sharing a
    # prefix can't cross-contaminate. Keyed by resolved path -> (old_pref, new_pref).
    prefix_remap: dict[Path, tuple[str, str]] = {}
    for path in paths:
        old_id = _make_id(str(path))
        try:
            rel = path.relative_to(root)
        except ValueError:
            try:
                rel = path.resolve().relative_to(root)
            except ValueError:
                continue
        new_id = _file_node_id(rel)
        if old_id != new_id:
            id_remap[old_id] = new_id
        old_pref = _file_node_id(path)
        if old_pref != new_id:
            prefix_remap[path.resolve()] = (old_pref, new_id)
    if id_remap:
        for n in all_nodes:
            if n.get("id") in id_remap:
                n["id"] = id_remap[n["id"]]
        for e in all_edges:
            if e.get("source") in id_remap:
                e["source"] = id_remap[e["source"]]
            if e.get("target") in id_remap:
                e["target"] = id_remap[e["target"]]
    if prefix_remap:
        sym_remap: dict[str, str] = {}
        for n in all_nodes:
            sf = n.get("source_file")
            if not sf:
                continue
            # Package nodes carry a canonical name-keyed id (pkg_<name>) that must
            # stay identical across every manifest that references the package, so
            # they are exempt from the file-stem prefix remap (#1377), like the
            # type=module anchors (#1327).
            if n.get("type") == "package":
                continue
            try:
                entry = prefix_remap.get(Path(sf).resolve())
            except Exception:
                continue
            if entry is None:
                continue
            old_pref, new_pref = entry
            nid = n.get("id", "")
            if nid.startswith(old_pref + "_"):
                new_nid = new_pref + nid[len(old_pref) :]
                if new_nid != nid:
                    sym_remap[nid] = new_nid
        if sym_remap:
            for n in all_nodes:
                if n.get("id") in sym_remap:
                    n["id"] = sym_remap[n["id"]]
            for e in all_edges:
                if e.get("source") in sym_remap:
                    e["source"] = sym_remap[e["source"]]
                if e.get("target") in sym_remap:
                    e["target"] = sym_remap[e["target"]]
            # raw_calls carry caller_nid (a symbol id) consumed by the cross-file
            # call pass below, after this remap — rewrite it too or those edges
            # would dangle on their (stale) source.
            for rc in all_raw_calls:
                cn = rc.get("caller_nid")
                if cn in sym_remap:
                    rc["caller_nid"] = sym_remap[cn]

    _disambiguate_colliding_node_ids(all_nodes, all_edges, all_raw_calls, root)
    _rewire_unique_stub_nodes(all_nodes, all_edges)

    # Add cross-file class-level edges (Python only - uses Python parser internally)
    py_paths = [p for p in paths if p.suffix == ".py"]
    if py_paths:
        py_results = [r for r, p in zip(per_file, paths) if p.suffix == ".py"]
        try:
            cross_file_edges = _resolve_cross_file_imports(py_results, py_paths)
            all_edges.extend(cross_file_edges)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Cross-file import resolution failed, skipping: %s", exc
            )

    # Cross-file call resolution for all languages
    # Each extractor saved unresolved calls in raw_calls. Now that we have all
    # nodes from all files, resolve any callee that exists in another file.
    # Build name → ALL matching node IDs so we can skip ambiguous common names
    # (e.g. "log", "execute", "find") that appear in multiple files — resolving
    # those inflates god_nodes ranking with spurious cross-file edges.
    # Build label -> node_id index for cross-file call resolution.
    # Skip rationale nodes (their labels are docstring text, not callable
    # identifiers, and they were polluting matches for short names — #563).
    global_label_to_nids: dict[str, list[str]] = {}
    for n in all_nodes:
        if n.get("file_type") == "rationale":
            continue
        raw = n.get("label", "")
        normalised = raw.strip("()").lstrip(".")
        if normalised:
            key = normalised.lower()
            global_label_to_nids.setdefault(key, []).append(n["id"])

    # Build evidence index from import edges so cross-file calls backed by an
    # explicit import statement can be promoted from INFERRED to EXTRACTED.
    # Direct symbol imports (`import { foo }` / `const { foo } = require()`) are
    # the strongest evidence — caller's file_id has an `imports` edge directly to
    # the callee's symbol id. Module imports (`imports_from`) are weaker but still
    # confirm the caller pulled in the callee's source file.
    file_to_symbol_imports: dict[str | None, set[str]] = {}
    file_to_module_imports: dict[str | None, set[str]] = {}
    for e in all_edges:
        if e.get("relation") == "imports":
            file_to_symbol_imports.setdefault(e["source"], set()).add(e["target"])
        elif e.get("relation") == "imports_from":
            file_to_module_imports.setdefault(e["source"], set()).add(e["target"])

    # Map each node back to its containing file_id so we can ask
    # "did the caller's file import the callee's file?"
    # Use relativized paths to match how file node IDs were remapped above (#502).
    nid_to_file_nid: dict[str, str] = {}
    for n in all_nodes:
        sf = n.get("source_file")
        if not sf:
            continue
        sf_path = Path(sf)
        try:
            sf_rel = sf_path.relative_to(root) if sf_path.is_absolute() else sf_path
        except ValueError:
            sf_rel = sf_path
        nid_to_file_nid[n["id"]] = _file_node_id(sf_rel)

    existing_pairs = {(e["source"], e["target"]) for e in all_edges}
    for rc in all_raw_calls:
        callee = rc.get("callee", "")
        if not callee:
            continue
        if callee in _LANGUAGE_BUILTIN_GLOBALS:
            continue
        # Skip member-call callees: obj.log() → "log" has no import evidence
        # and collides with any top-level function named "log" in the corpus.
        if rc.get("is_member_call"):
            continue
        candidates = global_label_to_nids.get(callee.lower(), [])
        if not candidates:
            continue
        caller = rc["caller_nid"]
        caller_file_nid = nid_to_file_nid.get(caller)
        imported_symbols = file_to_symbol_imports.get(caller_file_nid, set())
        imported_modules = file_to_module_imports.get(caller_file_nid, set())

        def _has_import_evidence(candidate_id: str) -> bool:
            # Direct symbol import (`import { foo }`) is the strongest evidence:
            # the caller's file has an `imports` edge straight to this symbol.
            # A module import (`import './helper.js'`) confirms the caller pulled
            # in the file the candidate lives in.
            candidate_file_nid = nid_to_file_nid.get(candidate_id)
            return candidate_id in imported_symbols or (
                candidate_file_nid is not None
                and candidate_file_nid in imported_modules
            )

        if len(candidates) == 1:
            tgt = candidates[0]
            has_import_evidence = _has_import_evidence(tgt)
        else:
            # Ambiguous name (defined in 2+ files). Don't bail outright (#1219):
            # if the caller has explicit import evidence pointing at exactly one
            # of the candidates, that named import disambiguates unambiguously.
            # Prefer direct symbol-import matches; fall back to module-import
            # matches only when they too collapse to a single target. Without a
            # unique evidence-backed pick we skip, preserving the #543 guard
            # against over-connecting common short names (log, execute, find).
            symbol_matches = [c for c in candidates if c in imported_symbols]
            if len(symbol_matches) == 1:
                tgt = symbol_matches[0]
            else:
                module_matches = [
                    c
                    for c in candidates
                    if (cf := nid_to_file_nid.get(c)) is not None
                    and cf in imported_modules
                ]
                if len(module_matches) == 1:
                    tgt = module_matches[0]
                else:
                    continue
            has_import_evidence = True
        if tgt != caller and (caller, tgt) not in existing_pairs:
            existing_pairs.add((caller, tgt))
            # Promote to EXTRACTED when there's a direct import edge from the
            # caller's file pointing at either the callee symbol itself or the
            # file the callee lives in.
            if has_import_evidence:
                confidence = "EXTRACTED"
                confidence_score = 1.0
            else:
                confidence = "INFERRED"
                confidence_score = 0.8
            all_edges.append(
                {
                    "source": caller,
                    "target": tgt,
                    "relation": "calls",
                    "context": "call",
                    "confidence": confidence,
                    "confidence_score": confidence_score,
                    "source_file": rc.get("source_file", ""),
                    "source_location": rc.get("source_location"),
                    "weight": 1.0,
                }
            )

    # Cross-file Python qualified class-method resolution (#1446). Additive, runs
    # after id-disambiguation, with a single-definition guard.
    py_paths = [p for p in paths if p.suffix == ".py"]
    if py_paths:
        try:
            _resolve_python_member_calls(per_file, all_nodes, all_edges)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Python member-call resolution failed, skipping: %s", exc
            )

    # Relativize source_file fields so paths are portable across machines (#555)
    for item in all_nodes + all_edges:
        sf = item.get("source_file")
        if not sf:
            continue
        sf_path = Path(sf)
        if not sf_path.is_absolute():
            continue
        try:
            item["source_file"] = sf_path.relative_to(root).as_posix()
        except ValueError:
            pass

    # Tag AST provenance so the incremental watch rebuild can distinguish
    # AST-extracted nodes from semantic/LLM nodes. On a full re-extraction
    # the watcher drops any AST-marked node missing from the fresh output
    # even when its source file still exists (#1116).
    for n in all_nodes:
        n["_origin"] = "ast"

    return {
        "nodes": all_nodes,
        "edges": all_edges,
        "input_tokens": 0,
        "output_tokens": 0,
    }


# ── File discovery ────────────────────────────────────────────────────────────

# Directories that are never source input: dependency trees, build output,
# caches, VCS, and the engine's own output dir.
_SKIP_DIRS = {
    "venv",
    ".venv",
    "env",
    ".env",
    "node_modules",
    "__pycache__",
    ".git",
    "dist",
    "build",
    "target",
    "out",
    "site-packages",
    "lib64",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    "*.egg-info",
    "matrix-out",  # never treat own output as source input
    "coverage",
    "lcov-report",
    "visual-tests",
    "visual-test",
    "__snapshots__",
    "snapshots",
    "storybook-static",
    "dist-protected",
    ".next",
    ".nuxt",
    ".turbo",
    ".angular",
    ".idea",
    ".cache",
    ".parcel-cache",
    ".svelte-kit",
    ".terraform",
    ".serverless",
    ".matrix",  # the engine's own extraction cache — never index self-generated data
    ".worktrees",  # git worktree convention — sibling checkouts, always redundant
}


def _is_noise_dir(part: str, parent: "Path | None" = None) -> bool:
    """Return True if this directory name looks like a venv, cache, or dep dir."""
    if part in _SKIP_DIRS:
        return True
    if part.endswith("_venv") or part.endswith("_env"):
        return True
    if part.endswith(".egg-info"):
        return True
    if part == "worktrees" and parent is not None and parent.name.startswith("."):
        return True
    return False


_VCS_MARKERS = (".git", ".hg", ".svn", "_darcs", ".fossil")


def _parse_gitignore_line(raw: str) -> str:
    """Parse one raw line from an ignore file per gitignore spec."""
    line = raw.rstrip("\n\r")
    line = line.lstrip()
    if not line or line.startswith("#"):
        return ""
    line = re.sub(r"\s+#+[^\\].*$", "", line)
    line = line.replace("\\#", "#")
    line = re.sub(r"(?<!\\) +$", "", line)
    return line


def _find_vcs_root(start: Path) -> Path | None:
    """Walk upward from start; return the first directory containing a VCS marker."""
    current = start.resolve()
    home = Path.home()
    while True:
        if any((current / m).exists() for m in _VCS_MARKERS):
            return current
        parent = current.parent
        if parent == current or current == home:
            return None
        current = parent


def _load_matrixignore(root: Path) -> list[tuple[Path, str]]:
    """Read .gitignore and .matrixignore files; return (anchor_dir, pattern) pairs.

    Patterns are returned outer-first so inner (closer) rules win via
    last-match-wins. Walk ceiling: the nearest VCS root, else the scan root.
    """
    root = root.resolve()
    ceiling = _find_vcs_root(root) or root

    dirs: list[Path] = []
    current = root
    while True:
        dirs.append(current)
        if current == ceiling:
            break
        current = current.parent
    dirs.reverse()  # ceiling first, scan root last

    patterns: list[tuple[Path, str]] = []
    for d in dirs:
        for fname in (".gitignore", ".matrixignore"):
            ignore_file = d / fname
            if ignore_file.exists():
                for raw in ignore_file.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines():
                    line = _parse_gitignore_line(raw)
                    if line:
                        patterns.append((d, line))
    return patterns


def _is_ignored(
    path: Path,
    root: Path,
    patterns: list[tuple[Path, str]],
    *,
    _cache: dict[Path, bool] | None = None,
) -> bool:
    """Return True if the path should be ignored per ignore patterns.

    Gitignore last-match-wins; a ! pattern cannot re-include a file whose
    ancestor directory is already excluded. ``_cache`` memoises ancestor results.
    """
    if not patterns:
        return False

    def _eval(target: Path) -> bool:
        if _cache is not None and target in _cache:
            return _cache[target]

        def _matches(rel: str, p: str, anchored: bool) -> bool:
            if anchored:
                return fnmatch.fnmatch(rel, p)
            parts = rel.split("/")
            if fnmatch.fnmatch(rel, p):
                return True
            if fnmatch.fnmatch(target.name, p):
                return True
            for i, part in enumerate(parts):
                if fnmatch.fnmatch(part, p):
                    return True
                if fnmatch.fnmatch("/".join(parts[: i + 1]), p):
                    return True
            return False

        result = False
        for anchor, pattern in patterns:
            negated = pattern.startswith("!")
            raw = pattern[1:] if negated else pattern
            anchored = raw.startswith("/")
            p = raw.strip("/")
            if not p:
                continue

            matched = False
            if anchored:
                try:
                    rel_anchor = str(target.relative_to(anchor)).replace(os.sep, "/")
                    matched = _matches(rel_anchor, p, anchored=True)
                except ValueError:
                    pass
            else:
                try:
                    rel = str(target.relative_to(root)).replace(os.sep, "/")
                    matched = _matches(rel, p, anchored=False)
                except ValueError:
                    pass
                if not matched and anchor != root:
                    try:
                        rel_anchor = str(target.relative_to(anchor)).replace(
                            os.sep, "/"
                        )
                        matched = _matches(rel_anchor, p, anchored=False)
                    except ValueError:
                        pass

            if matched:
                result = not negated  # last match wins; ! flips to un-ignore
        if _cache is not None:
            _cache[target] = result
        return result

    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return _eval(path)

    ancestor = root
    for part in rel_parts[:-1]:
        ancestor = ancestor / part
        if _eval(ancestor):
            return True
    return _eval(path)


def collect_files(
    target: Path, *, follow_symlinks: bool = False, root: Path | None = None
) -> list[Path]:
    if target.is_file():
        return [target]
    _EXTENSIONS = set(_DISPATCH.keys())
    ignore_root = root if root is not None else target
    patterns = _load_matrixignore(ignore_root)
    # Shared across all _is_ignored calls in this scan so ancestor-directory
    # results are memoised instead of re-evaluated per file.
    ignore_cache: dict[Path, bool] = {}

    def _ignored(p: Path) -> bool:
        return bool(
            patterns and _is_ignored(p, ignore_root, patterns, _cache=ignore_cache)
        )

    if not follow_symlinks:
        # The old rglob filter rejected paths with a noise component anywhere,
        # including components of target itself — preserve that.
        if any(_is_noise_dir(part) for part in target.parts):
            return []
        # When negation (!) patterns exist, skip directory-level ignore pruning
        # so negated files inside ignored dirs can still be reached (same
        # conservatism as detect's scan walk).
        has_negation = any(pat.startswith("!") for _, pat in patterns)
        results: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(target):
            dp = Path(dirpath)
            dirnames[:] = [
                d
                for d in dirnames
                if not _is_noise_dir(d) and (has_negation or not _ignored(dp / d))
            ]
            for fname in filenames:
                p = dp / fname
                if p.suffix in _EXTENSIONS and not _ignored(p):
                    results.append(p)
        return sorted(results)
    # Walk with symlink following + cycle detection
    results = []
    for dirpath, dirnames, filenames in os.walk(target, followlinks=True):
        if os.path.islink(dirpath):
            real = os.path.realpath(dirpath)
            parent_real = os.path.realpath(os.path.dirname(dirpath))
            if parent_real == real or parent_real.startswith(real + os.sep):
                dirnames.clear()
                continue
        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not _is_noise_dir(d)]
        for fname in filenames:
            p = dp / fname
            if p.suffix in _EXTENSIONS and not _ignored(p):
                results.append(p)
    return sorted(results)
