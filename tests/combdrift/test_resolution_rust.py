"""resolve_anchor / fingerprint_anchor against Rust repos: the §6 verdict matrix.

Exercises the full per-language contract end-to-end through the filesystem seam:
found(fresh), missing->stale, breaking->stale, additive->NOT-stale,
parse_error->unverifiable, file-missing->stale, the false-stale pin (an unrelated
edit must not flip the verdict), the Law-1 indirect guards (an absent method whose
impl may live in another file, a const, a glob/use import all read unverifiable,
never missing), the `pub use` re-export follow, and portability (a token minted
against one repo root recomputes identically against another).

The load-bearing Rust guards, pinned here and checked with a deliberate one-line
break of each (RULE-2) during development:
  - an absent `Type.method` is `symbol_indirect` (the impl may be in another file),
    NEVER `symbol_missing` — Rust methods are not file-local;
  - a generic and a non-generic form of the same function mint the identical
    token — generics live in a separate field and never change the param count.
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

# A single Rust file exercising a free function, a struct, a method in an impl
# block keyed on its type, and a module-level const. Line numbers are asserted.
_AUTH = (
    "pub fn refresh(token: String) -> String {\n"  # 1
    "    token\n"  # 2
    "}\n"  # 3
    "\n"  # 4
    "pub struct Session {\n"  # 5
    "    name: String,\n"  # 6
    "}\n"  # 7
    "\n"  # 8
    "impl Session {\n"  # 9
    "    pub fn login(&self, user: String) -> String {\n"  # 10
    "        user\n"  # 11
    "    }\n"  # 12
    "}\n"  # 13
    "\n"  # 14
    "const MAX_AGE: i32 = 3600;\n"  # 15
)

# `good` and `also_good` recover clean; `bad` sits in the error region.
_BROKEN = "pub fn good() {}\npub fn bad( {\npub fn also_good() {}\n"


def _repo(make_repo, files: dict[str, str] | None = None, name: str = "repo") -> str:
    return make_repo(files or {"pkg/auth.rs": _AUTH}, name=name)


# --- found (existence) --------------------------------------------------------


def test_rust_function_found_with_location(make_repo):
    # No baseline fingerprint, so no shape comparison ever ran: the symbol
    # resolves with its location, but `ok` would claim a comparison that
    # never happened, so an uncompared callable reports no_fingerprint.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.rs", "refresh"))
    assert res.found is True
    assert res.reason == REASON_NO_FINGERPRINT
    assert res.location == "pkg/auth.rs:1"


def test_rust_in_file_method_resolves_as_dotted(make_repo):
    # A method declared in an in-file impl block resolves as "Type.method".
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.rs", "Session.login"))
    assert res.found is True
    assert res.location == "pkg/auth.rs:10"


def test_rust_struct_type_found(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.rs", "Session"))
    assert res.found is True
    assert res.location == "pkg/auth.rs:5"


def test_rust_symbol_none_is_file_presence(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.rs", None))
    assert res.found is True
    assert res.location == "pkg/auth.rs:1"


# --- missing -> stale ---------------------------------------------------------


def test_rust_truly_absent_symbol_is_missing(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.rs", "Deleted"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)


# --- Law-1 indirect guards: unverifiable, never missing -----------------------


def test_rust_absent_method_is_indirect_not_missing(make_repo):
    # THE pinned Law-1 false-stale guard: Session's impl is right here and has no
    # `Gone`, yet the verdict is indirect — another impl block for Session may
    # live in a different file, so the method is never provably deleted.
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.rs", "Session.Gone"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_rust_absent_method_on_impl_less_type_is_indirect(make_repo):
    # Same guard when no impl block is in this file at all: the impl may be elsewhere.
    files = {"pkg/s.rs": "pub struct Server {}\n"}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/s.rs", "Server.start"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_rust_const_is_symbol_indirect(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/auth.rs", "MAX_AGE"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_rust_glob_import_absence_is_indirect_not_missing(make_repo):
    # A glob import can inject any name, so an absent one is never provably deleted.
    files = {"pkg/m.rs": "use other::*;\npub fn u() {}\n"}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/m.rs", "Injected"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_rust_crate_use_reexport_is_indirect(make_repo):
    # A `pub use` re-binds a name from another module; a crate-absolute path is
    # not followable, so it stays unverifiable, never missing.
    files = {"pkg/api.rs": "pub use crate::backend::Thing;\n"}
    res = resolve_anchor(_repo(make_repo, files), Anchor("pkg/api.rs", "Thing"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


# --- breaking-change -> stale -------------------------------------------------


def test_rust_required_param_removed_is_signature_changed(make_repo):
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.rs", "refresh"))
    assert fp is not None and fp.startswith("combdrift-fp/1:")
    (Path(repo) / "pkg/auth.rs").write_text(
        "pub fn refresh() -> String {\n    String::new()\n}\n", encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.rs", "refresh", fp))
    assert res.found is True  # symbol still resolves; only its shape drifted
    assert res.reason.startswith(REASON_SIGNATURE_CHANGED)


def test_rust_method_required_param_removed_is_signature_changed(make_repo):
    # The in-file dotted method carries a real interface, so its drift is caught.
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.rs", "Session.login"))
    assert fp is not None
    (Path(repo) / "pkg/auth.rs").write_text(
        _AUTH.replace(
            "pub fn login(&self, user: String) -> String {",
            "pub fn login(&self) -> String {",
        ),
        encoding="utf-8",
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.rs", "Session.login", fp))
    assert res.found is True
    assert res.reason.startswith(REASON_SIGNATURE_CHANGED)


# --- additive -> NOT stale ----------------------------------------------------


def test_rust_added_variadic_param_is_ok(make_repo):
    # A trailing extern-C variadic keeps every previously-valid call valid.
    repo = _repo(
        make_repo, {"pkg/ffi.rs": 'pub unsafe extern "C" fn sink(a: i32) {}\n'}
    )
    fp = fingerprint_anchor(repo, Anchor("pkg/ffi.rs", "sink"))
    assert fp is not None
    (Path(repo) / "pkg/ffi.rs").write_text(
        'pub unsafe extern "C" fn sink(a: i32, ...) {}\n', encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("pkg/ffi.rs", "sink", fp))
    assert res.found is True
    assert res.reason == REASON_OK


# --- generics do not change counts (the second load-bearing pin) --------------


def test_rust_generics_do_not_change_the_fingerprint(make_repo):
    # A generic and a non-generic form of the same function mint the identical
    # token: generics live in a separate `type_parameters` field, so they never
    # change the parameter count.
    plain = _repo(
        make_repo, {"src/a.rs": "pub fn f(a: i32, b: i32) {}\n"}, name="plain"
    )
    generic = _repo(
        make_repo, {"src/a.rs": "pub fn f<T>(a: T, b: i32) {}\n"}, name="generic"
    )
    fp_plain = fingerprint_anchor(plain, Anchor("src/a.rs", "f"))
    fp_generic = fingerprint_anchor(generic, Anchor("src/a.rs", "f"))
    assert fp_plain is not None
    assert fp_plain == fp_generic


def test_rust_making_a_function_generic_is_not_stale(make_repo):
    # The end-to-end consequence: adding `<T>` after capture is never stale.
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.rs", "refresh"))
    (Path(repo) / "pkg/auth.rs").write_text(
        "pub fn refresh<T>(token: String) -> String {\n    token\n}\n", encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.rs", "refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK


# --- parse_error -> unverifiable ----------------------------------------------


def test_rust_symbol_in_error_region_is_parse_error(make_repo):
    repo = _repo(make_repo, {"pkg/broken.rs": _BROKEN})
    res = resolve_anchor(repo, Anchor("pkg/broken.rs", "bad"))
    assert res.found is False
    assert res.reason.startswith(REASON_PARSE_ERROR)


def test_rust_clean_symbol_found_despite_error(make_repo):
    repo = _repo(make_repo, {"pkg/broken.rs": _BROKEN})
    res = resolve_anchor(repo, Anchor("pkg/broken.rs", "good"))
    assert res.found is True
    assert res.location == "pkg/broken.rs:1"


# --- file-missing -> stale ----------------------------------------------------


def test_rust_missing_file_is_file_missing(make_repo):
    res = resolve_anchor(_repo(make_repo), Anchor("pkg/gone.rs", "refresh"))
    assert res.found is False
    assert res.reason.startswith(REASON_FILE_MISSING)


# --- false-stale pin: an unrelated edit must not flip the verdict --------------


def test_rust_unrelated_edit_is_not_stale(make_repo):
    repo = _repo(make_repo)
    fp = fingerprint_anchor(repo, Anchor("pkg/auth.rs", "refresh"))
    # Add a comment and an unrelated function; `refresh` itself is untouched.
    (Path(repo) / "pkg/auth.rs").write_text(
        "// a new unrelated comment\n"
        "pub fn sibling() {}\n"
        "\n"
        "pub fn refresh(token: String) -> String {\n"
        "    token\n"
        "}\n",
        encoding="utf-8",
    )
    res = resolve_anchor(repo, Anchor("pkg/auth.rs", "refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK  # unrelated edit is not a drift


# --- pub use re-export follow (the Rust mirror of JS/TS `export … from`) -------


def test_rust_follow_relative_reexport_to_found_locates_the_source(make_repo):
    # `pub use self::core::parse` follows to the submodule file src/api/core.rs.
    # The hop lands on a real callable, so the anchor resolves; it carries no
    # baseline fingerprint, so the followed shape was never compared and the
    # result reports no_fingerprint rather than a positive `ok`.
    repo = make_repo(
        {
            "src/api.rs": "pub use self::core::parse;\n",
            "src/api/core.rs": "pub fn parse(token: String) -> String {\n    token\n}\n",
        }
    )
    res = resolve_anchor(repo, Anchor("src/api.rs", "parse"))
    assert res.found is True
    assert res.reason == REASON_NO_FINGERPRINT
    assert res.location == "src/api/core.rs:1"  # points at the SOURCE declaration


def test_rust_follow_relative_reexport_source_deleted_is_stale(make_repo):
    repo = make_repo(
        {
            "src/api.rs": "pub use self::core::parse;\n",
            "src/api/core.rs": "pub fn other() {}\n",  # parse is gone at the source
        }
    )
    res = resolve_anchor(repo, Anchor("src/api.rs", "parse"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)
    assert "(via src/api/core.rs)" in res.reason


# --- portability: device-invariant fingerprint --------------------------------


def test_rust_fingerprint_is_root_invariant(make_repo):
    # The same file content at two different repo roots mints the identical token,
    # so a token minted on one device recomputes on another (no absolute path leaks).
    r1 = _repo(make_repo, name="root1")
    r2 = _repo(make_repo, name="root2")
    fp1 = fingerprint_anchor(r1, Anchor("pkg/auth.rs", "refresh"))
    fp2 = fingerprint_anchor(r2, Anchor("pkg/auth.rs", "refresh"))
    assert fp1 is not None
    assert fp1 == fp2


def test_rust_token_minted_at_one_root_verifies_at_another(make_repo):
    r1 = _repo(make_repo, name="rootA")
    r2 = _repo(make_repo, name="rootB")
    fp = fingerprint_anchor(r1, Anchor("pkg/auth.rs", "refresh"))
    res = resolve_anchor(r2, Anchor("pkg/auth.rs", "refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK
