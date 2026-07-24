"""C backend for the generic tree-sitter driver.

C's declaration model is flat — no classes, no methods, no module system this
tool resolves — so this backend maps it onto the language-neutral `Interface`
deliberately:

- a `function_definition` (a function WITH a body) and a bodiless prototype
  `declaration` (a header-style forward declaration whose declarator is a
  `function_declarator`) are both func-shaped and resolve as the symbol; a
  file-scope `declaration` that binds a variable or a `type_definition` (typedef)
  is present but not a callable we fingerprint;
- an explicit `(void)` parameter list, and an empty K&R `()` list, are both zero
  parameters; a trailing `...` is a `variadic_parameter` and sets `has_star`. C
  has no default, optional, or keyword parameter, so every plain parameter is
  required (req == max). Declarations guarded by `#ifdef`/`#if` are still at file
  scope, so this backend sees through those preprocessor wrappers when locating a
  declaration.

Because a C name can legitimately be supplied from OUTSIDE what this tool parses —
declared in a `#include`d header not in this snapshot, produced by a macro this
tool does not expand, or reachable only through a conditional-compilation branch —
an absent name that still occurs anywhere in the file's text (a call site, an
`#ifdef` operand, a macro invocation, a variable/typedef binding) reads as an
indirection (unverifiable), NEVER `missing`. Only a name with ZERO textual
occurrence in the file is provably absent from it — the anchor points at THIS
file, and a name that appears nowhere in it is genuinely not here. That textual-
occurrence line is the PRIMARY Law-1 false-stale guard for C. This module supplies
only observations; the generic driver in `combdrift.symbols` owns the found/
missing/indirect/parse_error/ambiguous tri-state.
"""

from __future__ import annotations

from collections.abc import Iterator

import tree_sitter_c as _ts_c
from tree_sitter import Language, Node

from hive.combdrift.fingerprint import Interface
from hive.combdrift.langs.base import LangSpec, ReexportEdge, node_text, walk_nodes

# The two forms that declare a top-level function by name: one with a body, one a
# bodiless prototype. A prototype shares the `declaration` node with variables, so
# a declaration only counts when its declarator resolves to a `function_declarator`.
_C_FUNCTION_HOSTS = frozenset({"function_definition", "declaration"})
# Preprocessor conditionals wrap file-scope declarations without nesting them in a
# new scope; a declaration guarded by one is still top-level, so lookup sees
# through them. `#ifndef` also parses as `preproc_ifdef`; `#elif`/`#else` branches
# nest as children of their `#if`/`#elif`, so recursion reaches every branch.
_C_PREPROC_WRAPPERS = frozenset(
    {"preproc_ifdef", "preproc_if", "preproc_elif", "preproc_else"}
)
# Declarator wrappers around the identifier a function/variable is finally named
# by: a returned pointer, an array, an initializer, or parentheses (a function
# pointer). Unwrapping these reaches the bare name a declaration binds.
_C_DECLARATOR_WRAPPERS = frozenset(
    {
        "pointer_declarator",
        "array_declarator",
        "init_declarator",
        "parenthesized_declarator",
    }
)
# Leaf nodes whose text is a C identifier occurrence (a use, a call target, an
# `#ifdef` operand, a binding name), used for the textual-occurrence guard.
_C_IDENT_NODES = frozenset({"identifier", "type_identifier", "field_identifier"})


def _find_c_symbols(root: Node, symbol: str) -> list[Node]:
    """Top-level function definitions/prototypes named `symbol` (>1 is ambiguous).

    C has no methods or classes, so there is no dotted "Type.method" case — a
    dotted anchor matches nothing here and defers to `indirect_detail`. A match is
    a `function_definition` or a bodiless prototype `declaration` (its declarator
    resolves to a `function_declarator` whose name is `symbol`), located at file
    scope INCLUDING inside `#ifdef`/`#if` branches, so a conditionally-compiled
    function is found rather than read as deleted.
    """
    if "." in symbol:
        return []
    matches: list[Node] = []
    for decl in _c_file_scope_decls(root):
        if decl.type not in _C_FUNCTION_HOSTS:
            continue
        fdecl = _c_function_declarator(decl)
        if fdecl is not None and _c_function_name(fdecl) == symbol:
            matches.append(decl)
    return matches


def _c_file_scope_decls(root: Node) -> Iterator[Node]:
    """Yield file-scope declarations, seeing through preprocessor conditionals."""
    for child in root.named_children:
        yield from _c_unwrap_preproc(child)


def _c_unwrap_preproc(node: Node) -> Iterator[Node]:
    """Yield `node`, or the declarations inside it if it is a preproc conditional."""
    if node.type in _C_PREPROC_WRAPPERS:
        for child in node.named_children:
            yield from _c_unwrap_preproc(child)
        return
    yield node


def _c_function_declarator(node: Node) -> Node | None:
    """The `function_declarator` a definition/declaration declares, else None.

    Unwraps a returned-pointer `pointer_declarator` so `char *f(...)` resolves like
    `int f(...)`. A variable, typedef, or initializer declarator resolves to None
    (not a function), so it is never matched as a callable.
    """
    decl = node.child_by_field_name("declarator")
    while decl is not None:
        if decl.type == "function_declarator":
            return decl
        if decl.type == "pointer_declarator":
            decl = decl.child_by_field_name("declarator")
        else:
            return None
    return None


def _c_function_name(fdecl: Node) -> str | None:
    """The bare name a `function_declarator` binds (None for a function pointer).

    A plain function's inner declarator is the `identifier`; a function-pointer
    declaration wraps it in a `parenthesized_declarator`, which is a data binding,
    not a function we resolve — so its name is left unreadable here on purpose.
    """
    inner = fdecl.child_by_field_name("declarator")
    if inner is not None and inner.type == "identifier":
        return node_text(inner)
    return None


# --- Interface extraction (Layer B) ------------------------------------------


def _c_interface(node: Node) -> Interface:
    """A language-neutral call-shape descriptor for one found C function."""
    req, maximum, has_star = _c_param_counts(_c_function_declarator(node))
    return Interface(
        category="func",
        is_generator=False,  # C has no generators
        req_positional=req,
        max_positional=maximum,
        has_star=has_star,
        has_kw=False,  # C has no **kwargs equivalent
        req_kwonly=0,  # C has no keyword-only parameters
        contract_decorators=frozenset(),
        base_count=0,
    )


def _c_param_counts(fdecl: Node | None) -> tuple[int, int, bool]:
    """Return (required, max positional, has variadic) for a function declarator.

    C has no optional/default/keyword parameter, so required == max over the plain
    `parameter_declaration` nodes. A lone `(void)` and an empty K&R `()` are both
    zero parameters; a `variadic_parameter` (`...`) sets has_star and, like
    Python's `*args`, counts toward neither total.
    """
    if fdecl is None:
        return 0, 0, False
    params = fdecl.child_by_field_name("parameters")
    if params is None:
        return 0, 0, False
    total = 0
    has_star = False
    for child in params.named_children:
        if child.type == "variadic_parameter":
            has_star = True
        elif child.type == "parameter_declaration" and not _is_void_marker(child):
            total += 1
    return total, total, has_star


def _is_void_marker(param_decl: Node) -> bool:
    """True for the lone `void` that marks an explicit zero-parameter list.

    `f(void)` carries a single parameter_declaration whose type is `void` and which
    has no declarator; `f(void *p)` has a declarator, so it is a real pointer
    parameter, not the sentinel.
    """
    if param_decl.child_by_field_name("declarator") is not None:
        return False
    type_node = param_decl.child_by_field_name("type")
    return type_node is not None and node_text(type_node) == "void"


# --- Indirection mechanism (the Law-1 false-stale observations) ---------------


def _c_indirect_detail(
    root: Node, path: str, name: str
) -> tuple[str | None, ReexportEdge | None]:
    """The mechanism making an absent `name` unverifiable rather than deleted.

    None means the name occurs NOWHERE in this file — not as a macro, not as a
    variable/typedef binding, not as any textual identifier — so it is provably
    absent from the file the anchor points at (→ missing → stale). A non-None
    detail means C's out-of-file supply routes leave absence unprovable:

    - a name `#define`d in this file is present but not a fingerprinted callable
      (`macro`);
    - a name bound as a file-scope variable or `typedef` is present but not a
      callable (`noncallable`);
    - a name that occurs anywhere else in the text — a call site, an `#ifdef`
      operand, a macro invocation, a use — may be declared in a `#include`d header
      not in this snapshot or produced by macro/conditional-compilation this tool
      does not resolve, so its absence is not provable (`referenced`).

    C carries no followable re-export edge combdrift resolves, so the second
    element is always None.
    """
    macros: set[str] = set()
    noncallable: set[str] = set()
    referenced: set[str] = set()
    for node in walk_nodes(root):
        node_type = node.type
        if node_type in ("preproc_def", "preproc_function_def"):
            macro = node.child_by_field_name("name")
            if macro is not None:
                text = node_text(macro)
                if text is not None:
                    macros.add(text)
        elif node_type == "type_definition":
            bound = _c_declarator_name(node.child_by_field_name("declarator"))
            if bound is not None:
                noncallable.add(bound)
        elif node_type == "declaration" and _c_function_declarator(node) is None:
            noncallable.update(_c_declaration_names(node))
        elif node_type in _C_IDENT_NODES:
            text = node_text(node)
            if text is not None:
                referenced.add(text)

    if name in macros:
        return "macro", None
    if name in noncallable:
        return "noncallable", None
    if name in referenced:
        return "referenced", None
    return None, None


def _c_declaration_names(decl: Node) -> list[str]:
    """Every bare name a non-function `declaration` binds (`int a, *b, c[2];`)."""
    names: list[str] = []
    for i in range(decl.child_count):
        if decl.field_name_for_child(i) == "declarator":
            bound = _c_declarator_name(decl.child(i))
            if bound is not None:
                names.append(bound)
    return names


def _c_declarator_name(declarator: Node | None) -> str | None:
    """The bare identifier a declarator finally binds, through pointer/array/init/typedef wrappers."""
    node = declarator
    while node is not None:
        if node.type in ("identifier", "type_identifier"):
            return node_text(node)
        if node.type in _C_DECLARATOR_WRAPPERS:
            node = node.child_by_field_name("declarator")
        else:
            return None
    return None


C = LangSpec(
    grammar=Language(_ts_c.language()),
    find_symbols=_find_c_symbols,
    extract_interface=_c_interface,
    indirect_detail=_c_indirect_detail,
)
