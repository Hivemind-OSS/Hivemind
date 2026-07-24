"""C++ backend for the generic tree-sitter driver — CONSERVATIVE fidelity.

C++'s declaration model is the richest of the systems languages, so this backend
maps it onto the language-neutral `Interface` deliberately and conservatively:

- a `function_definition` (with a body) and a bodiless prototype `declaration`
  are func-shaped; a `class_specifier`/`struct_specifier` is the class-like shape
  (its "bases" are the `base_class_clause` targets);
- a method is resolved by its qualified name `Class::method`: an inline body or a
  bodiless declaration inside the class body, AND an out-of-line definition
  (`ReturnType Class::method(...) { ... }`, whose declarator is a
  `qualified_identifier`) both resolve as belonging to `Class`;
- `namespace ns { ... }` qualifies the names inside it, so a namespaced entity is
  anchored qualified (`ns::thing`, `ns::Class::method`) — the C++ scope operator
  `::` is the separator, mirroring how Go/Rust express a dotted `Type.method`;
- a trailing `...` (the C-style variadic) sets `has_star`; a defaulted parameter
  (`int b = 2`) is `optional_parameter_declaration` and counts toward max but not
  required; `(void)` and `()` are both zero parameters. Templates are ignored
  (conservative), so a generic and non-generic form of the same function match.

**Overloading is the safety valve.** If a name resolves to MORE THAN ONE
declaration in the file (an overload set — same name, different parameter lists),
the generic driver leaves `interface=None`, so the symbol reads found-but-
unverifiable and is NEVER reported as a breaking signature change on the strength
of one overload's shape. The shape fingerprint is emitted only for an unambiguous
single declaration. Full overload/template signature modeling is deferred (§8).

Because a C++ member can legitimately be supplied from OUTSIDE what this file
holds — a method declared in a class body here but DEFINED out-of-line in another
translation unit, a member added where an OPEN namespace is reopened elsewhere, a
name from a `#include`d header, a macro this tool does not expand — an absent
qualified name whose enclosing class or namespace is present here, and any bare
name that still occurs anywhere in the file's text, read as an indirection
(unverifiable), NEVER `missing`. Only a name with zero footing in the file is
provably absent from it. That is the Law-1 false-stale guard for C++. This module
supplies only observations; the generic driver in `combdrift.symbols` owns the
found/missing/indirect/parse_error/ambiguous tri-state.
"""

from __future__ import annotations

from typing import Iterator

import tree_sitter_cpp as _ts_cpp
from tree_sitter import Language, Node

from hive.combdrift.fingerprint import Interface
from hive.combdrift.langs.base import (
    LangSpec,
    ReexportEdge,
    node_name,
    node_text,
    walk_nodes,
)

# The class-like type declarations that resolve as a symbol and carry a class
# interface (base_count from their base_class_clause).
_CPP_TYPE_NODES = frozenset({"class_specifier", "struct_specifier"})
# The two hosts that declare a top-level/namespaced FREE function by name: one
# with a body, one a bodiless prototype (a `declaration` counts only when its
# declarator resolves to a `function_declarator`).
_CPP_FUNCTION_HOSTS = frozenset({"function_definition", "declaration"})
# The two hosts for a member inside a class body: an inline `function_definition`
# and a bodiless `field_declaration` prototype.
_CPP_MEMBER_HOSTS = frozenset({"function_definition", "field_declaration"})
# The declaration forms a `template_declaration` may wrap; the template header is
# transparent to lookup, so a generic function/class resolves like a plain one and
# making a symbol generic (adding `<T>`) is never a drift — the `<T>` params live
# outside the parameter list and never reach the arity count.
_CPP_TEMPLATABLE = (
    _CPP_TYPE_NODES | _CPP_FUNCTION_HOSTS | frozenset({"field_declaration"})
)
# Declarator wrappers around the `function_declarator`: a returned pointer/
# reference (`int *f()`, `int &f()`). Unwrapping these reaches the callable.
_CPP_DECLARATOR_WRAPPERS = frozenset({"pointer_declarator", "reference_declarator"})
# Leaf nodes whose text is a C++ identifier occurrence (a use, a call target, a
# binding name), used for the textual-occurrence half of the Law-1 guard.
_CPP_IDENT_NODES = frozenset(
    {"identifier", "field_identifier", "type_identifier", "namespace_identifier"}
)


# --- symbol resolution: a scope-qualified declaration walk --------------------


def _find_cpp_symbols(root: Node, symbol: str) -> list[Node]:
    """Every declaration matching `symbol` (>1 means an ambiguous overload set).

    A bare name resolves to a GLOBAL free function or a global class/struct — a
    namespaced or member entity is anchored qualified. A qualified name splits on
    `::` into an enclosing scope (namespaces and/or a class) and a final member;
    it matches an in-class member, an out-of-line definition, or a namespaced free
    function whose reconstructed scope equals the anchor's. More than one match
    (an overload set, or a same-file declaration + out-of-line definition) leaves
    the shape ambiguous — the driver emits no single interface.
    """
    scope_q, member = _split_qualified(symbol)
    return [
        node
        for scope, name, node in _iter_scoped_decls(root, ())
        if name == member and scope == scope_q
    ]


def _split_qualified(symbol: str) -> tuple[tuple[str, ...], str]:
    """Split `A::B::C` into (("A","B"), "C"); a bare name into ((), name)."""
    parts = tuple(p for p in symbol.split("::") if p)
    if len(parts) <= 1:
        return (), symbol
    return parts[:-1], parts[-1]


def _iter_scoped_decls(
    container: Node, prefix: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], str, Node]]:
    """Yield (scope, name, node) for every resolvable declaration under container.

    `container` is a translation_unit, a namespace `declaration_list`, or a class
    `field_declaration_list`; `prefix` is the scope it sits in. A namespace
    extends the scope by its segments; a class yields itself (a type) and recurses
    for its members; a free function/prototype yields at `prefix` extended by any
    qualified-declarator scope (the out-of-line `Class::method` case).
    """
    for raw in container.named_children:
        child = _unwrap_template(raw)
        ctype = child.type
        if ctype == "namespace_definition":
            body = child.child_by_field_name("body")
            if body is not None:
                yield from _iter_scoped_decls(body, prefix + _ns_segments(child))
        elif ctype == "linkage_specification":
            # `extern "C" { ... }` is transparent to scope.
            body = child.child_by_field_name("body")
            if body is not None:
                yield from _iter_scoped_decls(body, prefix)
        elif ctype in _CPP_TYPE_NODES:
            name = node_name(child)
            if name is not None:
                yield prefix, name, child
                body = child.child_by_field_name("body")
                if body is not None:
                    yield from _iter_class_members(body, prefix + (name,))
        elif ctype in _CPP_FUNCTION_HOSTS:
            info = _declarator_info(child)
            if info is not None:
                scope_segs, name = info
                yield prefix + scope_segs, name, child


def _iter_class_members(
    body: Node, prefix: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], str, Node]]:
    """Yield (scope, name, node) for the members declared in a class body."""
    for raw in body.named_children:
        child = _unwrap_template(raw)
        ctype = child.type
        if ctype in _CPP_MEMBER_HOSTS:
            info = _declarator_info(child)
            if info is not None:
                scope_segs, name = info
                yield prefix + scope_segs, name, child
        elif ctype in _CPP_TYPE_NODES:
            type_name = node_name(child)
            if type_name is not None:
                yield prefix, type_name, child
                inner = child.child_by_field_name("body")
                if inner is not None:
                    yield from _iter_class_members(inner, prefix + (type_name,))


def _unwrap_template(node: Node) -> Node:
    """A `template_declaration` is transparent: the declaration it wraps, else node."""
    if node.type != "template_declaration":
        return node
    for child in node.named_children:
        if child.type in _CPP_TEMPLATABLE:
            return child
    return node


def _ns_segments(ns_node: Node) -> tuple[str, ...]:
    """The scope segments a `namespace_definition` contributes ((), if anonymous).

    A plain `namespace ns` is one segment; a C++17 `namespace a::b` carries a
    `nested_namespace_specifier` whose text splits on `::`; an anonymous namespace
    has no name field and adds no qualification.
    """
    name = ns_node.child_by_field_name("name")
    text = node_text(name) if name is not None else None
    return tuple(seg for seg in text.split("::")) if text else ()


def _declarator_info(node: Node) -> tuple[tuple[str, ...], str] | None:
    """(qualified-scope segments, final name) for a function host, else None.

    None for anything that is not a named function — a data member, a variable
    `declaration`, a function pointer, an operator/destructor/conversion name we
    do not model. A plain declarator adds no scope; a `qualified_identifier`
    declarator (an out-of-line definition) contributes the `Class::` / `ns::`
    scope it names.
    """
    fdecl = _function_declarator(node)
    if fdecl is None:
        return None
    return _name_from_declarator(fdecl.child_by_field_name("declarator"))


def _function_declarator(node: Node) -> Node | None:
    """The `function_declarator` a host declares, through pointer/reference wrappers."""
    decl = node.child_by_field_name("declarator")
    while decl is not None:
        if decl.type == "function_declarator":
            return decl
        if decl.type in _CPP_DECLARATOR_WRAPPERS:
            decl = decl.child_by_field_name("declarator")
        else:
            return None
    return None


def _name_from_declarator(name_node: Node | None) -> tuple[tuple[str, ...], str] | None:
    """Map a function_declarator's inner declarator to (scope segments, name).

    A plain `identifier`/`field_identifier` is an unscoped name; a
    `qualified_identifier` flattens to its `Class::`/`ns::` scope plus final name.
    Any other form (operator/destructor/conversion/function-pointer) is left
    unresolved (None) — conservative, so it never matches as a simple callable.
    """
    if name_node is None:
        return None
    if name_node.type in ("identifier", "field_identifier"):
        text = node_text(name_node)
        return ((), text) if text else None
    if name_node.type == "qualified_identifier":
        return _flatten_qualified(name_node)
    return None


def _flatten_qualified(qid: Node) -> tuple[tuple[str, ...], str] | None:
    """Flatten `A::B::c` into (("A","B"), "c"); returns None if it has no final name."""
    segments: list[str] = []
    node: Node | None = qid
    while node is not None and node.type == "qualified_identifier":
        scope = node.child_by_field_name("scope")
        scope_text = node_text(scope) if scope is not None else None
        if scope_text:
            segments.extend(scope_text.split("::"))
        node = node.child_by_field_name("name")
    final = node_text(node) if node is not None else None
    if not final:
        return None
    if "::" in final:  # defensive: a scope that parsed onto the final leaf
        *more, final = final.split("::")
        segments.extend(more)
    return tuple(segments), final


# --- Interface extraction (Layer B) ------------------------------------------


def _cpp_interface(node: Node) -> Interface:
    """A language-neutral call-shape descriptor for one found C++ declaration."""
    if node.type in _CPP_TYPE_NODES:
        return Interface(
            category="class",
            is_generator=False,
            req_positional=0,
            max_positional=0,
            has_star=False,
            has_kw=False,
            req_kwonly=0,
            contract_decorators=frozenset(),
            base_count=_cpp_base_count(node),
        )
    req, maximum, has_star = _cpp_param_counts(_function_declarator(node))
    return Interface(
        category="func",
        is_generator=False,  # C++ has no generator declaration form in v1
        req_positional=req,
        max_positional=maximum,
        has_star=has_star,
        has_kw=False,  # C++ has no **kwargs equivalent
        req_kwonly=0,  # C++ has no keyword-only parameters
        contract_decorators=frozenset(),
        base_count=0,
    )


def _cpp_param_counts(fdecl: Node | None) -> tuple[int, int, bool]:
    """Return (required, max positional, has variadic) for a function declarator.

    A plain `parameter_declaration` is required (req and max); an
    `optional_parameter_declaration` (a defaulted parameter) counts toward max but
    not required, so adding a default is additive, never breaking. A lone `(void)`
    and an empty `()` are both zero parameters; a trailing `...` is an unnamed
    token in the parameter list and sets has_star, counting toward neither total.
    """
    if fdecl is None:
        return 0, 0, False
    params = fdecl.child_by_field_name("parameters")
    if params is None:
        return 0, 0, False
    # The C-style variadic `...` is an unnamed token in the parameter_list, so it
    # is only visible among all children, not the named ones.
    has_star = any(child.type == "..." for child in params.children)
    required = maximum = 0
    for child in params.named_children:
        if child.type == "parameter_declaration":
            if _is_void_marker(child):
                continue
            required += 1
            maximum += 1
        elif child.type == "optional_parameter_declaration":
            maximum += 1
        elif child.type == "variadic_parameter_declaration":
            has_star = True
    return required, maximum, has_star


def _is_void_marker(param_decl: Node) -> bool:
    """True for the lone `void` that marks an explicit zero-parameter list.

    `f(void)` carries a single parameter_declaration whose type is `void` and
    which has no declarator; `f(void *p)` has a declarator, so it is a real
    pointer parameter, not the sentinel.
    """
    if param_decl.child_by_field_name("declarator") is not None:
        return False
    type_node = param_decl.child_by_field_name("type")
    return type_node is not None and node_text(type_node) == "void"


def _cpp_base_count(class_node: Node) -> int:
    """Count the base classes on a class/struct (0 when it has no base clause)."""
    clause = next(
        (c for c in class_node.named_children if c.type == "base_class_clause"), None
    )
    if clause is None:
        return 0
    # A base is a type target; an `access_specifier` (public/private/protected)
    # and the `virtual` keyword are qualifiers, not bases.
    return sum(1 for c in clause.named_children if c.type != "access_specifier")


# --- Indirection mechanism (the Law-1 false-stale observations) ---------------


def _cpp_indirect_detail(
    root: Node, path: str, name: str
) -> tuple[str | None, ReexportEdge | None]:
    """The mechanism making an absent `name` unverifiable rather than deleted.

    None means the name has zero footing in this file — provable absence
    (→ missing → stale). A non-None detail means C++'s out-of-file supply routes
    leave absence unprovable:

    - a qualified `Class::member` whose class is present here may be DEFINED
      out-of-line in another translation unit (`member_out_of_line`); a
      `ns::member` whose namespace is present may be added where that OPEN
      namespace is reopened elsewhere (`namespace_reopened`);
    - a bare name bound as a file-scope variable, `typedef`, or `using`-alias is
      present but not a callable (`noncallable`); a `#define`d name is present but
      not a fingerprinted callable (`macro`); a name declared only inside a
      namespace/class is present but not a bare global callable (`scoped`);
    - any name that still occurs anywhere in the file's text — a call site, a
      qualified-id use, a template argument — may come from a `#include`d header
      or a macro this tool does not resolve, so its absence is not provable
      (`referenced`).

    C++ carries no followable re-export edge combdrift resolves, so the second
    element is always None.
    """
    scan = _CppScan(root)
    scope_q, member = _split_qualified(name)
    if scope_q:
        container = scope_q[-1]
        if container in scan.types:
            return "member_out_of_line", None
        if container in scan.namespaces:
            return "namespace_reopened", None
        if member in scan.occurrences or name in scan.occurrences:
            return "referenced", None
        return None, None
    if name in scan.macros:
        return "macro", None
    if name in scan.noncallable:
        return "noncallable", None
    if name in scan.declared or name in scan.types:
        return "scoped", None
    if name in scan.occurrences:
        return "referenced", None
    return None, None


class _CppScan:
    """One walk of the tree gathering the sets the indirect guard consults."""

    def __init__(self, root: Node) -> None:
        self.types: set[str] = set()  # class/struct names, any scope
        self.namespaces: set[str] = set()  # namespace segment names
        self.declared: set[str] = set()  # function/method names, any scope
        self.macros: set[str] = set()  # #define names
        self.noncallable: set[str] = set()  # file-scope vars, typedefs, using-aliases
        self.occurrences: set[str] = set()  # every identifier + qualified-id text
        for node in walk_nodes(root):
            self._visit(node)

    def _visit(self, node: Node) -> None:
        ntype = node.type
        if ntype in _CPP_TYPE_NODES:
            name = node_name(node)
            if name is not None:
                self.types.add(name)
        elif ntype == "namespace_definition":
            self.namespaces.update(_ns_segments(node))
        elif ntype in ("preproc_def", "preproc_function_def"):
            macro = node.child_by_field_name("name")
            text = node_text(macro) if macro is not None else None
            if text is not None:
                self.macros.add(text)
        elif ntype == "alias_declaration":  # using Id = ...;
            name = node_name(node)
            if name is not None:
                self.noncallable.add(name)
        elif ntype == "type_definition":  # typedef ...;
            self.noncallable.update(_declaration_binding_names(node))
        elif ntype in ("function_definition", "field_declaration"):
            info = _declarator_info(node)
            if info is not None:
                self.declared.add(info[1])
        elif ntype == "declaration":
            info = _declarator_info(node)
            if info is not None:
                self.declared.add(info[1])
            else:  # a non-function declaration binds variables
                self.noncallable.update(_declaration_binding_names(node))
        if ntype == "qualified_identifier":
            text = node_text(node)
            if text is not None:
                self.occurrences.add(text)
        elif ntype in _CPP_IDENT_NODES:
            text = node_text(node)
            if text is not None:
                self.occurrences.add(text)


def _declaration_binding_names(decl: Node) -> list[str]:
    """Every bare name a non-function declaration/typedef binds."""
    names: list[str] = []
    for i in range(decl.child_count):
        if decl.field_name_for_child(i) == "declarator":
            bound = _declarator_binding_name(decl.child(i))
            if bound is not None:
                names.append(bound)
    return names


def _declarator_binding_name(declarator: Node | None) -> str | None:
    """The bare identifier a data declarator finally binds, through wrappers."""
    node = declarator
    while node is not None:
        if node.type in ("identifier", "type_identifier", "field_identifier"):
            text = node_text(node)
            return text
        if node.type in _CPP_DECLARATOR_WRAPPERS or node.type in (
            "array_declarator",
            "init_declarator",
        ):
            node = node.child_by_field_name("declarator")
        else:
            return None
    return None


CPP = LangSpec(
    grammar=Language(_ts_cpp.language()),
    find_symbols=_find_cpp_symbols,
    extract_interface=_cpp_interface,
    indirect_detail=_cpp_indirect_detail,
)
