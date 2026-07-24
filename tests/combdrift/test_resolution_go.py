"""resolve_anchor / fingerprint_anchor against Go repos: the §6 verdict matrix.

Exercises the full per-language contract end-to-end through the filesystem seam:
found(fresh), missing->stale, breaking->stale, additive->NOT-stale,
parse_error->unverifiable, file-missing->stale, the false-stale pin (an unrelated
edit must not flip the verdict), the Law-1 indirect guards (a same-package /
dot-import name reads unverifiable, never missing), and portability (a token
minted against one repo root recomputes identically against another — the
device-invariant fingerprint).
"""

from __future__ import annotations

from pathlib import Path

from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
from hive.combdrift.types import (
    REASON_FILE_MISSING,
    REASON_OK,
    REASON_PARSE_ERROR,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_INDIRECT,
    REASON_SYMBOL_MISSING,
    Anchor,
)

# A single-package Go file exercising a free function, a method keyed on its
# receiver, a struct type, and a package-level var. Line numbers are asserted.
_AUTH = (
    "package auth\n"  # 1
    "\n"  # 2
    "func Refresh(token string) string {\n"  # 3
    "\treturn token\n"  # 4
    "}\n"  # 5
    "\n"  # 6
    "func (s *Session) Login(user string) string {\n"  # 7
    "\treturn user\n"  # 8
    "}\n"  # 9
    "\n"  # 10
    "type Session struct {\n"  # 11
    "\tName string\n"  # 12
    "}\n"  # 13
    "\n"  # 14
    "var MaxAge = 3600\n"  # 15
)

_BROKEN = "package p\nfunc Good() {}\nfunc Bad( {\nfunc AlsoGood() {}\n"


def _repo(make_repo, files: dict[str, str] | None = None, name: str = "repo") -> str:
    return make_repo(files or {"pkg/auth.go": _AUTH}, name=name)


# --- found (fresh) ------------------------------------------------------------


def test_go_function_found_with_location(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.go", "Refresh"))
    assert res.found is True
    assert res.reason == REASON_OK
    assert res.location == "pkg/auth.go:3"


def test_go_method_found_with_location(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.go", "Session.Login"))
    assert res.found is True
    assert res.location == "pkg/auth.go:7"


def test_go_struct_type_found(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.go", "Session"))
    assert res.found is True
    assert res.location == "pkg/auth.go:11"


def test_go_symbol_none_is_file_presence(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.go", None))
    assert res.found is True
    assert res.location == "pkg/auth.go:1"


# --- missing -> stale ---------------------------------------------------------


def test_go_truly_absent_symbol_is_missing(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.go", "Deleted"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)


# --- Law-1 indirect guards: unverifiable, never missing -----------------------


def test_go_package_var_is_symbol_indirect(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.go", "MaxAge"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_go_dot_import_absence_is_symbol_indirect_not_missing(make_repo):
    files = {"pkg/m.go": 'package p\nimport . "math"\nfunc U() {}\n'}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/m.go", "Sqrt"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_go_absent_method_is_symbol_indirect_not_missing(make_repo):
    # Methods live at package scope: an absent one may be in a sibling file.
    files = {"pkg/s.go": "package p\ntype Server struct{}\n"}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/s.go", "Server.Gone"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


# --- breaking-change -> stale -------------------------------------------------


def test_go_required_param_removed_is_signature_changed(make_repo):
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.go", "Refresh"))
    assert fp is not None and fp.startswith("combdrift-fp/1:")
    (Path(repo) / "pkg/auth.go").write_text(
        'package auth\nfunc Refresh() string {\n\treturn ""\n}\n', encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.go", "Refresh", fp))
    assert res.found is True  # symbol still resolves; only its shape drifted
    assert res.reason.startswith(REASON_SIGNATURE_CHANGED)


# --- additive -> NOT stale ----------------------------------------------------


def test_go_added_variadic_param_is_ok(make_repo):
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.go", "Refresh"))
    # A trailing variadic keeps every previously-valid call valid (additive).
    (Path(repo) / "pkg/auth.go").write_text(
        "package auth\nfunc Refresh(token string, rest ...int) string {\n"
        "\treturn token\n}\n",
        encoding="utf-8",
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.go", "Refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK


# --- parse_error -> unverifiable ----------------------------------------------


def test_go_symbol_in_error_region_is_parse_error(make_repo):
    repo = _repo(make_repo, {"pkg/broken.go": _BROKEN})
    res = resolve_anchor(repo, Anchor("pkg/broken.go", "Bad"))
    assert res.found is False
    assert res.reason.startswith(REASON_PARSE_ERROR)


def test_go_clean_symbol_found_despite_error(make_repo):
    repo = _repo(make_repo, {"pkg/broken.go": _BROKEN})
    res = resolve_anchor(repo, Anchor("pkg/broken.go", "Good"))
    assert res.found is True
    assert res.location == "pkg/broken.go:2"


# --- file-missing -> stale ----------------------------------------------------


def test_go_missing_file_is_file_missing(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/gone.go", "Refresh"))
    assert res.found is False
    assert res.reason.startswith(REASON_FILE_MISSING)


# --- false-stale pin: an unrelated edit must not flip the verdict --------------


def test_go_unrelated_edit_is_not_stale(make_repo):
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.go", "Refresh"))
    # Add a comment and an unrelated function; `Refresh` itself is untouched.
    (Path(repo) / "pkg/auth.go").write_text(
        "package auth\n"
        "// a new unrelated comment\n"
        "func Sibling() {}\n"
        "\n"
        "func Refresh(token string) string {\n"
        "\treturn token\n"
        "}\n",
        encoding="utf-8",
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.go", "Refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK  # unrelated edit is not a drift


# --- portability: device-invariant fingerprint --------------------------------


def test_go_fingerprint_is_root_invariant(make_repo):
    # The same file content at two different repo roots mints the identical token,
    # so a token minted on one device recomputes on another (no absolute path leaks).
    r1 = _repo(make_repo, name="root1")
    r2 = _repo(make_repo, name="root2")
    fp1 = fingerprint_anchor(r1, Anchor("pkg/auth.go", "Refresh"))
    fp2 = fingerprint_anchor(r2, Anchor("pkg/auth.go", "Refresh"))
    assert fp1 is not None
    assert fp1 == fp2


def test_go_token_minted_at_one_root_verifies_at_another(make_repo):
    r1 = _repo(make_repo, name="rootA")
    r2 = _repo(make_repo, name="rootB")
    fp = fingerprint_anchor(r1, Anchor("pkg/auth.go", "Refresh"))
    res = resolve_anchor(r2, Anchor("pkg/auth.go", "Refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK
