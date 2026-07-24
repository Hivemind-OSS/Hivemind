"""Locate a symbol in source text: locate(source, path, symbol) -> SymbolLookup.

The public seam is `locate`; the backend is inferred from the path extension,
never named by callers. Python is handled by the stdlib `ast` here; every
tree-sitter language is resolved by a per-language `LangSpec` from
`combdrift.langs`, driven by the generic `_locate_treesitter` below; a `.sql` or
`*.schema.json` path resolves against a declared database schema. Source is
treated strictly as data — parsed, never imported or executed — so the verifier
stays safe against untrusted repositories.

This module owns the honesty policy (Law 1) for every backend: the found/missing/
indirect/parse_error/ambiguous tri-state is decided here and nowhere else. A
LangSpec supplies only observations (which declarations exist, their interface,
what indirection could supply an absent name); the driver alone maps them onto a
verdict, so the false-stale decision has exactly one owner across all languages.

tree-sitter is error-recovering: a malformed file still yields a tree with ERROR
nodes instead of raising. The resolution policy is therefore "found-despite-
error" — a symbol whose own declaration parses cleanly resolves even when another
region of the file is broken, while a name that cannot be cleanly located in a
file that failed to parse is reported parse_error (unverifiable), never missing.
A syntax error thus never masquerades as a deleted symbol.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from typing import Literal

from hive.combdrift import schema
from hive.combdrift.fingerprint import Interface
from hive.combdrift.langs import REGISTRY
from hive.combdrift.langs.base import LangSpec, ReexportEdge, parser_for

LookupStatus = Literal["found", "missing", "indirect", "parse_error", "unsupported"]


@dataclass(frozen=True, slots=True)
class SymbolLookup:
    status: LookupStatus
    lineno: int | None = None  # 1-based; 1 for a file-presence (symbol=None) hit
    # mechanism for "indirect", parser message for "parse_error"
    detail: str | None = None
    # call-shape descriptor; populated iff status == "found" and a single
    # callable was requested. None for an overloaded/ambiguous symbol.
    interface: Interface | None = None
    # set iff status == "indirect" AND the indirection is a followable edge
    reexport: ReexportEdge | None = None


# Schema-source suffixes, checked before the Python/registry dispatch so a
# declared database schema is resolved as data by combdrift.schema. A bare
# `.json` is intentionally absent (it stays unsupported); only the explicit
# `*.schema.json` suffix routes here.
_SCHEMA_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".schema.json", schema.JSON_SCHEMA),
    (".sql", schema.SQL),
)


def locate(source: str, path: str, symbol: str | None) -> SymbolLookup:
    """Find `symbol` in `source`, choosing a backend from `path`'s extension.

    A bare symbol resolves to a top-level function or class, or a const/let/var
    bound to a function/arrow. A dotted "Class.method" resolves to a method in
    that top-level class's body. When `symbol` is None the result reports only
    that the file is present and parses. A `.sql` or `*.schema.json` path resolves
    against a declared database schema; a `.py` path uses the stdlib `ast`; any
    other extension in the langs registry uses its tree-sitter LangSpec. An
    unknown extension yields status "unsupported"; a file that will not parse
    yields "parse_error".
    """
    kind = _schema_kind_for_path(path)
    if kind is not None:
        return _locate_schema(source, kind, symbol)
    _root, ext = os.path.splitext(path)
    ext = ext.lower()
    if ext == ".py":
        return _locate_python(source, path, symbol)
    spec = REGISTRY.get(ext)
    if spec is None:
        return SymbolLookup(status="unsupported")
    return _locate_treesitter(source, path, symbol, spec)


def _schema_kind_for_path(path: str) -> str | None:
    """The schema source kind for `path`, or None if it is not a schema source."""
    low = path.lower()
    for suffix, kind in _SCHEMA_SUFFIXES:
        if low.endswith(suffix):
            return kind
    return None


def _locate_schema(source: str, kind: str, symbol: str | None) -> SymbolLookup:
    """Map a schema.SchemaResolution onto SymbolLookup.

    `interface` is always None: schema anchors carry no Layer-B fingerprint in v1,
    so a fingerprint hand-set on a schema anchor degrades to unverifiable (never
    stale) through the existing `interface is None` branch.
    """
    resolution = schema.resolve_symbol(source, kind, symbol)
    return SymbolLookup(
        status=resolution.status,
        lineno=resolution.lineno,
        detail=resolution.detail,
        interface=None,
    )


# --- Python (stdlib ast) -----------------------------------------------------

_PY_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _locate_python(source: str, path: str, symbol: str | None) -> SymbolLookup:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return SymbolLookup(status="parse_error", detail=exc.msg)
    if symbol is None:
        return SymbolLookup(status="found", lineno=1)
    if "." in symbol:
        return _locate_python_method(tree, path, symbol)
    return _locate_python_name(tree, path, symbol)


def _locate_python_name(tree: ast.Module, path: str, symbol: str) -> SymbolLookup:
    """Resolve a bare name to a top-level callable, else split missing/indirect."""
    matches = [
        n for n in tree.body if isinstance(n, _PY_DEF_NODES) and n.name == symbol
    ]
    if matches:
        return _python_found(matches)
    detail, edge = _python_indirect_detail(tree, path, symbol)
    if detail is not None:
        return SymbolLookup(status="indirect", detail=detail, reexport=edge)
    return SymbolLookup(status="missing")


def _locate_python_method(tree: ast.Module, path: str, symbol: str) -> SymbolLookup:
    """Resolve a one-level "Class.method", inheritance-aware (see module rule)."""
    class_name, _, member = symbol.partition(".")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            members = [
                c
                for c in node.body
                if isinstance(c, _PY_DEF_NODES) and c.name == member
            ]
            if members:
                return _python_found(members)
            # The class is here but the method is not. With a base class the
            # method may be inherited, so its absence is not provable.
            if node.bases:
                return SymbolLookup(status="indirect", detail="maybe_inherited")
            return SymbolLookup(status="missing")
    # A class reached only via re-export is not followed for its method: one hop
    # resolves the class, not the dotted member, so the edge is left off.
    detail, _edge = _python_indirect_detail(tree, path, class_name)
    if detail is not None:
        return SymbolLookup(status="indirect", detail=detail)
    return SymbolLookup(status="missing")


def _python_found(
    matches: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef],
) -> SymbolLookup:
    """Build a found result; an interface only when a single definition matched.

    More than one definition of the same name (typing overloads, redefinition)
    is an ambiguous shape: it exists, but no single interface can be compared, so
    interface is left None and Layer B routes to unverifiable.
    """
    interface = _python_interface(matches[0]) if len(matches) == 1 else None
    return SymbolLookup(status="found", lineno=matches[0].lineno, interface=interface)


def _python_interface(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> Interface:
    if isinstance(node, ast.ClassDef):
        return Interface(
            category="class",
            is_generator=False,
            req_positional=0,
            max_positional=0,
            has_star=False,
            has_kw=False,
            req_kwonly=0,
            contract_decorators=_python_contract_decorators(node),
            base_count=len(node.bases),
        )
    args = node.args  # FunctionDef / AsyncFunctionDef
    positional = list(args.posonlyargs) + list(args.args)
    return Interface(
        category="async_func" if isinstance(node, ast.AsyncFunctionDef) else "func",
        is_generator=_python_is_generator(node),
        req_positional=len(positional) - len(args.defaults),
        max_positional=len(positional),
        has_star=args.vararg is not None,
        has_kw=args.kwarg is not None,
        req_kwonly=sum(1 for default in args.kw_defaults if default is None),
        contract_decorators=_python_contract_decorators(node),
        base_count=0,
    )


_PY_CONTRACT_DECORATORS = frozenset({"property", "staticmethod", "classmethod"})


def _python_contract_decorators(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> frozenset[str]:
    names = {
        name
        for dec in node.decorator_list
        if (name := _python_decorator_name(dec)) in _PY_CONTRACT_DECORATORS
    }
    return frozenset(names)


def _python_decorator_name(dec: ast.expr) -> str | None:
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        return dec.attr
    if isinstance(dec, ast.Call):
        return _python_decorator_name(dec.func)
    return None


def _python_is_generator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True iff the function's OWN body yields (nested scopes do not count)."""
    return any(_node_has_yield(stmt) for stmt in node.body)


def _node_has_yield(node: ast.AST) -> bool:
    if isinstance(node, (ast.Yield, ast.YieldFrom)):
        return True
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return False  # a nested scope's yields belong to it, not to us
    return any(_node_has_yield(child) for child in ast.iter_child_nodes(node))


def _python_indirect_detail(
    tree: ast.Module, path: str, name: str
) -> tuple[str | None, ReexportEdge | None]:
    """The mechanism making `name` present-but-not-a-top-level-callable, or None.

    None means the name is bound nowhere — provably absent (→ missing → stale).
    A non-None detail means some indirection could supply it (import/re-export,
    data binding, nested def, wildcard import, or module __getattr__), so its
    absence is not provable and the result is unverifiable, never stale.

    The second element is a followable `ReexportEdge` iff the indirection is a
    relative, named, non-shadowed import of `name`; for every other mechanism
    it is None, so the one-hop follow can never widen beyond that case.
    """
    imported: set[str] = set()
    assigned: set[str] = set()
    declared: set[str] = set()  # def/class names at any nesting depth
    has_wildcard = False
    edges: dict[str, ReexportEdge] = {}  # visible name -> followable edge
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    has_wildcard = True
                    continue
                visible = alias.asname or alias.name
                imported.add(visible)
                # Relative (level>=1) and module-qualified (`from .M import N`) is
                # the only followable shape; `from . import N` (module is None)
                # and absolute imports (level 0) are not.
                if node.level >= 1 and node.module is not None:
                    edges[visible] = _py_make_edge(
                        path, node.level, node.module, alias.name
                    )
        elif isinstance(node, _PY_DEF_NODES):
            declared.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _collect_target_names(target, assigned)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            _collect_target_names(node.target, assigned)

    if name in imported:
        # A local def/class/assignment shadows the import, so following it would
        # fingerprint the wrong value; suppress the edge in that case.
        shadowed = name in declared or name in assigned
        return "reexport", (None if shadowed else edges.get(name))
    # A top-level callable was ruled out before this call, so a declared hit is
    # necessarily a nested/conditional definition.
    if name in declared:
        return "nested", None
    if name in assigned:
        return "noncallable", None
    if name in _python_all_members(tree):
        return "reexport", None
    if has_wildcard:
        return "wildcard", None
    if _python_has_module_getattr(tree):
        return "module_getattr", None
    return None, None


def _py_make_edge(
    importer_path: str, level: int, module: str, original: str
) -> ReexportEdge:
    """Compute the repo-relative candidate paths for `from <dots><module> import`.

    `level` leading dots ascend `level-1` directories from the importer's own
    directory; the dotted `module` becomes path segments under that base. The
    result is pure string math (os.path only) — a candidate that walks above the
    repo is rejected later by the resolution layer's containment check.
    """
    base = os.path.normpath(
        os.path.join(
            os.path.dirname(importer_path),
            *([os.pardir] * (level - 1)),
            *module.split("."),
        )
    )
    return ReexportEdge(
        name=original,
        module_candidates=(f"{base}.py", os.path.join(base, "__init__.py")),
        submodule_candidates=(
            os.path.join(base, f"{original}.py"),
            os.path.join(base, original, "__init__.py"),
        ),
    )


def _collect_target_names(target: ast.expr, acc: set[str]) -> None:
    """Collect names bound by an assignment target, recursing into unpacking."""
    if isinstance(target, ast.Name):
        acc.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _collect_target_names(elt, acc)
    elif isinstance(target, ast.Starred):
        _collect_target_names(target.value, acc)
    # Attribute / Subscript targets bind no new module-level name; ignored.


def _python_all_members(tree: ast.Module) -> set[str]:
    """String entries of a top-level `__all__` list/tuple literal."""
    members: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            members |= _string_literals(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
            and node.value is not None
        ):
            members |= _string_literals(node.value)
    return members


def _string_literals(value: ast.expr) -> set[str]:
    if not isinstance(value, (ast.List, ast.Tuple)):
        return set()
    return {
        elt.value
        for elt in value.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    }


def _python_has_module_getattr(tree: ast.Module) -> bool:
    """True iff the module defines a top-level PEP 562 `__getattr__`."""
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__getattr__"
        for node in tree.body
    )


# --- Generic tree-sitter driver (over a combdrift.langs LangSpec) -------------


def _locate_treesitter(
    source: str, path: str, symbol: str | None, spec: LangSpec
) -> SymbolLookup:
    """Resolve `symbol` through a tree-sitter LangSpec; sole owner of the tri-state.

    This is the one place the found/missing/indirect/parse_error/ambiguous verdict
    is decided for every tree-sitter language (Law 1 — the false-stale call is
    written once). The LangSpec supplies only observations — `find_symbols`,
    `extract_interface`, `indirect_detail` — and this driver interprets them:
    zero clean matches with a non-None indirection detail is `indirect`, with a
    None detail is provable-absence `missing`; a name absent only because the
    parse failed is `parse_error` (unverifiable), never missing; more than one
    clean match is an ambiguous shape (found, interface left None).
    """
    root = parser_for(spec.grammar).parse(source.encode("utf-8")).root_node

    if symbol is None:
        if root.has_error:
            return SymbolLookup(status="parse_error", detail="syntax error")
        return SymbolLookup(status="found", lineno=1)

    nodes = spec.find_symbols(root, symbol)
    clean = [node for node in nodes if not node.has_error]
    if clean:
        # More than one declaration of the same name (overload signatures, .d.ts
        # merges) is an ambiguous shape: exists, but no single interface.
        interface = spec.extract_interface(clean[0]) if len(clean) == 1 else None
        return SymbolLookup(
            status="found", lineno=clean[0].start_point[0] + 1, interface=interface
        )
    # Not cleanly located. A name that is absent only because the parse failed
    # (the error may have swallowed its declaration) is unverifiable, not missing.
    if nodes or root.has_error:
        return SymbolLookup(status="parse_error", detail="syntax error")
    # Clean parse, not a resolvable top-level callable: split absent vs indirect.
    detail, edge = spec.indirect_detail(root, path, symbol)
    if detail is not None:
        return SymbolLookup(status="indirect", detail=detail, reexport=edge)
    return SymbolLookup(status="missing")
