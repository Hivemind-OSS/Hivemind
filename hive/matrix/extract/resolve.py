"""Cross-file second pass: id disambiguation, stub rewiring, and the symbol
resolution facts (JS/TS + Python) that turn module imports into symbol edges."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .configs import _JS_CACHE_BYPASS_SUFFIXES, _resolve_js_module_path
from .support import _file_stem, _make_id, _read_text

if TYPE_CHECKING:
    from tree_sitter import Node


# ── Zig ───────────────────────────────────────────────────────────────────────


# ── PowerShell ────────────────────────────────────────────────────────────────

# ── PowerShell manifest (.psd1) ──────────────────────────────────────────────

# Keys in a .psd1 whose values are module names/paths we treat as imports.
# ── Cross-file import resolution ──────────────────────────────────────────────


def _source_key(source_file: str, root: Path) -> str:
    if not source_file:
        return ""
    source_path = Path(source_file)
    try:
        return str(source_path.resolve().relative_to(root))
    except Exception:
        return str(source_path)


def _disambiguate_colliding_node_ids(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    raw_calls: list[dict[str, Any]],
    root: Path,
) -> None:
    """Rewrite only colliding node IDs, using source path as the disambiguator.

    Module anchor nodes (#1327) are exempt: ``import CoreKit`` from three files
    yields three ``type=module`` nodes with the same id but different
    source_files. Those are the *same* module, not distinct same-named symbols,
    so they must collapse to one shared node — disambiguating them by path would
    scatter a single module across N file-qualified duplicates.
    """
    by_id: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        if node.get("type") == "module":
            continue
        nid = node.get("id")
        if isinstance(nid, str) and nid:
            by_id.setdefault(nid, []).append(node)

    remap: dict[tuple[str, str], str] = {}
    ambiguous_ids: set[str] = set()
    for old_id, group in by_id.items():
        source_keys = {
            _source_key(str(node.get("source_file", "")), root) for node in group
        }
        if len(group) < 2 or len(source_keys) < 2:
            continue
        ambiguous_ids.add(old_id)
        for node in group:
            source_key = _source_key(str(node.get("source_file", "")), root)
            if not source_key:
                continue
            new_id = _make_id(source_key, old_id)
            remap[(old_id, source_key)] = new_id
            if new_id != old_id:
                node["id"] = new_id

    if not remap:
        return

    unambiguous_remaps: dict[str, str] = {}
    for old_id, group in by_id.items():
        if old_id in ambiguous_ids:
            continue
        candidates = {
            node["id"]
            for node in group
            if isinstance(node.get("id"), str) and node["id"] != old_id
        }
        if len(candidates) == 1:
            unambiguous_remaps[old_id] = next(iter(candidates))

    for edge in edges:
        edge_source_key = _source_key(str(edge.get("source_file", "")), root)
        edge_src_remap_key = (edge.get("source", ""), edge_source_key)
        edge_tgt_remap_key = (edge.get("target", ""), edge_source_key)
        if edge_src_remap_key in remap:
            edge["source"] = remap[edge_src_remap_key]
        elif edge.get("source") in unambiguous_remaps:
            edge["source"] = unambiguous_remaps[str(edge["source"])]
        if edge_tgt_remap_key in remap:
            edge["target"] = remap[edge_tgt_remap_key]
        elif edge.get("target") in unambiguous_remaps:
            edge["target"] = unambiguous_remaps[str(edge["target"])]

    for raw_call in raw_calls:
        call_source_key = _source_key(str(raw_call.get("source_file", "")), root)
        caller_key = (raw_call.get("caller_nid", ""), call_source_key)
        if caller_key in remap:
            raw_call["caller_nid"] = remap[caller_key]
        elif raw_call.get("caller_nid") in unambiguous_remaps:
            raw_call["caller_nid"] = unambiguous_remaps[str(raw_call["caller_nid"])]


def _node_label_key(node: dict[str, Any]) -> str:
    label = str(node.get("label", "")).strip()
    return re.sub(r"[^a-zA-Z0-9]+", "", label).lower()


def _is_type_like_definition(node: dict[str, Any]) -> bool:
    label = str(node.get("label", "")).strip()
    if not label:
        return False
    if label.endswith(")") or label.startswith("."):
        return False
    if "." in label:
        return False
    return node.get("file_type") == "code"


def _rewire_unique_stub_nodes(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> None:
    """Map unresolved no-source stubs to a unique real definition with the same label."""
    real_by_label: dict[str, list[dict[str, Any]]] = {}
    stubs: list[dict[str, Any]] = []

    for node in nodes:
        key = _node_label_key(node)
        if not key:
            continue
        if node.get("source_file"):
            if _is_type_like_definition(node):
                real_by_label.setdefault(key, []).append(node)
            continue
        stubs.append(node)

    remap: dict[str, str] = {}
    drop_ids: set[str] = set()
    for stub in stubs:
        stub_id = str(stub.get("id", ""))
        if not stub_id:
            continue
        candidates = real_by_label.get(_node_label_key(stub), [])
        if len(candidates) != 1:
            continue
        target_id = candidates[0].get("id")
        if isinstance(target_id, str) and target_id and target_id != stub_id:
            remap[stub_id] = target_id
            drop_ids.add(stub_id)

    if not remap:
        return

    for edge in edges:
        if edge.get("source") in remap:
            edge["source"] = remap[str(edge["source"])]
        if edge.get("target") in remap:
            edge["target"] = remap[str(edge["target"])]

    nodes[:] = [node for node in nodes if node.get("id") not in drop_ids]


def _js_source_path(source_file: str, root: Path) -> Path | None:
    if not source_file:
        return None
    path = Path(source_file)
    if not path.is_absolute():
        path = root / path
    try:
        return path.resolve()
    except Exception:
        return path


@dataclass(frozen=True)
class _SymbolDeclarationFact:
    file_path: Path
    name: str
    line: int


@dataclass(frozen=True)
class _SymbolImportFact:
    file_path: Path
    local_name: str
    target_path: Path
    imported_name: str
    line: int


@dataclass(frozen=True)
class _SymbolAliasFact:
    file_path: Path
    alias: str
    target_name: str
    line: int


@dataclass(frozen=True)
class _SymbolExportFact:
    file_path: Path
    exported_name: str
    line: int
    local_name: str | None = None
    target_path: Path | None = None
    target_name: str | None = None


@dataclass(frozen=True)
class _StarExportFact:
    file_path: Path
    target_path: Path
    line: int


@dataclass(frozen=True)
class _SymbolUseFact:
    file_path: Path
    source_id: str
    local_name: str
    relation: str
    context: str
    line: int


@dataclass
class _SymbolResolutionFacts:
    declarations: list[_SymbolDeclarationFact] = field(default_factory=list)
    imports: list[_SymbolImportFact] = field(default_factory=list)
    aliases: list[_SymbolAliasFact] = field(default_factory=list)
    exports: list[_SymbolExportFact] = field(default_factory=list)
    star_exports: list[_StarExportFact] = field(default_factory=list)
    uses: list[_SymbolUseFact] = field(default_factory=list)
    # File-to-file submodule imports from `from pkg import submod` (#1146).
    # Each entry is (importing_file, submodule_file, line).
    module_imports: list[tuple[Path, Path, int]] = field(default_factory=list)


def _apply_symbol_resolution_facts(
    paths: list[Path],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    root: Path,
    facts: _SymbolResolutionFacts,
) -> None:
    """Apply language-provided import/export/use facts to graph edges."""
    if not (
        facts.declarations
        or facts.imports
        or facts.aliases
        or facts.exports
        or facts.star_exports
        or facts.uses
        or facts.module_imports
    ):
        return

    path_by_resolved = {path.resolve(): path for path in paths}
    source_file_id = {path.resolve(): _make_id(str(path)) for path in paths}
    symbol_nodes: dict[tuple[Path, str], str] = {}
    for node in nodes:
        source_path = _js_source_path(str(node.get("source_file", "")), root)
        if source_path is None:
            continue
        label = str(node.get("label", "")).strip().strip("()").lstrip(".")
        if label and node.get("id"):
            symbol_nodes[(source_path, label)] = str(node["id"])

    def ensure_symbol_node(path: Path, name: str, line: int) -> str:
        resolved_path = path.resolve()
        existing = symbol_nodes.get((resolved_path, name))
        if existing is not None:
            return existing
        node_id = _make_id(_file_stem(path), name)
        symbol_nodes[(resolved_path, name)] = node_id
        nodes.append(
            {
                "id": node_id,
                "label": name,
                "file_type": "code",
                "source_file": str(path),
                "source_location": f"L{line}",
            }
        )
        return node_id

    existing_edges = {
        (
            str(edge.get("source")),
            str(edge.get("target")),
            str(edge.get("relation")),
            str(edge.get("context") or ""),
        )
        for edge in edges
    }

    def add_edge(
        source: str,
        target: str,
        relation: str,
        context: str,
        line: int,
        source_path: Path,
    ) -> None:
        key = (source, target, relation, context or "")
        if key in existing_edges:
            return
        existing_edges.add(key)
        edges.append(
            {
                "source": source,
                "target": target,
                "relation": relation,
                "context": context,
                "confidence": "EXTRACTED",
                "source_file": str(source_path),
                "source_location": f"L{line}",
                "weight": 1.0,
            }
        )

    for declaration in facts.declarations:
        ensure_symbol_node(declaration.file_path, declaration.name, declaration.line)

    local_aliases_by_file: dict[Path, dict[str, tuple[Path, str]]] = {}
    for import_fact in facts.imports:
        file_path = import_fact.file_path.resolve()
        local_aliases_by_file.setdefault(file_path, {})[import_fact.local_name] = (
            import_fact.target_path.resolve(),
            import_fact.imported_name,
        )

    pending_aliases_by_file: dict[Path, list[_SymbolAliasFact]] = {}
    for alias_fact in facts.aliases:
        pending_aliases_by_file.setdefault(alias_fact.file_path.resolve(), []).append(
            alias_fact
        )

    for file_path, aliases in pending_aliases_by_file.items():
        local_aliases = local_aliases_by_file.setdefault(file_path, {})
        changed = True
        while changed:
            changed = False
            for alias_fact in aliases:
                if alias_fact.alias in local_aliases:
                    continue
                alias_origin = local_aliases.get(alias_fact.target_name)
                if alias_origin is not None:
                    local_aliases[alias_fact.alias] = alias_origin
                    changed = True

    named_exports_by_file: dict[Path, dict[str, tuple[Path, str]]] = {}
    star_exports_by_file: dict[Path, list[Path]] = {}

    for star_fact in facts.star_exports:
        source_path = star_fact.file_path.resolve()
        target_path = star_fact.target_path.resolve()
        star_exports_by_file.setdefault(source_path, []).append(target_path)
        source_id = source_file_id.get(source_path)
        if source_id is not None:
            add_edge(
                source_id,
                _make_id(str(path_by_resolved.get(target_path, target_path))),
                "re_exports",
                "export",
                star_fact.line,
                star_fact.file_path,
            )

    for export_fact in facts.exports:
        file_path = export_fact.file_path.resolve()
        origin: tuple[Path, str] | None = None
        if export_fact.target_path is not None and export_fact.target_name is not None:
            origin = (export_fact.target_path.resolve(), export_fact.target_name)
        elif export_fact.local_name is not None:
            origin = local_aliases_by_file.get(file_path, {}).get(
                export_fact.local_name
            )
            if origin is None and (file_path, export_fact.local_name) in symbol_nodes:
                origin = (file_path, export_fact.local_name)
        if origin is None:
            continue
        named_exports_by_file.setdefault(file_path, {})[export_fact.exported_name] = (
            origin
        )
        if origin[0] != file_path:
            source_id = source_file_id.get(file_path)
            if source_id is not None:
                add_edge(
                    source_id,
                    _make_id(str(path_by_resolved.get(origin[0], origin[0]))),
                    "re_exports",
                    "export",
                    export_fact.line,
                    export_fact.file_path,
                )

    def resolve_exported_origin(
        target_path: Path, imported_name: str, seen: set[tuple[Path, str]] | None = None
    ) -> tuple[Path, str]:
        target_path = target_path.resolve()
        key = (target_path, imported_name)
        if seen is None:
            seen = set()
        if key in seen:
            return key
        seen.add(key)
        origin = named_exports_by_file.get(target_path, {}).get(imported_name)
        if origin is not None:
            return resolve_exported_origin(origin[0], origin[1], seen)
        for star_target in star_exports_by_file.get(target_path, []):
            star_key = (star_target, imported_name)
            if star_key in symbol_nodes:
                return star_key
            resolved = resolve_exported_origin(star_target, imported_name, seen)
            if resolved in symbol_nodes:
                return resolved
        return key

    for import_fact in facts.imports:
        source_id = source_file_id.get(import_fact.file_path.resolve())
        if source_id is None:
            continue
        origin_path, origin_symbol = resolve_exported_origin(
            import_fact.target_path,
            import_fact.imported_name,
        )
        target_id = symbol_nodes.get((origin_path, origin_symbol))
        if target_id is None:
            continue
        add_edge(
            source_id,
            target_id,
            "imports",
            "import",
            import_fact.line,
            import_fact.file_path,
        )

    # #1146: emit file-to-file imports_from edges for package-form submodule imports.
    for from_path, to_path, line in facts.module_imports:
        try:
            from_rel = from_path.relative_to(root)
            to_rel = to_path.relative_to(root)
        except ValueError:
            continue
        source_id = _make_id(_file_stem(from_rel))
        target_id = _make_id(_file_stem(to_rel))
        add_edge(
            source_id, target_id, "imports_from", "submodule_import", line, from_path
        )

    for use_fact in facts.uses:
        file_path = use_fact.file_path.resolve()
        target_id = None
        unresolved_origin = local_aliases_by_file.get(file_path, {}).get(
            use_fact.local_name
        )
        if unresolved_origin is not None:
            origin_path, origin_symbol = resolve_exported_origin(*unresolved_origin)
            target_id = symbol_nodes.get((origin_path, origin_symbol))
        if target_id is None and use_fact.relation in ("inherits", "implements"):
            # Same-file fallback for HERITAGE only: a base declared in the same
            # file (`class X extends Y`, `interface A extends B`) has no import
            # alias, so resolve it directly against the file's own symbol nodes.
            # Scoped to heritage because same-file calls/uses already resolve via
            # the dedicated call-graph pass; widening this would duplicate those
            # edges. Import resolution still takes precedence (#1095).
            target_id = symbol_nodes.get((file_path, use_fact.local_name))
        if target_id is None:
            continue
        add_edge(
            use_fact.source_id,
            target_id,
            use_fact.relation,
            use_fact.context,
            use_fact.line,
            use_fact.file_path,
        )


def _parse_js_tree(path: Path) -> tuple[bytes, Node] | None:
    try:
        from tree_sitter import Language, Parser

        if path.suffix in (".ts", ".tsx"):
            import tree_sitter_typescript as tstypescript

            language = Language(tstypescript.language_typescript())
        else:
            import tree_sitter_javascript as tsjavascript

            language = Language(tsjavascript.language())
        source = path.read_bytes()
        parser = Parser(language)
        return source, parser.parse(source).root_node
    except Exception:
        return None


def _walk_js_tree(node: Node) -> Iterator[Node]:
    # Iterative DFS avoids Python's O(depth) generator-chain overhead.
    # Recursive yield-from creates one generator frame per level — at 26+
    # levels deep each leaf's value had to propagate through 26 frames.
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _js_module_specifier(node: Node, source: bytes) -> str | None:
    source_node = node.child_by_field_name("source")
    if source_node is None:
        for child in node.children:
            if child.type == "string":
                source_node = child
                break
    if source_node is None:
        return None
    raw = _read_text(source_node, source).strip()
    return raw.strip("'\"`") or None


def _js_named_specifiers(
    node: Node, source: bytes, specifier_type: str
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for child in _walk_js_tree(node):
        if child.type != specifier_type:
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            continue
        alias_node = child.child_by_field_name("alias")
        name = _read_text(name_node, source)
        exposed = _read_text(alias_node, source) if alias_node is not None else name
        if name and exposed:
            pairs.append((name, exposed))
    return pairs


def _js_export_clause(node: Node) -> Node | None:
    for child in node.children:
        if child.type == "export_clause":
            return child
    return None


def _js_export_statement_is_star(node: Node) -> bool:
    return any(child.type == "*" for child in node.children)


def _js_lexical_aliases(node: Node, source: bytes) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    if node.type != "lexical_declaration":
        return aliases
    for child in node.children:
        if child.type != "variable_declarator":
            continue
        name_node = child.child_by_field_name("name")
        value_node = child.child_by_field_name("value")
        if (
            name_node is not None
            and value_node is not None
            and value_node.type in ("identifier", "type_identifier")
        ):
            aliases.append(
                (_read_text(name_node, source), _read_text(value_node, source))
            )
    return aliases


def _js_exported_declaration_names(node: Node, source: bytes) -> list[str]:
    names: list[str] = []
    declaration = node.child_by_field_name("declaration")
    if declaration is None:
        return names

    if declaration.type == "lexical_declaration":
        names.extend(
            alias for alias, _target in _js_lexical_aliases(declaration, source)
        )
        return names

    if declaration.type in (
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "function_declaration",
    ):
        name_node = declaration.child_by_field_name("name")
        if name_node is not None:
            names.append(_read_text(name_node, source))
    return names


def _js_default_import_name(node: Node, source: bytes) -> str | None:
    """Local binding of a default import: the `Foo` in `import Foo from './x'`.

    The default binding is a bare identifier child of the import_clause (named
    imports live in a `named_imports` node, namespace imports in a
    `namespace_import` node), so it is also picked up from the mixed form
    `import Foo, { Bar } from './x'`.
    """
    for child in node.children:
        if child.type == "import_clause":
            for sub in child.children:
                if sub.type == "identifier":
                    return _read_text(sub, source)
    return None


def _js_default_export_name(node: Node, source: bytes) -> str | None:
    """Local name of a default export, or None for anonymous defaults.

    Handles `export default class Foo {}`, `export default function foo() {}`,
    `export default abstract class Foo {}` (name on the `declaration` field) and
    `export default Foo` (an identifier on the `value` field). Anonymous defaults
    (`export default class {}`, `export default {...}`) have no resolvable symbol
    and return None.
    """
    if not any(child.type == "default" for child in node.children):
        return None
    declaration = node.child_by_field_name("declaration")
    if declaration is not None:
        name_node = declaration.child_by_field_name("name")
        return _read_text(name_node, source) if name_node is not None else None
    value = node.child_by_field_name("value")
    if value is not None and value.type == "identifier":
        return _read_text(value, source)
    return None


def _js_top_level_function_bodies(
    path: Path, root_node: Node, source: bytes
) -> list[tuple[str, Node]]:
    bodies: list[tuple[str, Node]] = []
    stem = _file_stem(path)
    for node in root_node.children:
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is not None and body is not None:
                bodies.append((_make_id(stem, _read_text(name_node, source)), body))
            continue
        if node.type != "lexical_declaration":
            continue
        for child in node.children:
            if child.type != "variable_declarator":
                continue
            name_node = child.child_by_field_name("name")
            value_node = child.child_by_field_name("value")
            if (
                name_node is not None
                and value_node is not None
                and value_node.type == "arrow_function"
            ):
                bodies.append(
                    (_make_id(stem, _read_text(name_node, source)), value_node)
                )
    return bodies


def _js_call_identifier(node: Node, source: bytes) -> str | None:
    if node.type != "call_expression":
        return None
    function_node = node.child_by_field_name("function")
    if function_node is None:
        for child in node.children:
            if child.is_named:
                function_node = child
                break
    if function_node is not None and function_node.type in (
        "identifier",
        "type_identifier",
    ):
        return _read_text(function_node, source)
    return None


_JS_PRIMITIVE_TYPES = frozenset(
    {
        "string",
        "number",
        "boolean",
        "any",
        "unknown",
        "void",
        "never",
        "object",
        "null",
        "undefined",
        "bigint",
        "symbol",
        "this",
    }
)


def _ts_heritage_clause_entries(clause_node: Node, source: bytes) -> list[str]:
    """Return base/interface type names from an extends_clause or implements_clause."""
    out: list[str] = []
    for child in clause_node.children:
        if not child.is_named:
            continue
        if child.type in ("identifier", "type_identifier"):
            name = _read_text(child, source)
            if name:
                out.append(name)
        elif child.type == "generic_type":
            name_node = child.child_by_field_name("name")
            if name_node is None:
                for sub in child.children:
                    if sub.type in (
                        "type_identifier",
                        "nested_type_identifier",
                        "identifier",
                    ):
                        name_node = sub
                        break
            if name_node is not None:
                text = _read_text(name_node, source).rsplit(".", 1)[-1]
                if text:
                    out.append(text)
        elif child.type == "nested_type_identifier":
            text = _read_text(child, source).rsplit(".", 1)[-1]
            if text:
                out.append(text)
    return out


def _ts_collect_type_refs(
    node: Node | None, source: bytes, generic: bool, out: list[tuple[str, str]]
) -> None:
    """Walk a TS type annotation tree; append (name, role) tuples.

    role is 'type' for the outermost type position and 'generic_arg' for entries
    that appear inside `type_arguments`.
    """
    if node is None:
        return
    t = node.type
    if t == "type_annotation":
        for c in node.children:
            if c.is_named:
                _ts_collect_type_refs(c, source, generic, out)
        return
    if t in ("type_identifier", "identifier"):
        name = _read_text(node, source)
        if name and name not in _JS_PRIMITIVE_TYPES:
            out.append((name, "generic_arg" if generic else "type"))
        return
    if t == "nested_type_identifier":
        tail = _read_text(node, source).rsplit(".", 1)[-1]
        if tail and tail not in _JS_PRIMITIVE_TYPES:
            out.append((tail, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            text = _read_text(name_node, source).rsplit(".", 1)[-1]
            if text and text not in _JS_PRIMITIVE_TYPES:
                out.append((text, "generic_arg" if generic else "type"))
        else:
            for c in node.children:
                if c.type in ("type_identifier", "nested_type_identifier"):
                    text = _read_text(c, source).rsplit(".", 1)[-1]
                    if text and text not in _JS_PRIMITIVE_TYPES:
                        out.append((text, "generic_arg" if generic else "type"))
                    break
        for c in node.children:
            if c.type == "type_arguments":
                for sub in c.children:
                    if sub.is_named:
                        _ts_collect_type_refs(sub, source, True, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _ts_collect_type_refs(c, source, generic, out)


def _ts_walk_class_members(
    class_node: Node,
    source: bytes,
    path: Path,
    class_nid: str,
    facts: _SymbolResolutionFacts,
) -> None:
    """Emit type-relation and type-reference use facts for a class declaration node."""
    for child in class_node.children:
        if child.type == "class_heritage":
            for clause in child.children:
                if clause.type == "extends_clause":
                    for name in _ts_heritage_clause_entries(clause, source):
                        facts.uses.append(
                            _SymbolUseFact(
                                path,
                                class_nid,
                                name,
                                "inherits",
                                "type",
                                clause.start_point[0] + 1,
                            )
                        )
                elif clause.type == "implements_clause":
                    for name in _ts_heritage_clause_entries(clause, source):
                        facts.uses.append(
                            _SymbolUseFact(
                                path,
                                class_nid,
                                name,
                                "implements",
                                "type",
                                clause.start_point[0] + 1,
                            )
                        )
        elif child.type == "extends_type_clause":
            # Interface heritage (`interface A extends B, C`) is an
            # extends_type_clause node, NOT a class_heritage. Its base entries
            # are the same node types extends_clause holds, so the helper is
            # reusable. Without this branch interface inheritance is dropped (#1095).
            for name in _ts_heritage_clause_entries(child, source):
                facts.uses.append(
                    _SymbolUseFact(
                        path,
                        class_nid,
                        name,
                        "inherits",
                        "type",
                        child.start_point[0] + 1,
                    )
                )

    body = class_node.child_by_field_name("body")
    if body is None:
        return

    for member in body.children:
        m_line = member.start_point[0] + 1
        if member.type in (
            "method_definition",
            "method_signature",
            "abstract_method_signature",
        ):
            name_node = member.child_by_field_name("name")
            if name_node is None:
                continue
            method_name = _read_text(name_node, source)
            method_nid = _make_id(class_nid, method_name)
            params = member.child_by_field_name("parameters")
            if params is not None:
                for p in params.children:
                    if p.type not in ("required_parameter", "optional_parameter"):
                        continue
                    type_anno = p.child_by_field_name("type")
                    if type_anno is None:
                        continue
                    refs: list[tuple[str, str]] = []
                    _ts_collect_type_refs(type_anno, source, False, refs)
                    for name, role in refs:
                        ctx = (
                            "generic_arg" if role == "generic_arg" else "parameter_type"
                        )
                        facts.uses.append(
                            _SymbolUseFact(
                                path, method_nid, name, "references", ctx, m_line
                            )
                        )
            return_type = member.child_by_field_name("return_type")
            if return_type is not None:
                refs = []
                _ts_collect_type_refs(return_type, source, False, refs)
                for name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "return_type"
                    facts.uses.append(
                        _SymbolUseFact(
                            path, method_nid, name, "references", ctx, m_line
                        )
                    )
        elif member.type in ("public_field_definition", "property_signature"):
            type_anno = None
            for c in member.children:
                if c.type == "type_annotation":
                    type_anno = c
                    break
            if type_anno is None:
                continue
            refs = []
            _ts_collect_type_refs(type_anno, source, False, refs)
            for name, role in refs:
                ctx = "generic_arg" if role == "generic_arg" else "field"
                facts.uses.append(
                    _SymbolUseFact(path, class_nid, name, "references", ctx, m_line)
                )


def _collect_js_symbol_resolution_facts(
    paths: list[Path], facts: _SymbolResolutionFacts
) -> None:
    js_paths = [
        path
        for path in paths
        if path.suffix in _JS_CACHE_BYPASS_SUFFIXES and path.suffix != ".vue"
    ]
    if not js_paths:
        return

    trees: dict[Path, tuple[bytes, Node]] = {}

    for path in js_paths:
        resolved_path = path.resolve()
        parsed = _parse_js_tree(path)
        if parsed is None:
            continue
        source, root_node = parsed
        trees[resolved_path] = parsed

        for node in _walk_js_tree(root_node):
            if node.type == "export_statement":
                for name in _js_exported_declaration_names(node, source):
                    facts.declarations.append(
                        _SymbolDeclarationFact(path, name, node.start_point[0] + 1)
                    )

            if node.type != "import_statement":
                continue
            raw_module = _js_module_specifier(node, source)
            if raw_module is None:
                continue
            target_path = _resolve_js_module_path(raw_module, path.parent)
            if target_path is None:
                continue
            target_path = target_path.resolve()
            for imported_name, local_name in _js_named_specifiers(
                node, source, "import_specifier"
            ):
                facts.imports.append(
                    _SymbolImportFact(
                        path,
                        local_name,
                        target_path,
                        imported_name,
                        node.start_point[0] + 1,
                    )
                )
            default_local = _js_default_import_name(node, source)
            if default_local is not None:
                facts.imports.append(
                    _SymbolImportFact(
                        path,
                        default_local,
                        target_path,
                        "default",
                        node.start_point[0] + 1,
                    )
                )

        for node in _walk_js_tree(root_node):
            for alias, target in _js_lexical_aliases(node, source):
                facts.aliases.append(
                    _SymbolAliasFact(path, alias, target, node.start_point[0] + 1)
                )

    for path in js_paths:
        resolved_path = path.resolve()
        parsed = trees.get(resolved_path)
        if parsed is None:
            continue
        source, root_node = parsed

        for node in _walk_js_tree(root_node):
            if node.type != "export_statement":
                continue

            raw_module = _js_module_specifier(node, source)
            export_clause = _js_export_clause(node)
            if raw_module is not None:
                target_path = _resolve_js_module_path(raw_module, path.parent)
                if target_path is None:
                    continue
                target_path = target_path.resolve()
                if _js_export_statement_is_star(node):
                    facts.star_exports.append(
                        _StarExportFact(path, target_path, node.start_point[0] + 1)
                    )
                if export_clause is not None:
                    for original_name, exported_name in _js_named_specifiers(
                        export_clause, source, "export_specifier"
                    ):
                        facts.exports.append(
                            _SymbolExportFact(
                                path,
                                exported_name,
                                node.start_point[0] + 1,
                                target_path=target_path,
                                target_name=original_name,
                            )
                        )
                continue

            if export_clause is not None:
                for local_name, exported_name in _js_named_specifiers(
                    export_clause, source, "export_specifier"
                ):
                    facts.exports.append(
                        _SymbolExportFact(
                            path,
                            exported_name,
                            node.start_point[0] + 1,
                            local_name=local_name,
                        )
                    )
                continue

            for exported_name in _js_exported_declaration_names(node, source):
                facts.exports.append(
                    _SymbolExportFact(
                        path,
                        exported_name,
                        node.start_point[0] + 1,
                        local_name=exported_name,
                    )
                )

            # `export default class Foo {}` / `export default foo` exposes the
            # symbol under the name "default"; record that so a default import
            # (imported_name="default") resolves to it. `export { X as default }`
            # is already handled via the export_clause path above.
            default_name = _js_default_export_name(node, source)
            if default_name is not None:
                facts.exports.append(
                    _SymbolExportFact(
                        path,
                        "default",
                        node.start_point[0] + 1,
                        local_name=default_name,
                    )
                )

    for path in js_paths:
        resolved_path = path.resolve()
        parsed = trees.get(resolved_path)
        if parsed is None:
            continue
        source, root_node = parsed
        for source_id, body in _js_top_level_function_bodies(path, root_node, source):
            for node in _walk_js_tree(body):
                call_name = _js_call_identifier(node, source)
                if call_name is None:
                    continue
                facts.uses.append(
                    _SymbolUseFact(
                        path,
                        source_id,
                        call_name,
                        "calls",
                        "call",
                        node.start_point[0] + 1,
                    )
                )

    for path in js_paths:
        resolved_path = path.resolve()
        parsed = trees.get(resolved_path)
        if parsed is None:
            continue
        source, root_node = parsed
        stem = _file_stem(path)
        for node in _walk_js_tree(root_node):
            if node.type not in (
                "class_declaration",
                "abstract_class_declaration",
                "interface_declaration",
            ):
                continue
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            class_name = _read_text(name_node, source)
            if not class_name:
                continue
            class_nid = _make_id(stem, class_name)
            _ts_walk_class_members(node, source, path, class_nid, facts)


def _parse_python_tree(path: Path) -> tuple[bytes, Node] | None:
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser

        source = path.read_bytes()
        parser = Parser(Language(tspython.language()))
        return source, parser.parse(source).root_node
    except Exception:
        return None


def _walk_python_tree(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from _walk_python_tree(child)


def _python_import_from_module(node: Node, source: bytes) -> tuple[int, str] | None:
    level = 0
    module_name = ""
    for child in node.children:
        if child.type == "import":
            break
        if child.type == "relative_import":
            raw = _read_text(child, source)
            level = len(raw) - len(raw.lstrip("."))
            remainder = raw.lstrip(".")
            if remainder:
                module_name = remainder
            for sub in child.children:
                if sub.type == "dotted_name":
                    module_name = _read_text(sub, source)
        elif child.type == "dotted_name":
            module_name = _read_text(child, source)
    if level == 0 and not module_name:
        return None
    return level, module_name


def _python_imported_names(node: Node, source: bytes) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    past_import = False
    for child in node.children:
        if child.type == "import":
            past_import = True
            continue
        if not past_import:
            continue
        if child.type == "dotted_name":
            name = _read_text(child, source)
            names.append((name, name.split(".")[-1]))
        elif child.type == "aliased_import":
            name_node = child.child_by_field_name("name")
            alias_node = child.child_by_field_name("alias")
            if name_node is None:
                continue
            name = _read_text(name_node, source)
            local = (
                _read_text(alias_node, source)
                if alias_node is not None
                else name.split(".")[-1]
            )
            names.append((name, local))
    return names


def _resolve_python_module_path(
    module_name: str, current_path: Path, root: Path, level: int
) -> Path | None:
    if level > 0:
        base = current_path.parent
        for _ in range(level - 1):
            base = base.parent
        candidate = base / module_name.replace(".", "/") if module_name else base
    else:
        candidate = root / module_name.replace(".", "/")

    if candidate.is_dir():
        init_path = candidate / "__init__.py"
        if init_path.is_file():
            return init_path
    if candidate.is_file():
        return candidate
    py_candidate = candidate.with_suffix(".py")
    if py_candidate.is_file():
        return py_candidate
    return None


def _python_top_level_function_bodies(
    path: Path, root_node: Node, source: bytes
) -> list[tuple[str, Node]]:
    bodies: list[tuple[str, Node]] = []
    stem = _file_stem(path)
    for node in root_node.children:
        if node.type != "function_definition":
            continue
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is not None and body is not None:
            bodies.append((_make_id(stem, _read_text(name_node, source)), body))
    return bodies


def _python_call_identifier(node: Node, source: bytes) -> str | None:
    if node.type != "call":
        return None
    function_node = node.child_by_field_name("function")
    if function_node is not None and function_node.type == "identifier":
        return _read_text(function_node, source)
    return None


def _collect_python_symbol_resolution_facts(
    paths: list[Path],
    root: Path,
    facts: _SymbolResolutionFacts,
) -> None:
    py_paths = [path for path in paths if path.suffix == ".py"]
    if not py_paths:
        return

    trees: dict[Path, tuple[bytes, Node]] = {}
    for path in py_paths:
        parsed = _parse_python_tree(path)
        if parsed is None:
            continue
        source, root_node = parsed
        trees[path.resolve()] = parsed

        for node in _walk_python_tree(root_node):
            if node.type != "import_from_statement":
                continue
            module = _python_import_from_module(node, source)
            if module is None:
                continue
            level, module_name = module
            target_path = _resolve_python_module_path(module_name, path, root, level)
            if target_path is None:
                continue
            # #1146: `from pkg import submod` — if the target is a package
            # (__init__.py) and an imported name matches a submodule file on
            # disk, emit a file-level import edge to that submodule rather
            # than only to the package.
            pkg_dir = target_path.parent if target_path.name == "__init__.py" else None
            for imported_name, local_name in _python_imported_names(node, source):
                line = node.start_point[0] + 1
                if pkg_dir is not None:
                    sub_py = pkg_dir / f"{imported_name}.py"
                    sub_pkg = pkg_dir / imported_name / "__init__.py"
                    submodule = (
                        sub_py
                        if sub_py.is_file()
                        else (sub_pkg if sub_pkg.is_file() else None)
                    )
                    if submodule is not None:
                        facts.module_imports.append((path, submodule, line))
                        continue
                facts.imports.append(
                    _SymbolImportFact(
                        path, local_name, target_path, imported_name, line
                    )
                )
                if path.name == "__init__.py":
                    facts.exports.append(
                        _SymbolExportFact(
                            path,
                            local_name,
                            line,
                            target_path=target_path,
                            target_name=imported_name,
                        )
                    )

    for path in py_paths:
        parsed = trees.get(path.resolve())
        if parsed is None:
            continue
        source, root_node = parsed
        for source_id, body in _python_top_level_function_bodies(
            path, root_node, source
        ):
            for node in _walk_python_tree(body):
                call_name = _python_call_identifier(node, source)
                if call_name is None:
                    continue
                facts.uses.append(
                    _SymbolUseFact(
                        path,
                        source_id,
                        call_name,
                        "calls",
                        "call",
                        node.start_point[0] + 1,
                    )
                )


def _augment_symbol_resolution_edges(
    paths: list[Path],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    root: Path,
) -> None:
    facts = _SymbolResolutionFacts()
    _collect_js_symbol_resolution_facts(paths, facts)
    _collect_python_symbol_resolution_facts(paths, root, facts)
    _apply_symbol_resolution_facts(paths, nodes, edges, root, facts)


def _resolve_cross_file_imports(
    per_file: list[dict[str, Any]],
    paths: list[Path],
) -> list[dict[str, Any]]:
    """
    Two-pass import resolution: turn file-level imports into class-level edges.

    Pass 1 - build a global map: class/function name → node_id, per stem.
    Pass 2 - for each `from .module import Name`, look up Name in the global
              map and add a direct INFERRED edge from each class in the
              importing file to the imported entity.

    This turns:
        auth.py --imports_from--> models.py          (obvious, filtered out)
    Into:
        DigestAuth --uses--> Response  [INFERRED]    (cross-file, interesting!)
        BasicAuth  --uses--> Request   [INFERRED]
    """
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
    except ImportError:
        return []

    language = Language(tspython.language())
    parser = Parser(language)

    # Pass 1: _file_stem(path) → {ClassName: node_id}
    # Keyed by directory-qualified stem (e.g. "auth_models") to avoid collisions
    # when multiple files share the same filename in different directories.
    # A secondary bare-stem index handles absolute imports where only the module
    # name is known — first writer wins when names collide (inherently ambiguous).
    stem_to_entities: dict[str, dict[str, str]] = {}
    bare_to_qualified: dict[str, str] = {}
    for file_result in per_file:
        for node in file_result.get("nodes", []):
            src = node.get("source_file", "")
            if not src:
                continue
            src_path = Path(src)
            fq_stem = _file_stem(src_path)
            label = node.get("label", "")
            nid = node.get("id", "")
            # Index class-level entities only. Function/method labels end in "()"
            # so are excluded by the `endswith(")")` filter; file nodes end in ".py";
            # private/internal labels start with "_"; rationale nodes carry
            # file_type=="rationale" and must never participate in cross-file
            # import resolution (#563).
            if (
                label
                and not label.endswith((")", ".py"))
                and "_" not in label[:1]
                and node.get("file_type") != "rationale"
            ):
                stem_to_entities.setdefault(fq_stem, {})[label] = nid
                if src_path.stem not in bare_to_qualified:
                    bare_to_qualified[src_path.stem] = fq_stem

    # Pass 2: for each file, find `from .X import A, B, C` and resolve
    new_edges: list[dict[str, Any]] = []

    for file_result, path in zip(per_file, paths):
        stem = _file_stem(path)
        str_path = str(path)

        # Find all classes defined in this file (the importers).
        # Excludes rationale nodes whose labels happen not to end in ")" or ".py"
        # but which must never be treated as importing entities (#563).
        local_classes = [
            n["id"]
            for n in file_result.get("nodes", [])
            if n.get("source_file") == str_path
            and not n["label"].endswith((")", ".py"))
            and n["id"] != _make_id(stem)  # exclude file-level node
            and n.get("file_type") != "rationale"
        ]
        if not local_classes:
            continue

        # Parse imports from this file
        try:
            source = path.read_bytes()
            tree = parser.parse(source)
        except Exception:
            continue

        def walk_imports(node: Node) -> None:
            if node.type == "import_from_statement":
                # Find the module name - handles both absolute and relative imports.
                # Relative: `from .models import X` → relative_import → dotted_name
                # Absolute: `from models import X`  → module_name field
                # target_fq is the directory-qualified stem used as the key in
                # stem_to_entities. Relative imports are resolved exactly via the
                # importing file's directory; absolute imports fall back to the
                # bare-stem secondary index (first-writer-wins when names collide).
                target_fq: str | None = None
                for child in node.children:
                    if child.type == "relative_import":
                        for sub in child.children:
                            if sub.type == "dotted_name":
                                raw = source[sub.start_byte : sub.end_byte].decode(
                                    "utf-8", errors="replace"
                                )
                                bare = raw.split(".")[-1]
                                # Resolve relative import to exact qualified stem.
                                candidate = path.parent / f"{bare}.py"
                                target_fq = _file_stem(candidate)
                                break
                        break
                    if child.type == "dotted_name" and target_fq is None:
                        raw = source[child.start_byte : child.end_byte].decode(
                            "utf-8", errors="replace"
                        )
                        bare = raw.split(".")[-1]
                        target_fq = bare_to_qualified.get(bare)

                if not target_fq or target_fq not in stem_to_entities:
                    return

                # Collect imported names: dotted_name children of import_from_statement
                # that come AFTER the 'import' keyword token.
                imported_names: list[str] = []
                past_import_kw = False
                for child in node.children:
                    if child.type == "import":
                        past_import_kw = True
                        continue
                    if not past_import_kw:
                        continue
                    if child.type == "dotted_name":
                        imported_names.append(
                            source[child.start_byte : child.end_byte].decode(
                                "utf-8", errors="replace"
                            )
                        )
                    elif child.type == "aliased_import":
                        # `import X as Y` - take the original name
                        name_node = child.child_by_field_name("name")
                        if name_node:
                            imported_names.append(
                                source[
                                    name_node.start_byte : name_node.end_byte
                                ].decode("utf-8", errors="replace")
                            )

                line = node.start_point[0] + 1
                for name in imported_names:
                    tgt_nid = stem_to_entities[target_fq].get(name)
                    if tgt_nid:
                        for src_class_nid in local_classes:
                            new_edges.append(
                                {
                                    "source": src_class_nid,
                                    "target": tgt_nid,
                                    "relation": "uses",
                                    "confidence": "INFERRED",
                                    "source_file": str_path,
                                    "source_location": f"L{line}",
                                    "weight": 0.8,
                                }
                            )
            for child in node.children:
                walk_imports(child)

        walk_imports(tree.root_node)

    return new_edges


def _resolve_python_member_calls(
    per_file: list[dict[str, Any]],
    all_nodes: list[dict[str, Any]],
    all_edges: list[dict[str, Any]],
) -> None:
    """Resolve cross-file Python qualified class-method calls (``ClassName.method()``)
    to the class-qualified method node (#1446).

    The shared cross-file call pass drops every ``is_member_call`` because a bare
    method name (``log``) collides across the corpus and inflates god-nodes
    (#543/#1219). That guard is right for *instance* calls (``obj.method()``) but
    misses *class-qualified* calls (``ClassName.method()``), where the receiver is
    an explicitly-named class — an exact, unambiguous reference. This pass uses the
    receiver captured by the extractor, and when it is a capitalized name resolving
    to exactly one class node that owns the called method, emits an EXTRACTED
    ``calls`` edge. Purely additive (only member calls the shared pass skipped),
    with a single-definition god-node guard.

    Must run after id-disambiguation so node ids and caller_nids are final.
    """

    def _key(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "", str(label)).lower()

    node_by_id: dict[Any, dict[str, Any]] = {n.get("id"): n for n in all_nodes}

    # A class owns methods: it is the source of one or more `method` edges. Index
    # class label -> owning class node ids (len != 1 is the god-node guard), and
    # (class_node_id, method_key) -> method_node_id.
    class_def_nids: dict[str, list[Any]] = {}
    method_index: dict[tuple[Any, str], Any] = {}
    for e in all_edges:
        if e.get("relation") != "method":
            continue
        src, tgt = e.get("source"), e.get("target")
        cnode = node_by_id.get(src)
        if cnode is not None:
            class_def_nids.setdefault(_key(cnode.get("label", "")), []).append(src)
        tnode = node_by_id.get(tgt)
        if tnode is not None:
            method_index[(src, _key(tnode.get("label", "")))] = tgt
    if not class_def_nids:
        return
    # A class with N methods produced N entries; collapse to a unique set.
    for k in list(class_def_nids):
        class_def_nids[k] = sorted(set(class_def_nids[k]))

    all_raw_calls: list[dict[str, Any]] = []
    for result in per_file:
        all_raw_calls.extend(result.get("raw_calls", []))

    existing_pairs = {(e.get("source"), e.get("target")) for e in all_edges}
    for rc in all_raw_calls:
        if not rc.get("is_member_call"):
            continue
        receiver = rc.get("receiver")
        callee = rc.get("callee")
        caller = rc.get("caller_nid")
        if not receiver or not callee or not caller:
            continue
        # Only a capitalized receiver is treated as a class reference, so an
        # instance/module (`self`, `obj`, `config`) never collides with a
        # same-spelled class via the case-folding key.
        if not receiver[:1].isupper():
            continue
        class_nids = class_def_nids.get(_key(receiver), [])
        if len(class_nids) != 1:  # absent or ambiguous -> bail (god-node guard)
            continue
        method_nid = method_index.get((class_nids[0], _key(callee)))
        if not method_nid or method_nid == caller:
            continue
        if (caller, method_nid) in existing_pairs:
            continue
        existing_pairs.add((caller, method_nid))
        # EXTRACTED: a qualified `ClassName.method()` is an explicit, unambiguous
        # static reference (unlike a bare instance member call), and the class
        # resolved to exactly one definition that owns the method.
        all_edges.append(
            {
                "source": caller,
                "target": method_nid,
                "relation": "calls",
                "context": "call",
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": rc.get("source_file", ""),
                "source_location": rc.get("source_location"),
                "weight": 1.0,
            }
        )
