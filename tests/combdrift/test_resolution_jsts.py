"""resolve_anchor against JS/TS repos: full path -> AnchorResult behavior."""

from __future__ import annotations

from pathlib import Path

from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
from hive.combdrift.types import (
    REASON_NO_SYMBOL_REQUESTED,
    REASON_OK,
    REASON_PARSE_ERROR,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_INDIRECT,
    REASON_UNSUPPORTED_LANGUAGE,
    Anchor,
)


def test_ts_function_found_with_location(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "refresh"))
    assert res.found is True
    assert res.reason == REASON_OK
    assert res.location == "src/auth.ts:1"


def test_ts_arrow_const_found(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "revoke"))
    assert res.found is True
    assert res.location == "src/auth.ts:5"


def test_ts_class_found(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "Session"))
    assert res.found is True
    assert res.location == "src/auth.ts:9"


def test_ts_method_found(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "Session.login"))
    assert res.found is True
    assert res.location == "src/auth.ts:10"


def test_ts_async_method_found(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "Session.logout"))
    assert res.found is True
    assert res.location == "src/auth.ts:14"


def test_ts_symbol_none_is_file_presence(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", None))
    assert res.found is True
    assert res.reason == REASON_NO_SYMBOL_REQUESTED
    assert res.location == "src/auth.ts:1"


def test_ts_data_const_is_symbol_indirect(tmp_ts_repo: Path):
    # A value-only const is present-but-indirect -> unverifiable, never stale.
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "MAX_AGE"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_ts_interface_is_symbol_indirect(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "User"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_ts_type_alias_is_symbol_indirect(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "Id"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_ts_enum_is_symbol_indirect(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "Role"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_ts_truly_absent_symbol_is_missing(tmp_ts_repo: Path):
    # Guard: a name bound nowhere in a clean TS file stays missing (stale).
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "deletedFn"))
    assert res.found is False
    assert res.reason.startswith("symbol_missing")


def test_tsx_arrow_component_found(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/component.tsx", "Button"))
    assert res.found is True
    assert res.location == "src/component.tsx:1"


def test_tsx_default_component_found(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/component.tsx", "App"))
    assert res.found is True
    assert res.location == "src/component.tsx:5"


def test_js_function_arrow_method(tmp_ts_repo: Path):
    repo = str(tmp_ts_repo)
    assert (
        resolve_anchor(repo, Anchor("src/util.js", "helper")).location
        == "src/util.js:1"
    )
    assert (
        resolve_anchor(repo, Anchor("src/util.js", "arrow")).location == "src/util.js:4"
    )
    assert (
        resolve_anchor(repo, Anchor("src/util.js", "Widget.render")).location
        == "src/util.js:6"
    )


def test_dts_ambient_function_found(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("types/api.d.ts", "fetchUser"))
    assert res.found is True
    assert res.location == "types/api.d.ts:1"


def test_dts_ambient_class_and_method_found(tmp_ts_repo: Path):
    repo = str(tmp_ts_repo)
    assert resolve_anchor(repo, Anchor("types/api.d.ts", "Client")).found is True
    assert resolve_anchor(repo, Anchor("types/api.d.ts", "Client.send")).found is True


def test_ts_clean_symbol_found_despite_error(tmp_ts_repo: Path):
    # `good` parses cleanly even though `bad` below it is a syntax error.
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/broken.ts", "good"))
    assert res.found is True
    assert res.location == "src/broken.ts:1"


def test_ts_symbol_in_error_region_is_parse_error(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/broken.ts", "bad"))
    assert res.found is False
    assert res.reason.startswith(REASON_PARSE_ERROR)


def test_ts_absent_name_in_broken_file_is_parse_error(tmp_ts_repo: Path):
    # Absence in an unparseable file is unverifiable, not a deletion signal.
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/broken.ts", "never_here"))
    assert res.found is False
    assert res.reason.startswith(REASON_PARSE_ERROR)


def test_unsupported_file_type(tmp_ts_repo: Path):
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/data.json", None))
    assert res.found is False
    assert res.reason.startswith(REASON_UNSUPPORTED_LANGUAGE)


# --- Layer B: JS/TS fingerprint capture + drift -------------------------------


def test_ts_fingerprint_anchor_mints_token(tmp_ts_repo: Path):
    fp = fingerprint_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "refresh"))
    assert fp is not None
    assert fp.startswith("combdrift-fp/1:")


def test_ts_fingerprint_anchor_none_for_interface(tmp_ts_repo: Path):
    # A type-only declaration has no single callable interface to capture.
    assert fingerprint_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "User")) is None


def test_ts_fingerprint_match_is_ok(tmp_ts_repo: Path):
    fp = fingerprint_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "refresh"))
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK


def test_ts_param_removed_is_signature_changed(tmp_ts_repo: Path):
    # refresh(token: string) -> refresh(): a required parameter is removed.
    fp = fingerprint_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "refresh"))
    (tmp_ts_repo / "src/auth.ts").write_text(
        "export function refresh(): string {\n  return '';\n}\n", encoding="utf-8"
    )
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "refresh", fp))
    assert res.found is True
    assert res.location == "src/auth.ts:1"
    assert res.reason.startswith(REASON_SIGNATURE_CHANGED)


def test_ts_added_optional_param_is_ok(tmp_ts_repo: Path):
    fp = fingerprint_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "refresh"))
    (tmp_ts_repo / "src/auth.ts").write_text(
        "export function refresh(token: string, opts?: object): string {\n"
        "  return token;\n}\n",
        encoding="utf-8",
    )
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/auth.ts", "refresh", fp))
    assert res.found is True
    assert res.reason == REASON_OK


def test_python_file_in_same_repo_still_resolves(tmp_ts_repo: Path):
    # The .py path goes through the ast backend even in a JS/TS repo.
    res = resolve_anchor(str(tmp_ts_repo), Anchor("src/legacy.py", "old"))
    assert res.found is True
    assert res.location == "src/legacy.py:1"
