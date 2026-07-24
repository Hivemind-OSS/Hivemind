"""Shared foundation for the per-language tree-sitter backends.

`LangSpec` is the uniform shape each tree-sitter language supplies to the generic
driver in `combdrift.symbols`. A LangSpec carries only language-specific
*observations*; it never decides found/missing/indirect/parse_error/ambiguous —
`symbols.locate` owns that tri-state over every LangSpec, so the false-stale
decision (Law 1) is written exactly once.

This module also holds the grammar-blind tree-sitter primitives every backend
reuses (node name/text, a whole-tree walk) and a per-grammar parser cache. It
imports no language module and nothing from `combdrift.symbols`, so the langs
dependency graph stays acyclic: base -> (fingerprint, tree_sitter) only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

from tree_sitter import Language, Node, Parser

from hive.combdrift.fingerprint import Interface


@dataclass(frozen=True, slots=True)
class ReexportEdge:
    """A followable named binding of `name` from another module.

    Populated only for a relative, named, non-shadowed import/re-export. The
    candidate lists are repo-relative target files in resolution-precedence
    order, precomputed as pure os.path string math — no module in the parse layer
    touches the filesystem, so the probing of these paths is left to the
    language-blind resolution layer.
    """

    name: str  # ORIGINAL (pre-alias) name to resolve in the target module
    module_candidates: tuple[str, ...]  # files the specifier may denote
    submodule_candidates: tuple[
        str, ...
    ]  # files meaning `name` is a SUBMODULE (Python only)


@dataclass(frozen=True, slots=True)
class LangSpec:
    """A per-language tree-sitter backend for the generic driver in symbols.py.

    A LangSpec supplies only the language-specific observations; the driver
    (`symbols._locate_treesitter`) alone maps them onto the honesty policy, so
    every tree-sitter language shares one owner of the found/missing/indirect/
    parse_error/ambiguous tri-state (Law 1).

    - grammar: the tree-sitter Language the driver parses source with.
    - find_symbols(root, symbol): every top-level declaration named `symbol`
      (a dotted "Type.method" resolves to matching members). More than one match
      is an ambiguous shape (interface left None); zero matches routes to
      indirect/missing below.
    - extract_interface(node): the language-neutral call-shape descriptor for a
      single found declaration — called only when exactly one clean match exists.
    - indirect_detail(root, path, name): for a name with no clean top-level
      declaration, the mechanism that could still supply it (import/re-export,
      data binding, nested decl, inheritance, ...) plus a followable ReexportEdge
      when the indirection is a relative named import, else (None, None) for
      provable absence. The driver maps a non-None detail to indirect, None to
      missing — the language never makes that call itself.
    """

    grammar: Language
    find_symbols: Callable[[Node, str], list[Node]]
    extract_interface: Callable[[Node], Interface]
    indirect_detail: Callable[[Node, str, str], tuple[str | None, ReexportEdge | None]]


# One reusable Parser per grammar object, built on first use. The grammars are
# process-lifetime singletons (module globals in each language backend), so
# keying the cache by object identity is stable and never collides.
_PARSER_CACHE: dict[int, Parser] = {}


def parser_for(grammar: Language) -> Parser:
    """Return the cached tree-sitter Parser bound to `grammar`."""
    key = id(grammar)
    parser = _PARSER_CACHE.get(key)
    if parser is None:
        parser = Parser(grammar)
        _PARSER_CACHE[key] = parser
    return parser


def node_name(node: Node) -> str | None:
    name = node.child_by_field_name("name")
    return (
        name.text.decode("utf-8")
        if name is not None and name.text is not None
        else None
    )


def node_text(node: Node) -> str | None:
    return node.text.decode("utf-8") if node.text is not None else None


def walk_nodes(root: Node) -> Iterator[Node]:
    """Yield every named node in the tree (root included)."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.named_children)
