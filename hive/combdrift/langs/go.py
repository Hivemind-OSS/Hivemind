"""Go backend for the generic tree-sitter driver.

Go's idioms differ from the class-based languages, so this backend maps them onto
the language-neutral `Interface` deliberately:

- a free function is a `function_declaration`; a method is a `method_declaration`
  keyed on its RECEIVER type, not on a class body (Go has no class body);
- a `type_declaration` whose spec is a struct or interface is the class-like
  shape, and its "bases" are the EMBEDDED types (Go embedding, not inheritance);
- a variadic `...T` parameter is the `has_star` flag; Go has no default,
  optional, or keyword parameters, so every non-variadic parameter is required.

Because a Go package spans several files and combdrift sees only one, absence is
rarely provable: a name used-but-not-declared here, a name a dot/blank import
could inject, and any absent `Type.method` (methods live at package scope, in any
file) all read as an indirection (unverifiable), never `missing`. That is the
Law-1 false-stale guard for Go. This module supplies only observations; the
generic driver in `combdrift.symbols` owns the found/missing/indirect/parse_error/
ambiguous tri-state.
"""

from __future__ import annotations

import tree_sitter_go as _ts_go
from tree_sitter import Language, Node

from hive.combdrift.fingerprint import Interface
from hive.combdrift.langs.base import (
    LangSpec,
    ReexportEdge,
    node_name,
    node_text,
    walk_nodes,
)

# A free function and a method both extract a func-shaped interface (the method's
# receiver is not part of its call arity).
_GO_FUNCTION_NODES = frozenset({"function_declaration", "method_declaration"})
# The named forms inside a `type ( ... )` group: a concrete type or a `= alias`.
_GO_TYPE_SPEC_NODES = frozenset({"type_spec", "type_alias"})
# Leaf nodes whose text is a Go identifier occurrence (a use or a non-top-level
# binding), used to decide "referenced here but declared in a sibling file".
_GO_IDENT_NODES = frozenset({"identifier", "type_identifier", "field_identifier"})


def _find_go_symbols(root: Node, symbol: str) -> list[Node]:
    """Top-level declarations matching `symbol` (>1 means an ambiguous shape).

    A bare name resolves to a top-level `func` or a named type (struct, interface,
    or any `type` spec/alias). A dotted "Type.method" resolves to a
    `method_declaration` whose receiver type is `Type` and whose name is the
    member — Go methods are keyed on the receiver, never nested in a class body.
    """
    if "." in symbol:
        type_name, _, member = symbol.partition(".")
        return [
            child
            for child in root.named_children
            if child.type == "method_declaration"
            and node_name(child) == member
            and _receiver_type_name(child) == type_name
        ]
    matches: list[Node] = []
    for child in root.named_children:
        if child.type == "function_declaration" and node_name(child) == symbol:
            matches.append(child)
        elif child.type == "type_declaration":
            matches.extend(
                spec
                for spec in child.named_children
                if spec.type in _GO_TYPE_SPEC_NODES and node_name(spec) == symbol
            )
    return matches


def _receiver_type_name(method_node: Node) -> str | None:
    """The base type name a method is declared on, dereferencing `*T`/`T[…]`."""
    receiver = method_node.child_by_field_name("receiver")
    if receiver is None:
        return None
    for param in receiver.named_children:
        if param.type == "parameter_declaration":
            return _base_type_name(param.child_by_field_name("type"))
    return None


def _base_type_name(type_node: Node | None) -> str | None:
    """The first `type_identifier` under a type node (sees through `*` / generics)."""
    if type_node is None:
        return None
    for node in walk_nodes(type_node):
        if node.type == "type_identifier":
            return node_text(node)
    return None


# --- Interface extraction (Layer B) ------------------------------------------


def _go_interface(node: Node) -> Interface:
    """A language-neutral call-shape descriptor for one found Go declaration."""
    if node.type in _GO_FUNCTION_NODES:
        req, maximum, has_star = _go_param_counts(
            node.child_by_field_name("parameters")
        )
        return Interface(
            category="func",
            is_generator=False,
            req_positional=req,
            max_positional=maximum,
            has_star=has_star,
            has_kw=False,  # Go has no **kwargs equivalent
            req_kwonly=0,  # Go has no keyword-only parameters
            contract_decorators=frozenset(),
            base_count=0,
        )
    # A named type: class-shaped, its bases are the embedded types.
    return Interface(
        category="class",
        is_generator=False,
        req_positional=0,
        max_positional=0,
        has_star=False,
        has_kw=False,
        req_kwonly=0,
        contract_decorators=frozenset(),
        base_count=_go_embed_count(node),
    )


def _go_param_counts(params: Node | None) -> tuple[int, int, bool]:
    """Return (required, max positional, has variadic) for a parameter list.

    Go has no optional or default parameters, so required == max for the
    non-variadic parameters; a trailing `...T` sets has_star and, like Python's
    `*args`, counts toward neither total.
    """
    if params is None:
        return 0, 0, False
    total = 0
    has_star = False
    for child in params.named_children:
        if child.type == "variadic_parameter_declaration":
            has_star = True
        elif child.type == "parameter_declaration":
            total += _param_name_count(child)
    return total, total, has_star


def _param_name_count(param_decl: Node) -> int:
    """Positional slots a `parameter_declaration` contributes.

    One `parameter_declaration` may share a type across several names
    (`a, b int` == two parameters); an unnamed declaration (`int`, as in a
    signature-only type) is a single positional slot.
    """
    names = sum(
        1
        for i in range(param_decl.child_count)
        if param_decl.field_name_for_child(i) == "name"
    )
    return names if names > 0 else 1


def _go_embed_count(spec_node: Node) -> int:
    """Count embedded types on a struct/interface spec (0 for any other type)."""
    type_node = spec_node.child_by_field_name("type")
    if type_node is None:
        return 0
    if type_node.type == "struct_type":
        for body in type_node.named_children:
            if body.type == "field_declaration_list":
                # An embedded field has a type but no field name.
                return sum(
                    1
                    for field in body.named_children
                    if field.type == "field_declaration"
                    and field.child_by_field_name("name") is None
                )
        return 0
    if type_node.type == "interface_type":
        # An embedded interface is a `type_elem`; a `method_elem` is a method.
        return sum(1 for elem in type_node.named_children if elem.type == "type_elem")
    return 0


# --- Indirection mechanism (the Law-1 false-stale observations) ---------------


def _go_indirect_detail(
    root: Node, path: str, name: str
) -> tuple[str | None, ReexportEdge | None]:
    """The mechanism making an absent `name` unverifiable rather than deleted.

    None means the name is bound and used nowhere in this file and no opaque
    import could supply it — provable absence (→ missing → stale). A non-None
    detail means Go's package model leaves absence unprovable:

    - a dotted `Type.method` may be declared on the type in another file of the
      same package (methods are package-scoped), so it is never provably absent;
    - a bare name bound as a package-level `var`/`const` is present but not a
      callable/type (`noncallable`);
    - a bare name used in this file but not declared here is supplied by a sibling
      file of the same package (`same_package`);
    - a dot import (`import . "pkg"`) can inject any exported name (`dot_import`);
    - a blank import (`import _ "pkg"`) marks reliance on an external package whose
      surface combdrift cannot see, so absence stays conservatively unverifiable
      (`blank_import`).

    Go carries no followable re-export edge combdrift resolves, so the second
    element is always None.
    """
    if "." in name:
        return "same_package", None

    var_bound: set[str] = set()
    referenced: set[str] = set()
    has_dot_import = False
    has_blank_import = False
    for node in walk_nodes(root):
        node_type = node.type
        if node_type in ("var_spec", "const_spec"):
            bound = node_name(node)
            if bound is not None:
                var_bound.add(bound)
        elif node_type == "import_spec":
            kind = _import_spec_kind(node)
            if kind == "dot":
                has_dot_import = True
            elif kind == "blank":
                has_blank_import = True
        elif node_type in _GO_IDENT_NODES:
            text = node_text(node)
            if text is not None:
                referenced.add(text)

    if name in var_bound:
        return "noncallable", None
    if name in referenced:
        return "same_package", None
    if has_dot_import:
        return "dot_import", None
    if has_blank_import:
        return "blank_import", None
    return None, None


def _import_spec_kind(spec: Node) -> str:
    """Classify an import spec by its name field: dot, blank, alias, or plain."""
    name_node = spec.child_by_field_name("name")
    if name_node is None:
        return "plain"
    if name_node.type == "dot":
        return "dot"
    if name_node.type == "blank_identifier":
        return "blank"
    return "alias"


GO = LangSpec(
    grammar=Language(_ts_go.language()),
    find_symbols=_find_go_symbols,
    extract_interface=_go_interface,
    indirect_detail=_go_indirect_detail,
)
