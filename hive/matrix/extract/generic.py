"""The generic tree-sitter walker shared by the config-driven languages, plus
the per-language type/param reference collectors it dispatches to."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .support import (
    _LANGUAGE_BUILTIN_GLOBALS,
    _file_stem,
    _make_id,
    _read_text,
    _semantic_reference_edge,
)

if TYPE_CHECKING:
    from tree_sitter import Node


# ── LanguageConfig dataclass ─────────────────────────────────────────────────


@dataclass
class LanguageConfig:
    ts_module: str  # e.g. "tree_sitter_python"
    ts_language_fn: str = "language"  # attr to call: e.g. tslang.language()

    class_types: frozenset[str] = frozenset()
    function_types: frozenset[str] = frozenset()
    import_types: frozenset[str] = frozenset()
    call_types: frozenset[str] = frozenset()
    static_prop_types: frozenset[str] = frozenset()
    helper_fn_names: frozenset[str] = frozenset()
    container_bind_methods: frozenset[str] = frozenset()
    event_listener_properties: frozenset[str] = frozenset()

    # Name extraction
    name_field: str = "name"

    # Body detection
    body_field: str = "body"

    # Call name extraction
    call_function_field: str = "function"  # field on call node for callee
    call_accessor_node_types: frozenset[str] = frozenset()  # member/attribute nodes
    call_accessor_field: str = "attribute"  # field on accessor for method name
    call_accessor_object_field: str = ""  # field on accessor for the receiver/object

    # Stop recursion at these types in walk_calls
    function_boundary_types: frozenset[str] = frozenset()

    # Import handler: called for import nodes instead of generic handling
    import_handler: Callable[..., Any] | None = None

    # Optional custom name resolver for functions (C, C++ declarator unwrapping)
    resolve_function_name_fn: Callable[..., Any] | None = None

    # Extra label formatting for functions: if True, functions get "name()" label
    function_label_parens: bool = True

    # Extra walk hook called after generic dispatch (for JS arrow functions, C# namespaces, etc.)
    extra_walk_fn: Callable[..., Any] | None = None


# ── Generic helpers ───────────────────────────────────────────────────────────


_PYTHON_TYPE_CONTAINERS = frozenset(
    {
        "list",
        "dict",
        "set",
        "tuple",
        "frozenset",
        "type",
        "List",
        "Dict",
        "Set",
        "Tuple",
        "FrozenSet",
        "Type",
        "Optional",
        "Union",
        "Sequence",
        "Iterable",
        "Mapping",
        "MutableMapping",
        "Iterator",
        "Callable",
        "Awaitable",
        "AsyncIterable",
        "AsyncIterator",
        "Coroutine",
        "Generator",
        "AsyncGenerator",
        "ContextManager",
        "AsyncContextManager",
        "Annotated",
        "ClassVar",
        "Final",
        "Literal",
        "Concatenate",
        "ParamSpec",
        "TypeVar",
        "None",
        "Ellipsis",
    }
)


# Scalar builtins and test-mock names that appear as type annotations but carry
# no useful semantic meaning as graph nodes (#1147). Suppressed at the annotation
# walker level so they are never created as nodes or emitted as edges.
_PYTHON_ANNOTATION_NOISE = frozenset(
    {
        # scalar builtins
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "bytearray",
        "complex",
        "object",
        "True",
        "False",
        # unittest.mock
        "MagicMock",
        "Mock",
        "AsyncMock",
        "NonCallableMock",
        "NonCallableMagicMock",
        "PropertyMock",
        "patch",
        "sentinel",
    }
)


def _python_collect_type_refs(
    node: Node | None, source: bytes, generic: bool, out: list[tuple[str, str]]
) -> None:
    """Walk a Python type annotation; append (name, role) where role is 'type' or 'generic_arg'.

    Builtin/typing containers (list, dict, Optional, Union, …) are not emitted as refs themselves,
    but their nested type arguments still count as generic_arg.
    """
    if node is None:
        return
    t = node.type
    if t == "type":
        for c in node.children:
            if c.is_named:
                _python_collect_type_refs(c, source, generic, out)
        return
    if t == "identifier":
        name = _read_text(node, source)
        if (
            name
            and name not in _PYTHON_TYPE_CONTAINERS
            and name not in _PYTHON_ANNOTATION_NOISE
        ):
            out.append((name, "generic_arg" if generic else "type"))
        return
    if t == "attribute":
        tail = _read_text(node, source).rsplit(".", 1)[-1]
        if (
            tail
            and tail not in _PYTHON_TYPE_CONTAINERS
            and tail not in _PYTHON_ANNOTATION_NOISE
        ):
            out.append((tail, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        for c in node.children:
            if c.type == "identifier":
                container = _read_text(c, source)
                if (
                    container
                    and container not in _PYTHON_TYPE_CONTAINERS
                    and container not in _PYTHON_ANNOTATION_NOISE
                ):
                    out.append((container, "generic_arg" if generic else "type"))
            elif c.type == "type_parameter":
                for sub in c.children:
                    if sub.is_named:
                        _python_collect_type_refs(sub, source, True, out)
        return
    if t == "subscript":
        value = node.child_by_field_name("value")
        if value is not None:
            _python_collect_type_refs(value, source, generic, out)
        for c in node.children:
            if c is value or not c.is_named:
                continue
            _python_collect_type_refs(c, source, True, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _python_collect_type_refs(c, source, generic, out)


_GO_PREDECLARED_TYPES = frozenset(
    {
        "bool",
        "byte",
        "complex64",
        "complex128",
        "error",
        "float32",
        "float64",
        "int",
        "int8",
        "int16",
        "int32",
        "int64",
        "rune",
        "string",
        "uint",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "uintptr",
        "any",
        "comparable",
    }
)


def _go_collect_type_refs(
    node: Node | None, source: bytes, generic: bool, out: list[tuple[str, str]]
) -> None:
    """Walk a Go type expression; append (name, role) tuples."""
    if node is None:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text and text not in _GO_PREDECLARED_TYPES:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "qualified_type":
        text = _read_text(node, source).rsplit(".", 1)[-1]
        if text and text not in _GO_PREDECLARED_TYPES:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        type_field = node.child_by_field_name("type")
        if type_field is not None:
            sub: list[tuple[str, str]] = []
            _go_collect_type_refs(type_field, source, generic, sub)
            out.extend(sub)
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _go_collect_type_refs(arg, source, True, out)
        return
    if t in (
        "pointer_type",
        "slice_type",
        "array_type",
        "map_type",
        "channel_type",
        "parenthesized_type",
    ):
        for c in node.children:
            if c.is_named:
                _go_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _go_collect_type_refs(c, source, generic, out)


def _rust_collect_type_refs(
    node: Node | None, source: bytes, generic: bool, out: list[tuple[str, str]]
) -> None:
    """Walk a Rust type expression; append (name, role) tuples."""
    if node is None:
        return
    t = node.type
    if t == "primitive_type":
        return
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "scoped_type_identifier":
        text = _read_text(node, source).rsplit("::", 1)[-1]
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        name_node = node.child_by_field_name("type")
        if name_node is None:
            for c in node.children:
                if c.type in ("type_identifier", "scoped_type_identifier"):
                    name_node = c
                    break
        if name_node is not None:
            text = _read_text(name_node, source).rsplit("::", 1)[-1]
            if text:
                out.append((text, "generic_arg" if generic else "type"))
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _rust_collect_type_refs(arg, source, True, out)
        return
    if t in (
        "reference_type",
        "pointer_type",
        "array_type",
        "tuple_type",
        "slice_type",
    ):
        for c in node.children:
            if c.is_named:
                _rust_collect_type_refs(c, source, generic, out)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _rust_collect_type_refs(c, source, generic, out)


# ── C / C++ type-ref helpers ─────────────────────────────────────────────────

_C_PRIMITIVE_TYPE_NODES = frozenset(
    {
        "primitive_type",
        "sized_type_specifier",
        "auto",
        "placeholder_type_specifier",
    }
)


def _c_collect_type_refs(
    node: Node | None, source: bytes, generic: bool, out: list[tuple[str, str]]
) -> None:
    """Walk a C type expression; append (name, role) tuples for user-defined types.
    Skips primitive types and qualifiers; recognises type_identifier."""
    if node is None or node.type in _C_PRIMITIVE_TYPE_NODES:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t in (
        "pointer_declarator",
        "reference_declarator",
        "array_declarator",
        "type_qualifier",
        "type_descriptor",
        "abstract_pointer_declarator",
        "abstract_reference_declarator",
        "abstract_array_declarator",
    ):
        for c in node.children:
            if c.is_named:
                _c_collect_type_refs(c, source, generic, out)


def _cpp_collect_type_refs(
    node: Node | None, source: bytes, generic: bool, out: list[tuple[str, str]]
) -> None:
    """Walk a C++ type expression; append (name, role) tuples.
    Resolves qualified_identifier tails (std::string → string) and template_type
    base + arguments (std::vector<HttpClient> → vector + HttpClient as generic_arg)."""
    if node is None or node.type in _C_PRIMITIVE_TYPE_NODES:
        return
    t = node.type
    if t == "type_identifier":
        text = _read_text(node, source)
        if text:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "qualified_identifier":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            _cpp_collect_type_refs(name_node, source, generic, out)
        return
    if t == "template_type":
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            text = _read_text(name_node, source)
            if text:
                out.append((text, "generic_arg" if generic else "type"))
        args_node = node.child_by_field_name("arguments")
        if args_node is not None:
            for c in args_node.children:
                if c.is_named:
                    _cpp_collect_type_refs(c, source, True, out)
        return
    if t in (
        "type_descriptor",
        "pointer_declarator",
        "reference_declarator",
        "array_declarator",
        "type_qualifier",
        "abstract_pointer_declarator",
        "abstract_reference_declarator",
        "abstract_array_declarator",
    ):
        for c in node.children:
            if c.is_named:
                _cpp_collect_type_refs(c, source, generic, out)


# ── Scala type-ref helpers ───────────────────────────────────────────────────


def _python_collect_param_refs(
    params_node: Node | None, source: bytes
) -> list[tuple[str, str]]:
    """Collect type refs from each typed parameter under a `parameters` node."""
    out: list[tuple[str, str]] = []
    if params_node is None:
        return out
    for child in params_node.children:
        if child.type in ("typed_parameter", "typed_default_parameter"):
            type_node = child.child_by_field_name("type")
            _python_collect_type_refs(type_node, source, False, out)
    return out


def _find_body(node: Node, config: LanguageConfig) -> Node | None:
    """Find the body node using config.body_field."""
    return node.child_by_field_name(config.body_field)


# ── C/C++ function name helpers ───────────────────────────────────────────────


def _get_c_func_name(node: Node, source: bytes) -> str | None:
    """Recursively unwrap declarator to find the innermost identifier (C)."""
    if node.type == "identifier":
        return _read_text(node, source)
    decl = node.child_by_field_name("declarator")
    if decl:
        return _get_c_func_name(decl, source)
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None


def _get_cpp_func_name(node: Node, source: bytes) -> str | None:
    """Recursively unwrap declarator to find the innermost identifier (C++)."""
    if node.type == "identifier":
        return _read_text(node, source)
    if node.type in ("field_identifier", "destructor_name", "operator_name"):
        return _read_text(node, source)
    if node.type == "qualified_identifier":
        name_node = node.child_by_field_name("name")
        if name_node:
            return _read_text(name_node, source)
    decl = node.child_by_field_name("declarator")
    if decl:
        return _get_cpp_func_name(decl, source)
    for child in node.children:
        if child.type == "identifier":
            return _read_text(child, source)
    return None


# ── Generic extractor ─────────────────────────────────────────────────────────


def _extract_generic(path: Path, config: LanguageConfig) -> dict[str, Any]:
    """Generic AST extractor driven by LanguageConfig."""
    # Deferred to break the generic<->configs import cycle: configs imports
    # LanguageConfig/_get_*_func_name from this module, so it cannot be imported
    # at module load time here. These JS helpers are only needed at call time.
    from .configs import (
        _JS_FUNCTION_VALUE_TYPES,
        _dynamic_import_js,
        _js_extra_walk,
        _js_member_assignment_target,
    )

    try:
        mod = importlib.import_module(config.ts_module)
        from tree_sitter import Language, Parser

        lang_fn = getattr(mod, config.ts_language_fn, None)
        if lang_fn is None:
            # Fallback for PHP: try "language_php" then "language"
            lang_fn = getattr(mod, "language", None)
        if lang_fn is None:
            return {
                "nodes": [],
                "edges": [],
                "error": f"No language function in {config.ts_module}",
            }
        language = Language(lang_fn())
    except ImportError:
        return {"nodes": [], "edges": [], "error": f"{config.ts_module} not installed"}
    except TypeError as e:
        # tree-sitter version mismatch: old Language() expects (lib_path),
        # new Language() expects (language_capsule, name). Surface a hint
        # so users see the upgrade path instead of a bare TypeError.
        hint = (
            f"tree-sitter version mismatch for {config.ts_module}: {e}. "
            "Try: pip install --upgrade tree-sitter tree-sitter-languages"
        )
        return {"nodes": [], "edges": [], "error": hint}
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    try:
        parser = Parser(language)
        source = path.read_bytes()
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}

    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    function_bodies: list[tuple[str, Node]] = []
    pending_listen_edges: list[tuple[str, str, int]] = []
    # #1356: call expressions in property/field initializers (e.g.
    # `let vm = VM()`) live outside function bodies, so the call-walk never
    # reaches them. Collect (owner_nid, call_node) here and walk them too.
    initializer_nodes: list[tuple[str, Node]] = []

    def add_node(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append(
                {
                    "id": nid,
                    "label": label,
                    "file_type": "code",
                    "source_file": str_path,
                    "source_location": f"L{line}",
                }
            )

    def add_edge(
        src: str,
        tgt: str,
        relation: str,
        line: int,
        confidence: str = "EXTRACTED",
        weight: float = 1.0,
        context: str | None = None,
    ) -> None:
        edge = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        }
        if context:
            edge["context"] = context
        edges.append(edge)

    def ensure_named_node(name: str, line: int) -> str:
        nid = _make_id(stem, name)
        if nid in seen_ids:
            return nid
        nid = _make_id(name)
        if nid not in seen_ids:
            # The name isn't defined in this file, so this is a cross-file reference
            # (e.g. a `Thing` type annotation imported from another module). Emit a
            # SOURCELESS stub — like the inheritance-base path below — so the
            # corpus-level rewire can collapse it onto the real definition. A sourced
            # stub here makes _disambiguate_colliding_node_ids bake the referencing
            # file's path (with extension) into the id and blocks the rewire, which is
            # the phantom-duplicate-node bug (#1402).
            seen_ids.add(nid)
            nodes.append(
                {
                    "id": nid,
                    "label": name,
                    "file_type": "code",
                    "source_file": "",
                    "source_location": "",
                }
            )
        return nid

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)

    def walk(node: Node, parent_class_nid: str | None = None) -> None:
        t = node.type

        # Import types
        if t in config.import_types:
            if config.import_handler:
                imported_modules = config.import_handler(
                    node, source, file_nid, stem, edges, str_path
                )
                # Module-level import handlers (Swift) name a module, not a file
                # path, so there is no pre-existing node to anchor the edge to.
                # They return (id, label) pairs for which we materialize a
                # `type=module` node; otherwise build_from_json prunes every such
                # import edge as a dangling/external reference. The same module
                # imported from N files shares one id (file_type=code keeps
                # build.py validation happy; `type=module` exempts it from
                # id-disambiguation) so it collapses to one shared node (#1327).
                if imported_modules:
                    line = node.start_point[0] + 1
                    for mod_nid, mod_label in imported_modules:
                        if mod_nid not in seen_ids:
                            seen_ids.add(mod_nid)
                            nodes.append(
                                {
                                    "id": mod_nid,
                                    "label": mod_label,
                                    "file_type": "code",
                                    "type": "module",
                                    "source_file": str_path,
                                    "source_location": f"L{line}",
                                }
                            )
            # For export_statement: only return (skip children) if it's a re-export
            # (has a `from` source). Otherwise fall through to walk children which may
            # contain function_declaration, class_declaration, etc.
            if t == "export_statement":
                has_source = any(c.type == "string" for c in node.children)
                if not has_source:
                    for child in node.children:
                        walk(child, parent_class_nid)
            return

        # Class types
        if t in config.class_types:
            # Resolve class name
            name_node = node.child_by_field_name(config.name_field)
            if not name_node:
                return
            class_name = _read_text(name_node, source)
            class_nid = _make_id(stem, class_name)
            line = node.start_point[0] + 1
            add_node(class_nid, class_name, line)
            add_edge(file_nid, class_nid, "contains", line)

            # Python-specific: inheritance
            if config.ts_module == "tree_sitter_python":
                args = node.child_by_field_name("superclasses")
                if args:
                    for arg in args.children:
                        if arg.type == "identifier":
                            base = _read_text(arg, source)
                            base_nid = _make_id(stem, base)
                            if base_nid not in seen_ids:
                                base_nid = _make_id(base)
                                if base_nid not in seen_ids:
                                    nodes.append(
                                        {
                                            "id": base_nid,
                                            "label": base,
                                            "file_type": "code",
                                            "source_file": "",
                                            "source_location": "",
                                        }
                                    )
                                    seen_ids.add(base_nid)
                            add_edge(class_nid, base_nid, "inherits", line)

            # C++-specific: inheritance via base_class_clause (class and struct).
            # tree-sitter-cpp shape:
            #   class_specifier / struct_specifier
            #     base_class_clause
            #       access_specifier? ("public"/"protected"/"private")  -- skip
            #       "virtual"?                                          -- skip
            #       type_identifier                                     -- "Base"
            #       qualified_identifier                                -- "ns::Base"
            #       template_type                                       -- "Vec<int>"
            # Multiple bases are siblings separated by ',' tokens.
            if config.ts_module == "tree_sitter_cpp":
                for child in node.children:
                    if child.type != "base_class_clause":
                        continue
                    for sub in child.children:
                        base = ""
                        if sub.type == "type_identifier":
                            base = _read_text(sub, source)
                        elif sub.type == "qualified_identifier":
                            # Use the unqualified tail so "std::vector" matches
                            # a "vector" node id if one exists in the graph;
                            # fall back to the full qualified text otherwise.
                            tail = sub.child_by_field_name("name")
                            base = (
                                _read_text(tail, source)
                                if tail
                                else _read_text(sub, source)
                            )
                        elif sub.type == "template_type":
                            tname = sub.child_by_field_name("name")
                            base = (
                                _read_text(tname, source)
                                if tname
                                else _read_text(sub, source)
                            )
                        else:
                            continue
                        if not base:
                            continue
                        base_nid = _make_id(stem, base)
                        if base_nid not in seen_ids:
                            base_nid = _make_id(base)
                            if base_nid not in seen_ids:
                                nodes.append(
                                    {
                                        "id": base_nid,
                                        "label": base,
                                        "file_type": "code",
                                        "source_file": "",
                                        "source_location": "",
                                    }
                                )
                                seen_ids.add(base_nid)
                        add_edge(class_nid, base_nid, "inherits", line)

            # Find body and recurse
            body = _find_body(node, config)
            if body:
                for child in body.children:
                    walk(child, parent_class_nid=class_nid)
            return

        # Event listener property arrays: $listen = [Event::class => [Listener::class]]
        if (
            t == "property_declaration"
            and parent_class_nid
            and config.event_listener_properties
        ):
            handled_event_listener = False
            for element in node.children:
                if element.type != "property_element":
                    continue
                prop_name: str | None = None
                array_node = None
                for c in element.children:
                    if c.type == "variable_name":
                        for sc in c.children:
                            if sc.type == "name":
                                prop_name = _read_text(sc, source)
                                break
                    elif c.type == "array_creation_expression":
                        array_node = c
                if (
                    prop_name is None
                    or prop_name not in config.event_listener_properties
                    or array_node is None
                ):
                    continue
                handled_event_listener = True
                for entry in array_node.children:
                    if entry.type != "array_element_initializer":
                        continue
                    event_cls: str | None = None
                    listener_arr = None
                    for sub in entry.children:
                        if (
                            sub.type == "class_constant_access_expression"
                            and event_cls is None
                        ):
                            for sc in sub.children:
                                if sc.is_named and sc.type in (
                                    "name",
                                    "qualified_name",
                                ):
                                    event_cls = _read_text(sc, source)
                                    break
                        elif sub.type == "array_creation_expression":
                            listener_arr = sub
                    if not event_cls or listener_arr is None:
                        continue
                    for listener_entry in listener_arr.children:
                        if listener_entry.type != "array_element_initializer":
                            continue
                        for item in listener_entry.children:
                            if item.type != "class_constant_access_expression":
                                continue
                            for sc in item.children:
                                if sc.is_named and sc.type in (
                                    "name",
                                    "qualified_name",
                                ):
                                    listener_cls = _read_text(sc, source)
                                    line_no = item.start_point[0] + 1
                                    pending_listen_edges.append(
                                        (event_cls, listener_cls, line_no)
                                    )
                                    break
                            break
            if handled_event_listener:
                return

        if (
            config.ts_module == "tree_sitter_cpp"
            and t == "field_declaration"
            and parent_class_nid
        ):
            # Skip method prototypes (field_declaration with a function_declarator
            # is a member-function declaration, not a data member).
            decls = list(node.children_by_field_name("declarator"))
            is_method = any(
                d.type == "function_declarator"
                or (
                    d.type in ("pointer_declarator", "reference_declarator")
                    and any(c.type == "function_declarator" for c in d.children)
                )
                for d in decls
            )
            if not is_method:
                type_node = node.child_by_field_name("type")
                if type_node is not None:
                    line = node.start_point[0] + 1
                    refs: list[tuple[str, str]] = []
                    _cpp_collect_type_refs(type_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "field"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != parent_class_nid:
                            add_edge(
                                parent_class_nid,
                                target_nid,
                                "references",
                                line,
                                context=ctx,
                            )
            # Emit a node for each data member. Use children_by_field_name so we
            # only visit declarator children, not the type node (which would give
            # us the type name, not the field name). Handles int x, y; via
            # multiple declarator fields and static const int MAX = 100; via the
            # init_declarator → field_identifier recursion in _get_cpp_func_name.
            for decl in decls:
                name = _get_cpp_func_name(decl, source)
                if name:
                    line = decl.start_point[0] + 1
                    field_nid = _make_id(parent_class_nid, name)
                    add_node(field_nid, name, line)
                    add_edge(
                        parent_class_nid, field_nid, "defines", line, context="field"
                    )
            return

        # Function types
        if t in config.function_types:
            # Swift deinit/subscript have no name field — resolve before generic fallback
            if t == "deinit_declaration":
                func_name: str | None = "deinit"
            elif t == "subscript_declaration":
                func_name = "subscript"
            elif config.resolve_function_name_fn is not None:
                # C/C++ style: use declarator
                declarator = node.child_by_field_name("declarator")
                func_name = None
                if declarator:
                    func_name = config.resolve_function_name_fn(declarator, source)
            else:
                name_node = node.child_by_field_name(config.name_field)
                func_name = _read_text(name_node, source) if name_node else None

            if not func_name:
                return

            line = node.start_point[0] + 1
            if parent_class_nid:
                func_nid = _make_id(parent_class_nid, func_name)
                add_node(func_nid, f".{func_name}()", line)
                add_edge(parent_class_nid, func_nid, "method", line)
            else:
                func_nid = _make_id(stem, func_name)
                add_node(func_nid, f"{func_name}()", line)
                add_edge(file_nid, func_nid, "contains", line)

            if config.ts_module == "tree_sitter_python":
                params_node = node.child_by_field_name("parameters")
                for ref_name, role in _python_collect_param_refs(params_node, source):
                    ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != func_nid:
                        edges.append(
                            _semantic_reference_edge(
                                func_nid, target_nid, ctx, str_path, line
                            )
                        )
                return_type_node = node.child_by_field_name("return_type")
                if return_type_node is not None:
                    return_refs: list[tuple[str, str]] = []
                    _python_collect_type_refs(
                        return_type_node, source, False, return_refs
                    )
                    for ref_name, role in return_refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            edges.append(
                                _semantic_reference_edge(
                                    func_nid, target_nid, ctx, str_path, line
                                )
                            )

            if config.ts_module in ("tree_sitter_c", "tree_sitter_cpp"):
                collect = (
                    _cpp_collect_type_refs
                    if config.ts_module == "tree_sitter_cpp"
                    else _c_collect_type_refs
                )
                return_node = node.child_by_field_name("type")
                if return_node is not None:
                    refs = []
                    collect(return_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "return_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(
                                func_nid, target_nid, "references", line, context=ctx
                            )
                # function_declarator may be wrapped in pointer/reference declarators
                fn_decl = node.child_by_field_name("declarator")
                while fn_decl is not None and fn_decl.type in (
                    "pointer_declarator",
                    "reference_declarator",
                ):
                    fn_decl = fn_decl.child_by_field_name("declarator")
                if fn_decl is not None and fn_decl.type == "function_declarator":
                    params_node = fn_decl.child_by_field_name("parameters")
                    if params_node is not None:
                        for p in params_node.children:
                            if p.type != "parameter_declaration":
                                continue
                            ptype = p.child_by_field_name("type")
                            if ptype is None:
                                continue
                            refs = []
                            collect(ptype, source, False, refs)
                            for ref_name, role in refs:
                                ctx = (
                                    "generic_arg"
                                    if role == "generic_arg"
                                    else "parameter_type"
                                )
                                target_nid = ensure_named_node(ref_name, line)
                                if target_nid != func_nid:
                                    add_edge(
                                        func_nid,
                                        target_nid,
                                        "references",
                                        line,
                                        context=ctx,
                                    )

            body = _find_body(node, config)
            # JS/TS: capture `this.X = () => {}` / `this.X = function(){}`
            # assigned directly in this function/constructor body. They live
            # inside the body (otherwise only walked for calls), so without this
            # they are never emitted — the dominant miss on constructor-style
            # ("function Foo(){ this.bar = () => {} }") and many CommonJS repos.
            # Owner is the enclosing class when present (a constructor's methods
            # belong to the class), else the function itself.
            if body is not None and config.ts_module in (
                "tree_sitter_javascript",
                "tree_sitter_typescript",
            ):
                this_owner_nid = parent_class_nid if parent_class_nid else func_nid
                for stmt in body.children:
                    if stmt.type != "expression_statement":
                        continue
                    assign = next(
                        (c for c in stmt.children if c.type == "assignment_expression"),
                        None,
                    )
                    if assign is None:
                        continue
                    val = assign.child_by_field_name("right")
                    if val is None or val.type not in _JS_FUNCTION_VALUE_TYPES:
                        continue
                    tgt = _js_member_assignment_target(
                        assign.child_by_field_name("left"), source
                    )
                    if tgt is None or tgt[0] != "this":
                        continue
                    m_name = tgt[2]
                    m_line = stmt.start_point[0] + 1
                    m_nid = _make_id(this_owner_nid, m_name)
                    add_node(m_nid, f".{m_name}()", m_line)
                    add_edge(this_owner_nid, m_nid, "method", m_line)
                    m_body = val.child_by_field_name("body")
                    if m_body:
                        function_bodies.append((m_nid, m_body))
            if body:
                function_bodies.append((func_nid, body))
            return

        # JS/TS arrow functions — language-specific extra handling
        if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
            if _js_extra_walk(
                node,
                source,
                file_nid,
                stem,
                str_path,
                nodes,
                edges,
                seen_ids,
                function_bodies,
                parent_class_nid,
                add_node,
                add_edge,
            ):
                return

        # Python's `@property` / `@staticmethod` / `@classmethod` wrap the
        # inner function_definition in a `decorated_definition` node. The
        # default recurse below clears parent_class_nid, which would cause the
        # inner method to be emitted with a class-unqualified node id (e.g.
        # `file_baz` instead of `file_bar_baz`). That diverges from the
        # class-qualified id the rationale walker uses for the same method's
        # docstring, leaving the rationale edge dangling and the docstring
        # node orphaned (#1050). Treat decorated_definition as a transparent
        # wrapper so parent_class_nid propagates to the real function node.
        if t == "decorated_definition":
            for child in node.children:
                walk(child, parent_class_nid=parent_class_nid)
            return

        # Default: recurse
        for child in node.children:
            walk(child, parent_class_nid=None)

    walk(root)

    # ── Call-graph pass ───────────────────────────────────────────────────────
    label_to_nid: dict[str, str] = {}  # case-sensitive (Go, C#, Java, Kotlin, etc.)
    label_to_nid_ci: dict[str, str] = {}  # case-insensitive (PHP functions/classes)
    for n in nodes:
        raw = n["label"]
        normalised = raw.strip("()").lstrip(".")
        label_to_nid[normalised] = n["id"]
        label_to_nid_ci[normalised.lower()] = n["id"]

    seen_call_pairs: set[tuple[str, str]] = set()
    seen_dyn_import_pairs: set[tuple[str, str]] = set()
    seen_static_ref_pairs: set[tuple[str, str, str]] = set()
    seen_helper_ref_pairs: set[tuple[str, str, str]] = set()
    seen_bind_pairs: set[tuple[str, str, str]] = set()
    raw_calls: list[
        dict[str, Any]
    ] = []  # unresolved calls for cross-file resolution in extract()

    def _php_class_const_scope(n: Node) -> str | None:
        scope = n.child_by_field_name("scope")
        if scope is None:
            for c in n.children:
                if c.is_named and c.type in ("name", "qualified_name", "identifier"):
                    scope = c
                    break
        if scope is None:
            return None
        return _read_text(scope, source)

    def walk_calls(node: Node, caller_nid: str) -> None:
        if node.type in config.function_boundary_types:
            return

        if node.type in config.call_types:
            # JS/TS dynamic imports: await import('./foo.js')
            if config.ts_module in ("tree_sitter_javascript", "tree_sitter_typescript"):
                if _dynamic_import_js(
                    node, source, caller_nid, str_path, edges, seen_dyn_import_pairs
                ):
                    # Still recurse into children (import().then(...) may have calls)
                    for child in node.children:
                        walk_calls(child, caller_nid)
                    return

            callee_name: str | None = None
            is_member_call: bool = False
            member_receiver: str | None = None

            # Special handling per language
            if config.ts_module == "tree_sitter_cpp":
                # C++: function field, then field_expression/qualified_identifier
                func_node = (
                    node.child_by_field_name(config.call_function_field)
                    if config.call_function_field
                    else None
                )
                if func_node:
                    if func_node.type == "identifier":
                        callee_name = _read_text(func_node, source)
                    elif func_node.type in ("field_expression", "qualified_identifier"):
                        is_member_call = True
                        name = func_node.child_by_field_name(
                            "field"
                        ) or func_node.child_by_field_name("name")
                        if name:
                            callee_name = _read_text(name, source)
            else:
                # Generic: get callee from call_function_field
                func_node = (
                    node.child_by_field_name(config.call_function_field)
                    if config.call_function_field
                    else None
                )
                if func_node:
                    if func_node.type == "identifier":
                        callee_name = _read_text(func_node, source)
                    elif func_node.type in config.call_accessor_node_types:
                        is_member_call = True
                        if config.call_accessor_field:
                            attr = func_node.child_by_field_name(
                                config.call_accessor_field
                            )
                            if attr:
                                callee_name = _read_text(attr, source)
                        if config.call_accessor_object_field:
                            # Capture a simple-identifier receiver (e.g. `ClassName`
                            # in `ClassName.method()`) so cross-file member-call
                            # resolution can resolve qualified class-method calls
                            # (#1446). Chained receivers (`a.b.method()`) are skipped.
                            obj = func_node.child_by_field_name(
                                config.call_accessor_object_field
                            )
                            if obj is not None and obj.type == "identifier":
                                member_receiver = _read_text(obj, source)
                    else:
                        # Try reading the node directly (e.g. Java name field is the callee)
                        callee_name = _read_text(func_node, source)

            if callee_name and callee_name not in _LANGUAGE_BUILTIN_GLOBALS:
                # A capitalized-receiver member call (`ClassName.method()`) must defer
                # to receiver-based cross-file resolution: the bare method name can
                # collide with an in-file node — even the calling method itself, when a
                # viewset action delegates to a same-named service action — which would
                # match `tgt_nid == caller_nid` and silently drop the call (#1446). The
                # captured receiver is resolved later in _resolve_python_member_calls.
                if is_member_call and member_receiver and member_receiver[:1].isupper():
                    tgt_nid = None
                else:
                    tgt_nid = label_to_nid.get(callee_name)
                if tgt_nid and tgt_nid != caller_nid:
                    pair = (caller_nid, tgt_nid)
                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        line = node.start_point[0] + 1
                        edges.append(
                            {
                                "source": caller_nid,
                                "target": tgt_nid,
                                "relation": "calls",
                                "context": "call",
                                "confidence": "EXTRACTED",
                                "source_file": str_path,
                                "source_location": f"L{line}",
                                "weight": 1.0,
                            }
                        )
                elif callee_name and not tgt_nid:
                    # Callee not in this file — save for cross-file resolution in extract()
                    raw_calls.append(
                        {
                            "caller_nid": caller_nid,
                            "callee": callee_name,
                            "is_member_call": is_member_call,
                            "source_file": str_path,
                            "source_location": f"L{node.start_point[0] + 1}",
                            "receiver": member_receiver,
                        }
                    )

            # Helper function calls: config('foo.bar') → uses_config edge to "foo"
            if callee_name and callee_name in config.helper_fn_names:
                args_node = node.child_by_field_name("arguments")
                first_key: str | None = None
                if args_node:
                    for arg in args_node.children:
                        if arg.type != "argument":
                            continue
                        for inner in arg.children:
                            if inner.type == "string":
                                for sc in inner.children:
                                    if sc.type == "string_content":
                                        first_key = _read_text(sc, source)
                                        break
                                break
                        if first_key:
                            break
                if first_key:
                    segment = first_key.split(".")[0]
                    tgt_nid = label_to_nid_ci.get(
                        segment.lower()
                    ) or label_to_nid_ci.get(f"{segment}.php".lower())
                    if tgt_nid and tgt_nid != caller_nid:
                        relation = f"uses_{callee_name}"
                        pair3 = (caller_nid, tgt_nid, relation)
                        if pair3 not in seen_helper_ref_pairs:
                            seen_helper_ref_pairs.add(pair3)
                            line = node.start_point[0] + 1
                            edges.append(
                                {
                                    "source": caller_nid,
                                    "target": tgt_nid,
                                    "relation": relation,
                                    "confidence": "EXTRACTED",
                                    "confidence_score": 1.0,
                                    "source_file": str_path,
                                    "source_location": f"L{line}",
                                    "weight": 1.0,
                                }
                            )

            # Service container bindings: $this->app->bind(Foo::class, Bar::class)
            if (
                node.type == "member_call_expression"
                and callee_name
                and callee_name in config.container_bind_methods
            ):
                args_node = node.child_by_field_name("arguments")
                class_args: list[str] = []
                if args_node:
                    for arg in args_node.children:
                        if arg.type != "argument":
                            continue
                        for inner in arg.children:
                            if inner.type == "class_constant_access_expression":
                                cls = _php_class_const_scope(inner)
                                if cls:
                                    class_args.append(cls)
                                break
                        if len(class_args) >= 2:
                            break
                if len(class_args) == 2:
                    contract_name, impl_name = class_args
                    contract_nid = label_to_nid_ci.get(contract_name.lower())
                    impl_nid = label_to_nid_ci.get(impl_name.lower())
                    if contract_nid and impl_nid and contract_nid != impl_nid:
                        pair3 = (contract_nid, impl_nid, "bound_to")
                        if pair3 not in seen_bind_pairs:
                            seen_bind_pairs.add(pair3)
                            line = node.start_point[0] + 1
                            edges.append(
                                {
                                    "source": contract_nid,
                                    "target": impl_nid,
                                    "relation": "bound_to",
                                    "confidence": "EXTRACTED",
                                    "confidence_score": 1.0,
                                    "source_file": str_path,
                                    "source_location": f"L{line}",
                                    "weight": 1.0,
                                }
                            )

        # Static property access: Foo::$bar → uses_static_prop edge
        if node.type in config.static_prop_types:
            scope_node = node.child_by_field_name("scope")
            if scope_node is None:
                for child in node.children:
                    if child.is_named and child.type in (
                        "name",
                        "qualified_name",
                        "identifier",
                    ):
                        scope_node = child
                        break
            if scope_node is not None:
                class_name = _read_text(scope_node, source)
                tgt_nid = label_to_nid_ci.get(class_name.lower())
                if tgt_nid and tgt_nid != caller_nid:
                    pair3 = (caller_nid, tgt_nid, "uses_static_prop")
                    if pair3 not in seen_static_ref_pairs:
                        seen_static_ref_pairs.add(pair3)
                        line = node.start_point[0] + 1
                        edges.append(
                            {
                                "source": caller_nid,
                                "target": tgt_nid,
                                "relation": "uses_static_prop",
                                "confidence": "EXTRACTED",
                                "confidence_score": 1.0,
                                "source_file": str_path,
                                "source_location": f"L{line}",
                                "weight": 1.0,
                            }
                        )

        for child in node.children:
            walk_calls(child, caller_nid)

    for caller_nid, body_node in function_bodies:
        walk_calls(body_node, caller_nid)

    # #1356: walk property/field initializers (collected above). walk_calls
    # self-guards against re-entering function bodies and dedups via
    # seen_call_pairs, so a closure inside an initializer is not double-walked.
    for owner_nid, init_node in initializer_nodes:
        walk_calls(init_node, owner_nid)

    # ── Event listener pass ───────────────────────────────────────────────────
    seen_listen_pairs: set[tuple[str, str]] = set()
    for event_name, listener_name, line in pending_listen_edges:
        event_nid = label_to_nid_ci.get(event_name.lower())
        listener_nid = label_to_nid_ci.get(listener_name.lower())
        if not event_nid or not listener_nid or event_nid == listener_nid:
            continue
        pair2 = (event_nid, listener_nid)
        if pair2 in seen_listen_pairs:
            continue
        seen_listen_pairs.add(pair2)
        edges.append(
            {
                "source": event_nid,
                "target": listener_nid,
                "relation": "listened_by",
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": str_path,
                "source_location": f"L{line}",
                "weight": 1.0,
            }
        )

    # ── Clean edges ───────────────────────────────────────────────────────────
    valid_ids = seen_ids
    clean_edges = []
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in valid_ids and (
            tgt in valid_ids
            or edge["relation"] in ("imports", "imports_from", "re_exports")
        ):
            clean_edges.append(edge)

    result = {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}
    return result
