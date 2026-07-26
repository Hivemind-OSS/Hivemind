"""resolve_anchor / fingerprint_anchor against C++ repos: the §6 verdict matrix.

Exercises the full per-language contract end-to-end through the filesystem seam:
found(fresh — free function, class, inline method, in-class prototype, namespaced
function, out-of-line definition, nested namespace), missing->stale,
breaking->stale (UNAMBIGUOUS single-decl only), additive->NOT-stale,
parse_error->unverifiable, file-missing->stale, the false-stale pin (an unrelated
edit must not flip the verdict), and portability (a token minted against one repo
root recomputes identically against another).

C++ ships CONSERVATIVE fidelity (plan §8): existence always, the shape fingerprint
only for a single unambiguous declaration. The two load-bearing C++ guards are
pinned here and each checked with a deliberate one-line break of its logic (RULE-2)
during development:

  - OVERLOADING IS THE SAFETY VALVE: a name resolving to MORE THAN ONE declaration
    (an overload set, or a same-file declaration + out-of-line definition) is
    ambiguous -> interface unverifiable. It mints no fingerprint, and adding an
    overload after capture is NEVER reported as a breaking signature change — an
    overload set can never be a wrong "stale" verdict on one overload's shape.
  - OUT-OF-LINE / OPEN-NAMESPACE INDIRECTION: an absent `Class::member` whose class
    is present here may be DEFINED out-of-line in another translation unit; an
    absent `ns::member` whose namespace is present may be added where that OPEN
    namespace is reopened elsewhere. Both read `symbol_indirect` (unverifiable),
    NEVER `symbol_missing`. Only a name with zero footing in the file is missing.
"""

from __future__ import annotations

from pathlib import Path

from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
from hive.combdrift.types import (
    REASON_FILE_MISSING,
    REASON_FINGERPRINT_VERSION_MISMATCH,
    REASON_NO_FINGERPRINT,
    REASON_OK,
    REASON_PARSE_ERROR,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_INDIRECT,
    REASON_SYMBOL_MISSING,
    Anchor,
)

# A single C++ file exercising a free function, a class with an inline method and
# a bodiless in-class prototype, a file-scope variable, and a namespaced free
# function. Line numbers are asserted; the file has NO `#include`, so a genuinely
# absent name is provably missing.
_AUTH = (
    "int refresh(char *token) {\n"  # 1  free function (single decl)
    "    return 0;\n"  # 2
    "}\n"  # 3
    "\n"  # 4
    "class Session {\n"  # 5  class
    "public:\n"  # 6
    "    int login(char *user) {\n"  # 7  inline method (single decl)
    "        return 0;\n"  # 8
    "    }\n"  # 9
    "    int rotate(int n);\n"  # 10 in-class prototype (single decl)
    "};\n"  # 11
    "\n"  # 12
    "int max_age = 3600;\n"  # 13 file-scope variable -> noncallable indirect
    "\n"  # 14
    "namespace api {\n"  # 15
    "    int connect(int port) {\n"  # 16 namespaced free function -> api::connect
    "        return 0;\n"  # 17
    "    }\n"  # 18
    "}\n"  # 19
)

# `good` and `also_good` recover clean; `bad` sits in the error region.
_BROKEN = "int good(void) {}\nint bad( {\nint also_good(void) {}\n"


def _repo(make_repo, files: dict[str, str] | None = None, name: str = "repo") -> str:
    return make_repo(files or {"pkg/auth.cpp": _AUTH}, name=name)


# --- found (existence) --------------------------------------------------------


def test_cpp_free_function_found_with_location(make_repo):
    # A single unambiguous declaration HAS a comparable shape, but no baseline
    # fingerprint was minted here, so nothing was compared: the symbol resolves
    # with its location, and the uncompared shape reports no_fingerprint.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", "refresh"))
    assert res.found is True
    assert res.reason == REASON_NO_FINGERPRINT
    assert res.location == "pkg/auth.cpp:1"


def test_cpp_class_found_with_location(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", "Session"))
    assert res.found is True
    assert res.location == "pkg/auth.cpp:5"


def test_cpp_inline_method_resolves_as_qualified(make_repo):
    # An inline method resolves as the scope-qualified "Class::method".
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", "Session::login"))
    assert res.found is True
    assert res.location == "pkg/auth.cpp:7"


def test_cpp_in_class_prototype_found(make_repo):
    # A bodiless in-class prototype resolves like an inline method (existence).
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", "Session::rotate"))
    assert res.found is True
    assert res.location == "pkg/auth.cpp:10"


def test_cpp_namespaced_function_found(make_repo):
    # A free function inside `namespace api` is anchored qualified as api::connect.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", "api::connect"))
    assert res.found is True
    assert res.location == "pkg/auth.cpp:16"


def test_cpp_symbol_none_is_file_presence(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", None))
    assert res.found is True
    assert res.location == "pkg/auth.cpp:1"


def test_cpp_hpp_extension_resolves_through_cpp_backend(make_repo):
    # `.hpp` is registered to the C++ backend: a prototype in a header resolves.
    files = {
        "include/session.hpp": "class Session {\npublic:\n  int login(char *u);\n};\n"
    }
    res = resolve_anchor(
        _repo(make_repo, files), Anchor("include/session.hpp", "Session::login")
    )
    assert res.found is True
    assert res.location == "include/session.hpp:3"


def test_cpp_out_of_line_definition_belongs_to_class(make_repo):
    # An out-of-line definition `int Session::login(...) {}` is the sole declaration
    # of the method in this .cpp (the class body lives in the header) and resolves
    # as belonging to Session — the out-of-line `Class::method` case.
    files = {"src/session.cpp": "int Session::login(char *user) {\n    return 0;\n}\n"}
    res = resolve_anchor(
        _repo(make_repo, files), Anchor("src/session.cpp", "Session::login")
    )
    assert res.found is True
    assert res.location == "src/session.cpp:1"


def test_cpp_namespaced_class_and_method_resolve(make_repo):
    # A class and its inline method inside a namespace resolve as ns::Class and
    # ns::Class::method.
    files = {
        "src/w.cpp": (
            "namespace ns {\n"  # 1
            "class Widget {\n"  # 2
            "public:\n"  # 3
            "    int draw(int n) {\n"  # 4
            "        return n;\n"  # 5
            "    }\n"  # 6
            "};\n"  # 7
            "}\n"  # 8
        )
    }
    repo = _repo(make_repo, files)
    assert (
        resolve_anchor(repo, Anchor("src/w.cpp", "ns::Widget")).location
        == "src/w.cpp:2"
    )
    res = resolve_anchor(repo, Anchor("src/w.cpp", "ns::Widget::draw"))
    assert res.found is True
    assert res.location == "src/w.cpp:4"


# --- missing -> stale ---------------------------------------------------------


def test_cpp_truly_absent_symbol_is_missing(make_repo):
    # Zero footing in a no-include file: provably absent from it.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", "Deleted"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)


def test_cpp_absent_name_is_missing_even_with_an_include(make_repo):
    # The missing row is NOT weakened by a `#include`: the anchor points at THIS
    # file, and `gone` appears nowhere in it, so it is genuinely absent here.
    files = {"pkg/user.cpp": '#include "db.hpp"\nint load() {\n    return 0;\n}\n'}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/user.cpp", "gone"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)


# --- Law-1 indirect guards: unverifiable, never missing -----------------------


def test_cpp_file_scope_variable_is_symbol_indirect(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", "max_age"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_cpp_absent_method_on_present_class_is_indirect_not_missing(make_repo):
    # THE out-of-line guard: Session's body is right here and has no `gone`, yet the
    # verdict is indirect — the method may be DEFINED out-of-line in another
    # translation unit, so it is never provably deleted.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", "Session::gone"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_cpp_absent_method_on_struct_is_indirect(make_repo):
    # Same guard for a struct type present here whose member is not declared: the
    # member may be defined out-of-line elsewhere.
    files = {"pkg/cfg.cpp": "struct Config {\n    int port;\n};\n"}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/cfg.cpp", "Config::load"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_cpp_absent_member_of_open_namespace_is_indirect(make_repo):
    # THE open-namespace guard: `api` is present here but has no `gone`; a namespace
    # is OPEN and may be reopened in another translation unit, so an absent member
    # is never provably deleted.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", "api::gone"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_cpp_macro_defined_name_is_indirect_not_missing(make_repo):
    # A `#define`d name is present but not a callable combdrift fingerprints, and
    # this tool does not expand it -> indirect, never missing.
    files = {"pkg/m.cpp": "#define REGISTER(x) (x)\nint use() {\n    return 0;\n}\n"}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/m.cpp", "REGISTER"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_cpp_namespaced_function_by_bare_name_is_indirect(make_repo):
    # A namespaced function anchored by its BARE name is present but not a bare
    # global callable -> indirect (it must be anchored qualified), never missing.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.cpp", "connect"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_cpp_header_call_site_name_is_indirect_not_missing(make_repo):
    # `db_query` is called here but declared in a `#include`d header not in this
    # snapshot, so its absence is not provable -> indirect.
    files = {
        "pkg/user.cpp": '#include "db.hpp"\nint load() {\n    return db_query();\n}\n'
    }
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/user.cpp", "db_query"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


# --- breaking-change -> stale (UNAMBIGUOUS single-decl only) ------------------


def test_cpp_required_param_removed_is_signature_changed(make_repo):
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.cpp", "refresh"))
    assert fp is not None and fp.startswith("combdrift-fp/1:")
    (Path(repo) / "pkg/auth.cpp").write_text(
        _AUTH.replace("int refresh(char *token) {", "int refresh() {"), encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.cpp", "refresh", fp))
    assert res.found is True  # symbol still resolves; only its shape drifted
    assert res.reason.startswith(REASON_SIGNATURE_CHANGED)


def test_cpp_method_required_param_removed_is_signature_changed(make_repo):
    # The single inline method carries a real interface, so its drift is caught.
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.cpp", "Session::login"))
    assert fp is not None
    (Path(repo) / "pkg/auth.cpp").write_text(
        _AUTH.replace("int login(char *user) {", "int login() {"), encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.cpp", "Session::login", fp))
    assert res.found is True
    assert res.reason.startswith(REASON_SIGNATURE_CHANGED)


# --- overload set -> ambiguous -> unverifiable (THE pinned safety valve) -------


def test_cpp_overload_set_mints_no_fingerprint(make_repo):
    # An overload set is ambiguous: capture cannot mint a single shape for it.
    repo = _repo(
        make_repo,
        {
            "src/e.cpp": "int emit(int a) { return a; }\nint emit(double a) { return 0; }\n"
        },
    )
    assert fingerprint_anchor(repo, Anchor("src/e.cpp", "emit")) is None


def test_cpp_overload_set_is_found_but_unverifiable(make_repo):
    # The overload set still EXISTS (found), it simply has no single shape.
    repo = _repo(
        make_repo,
        {
            "src/e.cpp": "int emit(int a) { return a; }\nint emit(double a) { return 0; }\n"
        },
    )
    res = resolve_anchor(repo, Anchor("src/e.cpp", "emit"))
    assert res.found is True
    assert res.reason == REASON_OK


def test_cpp_adding_an_overload_is_never_a_breaking_verdict(make_repo):
    # THE load-bearing pin (§6 note ²): mint against a single unambiguous decl,
    # then ADD an overload. The name now resolves to >1 declaration -> ambiguous,
    # so the verdict is unverifiable, NEVER a wrong `signature_changed`.
    repo = _repo(make_repo, {"src/e.cpp": "int emit(int a) {\n    return a;\n}\n"})
    fp = fingerprint_anchor(repo, Anchor("src/e.cpp", "emit"))
    assert fp is not None
    (Path(repo) / "src/e.cpp").write_text(
        "int emit(int a) {\n    return a;\n}\nint emit(double a) {\n    return 0;\n}\n",
        encoding="utf-8",
    )
    res = resolve_anchor(repo, Anchor("src/e.cpp", "emit", fp))
    assert res.found is True  # the symbol still exists
    assert not res.reason.startswith(REASON_SIGNATURE_CHANGED)  # never a wrong break
    assert res.reason.startswith(REASON_FINGERPRINT_VERSION_MISMATCH)
    assert "ambiguous_overload" in res.reason


def test_cpp_same_file_declaration_and_out_of_line_definition_is_ambiguous(make_repo):
    # A same-file in-class declaration PLUS its out-of-line definition are two
    # declaration sites of the name -> ambiguous -> mints no fingerprint (a
    # conservative-fidelity consequence: without overload-signature modelling the
    # tool cannot prove they are the same function, so it stays unverifiable).
    files = {
        "src/s.cpp": (
            "class S {\npublic:\n  int f(int a);\n};\n"
            "int S::f(int a) {\n    return a;\n}\n"
        )
    }
    repo = _repo(make_repo, files)
    assert fingerprint_anchor(repo, Anchor("src/s.cpp", "S::f")) is None
    assert resolve_anchor(repo, Anchor("src/s.cpp", "S::f")).found is True


# --- additive -> NOT stale ----------------------------------------------------


def test_cpp_added_variadic_param_is_ok(make_repo):
    # A trailing `...` keeps every previously-valid call valid (additive).
    repo = _repo(
        make_repo, {"src/log.cpp": "int log_msg(char *m) {\n    return 0;\n}\n"}
    )
    fp = fingerprint_anchor(repo, Anchor("src/log.cpp", "log_msg"))
    assert fp is not None
    (Path(repo) / "src/log.cpp").write_text(
        "int log_msg(char *m, ...) {\n    return 0;\n}\n", encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("src/log.cpp", "log_msg", fp))
    assert res.found is True
    assert res.reason == REASON_OK


def test_cpp_added_default_param_is_ok(make_repo):
    # A new DEFAULTED parameter grows max but not required, so every previously-
    # valid call stays valid (additive), never stale.
    repo = _repo(make_repo, {"src/g.cpp": "int g(int a) {\n    return a;\n}\n"})
    fp = fingerprint_anchor(repo, Anchor("src/g.cpp", "g"))
    assert fp is not None
    (Path(repo) / "src/g.cpp").write_text(
        "int g(int a, int b = 2) {\n    return a;\n}\n", encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("src/g.cpp", "g", fp))
    assert res.found is True
    assert res.reason == REASON_OK


# --- void / empty parens do not change the fingerprint (a C++-specific pin) ----


def test_cpp_void_and_empty_param_lists_mint_identical_token(make_repo):
    # `f(void)` and `f()` are both zero parameters, so tightening `()` to `(void)`
    # mints the identical token and is never stale.
    empty = _repo(
        make_repo, {"src/a.cpp": "int f() {\n    return 0;\n}\n"}, name="empty"
    )
    voided = _repo(
        make_repo, {"src/a.cpp": "int f(void) {\n    return 0;\n}\n"}, name="voided"
    )
    fp_empty = fingerprint_anchor(empty, Anchor("src/a.cpp", "f"))
    fp_void = fingerprint_anchor(voided, Anchor("src/a.cpp", "f"))
    assert fp_empty is not None
    assert fp_empty == fp_void


# --- templates do not change counts (Rust-parity pin) -------------------------


def test_cpp_templates_do_not_change_the_fingerprint(make_repo):
    # A generic and a non-generic form of the same function mint the identical
    # token: the `<T>` parameters live outside the parameter list.
    plain = _repo(
        make_repo,
        {"src/a.cpp": "int f(int a, int b) {\n    return a;\n}\n"},
        name="plain",
    )
    generic = _repo(
        make_repo,
        {"src/a.cpp": "template <typename T>\nint f(T a, int b) {\n    return 0;\n}\n"},
        name="generic",
    )
    fp_plain = fingerprint_anchor(plain, Anchor("src/a.cpp", "f"))
    fp_generic = fingerprint_anchor(generic, Anchor("src/a.cpp", "f"))
    assert fp_plain is not None
    assert fp_plain == fp_generic


def test_cpp_making_a_function_a_template_is_not_stale(make_repo):
    # The end-to-end consequence: adding `<T>` after capture is never stale.
    repo = _repo(make_repo, {"src/a.cpp": "int f(int a) {\n    return a;\n}\n"})
    fp = fingerprint_anchor(repo, Anchor("src/a.cpp", "f"))
    (Path(repo) / "src/a.cpp").write_text(
        "template <typename T>\nint f(T a) {\n    return a;\n}\n", encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("src/a.cpp", "f", fp))
    assert res.found is True
    assert res.reason == REASON_OK


# --- parse_error -> unverifiable ----------------------------------------------


def test_cpp_symbol_in_error_region_is_parse_error(make_repo):
    repo = _repo(make_repo, {"pkg/broken.cpp": _BROKEN})
    res = resolve_anchor(repo, Anchor("pkg/broken.cpp", "bad"))
    assert res.found is False
    assert res.reason.startswith(REASON_PARSE_ERROR)


def test_cpp_clean_symbol_found_despite_error(make_repo):
    repo = _repo(make_repo, {"pkg/broken.cpp": _BROKEN})
    res = resolve_anchor(repo, Anchor("pkg/broken.cpp", "good"))
    assert res.found is True
    assert res.location == "pkg/broken.cpp:1"


# --- file-missing -> stale ----------------------------------------------------


def test_cpp_missing_file_is_file_missing(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/gone.cpp", "refresh"))
    assert res.found is False
    assert res.reason.startswith(REASON_FILE_MISSING)


# --- false-stale pin: an unrelated edit must not flip the verdict --------------


def test_cpp_unrelated_edit_is_not_stale(make_repo):
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.cpp", "refresh"))
    # Add a comment and an unrelated function; `refresh` itself is untouched.
    (Path(repo) / "pkg/auth.cpp").write_text(
        "// a new unrelated comment\n"
        "int sibling() {\n    return 1;\n}\n"
        "\n"
        "int refresh(char *token) {\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.cpp", "refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK  # unrelated edit is not a drift


# --- portability: device-invariant fingerprint --------------------------------


def test_cpp_fingerprint_is_root_invariant(make_repo):
    # The same file content at two different repo roots mints the identical token,
    # so a token minted on one device recomputes on another (no absolute path leaks).
    r1 = _repo(make_repo, name="root1")
    r2 = _repo(make_repo, name="root2")
    fp1 = fingerprint_anchor(r1, Anchor("pkg/auth.cpp", "refresh"))
    fp2 = fingerprint_anchor(r2, Anchor("pkg/auth.cpp", "refresh"))
    assert fp1 is not None
    assert fp1 == fp2


def test_cpp_token_minted_at_one_root_verifies_at_another(make_repo):
    r1 = _repo(make_repo, name="rootA")
    r2 = _repo(make_repo, name="rootB")
    fp = fingerprint_anchor(r1, Anchor("pkg/auth.cpp", "refresh"))
    res = resolve_anchor(r2, Anchor("pkg/auth.cpp", "refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK
