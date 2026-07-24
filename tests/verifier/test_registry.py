"""Contracts over the capability registry — frozen data, version-pinned.

The registry is the single owner of per-language knowledge: extensions, test
globs (the blast-radius glob union), root markers, and the tool recipes for
each evidence class. REGISTRY_VERSION is pinned to the row content by a golden
sha256: editing any row reds the golden until the version is consciously
bumped and the pin regenerated, so a receipt's registry_version is trustworthy.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from hive.verifier.ingest import INGESTERS
from hive.verifier.registry import (
    REGISTRY,
    REGISTRY_VERSION,
    LangRecipe,
    ToolRecipe,
    all_test_globs,
    recipe_for_file,
)

# The locked language set. Adding or dropping a language is a conscious edit
# HERE as well as in the registry — set equality, not subset.
LOCKED_LANGUAGES = frozenset(
    {"python", "javascript", "typescript", "go", "rust", "c", "cpp", "sql"}
)

# Golden content pins: REGISTRY_VERSION -> sha256 of the canonicalized rows.
# A red here means the rows changed: bump REGISTRY_VERSION and add its pin
# (the failure message carries the new hash), never silently re-pin the old key.
PINNED_REGISTRY_HASHES = {
    "r1": "0fba6e9eb7c9467f72bb0dffe16e67a1f573fff4bcbf1fd84a0b465238f2cbea",
    # r2: javascript class-1 formatter spelling corrected against the real
    # eslint ("--format sarif" names a nonexistent builtin; the @microsoft/
    # sarif package spelling is what resolves) — conformance fixture is proof.
    "r2": "ec61b21e985ddbf0fff7232144b6a9db6335a7539c6f8dd3db55bcda9d014812",
    # r3: sql class-2 gains --verbose — pg_prove's default output is the
    # prove harness summary, not TAP; --verbose passes the raw stream through
    # (proven against live pgTAP in the strict container).
    "r3": "5528d4816d78cdf8001f196226b7a6ff5394cb0b05f766ea9b40fddf9af693b4",
    # r4: ruby and bash rows removed — the registry covers the eight kept
    # languages (python, javascript, typescript, sql, go, rust, c, cpp); no
    # capability is claimed that hive-verifier does not provide.
    "r4": "989f745c733c0d629865c7f32866f79a3fc09717a6af66ac0b8a5fb437615730",
}


def _canonical_registry_bytes() -> bytes:
    payload = {lang: dataclasses.asdict(row) for lang, row in sorted(REGISTRY.items())}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _recipes() -> list[tuple[str, str, ToolRecipe]]:
    out: list[tuple[str, str, ToolRecipe]] = []
    for lang, row in REGISTRY.items():
        for slot in ("typecheck", "test", "adequacy"):
            recipe = getattr(row, slot)
            if recipe is not None:
                out.append((lang, slot, recipe))
    return out


# --- the language lock -------------------------------------------------------


def test_registry_covers_exactly_the_locked_languages() -> None:
    assert set(REGISTRY) == LOCKED_LANGUAGES


def test_registry_keys_match_row_langs() -> None:
    for lang, row in REGISTRY.items():
        assert row.lang == lang


# --- the golden version pin --------------------------------------------------


def test_registry_content_hash_matches_pinned_version() -> None:
    digest = hashlib.sha256(_canonical_registry_bytes()).hexdigest()
    assert REGISTRY_VERSION in PINNED_REGISTRY_HASHES, (
        f"REGISTRY_VERSION {REGISTRY_VERSION!r} has no golden pin; "
        f"add PINNED_REGISTRY_HASHES[{REGISTRY_VERSION!r}] = {digest!r}"
    )
    assert digest == PINNED_REGISTRY_HASHES[REGISTRY_VERSION], (
        f"registry rows changed but REGISTRY_VERSION is still {REGISTRY_VERSION!r}; "
        f"bump the version and pin the new content hash {digest!r}"
    )


# --- dispatch and shape invariants --------------------------------------------


def test_every_recipe_report_format_has_an_ingester() -> None:
    for lang, slot, recipe in _recipes():
        assert recipe.report_format in INGESTERS, (lang, slot, recipe.report_format)


def test_every_recipe_has_a_non_empty_available_probe() -> None:
    for lang, slot, recipe in _recipes():
        assert recipe.available_probe, (lang, slot)
        assert all(part for part in recipe.available_probe), (lang, slot)


def test_every_recipe_timeout_is_positive() -> None:
    for lang, slot, recipe in _recipes():
        assert recipe.timeout_s > 0, (lang, slot)


def test_no_row_carries_an_adequacy_recipe_in_v1() -> None:
    for lang, row in REGISTRY.items():
        assert row.adequacy is None, lang


def test_rows_are_frozen() -> None:
    row = REGISTRY["python"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.lang = "other"  # type: ignore[misc]
    assert row.typecheck is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.typecheck.timeout_s = 1  # type: ignore[misc]


def test_recipe_guards_reject_empty_argv_and_probe() -> None:
    with pytest.raises(ValueError):
        ToolRecipe(argv=(), report_format="junit", available_probe=("x", "--version"))
    with pytest.raises(ValueError):
        ToolRecipe(argv=("x",), report_format="junit", available_probe=())
    with pytest.raises(ValueError):
        ToolRecipe(
            argv=("x",), report_format="junit", available_probe=("x",), timeout_s=0
        )


def test_lang_recipe_guard_rejects_undotted_extensions() -> None:
    with pytest.raises(ValueError):
        LangRecipe(
            lang="x",
            extensions=("py",),
            test_globs=(),
            root_markers=(),
            typecheck=None,
            test=None,
        )


# --- extension resolution ------------------------------------------------------


def test_extensions_do_not_overlap_across_rows() -> None:
    seen: dict[str, str] = {}
    for lang, row in REGISTRY.items():
        for ext in row.extensions:
            assert ext not in seen, f"{ext} claimed by both {seen[ext]} and {lang}"
            seen[ext] = lang


def test_recipe_for_file_resolves_every_registered_extension() -> None:
    for lang, row in REGISTRY.items():
        for ext in row.extensions:
            resolved = recipe_for_file(f"src/pkg/module{ext}")
            assert resolved is row, (lang, ext)


def test_recipe_for_file_unknown_or_missing_extension_is_none() -> None:
    assert recipe_for_file("notes.txt") is None
    assert recipe_for_file("Makefile") is None
    assert recipe_for_file("src/.gitignore") is None  # dotfile, not an extension


# --- the test-glob union (the blast_radius kwarg source) -----------------------


def test_all_test_globs_is_the_sorted_union() -> None:
    union: set[str] = set()
    for row in REGISTRY.values():
        union.update(row.test_globs)
    assert all_test_globs() == tuple(sorted(union))


def test_all_test_globs_covers_the_engine_blind_spots() -> None:
    # matrix's _DEFAULT_TEST_GLOBS lacks these; the registry union is the
    # single glob owner and must carry them explicitly.
    globs = set(all_test_globs())
    assert "t/*.sql" in globs  # pgTAP layout


# --- ruled row cells (semantic pins on the operator rulings) --------------------


def test_c_and_cpp_class1_ship_clang_fsyntax_only_native_exit() -> None:
    for lang in ("c", "cpp"):
        recipe = REGISTRY[lang].typecheck
        assert recipe is not None
        assert recipe.report_format == "native-exit", lang
        assert "-fsyntax-only" in recipe.argv, lang


def test_sql_root_markers_degrade_to_repo_root() -> None:
    assert REGISTRY["sql"].root_markers == ()


def test_file_source_recipes_carry_the_report_placeholder() -> None:
    for lang, slot, recipe in _recipes():
        if recipe.report_source == "file":
            assert any("{report}" in part for part in recipe.argv), (lang, slot)
        else:
            assert not any("{report}" in part for part in recipe.argv), (lang, slot)
