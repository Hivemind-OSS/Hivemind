"""resolve_anchor / fingerprint_anchor against C repos: the §6 verdict matrix.

Exercises the full per-language contract end-to-end through the filesystem seam:
found(fresh, both a definition and a header-style prototype), missing->stale,
breaking->stale, additive->NOT-stale, parse_error->unverifiable,
file-missing->stale, the false-stale pin (an unrelated edit must not flip the
verdict), and portability (a token minted against one repo root recomputes
identically against another).

The load-bearing C guards, pinned here and each checked with a deliberate one-line
break (RULE-2) during development. C has no module system this tool resolves, so a
name can legitimately be supplied from outside what combdrift parses; the
textual-occurrence line is the primary Law-1 false-stale guard:
  - a name that occurs ANYWHERE in the file but has no clean declaration here — a
    call site whose declaration is in a `#include`d header, a `#define`d macro, or
    an `#ifdef` operand — is `symbol_indirect` (unverifiable), NEVER missing;
  - a function DEFINED inside an `#ifdef`/`#if` branch is still found, not missing;
  - only a name with ZERO textual occurrence in the file is provably absent from
    it (missing -> stale) — even when the file `#include`s a header, because the
    anchor points at THIS file and a name that appears nowhere in it is not here.
"""

from __future__ import annotations

from pathlib import Path

from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
from hive.combdrift.types import (
    REASON_FILE_MISSING,
    REASON_NO_FINGERPRINT,
    REASON_OK,
    REASON_PARSE_ERROR,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_INDIRECT,
    REASON_SYMBOL_MISSING,
    Anchor,
)

# A single C file exercising a function definition (with a body), a header-style
# bodiless prototype, and a file-scope variable. Line numbers are asserted, and
# the file has NO `#include`, so a genuinely absent name is provably missing.
_AUTH = (
    "int refresh(char *token) {\n"  # 1  function_definition
    "    return 0;\n"  # 2
    "}\n"  # 3
    "\n"  # 4
    "char *login(char *user);\n"  # 5  bodiless prototype (forward declaration)
    "\n"  # 6
    "int max_age = 3600;\n"  # 7  file-scope variable -> noncallable indirect
)

# `good` and `also_good` recover clean; `bad` sits in the error region.
_BROKEN = "int good(void) {}\nint bad( {\nint also_good(void) {}\n"


def _repo(make_repo, files: dict[str, str] | None = None, name: str = "repo") -> str:
    return make_repo(files or {"pkg/auth.c": _AUTH}, name=name)


# --- found (existence) --------------------------------------------------------


def test_c_function_definition_found_with_location(make_repo):
    # No baseline fingerprint, so no shape comparison ever ran: the symbol
    # resolves with its location, but `ok` would claim a comparison that
    # never happened, so an uncompared callable reports no_fingerprint.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.c", "refresh"))
    assert res.found is True
    assert res.reason == REASON_NO_FINGERPRINT
    assert res.location == "pkg/auth.c:1"


def test_c_prototype_declaration_found_with_location(make_repo):
    # A header-style forward declaration (no body) resolves like a definition.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.c", "login"))
    assert res.found is True
    assert res.location == "pkg/auth.c:5"


def test_c_symbol_none_is_file_presence(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.c", None))
    assert res.found is True
    assert res.location == "pkg/auth.c:1"


def test_c_header_extension_resolves_through_c_backend(make_repo):
    # `.h` is registered to the C backend: a prototype in a header resolves.
    files = {"include/auth.h": "char *login(char *user);\nint refresh(char *token);\n"}
    res = resolve_anchor(_repo(make_repo, files), Anchor("include/auth.h", "refresh"))
    assert res.found is True
    assert res.location == "include/auth.h:2"


# --- missing -> stale ---------------------------------------------------------


def test_c_truly_absent_symbol_is_missing(make_repo):
    # Zero textual occurrence in a no-include file: provably absent from it.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.c", "Deleted"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)


def test_c_absent_name_is_missing_even_with_an_include(make_repo):
    # The missing row is NOT weakened by the presence of a `#include`: the anchor
    # points at THIS file, and `gone` appears nowhere in it, so it is genuinely
    # absent here (a header supplying `gone` would mean it lives in the header).
    files = {"pkg/user.c": '#include "db.h"\nint load(void) {\n    return 0;\n}\n'}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/user.c", "gone"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)


# --- Law-1 indirect guards: unverifiable, never missing -----------------------


def test_c_file_scope_variable_is_symbol_indirect(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.c", "max_age"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_c_header_call_site_name_is_indirect_not_missing(make_repo):
    # THE header guard: `db_query` is called here but declared in a `#include`d
    # header not in this snapshot, so its absence is not provable -> indirect.
    files = {
        "pkg/user.c": '#include "db.h"\nint load(void) {\n    return db_query();\n}\n'
    }
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/user.c", "db_query"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_c_macro_defined_name_is_indirect_not_missing(make_repo):
    # THE macro guard: a `#define`d name is present but not a callable combdrift
    # fingerprints, and this tool does not expand it -> indirect, never missing.
    files = {"pkg/m.c": "#define REGISTER(x) (x)\nint use(void) {\n    return 0;\n}\n"}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/m.c", "REGISTER"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_c_conditional_compile_operand_is_indirect_not_missing(make_repo):
    # THE conditional-compile guard: `FEATURE_FLAG` occurs only as an `#ifdef`
    # operand; a naive scan could read it as absent, but it is referenced here.
    files = {
        "pkg/c.c": "#ifdef FEATURE_FLAG\nint enabled(void) {\n    return 1;\n}\n#endif\n"
    }
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/c.c", "FEATURE_FLAG"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_c_function_under_ifdef_is_found_not_missing(make_repo):
    # The other half of the conditional-compile guard: a function genuinely
    # DEFINED inside an `#ifdef` branch is at file scope and must be found, so a
    # real conditionally-compiled function is never a false-stale.
    files = {"pkg/c.c": "#ifdef DEBUG\nint dbg(char *m) {\n    return 0;\n}\n#endif\n"}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/c.c", "dbg"))
    assert res.found is True
    assert res.location == "pkg/c.c:2"


# --- breaking-change -> stale -------------------------------------------------


def test_c_required_param_removed_is_signature_changed(make_repo):
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.c", "refresh"))
    assert fp is not None and fp.startswith("combdrift-fp/1:")
    (Path(repo) / "pkg/auth.c").write_text(
        "int refresh(void) {\n    return 0;\n}\n", encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.c", "refresh", fp))
    assert res.found is True  # symbol still resolves; only its shape drifted
    assert res.reason.startswith(REASON_SIGNATURE_CHANGED)


# --- additive -> NOT stale ----------------------------------------------------


def test_c_added_variadic_param_is_ok(make_repo):
    # A trailing `...` keeps every previously-valid call valid (additive).
    repo = _repo(make_repo, {"src/log.c": "int log_msg(char *m) {\n    return 0;\n}\n"})
    fp = fingerprint_anchor(repo, Anchor("src/log.c", "log_msg"))
    assert fp is not None
    (Path(repo) / "src/log.c").write_text(
        "int log_msg(char *m, ...) {\n    return 0;\n}\n", encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("src/log.c", "log_msg", fp))
    assert res.found is True
    assert res.reason == REASON_OK


# --- void / empty parens do not change the fingerprint (a C-specific pin) ------


def test_c_void_and_empty_param_lists_mint_identical_token(make_repo):
    # `f(void)` and `f()` are both zero parameters, so tightening `()` to `(void)`
    # (a common C cleanup) mints the identical token and is never stale.
    empty = _repo(make_repo, {"src/a.c": "int f() {\n    return 0;\n}\n"}, name="empty")
    voided = _repo(
        make_repo, {"src/a.c": "int f(void) {\n    return 0;\n}\n"}, name="voided"
    )
    fp_empty = fingerprint_anchor(empty, Anchor("src/a.c", "f"))
    fp_void = fingerprint_anchor(voided, Anchor("src/a.c", "f"))
    assert fp_empty is not None
    assert fp_empty == fp_void


# --- parse_error -> unverifiable ----------------------------------------------


def test_c_symbol_in_error_region_is_parse_error(make_repo):
    repo = _repo(make_repo, {"pkg/broken.c": _BROKEN})
    res = resolve_anchor(repo, Anchor("pkg/broken.c", "bad"))
    assert res.found is False
    assert res.reason.startswith(REASON_PARSE_ERROR)


def test_c_clean_symbol_found_despite_error(make_repo):
    repo = _repo(make_repo, {"pkg/broken.c": _BROKEN})
    res = resolve_anchor(repo, Anchor("pkg/broken.c", "good"))
    assert res.found is True
    assert res.location == "pkg/broken.c:1"


# --- file-missing -> stale ----------------------------------------------------


def test_c_missing_file_is_file_missing(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/gone.c", "refresh"))
    assert res.found is False
    assert res.reason.startswith(REASON_FILE_MISSING)


# --- false-stale pin: an unrelated edit must not flip the verdict --------------


def test_c_unrelated_edit_is_not_stale(make_repo):
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.c", "refresh"))
    # Add a comment and an unrelated function; `refresh` itself is untouched.
    (Path(repo) / "pkg/auth.c").write_text(
        "// a new unrelated comment\n"
        "int sibling(void) {\n    return 1;\n}\n"
        "\n"
        "int refresh(char *token) {\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.c", "refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK  # unrelated edit is not a drift


# --- portability: device-invariant fingerprint --------------------------------


def test_c_fingerprint_is_root_invariant(make_repo):
    # The same file content at two different repo roots mints the identical token,
    # so a token minted on one device recomputes on another (no absolute path leaks).
    r1 = _repo(make_repo, name="root1")
    r2 = _repo(make_repo, name="root2")
    fp1 = fingerprint_anchor(r1, Anchor("pkg/auth.c", "refresh"))
    fp2 = fingerprint_anchor(r2, Anchor("pkg/auth.c", "refresh"))
    assert fp1 is not None
    assert fp1 == fp2


def test_c_token_minted_at_one_root_verifies_at_another(make_repo):
    r1 = _repo(make_repo, name="rootA")
    r2 = _repo(make_repo, name="rootB")
    fp = fingerprint_anchor(r1, Anchor("pkg/auth.c", "refresh"))
    res = resolve_anchor(r2, Anchor("pkg/auth.c", "refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK
