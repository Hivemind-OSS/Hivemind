"""Structural guards for the langs/ spec layer and the generic tree-sitter driver.

These do not re-test JS/TS verdicts (test_symbols.py owns those, unchanged); they
pin the refactor's invariants: the registry resolves extensions to the right
LangSpec, Python/unsupported extensions stay off the registry, each LangSpec
supplies observations (never a status), the three grammars share one set of
observation functions, the driver in symbols.py is generic (no hardcoded JS/TS
node sets — so a language is pruneable without touching the driver), and the
public `combdrift.symbols.ReexportEdge` API survived the move into langs.base.
"""

from __future__ import annotations

import inspect

import hive.combdrift.symbols as symbols_module
from hive.combdrift.langs import REGISTRY, LangSpec
from hive.combdrift.langs.base import ReexportEdge as BaseReexportEdge
from hive.combdrift.langs.base import parser_for
from hive.combdrift.langs.c import C
from hive.combdrift.langs.cpp import CPP
from hive.combdrift.langs.go import GO
from hive.combdrift.langs.javascript import JAVASCRIPT, TYPESCRIPT, TSX
from hive.combdrift.langs.rust import RUST
from hive.combdrift.symbols import ReexportEdge, locate
from tree_sitter import Language


def test_registry_maps_js_ts_extensions_to_expected_specs():
    assert REGISTRY[".js"] is JAVASCRIPT
    assert REGISTRY[".jsx"] is JAVASCRIPT
    assert REGISTRY[".mjs"] is JAVASCRIPT
    assert REGISTRY[".cjs"] is JAVASCRIPT
    assert REGISTRY[".ts"] is TYPESCRIPT
    assert REGISTRY[".mts"] is TYPESCRIPT
    assert REGISTRY[".cts"] is TYPESCRIPT
    assert REGISTRY[".tsx"] is TSX


def test_registry_maps_go_extension_to_go_spec():
    # `.go` resolves through the Go LangSpec.
    assert REGISTRY[".go"] is GO


def test_registry_maps_rust_extension_to_rust_spec():
    # `.rs` resolves through the Rust LangSpec added in this chunk.
    assert REGISTRY[".rs"] is RUST


def test_registry_maps_c_extensions_to_c_spec():
    # Both a C source and a C header resolve through the C LangSpec added here.
    assert REGISTRY[".c"] is C
    assert REGISTRY[".h"] is C


def test_registry_maps_cpp_extensions_to_cpp_spec():
    # The five C++ source/header flavors resolve through the C++ LangSpec (this,
    # the final WS-B chunk). `.h` intentionally stays C — the safe default for the
    # ambiguous header extension.
    assert REGISTRY[".cc"] is CPP
    assert REGISTRY[".cpp"] is CPP
    assert REGISTRY[".cxx"] is CPP
    assert REGISTRY[".hpp"] is CPP
    assert REGISTRY[".hh"] is CPP


def test_registry_holds_only_the_supported_flavors():
    # The exact set is the single source of which tree-sitter file types resolve:
    # the JS/TS family plus Go, Rust, C, and C++ (this closes WS-B). Python is on
    # the ast path; `.sql`/`*.schema.json` route through the schema seam.
    assert set(REGISTRY) == {
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".mts",
        ".cts",
        ".tsx",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".hh",
    }


def test_python_stays_off_the_registry():
    # `.py` resolves through the stdlib ast path in symbols, not a LangSpec.
    assert ".py" not in REGISTRY
    assert locate("def f():\n    return 1\n", "a.py", "f").status == "found"


def test_go_resolves_through_the_registry():
    # Go is supported now: a top-level func resolves rather than reading unsupported.
    assert locate("package p\nfunc Foo() {}\n", "main.go", "Foo").status == "found"


def test_c_resolves_through_the_registry():
    # C is supported now: a top-level function resolves rather than unsupported.
    assert (
        locate("int foo(void) {\n    return 0;\n}\n", "main.c", "foo").status == "found"
    )


def test_cpp_resolves_through_the_registry():
    # C++ is supported now (the final WS-B chunk): a free function resolves rather
    # than reading unsupported.
    assert (
        locate("int foo() {\n    return 0;\n}\n", "main.cpp", "foo").status == "found"
    )


def test_unknown_extensions_stay_unsupported():
    # C++ was the last real language WS-B adds, so there is no more "next" language
    # to point at: an invented extension that will never be a LangSpec, and the
    # Ruby/Bash removed from the shop entirely, all stay off the registry and read
    # unsupported. This is the load-bearing negative guard's final shape.
    for ext in (".zig", ".rb", ".sh"):
        assert ext not in REGISTRY
    assert locate("fn main() void {}\n", "main.zig", "main").status == "unsupported"


def test_langspec_shape_is_grammar_plus_three_observers():
    for spec in (JAVASCRIPT, TYPESCRIPT, TSX):
        assert isinstance(spec, LangSpec)
        assert isinstance(spec.grammar, Language)
        assert callable(spec.find_symbols)
        assert callable(spec.extract_interface)
        assert callable(spec.indirect_detail)


def test_flavors_share_observers_but_have_distinct_grammars():
    # One adapter file, three grammars: the logic is single-sourced, only the
    # tree-sitter Language differs.
    assert JAVASCRIPT.find_symbols is TYPESCRIPT.find_symbols is TSX.find_symbols
    assert (
        JAVASCRIPT.extract_interface
        is TYPESCRIPT.extract_interface
        is TSX.extract_interface
    )
    assert (
        JAVASCRIPT.indirect_detail is TYPESCRIPT.indirect_detail is TSX.indirect_detail
    )
    grammars = {id(JAVASCRIPT.grammar), id(TYPESCRIPT.grammar), id(TSX.grammar)}
    assert len(grammars) == 3


def test_langspec_observers_return_observations_not_a_status():
    # A LangSpec never decides the tri-state: find_symbols yields Nodes,
    # extract_interface an Interface, indirect_detail a (detail, edge) tuple.
    parser = parser_for(JAVASCRIPT.grammar)
    found = parser.parse(b"function f() {}\n").root_node
    nodes = JAVASCRIPT.find_symbols(found, "f")
    assert isinstance(nodes, list) and len(nodes) == 1
    assert JAVASCRIPT.extract_interface(nodes[0]).category == "func"

    absent = parser.parse(b"const X = 5;\n").root_node
    detail, edge = JAVASCRIPT.indirect_detail(absent, "a.js", "X")
    assert detail == "noncallable"
    assert edge is None


def test_parser_cache_is_one_reusable_parser_per_grammar():
    assert parser_for(JAVASCRIPT.grammar) is parser_for(JAVASCRIPT.grammar)
    assert parser_for(JAVASCRIPT.grammar) is not parser_for(TYPESCRIPT.grammar)


def test_reexport_edge_public_api_survived_the_move():
    # ReexportEdge moved to langs.base but must still import from combdrift.symbols
    # (its historical home) — same class object, not a shadow copy.
    assert ReexportEdge is BaseReexportEdge


def test_symbols_driver_is_generic_no_hardcoded_js_ts_logic():
    # The driver must not re-hardcode JS/TS node sets or grammars; that is what
    # makes a language pruneable without touching symbols.py.
    src = inspect.getsource(symbols_module)
    for banned in (
        "_TS_FUNCTION_NODES",
        "_TS_CLASS_NODES",
        "_TS_METHOD_NODES",
        "tree_sitter_javascript",
        "tree_sitter_typescript",
    ):
        assert banned not in src, f"{banned} leaked back into symbols.py"
    # The driver interprets a LangSpec's observations rather than parsing itself.
    assert "spec.find_symbols" in src
    assert "spec.indirect_detail" in src


def test_go_langspec_shape_and_own_observers():
    # Go is a proper LangSpec with its own grammar and its own three observers —
    # it does not share the JS/TS observation functions.
    assert isinstance(GO, LangSpec)
    assert isinstance(GO.grammar, Language)
    assert callable(GO.find_symbols)
    assert callable(GO.extract_interface)
    assert callable(GO.indirect_detail)
    assert GO.find_symbols is not JAVASCRIPT.find_symbols
    assert GO.indirect_detail is not JAVASCRIPT.indirect_detail
    assert id(GO.grammar) not in {
        id(JAVASCRIPT.grammar),
        id(TYPESCRIPT.grammar),
        id(TSX.grammar),
    }


def test_go_langspec_observers_return_observations_not_a_status():
    # A LangSpec never decides the tri-state: find_symbols yields Nodes,
    # extract_interface an Interface, indirect_detail a (detail, edge) tuple.
    parser = parser_for(GO.grammar)
    root = parser.parse(b"package p\nfunc f(a int) {}\n").root_node
    nodes = GO.find_symbols(root, "f")
    assert isinstance(nodes, list) and len(nodes) == 1
    assert GO.extract_interface(nodes[0]).category == "func"

    absent = parser.parse(b"package p\nvar X = 5\n").root_node
    detail, edge = GO.indirect_detail(absent, "x.go", "X")
    assert detail == "noncallable"
    assert edge is None


def test_rust_langspec_shape_and_own_observers():
    # Rust is a proper LangSpec with its own grammar and its own three observers —
    # it does not share the JS/TS or Go observation functions.
    assert isinstance(RUST, LangSpec)
    assert isinstance(RUST.grammar, Language)
    assert callable(RUST.find_symbols)
    assert callable(RUST.extract_interface)
    assert callable(RUST.indirect_detail)
    assert RUST.find_symbols is not JAVASCRIPT.find_symbols
    assert RUST.find_symbols is not GO.find_symbols
    assert RUST.indirect_detail is not GO.indirect_detail
    assert id(RUST.grammar) not in {
        id(JAVASCRIPT.grammar),
        id(TYPESCRIPT.grammar),
        id(TSX.grammar),
        id(GO.grammar),
    }


def test_rust_langspec_observers_return_observations_not_a_status():
    # A LangSpec never decides the tri-state: find_symbols yields Nodes,
    # extract_interface an Interface, indirect_detail a (detail, edge) tuple.
    parser = parser_for(RUST.grammar)
    root = parser.parse(b"fn f(a: i32) {}\n").root_node
    nodes = RUST.find_symbols(root, "f")
    assert isinstance(nodes, list) and len(nodes) == 1
    assert RUST.extract_interface(nodes[0]).category == "func"

    absent = parser.parse(b"const X: i32 = 5;\n").root_node
    detail, edge = RUST.indirect_detail(absent, "x.rs", "X")
    assert detail == "noncallable"
    assert edge is None


def test_c_langspec_shape_and_own_observers():
    # C is a proper LangSpec with its own grammar and its own three observers —
    # it does not share the JS/TS, Go, or Rust observation functions.
    assert isinstance(C, LangSpec)
    assert isinstance(C.grammar, Language)
    assert callable(C.find_symbols)
    assert callable(C.extract_interface)
    assert callable(C.indirect_detail)
    assert C.find_symbols is not JAVASCRIPT.find_symbols
    assert C.find_symbols is not GO.find_symbols
    assert C.find_symbols is not RUST.find_symbols
    assert C.indirect_detail is not GO.indirect_detail
    assert id(C.grammar) not in {
        id(JAVASCRIPT.grammar),
        id(TYPESCRIPT.grammar),
        id(TSX.grammar),
        id(GO.grammar),
        id(RUST.grammar),
    }


def test_c_langspec_observers_return_observations_not_a_status():
    # A LangSpec never decides the tri-state: find_symbols yields Nodes,
    # extract_interface an Interface, indirect_detail a (detail, edge) tuple.
    parser = parser_for(C.grammar)
    root = parser.parse(b"int f(int a) {\n    return a;\n}\n").root_node
    nodes = C.find_symbols(root, "f")
    assert isinstance(nodes, list) and len(nodes) == 1
    assert C.extract_interface(nodes[0]).category == "func"

    absent = parser.parse(b"int x = 5;\n").root_node
    detail, edge = C.indirect_detail(absent, "x.c", "x")
    assert detail == "noncallable"
    assert edge is None


def test_cpp_langspec_shape_and_own_observers():
    # C++ is a proper LangSpec with its own grammar and its own three observers —
    # it does not share the JS/TS, Go, Rust, or C observation functions.
    assert isinstance(CPP, LangSpec)
    assert isinstance(CPP.grammar, Language)
    assert callable(CPP.find_symbols)
    assert callable(CPP.extract_interface)
    assert callable(CPP.indirect_detail)
    assert CPP.find_symbols is not C.find_symbols
    assert CPP.find_symbols is not JAVASCRIPT.find_symbols
    assert CPP.indirect_detail is not C.indirect_detail
    assert id(CPP.grammar) not in {
        id(JAVASCRIPT.grammar),
        id(TYPESCRIPT.grammar),
        id(TSX.grammar),
        id(GO.grammar),
        id(RUST.grammar),
        id(C.grammar),
    }


def test_cpp_langspec_observers_return_observations_not_a_status():
    # A LangSpec never decides the tri-state: find_symbols yields Nodes,
    # extract_interface an Interface, indirect_detail a (detail, edge) tuple.
    parser = parser_for(CPP.grammar)
    root = parser.parse(b"int f(int a) {\n    return a;\n}\n").root_node
    nodes = CPP.find_symbols(root, "f")
    assert isinstance(nodes, list) and len(nodes) == 1
    assert CPP.extract_interface(nodes[0]).category == "func"

    absent = parser.parse(b"int x = 5;\n").root_node
    detail, edge = CPP.indirect_detail(absent, "x.cpp", "x")
    assert detail == "noncallable"
    assert edge is None


def test_cpp_overload_set_makes_find_symbols_return_more_than_one():
    # The safety valve at the driver seam: an overload set yields >1 declaration,
    # so the generic driver leaves interface=None (no single shape to fingerprint).
    parser = parser_for(CPP.grammar)
    root = parser.parse(
        b"int f(int a) { return a; }\nint f(double a) { return 0; }\n"
    ).root_node
    assert len(CPP.find_symbols(root, "f")) == 2
