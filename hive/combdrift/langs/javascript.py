"""JavaScript / TypeScript / TSX backend for the generic tree-sitter driver.

Re-homed verbatim from `combdrift.symbols`: the declaration-form node sets, the
class-body method model, interface (Layer B) extraction, and the import/re-export
indirection analysis. The verdicts are byte-identical to the pre-refactor engine.

This module supplies only observations through three callables wired into the
three `LangSpec`s below (one per grammar — `.js` family, `.ts` family, `.tsx`).
It never decides found/missing/indirect/parse_error/ambiguous — `symbols.locate`
owns that tri-state (Law 1). Accordingly, `indirect_detail` returns a
`(detail, edge)` tuple (the mechanism, if any) rather than a status.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import tree_sitter_javascript as _ts_javascript
import tree_sitter_typescript as _ts_typescript
from tree_sitter import Language, Node

from hive.combdrift.fingerprint import Interface
from hive.combdrift.langs.base import (
    LangSpec,
    ReexportEdge,
    node_name,
    node_text,
    walk_nodes,
)

# tree-sitter node types that declare a top-level function by name (the last is
# the bodiless form found in .d.ts ambient declarations).
_TS_FUNCTION_NODES = frozenset(
    {"function_declaration", "generator_function_declaration", "function_signature"}
)
# ...that declare a top-level class by name.
_TS_CLASS_NODES = frozenset({"class_declaration", "abstract_class_declaration"})
# ...that declare a member inside a class body (the last is the .d.ts form).
_TS_METHOD_NODES = frozenset({"method_definition", "method_signature"})
# Initializer node types that make a const/let/var binding "function-shaped",
# mirroring Python's def/class-only model.
_TS_FUNCTION_VALUES = frozenset(
    {"arrow_function", "function_expression", "function", "generator_function"}
)
# Wrappers that are transparent to top-level declaration lookup: `export`,
# `export default`, and `declare` should not hide the declaration they carry.
_TS_BINDING_NODES = frozenset({"lexical_declaration", "variable_declaration"})
_TS_EXPORT = "export_statement"
_TS_IMPORT = "import_statement"
_TS_AMBIENT = "ambient_declaration"
# Module-file resolution precedence for a relative specifier, and the extensions
# a specifier may already carry (`./m.ts`, `./m.js`). A `.js`-family specifier in
# a TS project also resolves to the `.ts`/`.tsx` source it was compiled from.
_TS_RESOLVE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_TS_REWRITE_EXTS = frozenset({".js", ".jsx", ".mjs", ".cjs"})
_TS_SPECIFIER_EXTS = frozenset(_TS_RESOLVE_EXTS) | {".mts", ".cts"}
# Value/type-only declarations: present, but never a resolvable callable.
_TS_TYPE_DECL_NODES = frozenset(
    {"interface_declaration", "type_alias_declaration", "enum_declaration"}
)
# Clause node types whose identifiers bind/re-export a name into the module.
_TS_IMPORT_NAME_NODES = frozenset(
    {"import_specifier", "export_specifier", "namespace_import", "namespace_export"}
)


def _find_ts_symbols(root: Node, symbol: str) -> list[Node]:
    """All top-level declarations matching `symbol` (>1 means an overload set)."""
    if "." in symbol:
        class_name, _, member = symbol.partition(".")
        matches: list[Node] = []
        for decl in _top_level_decls(root):
            if decl.type in _TS_CLASS_NODES and node_name(decl) == class_name:
                matches.extend(_find_methods(decl, member))
        return matches
    matches = []
    for decl in _top_level_decls(root):
        match = _match_named_decl(decl, symbol)
        if match is not None:
            matches.append(match)
    return matches


def _top_level_decls(root: Node) -> Iterator[Node]:
    """Yield top-level declarations, seeing through export/declare wrappers."""
    for child in root.named_children:
        yield from _unwrap(child)


def _unwrap(node: Node) -> Iterator[Node]:
    if node.type == _TS_EXPORT:
        inner = node.child_by_field_name("declaration") or node.child_by_field_name(
            "value"
        )
        if inner is not None:
            yield from _unwrap(inner)
        return
    if node.type == _TS_AMBIENT:
        for child in node.named_children:
            yield from _unwrap(child)
        return
    yield node


def _match_named_decl(decl: Node, symbol: str) -> Node | None:
    """Return the node declaring `symbol`, or None if this decl does not."""
    if decl.type in _TS_FUNCTION_NODES or decl.type in _TS_CLASS_NODES:
        return decl if node_name(decl) == symbol else None
    if decl.type in _TS_BINDING_NODES:
        return _match_function_binding(decl, symbol)
    return None


def _match_function_binding(decl: Node, symbol: str) -> Node | None:
    """A const/let/var binding matches only when bound to a function/arrow."""
    for child in decl.named_children:
        if child.type != "variable_declarator":
            continue
        value = child.child_by_field_name("value")
        if (
            node_name(child) == symbol
            and value is not None
            and value.type in _TS_FUNCTION_VALUES
        ):
            return child
    return None


def _find_methods(class_node: Node, member: str) -> list[Node]:
    body = class_node.child_by_field_name("body")
    if body is None:
        return []
    return [
        child
        for child in body.named_children
        if child.type in _TS_METHOD_NODES and node_name(child) == member
    ]


def _find_top_level_class(root: Node, class_name: str) -> Node | None:
    for decl in _top_level_decls(root):
        if decl.type in _TS_CLASS_NODES and node_name(decl) == class_name:
            return decl
    return None


def _ts_class_has_heritage(class_node: Node) -> bool:
    """True iff the class extends and/or implements anything."""
    return any(child.type == "class_heritage" for child in class_node.named_children)


def _ts_indirect_detail(
    root: Node, path: str, name: str
) -> tuple[str | None, ReexportEdge | None]:
    """The mechanism making `name` present-but-not-a-top-level-callable, or None.

    Mirrors the Python rule: None means the name is bound nowhere (provably
    absent → missing → stale); a non-None detail names an indirection that could
    supply the name (import/re-export, data/type binding, nested declaration,
    barrel star export, or a CommonJS dynamic export), making it unverifiable.

    The second element is a followable `ReexportEdge` iff the indirection is a
    relative, named, non-shadowed import/re-export of `name`; for every other
    mechanism it is None.
    """
    imported: set[str] = set()
    declared: set[str] = set()  # function/class declarations at any depth
    value_bound: set[str] = set()  # const/let/var, interface, type, enum
    has_barrel = False
    has_commonjs = False
    for node in walk_nodes(root):
        node_type = node.type
        if node_type in _TS_IMPORT_NAME_NODES:
            for ident in node.named_children:
                if ident.type == "identifier":
                    imported.add(node_text(ident) or "")
        elif node_type == "import_clause":
            # A default import binds a bare identifier directly under the clause.
            for child in node.named_children:
                if child.type == "identifier":
                    imported.add(node_text(child) or "")
        elif node_type == _TS_EXPORT and _is_barrel_export(node):
            has_barrel = True
        elif node_type in _TS_FUNCTION_NODES or node_type in _TS_CLASS_NODES:
            name_text = node_name(node)
            if name_text is not None:
                declared.add(name_text)
        elif node_type == "variable_declarator" or node_type in _TS_TYPE_DECL_NODES:
            name_text = node_name(node)
            if name_text is not None:
                value_bound.add(name_text)
        elif node_type == "assignment_expression" and _is_commonjs_export(node):
            has_commonjs = True

    if name in imported:
        # A local declaration/value binding shadows the import; following it
        # would fingerprint the wrong value, so the edge is suppressed.
        shadowed = name in declared or name in value_bound
        edge = None if shadowed else _ts_reexport_edges(root, path).get(name)
        return "reexport", edge
    # A top-level callable was ruled out before this call, so a declared hit is
    # necessarily a nested/conditional definition.
    if name in declared:
        return "nested", None
    if name in value_bound:
        return "noncallable", None
    if has_barrel:
        return "wildcard", None
    if has_commonjs:
        return "commonjs_dynamic", None
    return None, None


def _ts_reexport_edges(root: Node, path: str) -> dict[str, ReexportEdge]:
    """Followable edges keyed by visible name, from relative named import/re-export.

    Scans top-level `import`/`export … from` statements; a default import, a
    namespace import, a star re-export, and an `export { x }` without a source
    bind no followable named edge. JS/TS never yields submodule candidates.
    """
    edges: dict[str, ReexportEdge] = {}
    for stmt in root.named_children:
        if stmt.type not in (_TS_IMPORT, _TS_EXPORT):
            continue
        source = stmt.child_by_field_name("source")
        if source is None:
            continue  # `export { x }` with no `from` re-binds; supplies no module
        specifier = _ts_string_value(source)
        if specifier is None or not specifier.startswith("."):
            continue  # bare/absolute specifier needs node_modules; not followable
        candidates = _ts_module_candidates(path, specifier)
        for spec in walk_nodes(stmt):
            if spec.type not in ("import_specifier", "export_specifier"):
                continue
            name_node = spec.child_by_field_name("name")
            alias_node = spec.child_by_field_name("alias")
            original = node_text(name_node) if name_node is not None else None
            if original is None:
                continue
            visible = node_text(alias_node) if alias_node is not None else original
            if visible is None:
                continue
            edges[visible] = ReexportEdge(
                name=original, module_candidates=candidates, submodule_candidates=()
            )
    return edges


def _ts_string_value(node: Node) -> str | None:
    """The text of a string literal node, without surrounding quotes."""
    for child in node.named_children:
        if child.type == "string_fragment":
            return node_text(child)
    text = node_text(node)  # an empty string literal has no fragment child
    if text is not None and len(text) >= 2 and text[0] in "\"'`":
        return text[1:-1]
    return text


def _ts_module_candidates(importer_path: str, specifier: str) -> tuple[str, ...]:
    """Repo-relative files a relative specifier may denote, in resolution order."""
    base = os.path.normpath(os.path.join(os.path.dirname(importer_path), specifier))
    _stem, ext = os.path.splitext(base)
    if ext.lower() in _TS_SPECIFIER_EXTS:
        # An explicit extension leads; a .js-family import also resolves to the
        # TS source it compiles from.
        candidates = [base]
        if ext.lower() in _TS_REWRITE_EXTS:
            candidates += [_stem + ".ts", _stem + ".tsx"]
        return tuple(candidates)
    return tuple(
        [base + ext for ext in _TS_RESOLVE_EXTS]
        + [os.path.join(base, "index" + ext) for ext in _TS_RESOLVE_EXTS]
    )


def _is_barrel_export(node: Node) -> bool:
    """True for `export * from '...'` — a re-export that may supply any name."""
    children = node.named_children
    if not any(child.type == "string" for child in children):
        return False  # no source module to re-export from
    if any(child.type in ("export_clause", "namespace_export") for child in children):
        return False  # named (or namespaced) re-export, not a blanket star
    return node.child_by_field_name("declaration") is None


def _is_commonjs_export(node: Node) -> bool:
    """True for `module.exports = ...` or `exports.x = ...` assignment targets."""
    left = node.child_by_field_name("left")
    if left is None or left.type != "member_expression":
        return False
    obj = left.child_by_field_name("object")
    if obj is None:
        return False
    obj_text = node_text(obj)
    if obj_text == "exports":
        return True
    if obj_text == "module":
        prop = left.child_by_field_name("property")
        return prop is not None and node_text(prop) == "exports"
    return False


# --- JS/TS interface extraction (Layer B) ------------------------------------

# Standalone generator forms; method generators are flagged by a `*` token child.
_TS_GENERATOR_NODES = frozenset(
    {"generator_function_declaration", "generator_function"}
)
# method_definition keyword children that change how the member is invoked.
_TS_DECORATOR_BY_KEYWORD = {"static": "static", "get": "getter", "set": "setter"}


def _ts_interface(node: Node) -> Interface:
    """Extract a language-neutral call-shape descriptor from a JS/TS declaration."""
    if node.type in _TS_CLASS_NODES:
        return Interface(
            category="class",
            is_generator=False,
            req_positional=0,
            max_positional=0,
            has_star=False,
            has_kw=False,
            req_kwonly=0,
            contract_decorators=frozenset(),
            base_count=_ts_base_count(node),
        )
    callable_node = _ts_callable_node(node)
    req, maximum, has_star = _ts_param_counts(callable_node)
    return Interface(
        category="async_func" if _ts_is_async(callable_node) else "func",
        is_generator=_ts_is_generator(callable_node),
        req_positional=req,
        max_positional=maximum,
        has_star=has_star,
        has_kw=False,  # JS/TS has no **kwargs equivalent in v1
        req_kwonly=0,  # JS/TS has no keyword-only parameters
        contract_decorators=_ts_contract_decorators(node),
        base_count=0,
    )


def _ts_callable_node(node: Node) -> Node:
    """A const/let/var binding's callable is its value; everything else is itself."""
    if node.type == "variable_declarator":
        return node.child_by_field_name("value") or node
    return node


def _ts_param_counts(callable_node: Node) -> tuple[int, int, bool]:
    """Return (required positional, max positional, has rest) for a callable."""
    params = next(
        (c for c in callable_node.named_children if c.type == "formal_parameters"),
        None,
    )
    if params is None:
        # A parenless single-parameter arrow `x => ...`: one required, no rest.
        if callable_node.type == "arrow_function" and (
            callable_node.child_by_field_name("parameter") is not None
        ):
            return 1, 1, False
        return 0, 0, False
    required = maximum = 0
    has_star = False
    for param in params.named_children:
        kind = _ts_param_kind(param)
        if kind == "rest":
            has_star = True
        elif kind in ("required", "optional", "default"):
            maximum += 1
            if kind == "required":
                required += 1
    return required, maximum, has_star


def _ts_param_kind(param: Node) -> str:
    """Classify a parameter: rest, optional, default-valued, required, or other."""
    if any(child.type == "rest_pattern" for child in param.named_children):
        return "rest"
    if param.type == "optional_parameter":
        return "optional"
    if param.type == "required_parameter":
        # A required_parameter node with a default value is optional at call time.
        return (
            "default" if param.child_by_field_name("value") is not None else "required"
        )
    return "other"


def _ts_is_async(callable_node: Node) -> bool:
    return any(child.type == "async" for child in callable_node.children)


def _ts_is_generator(callable_node: Node) -> bool:
    if callable_node.type in _TS_GENERATOR_NODES:
        return True
    # A generator method carries a bare `*` token before its name.
    return any(child.type == "*" for child in callable_node.children)


def _ts_contract_decorators(node: Node) -> frozenset[str]:
    return frozenset(
        _TS_DECORATOR_BY_KEYWORD[child.type]
        for child in node.children
        if child.type in _TS_DECORATOR_BY_KEYWORD
    )


def _ts_base_count(class_node: Node) -> int:
    """Count extends + implements targets across the class heritage."""
    count = 0
    for child in class_node.named_children:
        if child.type != "class_heritage":
            continue
        for clause in child.named_children:
            if clause.type in ("extends_clause", "implements_clause"):
                count += sum(
                    1
                    for target in clause.named_children
                    if target.type != "type_arguments"
                )
    return count


# --- LangSpec adapter: indirection mechanism (bare + dotted) -------------------
# The generic driver in symbols.py maps a non-None detail onto `indirect` and a
# None detail onto `missing`; these functions only observe the mechanism, they
# never choose the status.


def _ts_method_indirect_detail(
    root: Node, path: str, symbol: str
) -> tuple[str | None, ReexportEdge | None]:
    """Mechanism for a dotted "Class.method" whose method is not in the class body.

    A present class with heritage may inherit/merge the member ("maybe_inherited");
    a present class with no heritage is provable absence (None → missing); an
    absent class defers to the class name's own indirection, with the edge dropped
    (one hop resolves a class, not its dotted member).
    """
    class_name, _, _member = symbol.partition(".")
    class_node = _find_top_level_class(root, class_name)
    if class_node is not None:
        if _ts_class_has_heritage(class_node):
            return "maybe_inherited", None
        return None, None
    detail, _edge = _ts_indirect_detail(root, path, class_name)
    return detail, None


def _indirect_detail(
    root: Node, path: str, name: str
) -> tuple[str | None, ReexportEdge | None]:
    """Dispatch to the bare-name or class-member indirection mechanism."""
    if "." in name:
        return _ts_method_indirect_detail(root, path, name)
    return _ts_indirect_detail(root, path, name)


# The three grammars share every observation function and differ only in the
# tree-sitter Language; the driver interprets their results identically.
JAVASCRIPT = LangSpec(
    grammar=Language(_ts_javascript.language()),
    find_symbols=_find_ts_symbols,
    extract_interface=_ts_interface,
    indirect_detail=_indirect_detail,
)
TYPESCRIPT = LangSpec(
    grammar=Language(_ts_typescript.language_typescript()),
    find_symbols=_find_ts_symbols,
    extract_interface=_ts_interface,
    indirect_detail=_indirect_detail,
)
TSX = LangSpec(
    grammar=Language(_ts_typescript.language_tsx()),
    find_symbols=_find_ts_symbols,
    extract_interface=_ts_interface,
    indirect_detail=_indirect_detail,
)
