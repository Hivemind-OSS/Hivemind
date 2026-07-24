"""The fixture corpus: one directory per language, its files, and dispatch.

A single source of truth for the per-language tests so the fixture set and its
per-language dispatch never drift.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES_ROOT = Path(__file__).parent / "fixtures"
GOLDEN_ROOT = Path(__file__).parent / "golden"

# language -> the fixture files extracted together as one corpus (cross-file
# resolution needs the whole set, e.g. app.js + util.js).
LANG_FILES: dict[str, tuple[str, ...]] = {
    "python": ("app.py",),
    "javascript": ("app.js", "util.js"),
    "typescript": ("app.ts", "util.ts"),
    "go": ("app.go",),
    "rust": ("app.rs",),
    "c": ("app.c", "util.h"),
    "cpp": ("app.cpp",),
    "sql": ("schema.sql",),
}


def lang_paths(lang: str) -> list[Path]:
    return [FIXTURES_ROOT / lang / f for f in LANG_FILES[lang]]


def all_fixture_paths() -> list[Path]:
    paths: list[Path] = []
    for lang in LANG_FILES:
        paths.extend(lang_paths(lang))
    return paths
