"""Chunk 2: the deep AST resolver. AST parse only — never imports/executes targets."""

from __future__ import annotations

from pathlib import Path

from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
from hive.combdrift.types import (
    REASON_FILE_MISSING,
    REASON_FINGERPRINT_VERSION_MISMATCH,
    REASON_NO_FINGERPRINT,
    REASON_NO_SYMBOL_REQUESTED,
    REASON_OK,
    REASON_PARSE_ERROR,
    REASON_PATH_OUTSIDE_REPO,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_INDIRECT,
    REASON_SYMBOL_MISSING,
    Anchor,
)


def test_resolve_file_and_symbol_found(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "refresh"))
    assert res.found is True
    # No baseline was recorded, so the call shape was never compared; existence
    # is the only thing proven here.
    assert res.reason == REASON_NO_FINGERPRINT
    # location is "<repo-relative-path>:<lineno>"; refresh is on line 4.
    assert res.location == "pkg/auth.py:4"
    assert res.path == "pkg/auth.py"
    assert res.symbol == "refresh"


def test_resolve_symbol_none_is_file_presence(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", None))
    assert res.found is True
    assert res.reason == REASON_NO_SYMBOL_REQUESTED
    # With no symbol requested, location points at the file (line 1 sentinel).
    assert res.location == "pkg/auth.py:1"


def test_resolve_class_and_method_found(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "Session.login"))
    assert res.found is True
    # A method has a comparable call shape, and none was recorded here.
    assert res.reason == REASON_NO_FINGERPRINT
    # login is defined on line 13 of pkg/auth.py.
    assert res.location == "pkg/auth.py:13"


def test_resolve_async_method_found(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "Session.logout"))
    assert res.found is True
    # Async-ness is part of the shape, so an uncompared async method is no more
    # verified than a sync one.
    assert res.reason == REASON_NO_FINGERPRINT


def test_resolve_classdef_as_symbol_found(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "Session"))
    assert res.found is True
    # A class carries a constructible shape of its own (category + base count),
    # so an un-fingerprinted class is uncompared, not exempt from comparison.
    assert res.reason == REASON_NO_FINGERPRINT
    # class Session is defined on line 12.
    assert res.location == "pkg/auth.py:12"


def test_resolve_async_function_found(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "revoke"))
    assert res.found is True
    assert res.reason == REASON_NO_FINGERPRINT


def test_resolve_missing_symbol_reason_names_symbol(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "nonexistent_fn"))
    assert res.found is False
    assert res.location is None
    # reason carries the machine code AND names the missing symbol for evidence.
    assert res.reason.startswith(REASON_SYMBOL_MISSING)
    assert "nonexistent_fn" in res.reason


def test_resolve_missing_method_on_existing_class(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "Session.nope"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)
    assert "Session.nope" in res.reason


def test_resolve_method_dotted_on_missing_class(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "Ghost.login"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)


def test_resolve_indirect_symbol_is_unverifiable_reason(tmp_repo: Path):
    # TOKEN_TTL is a module-level data binding: present, but not a callable.
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "TOKEN_TTL"))
    assert res.found is False
    assert res.location is None
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)
    assert "noncallable" in res.reason


def test_resolve_missing_file_reason(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/does_not_exist.py", "x"))
    assert res.found is False
    assert res.location is None
    assert res.reason.startswith(REASON_FILE_MISSING)


def test_resolve_path_outside_repo_dotdot(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("../escape.py", None))
    assert res.found is False
    assert res.reason.startswith(REASON_PATH_OUTSIDE_REPO)


def test_resolve_path_outside_repo_absolute(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("/etc/passwd", None))
    assert res.found is False
    assert res.reason.startswith(REASON_PATH_OUTSIDE_REPO)


def test_resolve_syntax_error_unverifiable(tmp_repo: Path):
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/broken.py", "oops"))
    assert res.found is False
    assert res.location is None
    assert res.reason.startswith(REASON_PARSE_ERROR)


# --- Layer B: capture (fingerprint_anchor) ------------------------------------


def test_fingerprint_anchor_mints_versioned_token(tmp_repo: Path):
    fp = fingerprint_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse"))
    assert fp is not None
    assert fp.startswith("combdrift-fp/1:")


def test_fingerprint_anchor_none_without_symbol(tmp_repo: Path):
    assert fingerprint_anchor(str(tmp_repo), Anchor("pkg/auth.py", None)) is None


def test_fingerprint_anchor_none_for_noncallable(tmp_repo: Path):
    # A data binding has no single-callable interface to capture.
    assert fingerprint_anchor(str(tmp_repo), Anchor("pkg/auth.py", "TOKEN_TTL")) is None


def test_fingerprint_anchor_none_for_missing_file(tmp_repo: Path):
    assert fingerprint_anchor(str(tmp_repo), Anchor("pkg/nope.py", "x")) is None


def test_fingerprint_anchor_none_outside_repo(tmp_repo: Path):
    assert fingerprint_anchor(str(tmp_repo), Anchor("../escape.py", "x")) is None


# --- Layer B: verify-time fingerprint comparison ------------------------------


def test_resolve_fingerprint_match_is_ok(tmp_repo: Path):
    fp = fingerprint_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse"))
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse", fp))
    assert res.found is True
    assert res.reason == REASON_OK
    assert res.location == "pkg/parser.py:1"


def test_resolve_fingerprint_mismatch_is_signature_changed(tmp_repo: Path):
    fp = fingerprint_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse"))  # 3 args
    (tmp_repo / "pkg/parser.py").write_text(
        "def parse(a, b):\n    return (a, b)\n", encoding="utf-8"
    )
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse", fp))
    # Drift is stale-signal but the symbol is still right here: found + located.
    assert res.found is True
    assert res.location == "pkg/parser.py:1"
    assert res.reason.startswith(REASON_SIGNATURE_CHANGED)
    assert "->" in res.reason  # carries expected -> current evidence


def test_resolve_fingerprint_additive_change_is_ok(tmp_repo: Path):
    fp = fingerprint_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse"))  # 3 args
    # An added optional argument breaks no previously-valid call.
    (tmp_repo / "pkg/parser.py").write_text(
        "def parse(a, b, c, d=1):\n    return (a, b, c, d)\n", encoding="utf-8"
    )
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse", fp))
    assert res.found is True
    assert res.reason == REASON_OK


def test_resolve_future_fingerprint_version_is_unverifiable(tmp_repo: Path):
    token = "combdrift-fp/9:func(req=0,max=0,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/parser.py", "parse", token))
    assert res.found is True
    assert res.reason.startswith(REASON_FINGERPRINT_VERSION_MISMATCH)


def test_resolve_fingerprint_without_symbol_is_inert(tmp_repo: Path):
    # A fingerprint with no symbol is never honored -> plain file presence.
    res = resolve_anchor(
        str(tmp_repo), Anchor("pkg/auth.py", None, "combdrift-fp/1:func(req=9)")
    )
    assert res.found is True
    assert res.reason == REASON_NO_SYMBOL_REQUESTED


# --- Layer B: a call shape that was never compared ----------------------------


def test_found_callable_without_fingerprint_is_no_fingerprint(tmp_repo: Path):
    # A real single callable whose shape COULD have been compared, but for which
    # no baseline was ever recorded. Existence is proven and nothing about the
    # shape is: found and location stay populated, only the reason abstains.
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "refresh"))
    assert res.found is True
    assert res.reason == REASON_NO_FINGERPRINT
    assert res.location == "pkg/auth.py:4"
    assert res.symbol == "refresh"


def test_minted_fingerprint_turns_the_same_anchor_ok(tmp_repo: Path):
    # The contrast pair for the anchor above: the identical anchor reports `ok`
    # only once a real baseline exists, so `ok` always means a comparison ran.
    fp = fingerprint_anchor(str(tmp_repo), Anchor("pkg/auth.py", "refresh"))
    assert fp is not None
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK
    assert res.location == "pkg/auth.py:4"


def test_file_scoped_anchor_is_not_shadowed_by_no_fingerprint(tmp_repo: Path):
    # A file-scoped anchor never claimed a call shape, so nothing was withheld:
    # it keeps its own reason instead of falling into the uncompared branch.
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", None))
    assert res.found is True
    assert res.reason == REASON_NO_SYMBOL_REQUESTED


def test_absence_is_not_shadowed_by_no_fingerprint(tmp_repo: Path):
    # A provable absence short-circuits before any shape question can arise; the
    # uncompared branch must never mask a missing symbol or a missing file.
    missing_symbol = resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "gone"))
    assert missing_symbol.found is False
    assert missing_symbol.reason.startswith(REASON_SYMBOL_MISSING)
    missing_file = resolve_anchor(str(tmp_repo), Anchor("pkg/absent.py", "gone"))
    assert missing_file.found is False
    assert missing_file.reason.startswith(REASON_FILE_MISSING)


def test_resolve_is_total_does_not_raise(tmp_repo: Path):
    # Every expected adverse condition returns a populated AnchorResult, never raises.
    for anchor in [
        Anchor("pkg/does_not_exist.py", "x"),
        Anchor("../escape.py", None),
        Anchor("pkg/broken.py", "oops"),
        Anchor("pkg/auth.py", "missing"),
    ]:
        res = resolve_anchor(str(tmp_repo), anchor)
        assert res.found is False
