"""Rust backend for the generic tree-sitter driver.

Rust's declaration model differs from the class-based languages, so this backend
maps it onto the language-neutral `Interface` deliberately:

- a `function_item` (or a bodiless `function_signature_item`) is func-shaped; its
  receiver `&self` is not part of its call arity, and its generic `<T>` parameters
  live in a separate `type_parameters` field, so counting the `parameters` field
  alone makes a generic and a non-generic form of the same function identical;
- a `struct_item`/`enum_item`/`trait_item` is the class-like shape. Structs and
  enums have no inheritance (base 0); a trait's "bases" are its SUPERTRAITS;
- a `variadic_parameter` (`...`, the extern-C form) is the `has_star` flag; Rust
  has no default, optional, or keyword parameter, so every plain parameter is
  required.

Because a Rust type's methods live in `impl` blocks that may sit in ANY file
(there is no single canonical class body), an absent `Type.method` is resolved
across the in-file impl blocks and, failing that, read as an indirection (the
impl may be elsewhere) — never `missing`. That is the Law-1 false-stale guard for
Rust. A `use`/`pub use` re-binds a name from another module, mirroring the JS/TS
`export … from` re-export; a `self::`/`super::` relative path yields a followable
`ReexportEdge`, every other form stays unfollowable (conservatively unverifiable).
This module supplies only observations; the generic driver in `combdrift.symbols`
owns the found/missing/indirect/parse_error/ambiguous tri-state.
"""

from __future__ import annotations

import os

import tree_sitter_rust as _ts_rust
from tree_sitter import Language, Node

from hive.combdrift.fingerprint import Interface
from hive.combdrift.langs.base import (
    LangSpec,
    ReexportEdge,
    node_name,
    node_text,
    walk_nodes,
)

# Both forms extract a func-shaped interface; the signature form is the bodiless
# declaration found in a trait body or an `extern` block.
_RUST_FUNCTION_NODES = frozenset({"function_item", "function_signature_item"})
# The top-level named kinds that resolve as a symbol: a callable and the three
# class-like type declarations. A const/static/type-alias/mod carrying the name
# is an indirection, not one of these.
_RUST_TYPE_NODES = frozenset({"struct_item", "enum_item", "trait_item"})
# File stems whose submodules live in their OWN directory rather than a same-named
# subdirectory (crate roots and the classic `mod.rs`).
_RUST_ROOT_MODULE_STEMS = frozenset({"mod", "lib", "main"})


def _find_rust_symbols(root: Node, symbol: str) -> list[Node]:
    """Top-level declarations matching `symbol` (>1 means an ambiguous shape).

    A bare name resolves to a top-level `fn` or a named type (struct, enum, or
    trait). A dotted "Type.method" resolves to a method declared in an in-file
    `impl` block for that type (inherent or trait impl) or in a same-named trait's
    body — Rust methods are not nested in a class body, and an impl block may live
    in another file, so a miss here is deferred to `indirect_detail`, never missing.
    """
    if "." in symbol:
        type_name, _, member = symbol.partition(".")
        matches: list[Node] = []
        for child in root.named_children:
            if child.type == "impl_item" and _impl_type_name(child) == type_name:
                matches.extend(_body_methods(child, member))
            elif child.type == "trait_item" and node_name(child) == type_name:
                matches.extend(_body_methods(child, member))
        return matches
    matches = []
    for child in root.named_children:
        if child.type == "function_item" and node_name(child) == symbol:
            matches.append(child)
        elif child.type in _RUST_TYPE_NODES and node_name(child) == symbol:
            matches.append(child)
    return matches


def _impl_type_name(impl_node: Node) -> str | None:
    """The base type name an `impl` block is for (sees through `Type<T>` generics)."""
    return _base_type_name(impl_node.child_by_field_name("type"))


def _base_type_name(type_node: Node | None) -> str | None:
    """The first `type_identifier` under a type node (through `Type<…>` / `&Type`)."""
    if type_node is None:
        return None
    if type_node.type == "type_identifier":
        return node_text(type_node)
    for node in walk_nodes(type_node):
        if node.type == "type_identifier":
            return node_text(node)
    return None


def _body_methods(container: Node, member: str) -> list[Node]:
    """Function declarations named `member` inside an impl/trait body."""
    body = container.child_by_field_name("body")
    if body is None:
        return []
    return [
        child
        for child in body.named_children
        if child.type in _RUST_FUNCTION_NODES and node_name(child) == member
    ]


# --- Interface extraction (Layer B) ------------------------------------------


def _rust_interface(node: Node) -> Interface:
    """A language-neutral call-shape descriptor for one found Rust declaration."""
    if node.type in _RUST_FUNCTION_NODES:
        req, maximum, has_star = _rust_param_counts(
            node.child_by_field_name("parameters")
        )
        return Interface(
            category="func",
            is_generator=False,
            req_positional=req,
            max_positional=maximum,
            has_star=has_star,
            has_kw=False,  # Rust has no **kwargs equivalent
            req_kwonly=0,  # Rust has no keyword-only parameters
            contract_decorators=frozenset(),
            base_count=0,
        )
    # A named type: class-shaped. Structs/enums do not inherit; a trait's bases
    # are its supertraits.
    return Interface(
        category="class",
        is_generator=False,
        req_positional=0,
        max_positional=0,
        has_star=False,
        has_kw=False,
        req_kwonly=0,
        contract_decorators=frozenset(),
        base_count=_rust_base_count(node),
    )


def _rust_param_counts(params: Node | None) -> tuple[int, int, bool]:
    """Return (required, max positional, has variadic) for a parameter list.

    Rust has no optional/default/keyword parameters, so required == max over the
    plain `parameter` nodes. A `self_parameter` receiver is not a call argument
    and a `variadic_parameter` (`...`, extern-C) sets has_star like Python's
    `*args`, counting toward neither total. Generic `<T>` parameters live in a
    separate `type_parameters` field, so they never reach this count.
    """
    if params is None:
        return 0, 0, False
    total = 0
    has_star = False
    for child in params.named_children:
        if child.type == "variadic_parameter":
            has_star = True
        elif child.type == "parameter":
            total += 1
    return total, total, has_star


def _rust_base_count(node: Node) -> int:
    """Count a trait's supertraits (0 for a struct/enum — Rust has no inheritance)."""
    if node.type != "trait_item":
        return 0
    bounds = node.child_by_field_name("bounds")
    if bounds is None:
        return 0
    # A supertrait bound is a type; a lifetime bound (`'a`) is not a base.
    return sum(1 for child in bounds.named_children if child.type != "lifetime")


# --- Indirection mechanism (the Law-1 false-stale observations) ---------------


def _rust_indirect_detail(
    root: Node, path: str, name: str
) -> tuple[str | None, ReexportEdge | None]:
    """The mechanism making an absent `name` unverifiable rather than deleted.

    None means the name is declared, imported, and glob/macro-injectable nowhere
    in this file — provable absence (→ missing → stale). A non-None detail means
    Rust's module model leaves absence unprovable:

    - a dotted `Type.method` may be declared in an `impl` block in another file
      (impls are not file-local), so it is never provably absent (`impl_elsewhere`);
    - a bare name bound as a `const`/`static` is present but not a callable/type
      (`noncallable`); a `type` alias (`type_alias`) or a `mod` (`submodule`) is
      likewise present but not one of the resolved kinds;
    - a bare name brought in by a `use`/`pub use` is a re-export (`reexport`),
      carrying a followable edge only for a `self::`/`super::` relative path;
    - a glob import (`use path::*`) can inject any name (`glob_import`), and a
      top-level macro invocation (e.g. `include!`) can generate items
      (`macro_generated`), so either leaves absence conservatively unverifiable.
    """
    if "." in name:
        return "impl_elsewhere", None

    data_bound: set[str] = set()  # const / static value bindings
    type_aliases: set[str] = set()
    submodules: set[str] = set()
    for node in walk_nodes(root):
        node_type = node.type
        if node_type in ("const_item", "static_item"):
            bound = node_name(node)
            if bound is not None:
                data_bound.add(bound)
        elif node_type == "type_item":
            bound = node_name(node)
            if bound is not None:
                type_aliases.add(bound)
        elif node_type == "mod_item":
            bound = node_name(node)
            if bound is not None:
                submodules.add(bound)
    named_uses, has_glob = _collect_uses(root)
    has_macro = any(child.type == "macro_invocation" for child in root.named_children)

    if name in data_bound:
        return "noncallable", None
    if name in type_aliases:
        return "type_alias", None
    if name in submodules:
        return "submodule", None
    if name in named_uses:
        return "reexport", _rust_reexport_edge(path, named_uses[name])
    if has_glob:
        return "glob_import", None
    if has_macro:
        return "macro_generated", None
    return None, None


def _collect_uses(root: Node) -> tuple[dict[str, list[str]], bool]:
    """Map every `use`-visible name to its full path segments; flag any glob.

    The path segments include the leading `self`/`super`/`crate` (when present)
    and the final bound name, so `_rust_reexport_edge` can resolve a followable
    relative target. A `use path::*` sets the glob flag and binds no name.
    """
    named: dict[str, list[str]] = {}
    glob = [False]
    for node in walk_nodes(root):
        if node.type == "use_declaration":
            _walk_use_tree(node.child_by_field_name("argument"), [], named, glob)
    return named, glob[0]


def _walk_use_tree(
    node: Node | None, prefix: list[str], named: dict[str, list[str]], glob: list[bool]
) -> None:
    """Accumulate the named bindings (and glob flag) under a use tree.

    `prefix` is the path carried down through a `foo::bar::{…}` group so each
    leaf's full path (leading qualifier … final name) is reconstructed.
    """
    if node is None:
        return
    node_type = node.type
    if node_type == "use_wildcard":
        glob[0] = True
    elif node_type == "identifier":
        seg = node_text(node)
        if seg:
            named[seg] = prefix + [seg]
    elif node_type == "scoped_identifier":
        segs = _path_segments(node)
        if segs:
            named[segs[-1]] = prefix + segs
    elif node_type == "use_as_clause":
        segs = _path_segments(node.child_by_field_name("path"))
        alias = node.child_by_field_name("alias")
        visible = (
            node_text(alias) if alias is not None else (segs[-1] if segs else None)
        )
        if visible and segs:
            named[visible] = prefix + segs
    elif node_type == "scoped_use_list":
        segs = _path_segments(node.child_by_field_name("path"))
        _walk_use_tree(node.child_by_field_name("list"), prefix + segs, named, glob)
    elif node_type == "use_list":
        for child in node.named_children:
            _walk_use_tree(child, prefix, named, glob)


def _path_segments(node: Node | None) -> list[str]:
    """Flatten a use path node into ordered segments (`self::core::parse` → 3)."""
    if node is None:
        return []
    node_type = node.type
    if node_type in ("identifier", "type_identifier"):
        text = node_text(node)
        return [text] if text else []
    if node_type in ("self", "super", "crate"):
        return [node_type]
    if node_type == "scoped_identifier":
        segs = _path_segments(node.child_by_field_name("path"))
        name = node.child_by_field_name("name")
        text = node_text(name) if name is not None else None
        return segs + [text] if text else segs
    return []


def _rust_reexport_edge(importer_path: str, segments: list[str]) -> ReexportEdge | None:
    """A followable edge for a `self::`/`super::` relative `use`, else None.

    Only a relative path resolves to an in-repo file with certainty; a `crate::`,
    bare, or external path needs the crate layout combdrift does not model, so it
    is left unfollowable (the name stays indirect → unverifiable). Pure os.path
    string math — no filesystem touch here; the resolution layer probes candidates.
    """
    if not segments:
        return None
    leading = segments[0]
    if leading == "self":
        base = _module_dir(importer_path)
        middle = segments[1:-1]
    elif leading == "super":
        ascend = 0
        while ascend < len(segments) and segments[ascend] == "super":
            ascend += 1
        base = _module_dir(importer_path)
        for _ in range(ascend):
            base = os.path.dirname(base)
        middle = segments[ascend:-1]
    else:
        return None  # crate/bare/external: crate layout unknown, not followable
    if not middle:
        return None  # no submodule path to descend; not cleanly followable
    final = segments[-1]
    target_base = os.path.normpath(os.path.join(base, *middle))
    return ReexportEdge(
        name=final,
        module_candidates=(f"{target_base}.rs", os.path.join(target_base, "mod.rs")),
        submodule_candidates=(
            os.path.join(target_base, f"{final}.rs"),
            os.path.join(target_base, final, "mod.rs"),
        ),
    )


def _module_dir(importer_path: str) -> str:
    """The directory a module's submodules live in (per Rust's file conventions)."""
    directory = os.path.dirname(importer_path)
    stem = os.path.splitext(os.path.basename(importer_path))[0]
    if stem in _RUST_ROOT_MODULE_STEMS:
        return directory
    return os.path.join(directory, stem)


RUST = LangSpec(
    grammar=Language(_ts_rust.language()),
    find_symbols=_find_rust_symbols,
    extract_interface=_rust_interface,
    indirect_detail=_rust_indirect_detail,
)
