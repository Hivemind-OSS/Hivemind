"""The capability registry — 8 declarative rows, the single owner of
per-language knowledge.

Rows are frozen data, not code branches: each names a language's extensions,
test globs, project-root markers, and the tool recipes for evidence classes 1
(typecheck) and 2 (tests), each pinned to a report format the ingest dispatch
can parse. Adding or removing a language is a row edit. Where a language has
no dependable checker or runner, the cell is honest: ``None`` means the class
abstains (``not_run``), never a fake pass.

The registry is also the single owner of test globs — the engine's default
glob set is narrower than these rows need, so callers pass
``all_test_globs()`` to ``matrix.blast_radius`` explicitly.

``REGISTRY_VERSION`` names the row content; a golden content hash in the test
suite pins the two together, so any row edit forces a conscious version bump.

Format cells are pinned by real tools, not aspiration: sqlfluff 4.x emits
SARIF natively (verified); clang ships ``-fsyntax-only`` -> native-exit by
operator ruling; ``cargo test`` and ``ctest`` own their formats as the rows
state.
Placeholders in argv: ``{files}`` (the scoped file list, spliced), ``{root}``
(the config dir), ``{report}`` (the minted report path when
``report_source == "file"``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hive.verifier.ingest import INGESTERS

ReportFormat = Literal[
    "junit", "tap", "sarif", "tsc", "pyright", "gotest", "cargo", "native-exit"
]

REGISTRY_VERSION = "r4"


@dataclass(frozen=True, slots=True)
class ToolRecipe:
    """One spawnable tool invocation and how to read what it produced."""

    argv: tuple[str, ...]
    report_format: ReportFormat
    available_probe: tuple[str, ...]
    report_source: Literal["stdout", "file"] = "stdout"
    extra_env: tuple[tuple[str, str], ...] = ()
    timeout_s: int = 120

    def __post_init__(self) -> None:
        if not self.argv or not all(self.argv):
            raise ValueError("recipe argv must be a non-empty tuple of non-empty parts")
        if not self.available_probe or not all(self.available_probe):
            raise ValueError("recipe available_probe must be a non-empty tuple")
        if self.timeout_s <= 0:
            raise ValueError("recipe timeout_s must be positive")


@dataclass(frozen=True, slots=True)
class LangRecipe:
    """One language row: how to recognize its files and verify a change to them."""

    lang: str
    extensions: tuple[str, ...]
    test_globs: tuple[str, ...]
    root_markers: tuple[str, ...]
    typecheck: ToolRecipe | None
    test: ToolRecipe | None
    adequacy: ToolRecipe | None = None

    def __post_init__(self) -> None:
        if not self.lang:
            raise ValueError("row lang must be non-empty")
        if not self.extensions or not all(
            ext.startswith(".") and len(ext) > 1 for ext in self.extensions
        ):
            raise ValueError("row extensions must be dot-prefixed, e.g. '.py'")


_CTEST = ToolRecipe(
    argv=("ctest", "--test-dir", "build", "--output-junit", "{report}"),
    report_format="junit",
    available_probe=("ctest", "--version"),
    report_source="file",
)

REGISTRY: dict[str, LangRecipe] = {
    "python": LangRecipe(
        lang="python",
        extensions=(".py",),
        test_globs=("test_*.py", "*_test.py"),
        root_markers=(
            "pyproject.toml",
            "pytest.ini",
            "setup.cfg",
            "tox.ini",
            "conftest.py",
        ),
        typecheck=ToolRecipe(
            argv=("pyright", "--outputjson", "{files}"),
            report_format="pyright",
            available_probe=("pyright", "--version"),
        ),
        test=ToolRecipe(
            argv=("pytest", "--junitxml", "{report}", "{files}"),
            report_format="junit",
            available_probe=("pytest", "--version"),
            report_source="file",
        ),
    ),
    "typescript": LangRecipe(
        lang="typescript",
        extensions=(".ts", ".tsx", ".mts", ".cts"),
        test_globs=("*.test.ts", "*.test.tsx", "*.spec.ts", "*.spec.tsx"),
        root_markers=(
            "tsconfig.json",
            "package.json",
            "vitest.config.ts",
            "vitest.config.js",
        ),
        # tsc is project-scoped: it reads tsconfig.json from the config dir the
        # spawn runs in; passing files would silently drop the project config.
        typecheck=ToolRecipe(
            argv=("tsc", "--noEmit"),
            report_format="tsc",
            available_probe=("tsc", "--version"),
        ),
        test=ToolRecipe(
            argv=(
                "vitest",
                "run",
                "--reporter",
                "junit",
                "--outputFile",
                "{report}",
                "{files}",
            ),
            report_format="junit",
            available_probe=("vitest", "--version"),
            report_source="file",
        ),
    ),
    "javascript": LangRecipe(
        lang="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs"),
        test_globs=("*.test.js", "*.spec.js", "*.test.mjs", "*.test.cjs"),
        root_markers=("package.json", "vitest.config.js", "vitest.config.mjs"),
        # SARIF via the target repo's eslint formatter dependency
        # (@microsoft/eslint-formatter-sarif). "--format sarif" would resolve a
        # BUILTIN formatter that does not exist (conformance-proven); the scoped
        # spelling resolves the package. Absent formatter -> empty stdout ->
        # ReportParseError -> errored, never a silent pass.
        typecheck=ToolRecipe(
            argv=("eslint", "--format", "@microsoft/sarif", "{files}"),
            report_format="sarif",
            available_probe=("eslint", "--version"),
        ),
        test=ToolRecipe(
            argv=("node", "--test", "--test-reporter", "tap", "{files}"),
            report_format="tap",
            available_probe=("node", "--version"),
        ),
    ),
    "go": LangRecipe(
        lang="go",
        extensions=(".go",),
        test_globs=("*_test.go",),
        root_markers=("go.mod",),
        typecheck=ToolRecipe(
            argv=("go", "vet", "./..."),
            report_format="gotest",
            available_probe=("go", "version"),
        ),
        # go tests are package-scoped: a bare *_test.go file list fails to
        # compile against unlisted package sources, so the recipe runs the
        # config-dir (go.mod) package tree — a superset of the scoped set,
        # conservative in the safe direction.
        test=ToolRecipe(
            argv=("gotestsum", "--junitfile", "{report}", "--", "./..."),
            report_format="junit",
            available_probe=("gotestsum", "--version"),
            report_source="file",
        ),
    ),
    "rust": LangRecipe(
        lang="rust",
        extensions=(".rs",),
        test_globs=("tests/*.rs", "*/tests/*.rs", "*_test.rs"),
        root_markers=("Cargo.toml",),
        typecheck=ToolRecipe(
            argv=("cargo", "check", "--message-format", "json"),
            report_format="cargo",
            available_probe=("cargo", "--version"),
            extra_env=(("CARGO_TERM_COLOR", "never"),),
        ),
        # nextest's JUnit path lives in its config file, not argv, so the row
        # cannot mint {report}; cargo test -> native-exit is the dependable
        # floor (a one-cell upgrade when a path-addressable format lands).
        test=ToolRecipe(
            argv=("cargo", "test"),
            report_format="native-exit",
            available_probe=("cargo", "--version"),
            extra_env=(("CARGO_TERM_COLOR", "never"),),
        ),
    ),
    "c": LangRecipe(
        lang="c",
        extensions=(".c", ".h"),
        test_globs=("test_*.c", "*_test.c"),
        root_markers=("CMakeLists.txt", "Makefile", "compile_commands.json"),
        typecheck=ToolRecipe(
            argv=("clang", "-fsyntax-only", "{files}"),
            report_format="native-exit",
            available_probe=("clang", "--version"),
        ),
        test=_CTEST,
    ),
    "cpp": LangRecipe(
        lang="cpp",
        extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hh"),
        test_globs=("test_*.cpp", "*_test.cpp", "*_test.cc"),
        root_markers=("CMakeLists.txt", "Makefile", "compile_commands.json"),
        typecheck=ToolRecipe(
            argv=("clang++", "-fsyntax-only", "{files}"),
            report_format="native-exit",
            available_probe=("clang++", "--version"),
        ),
        test=_CTEST,
    ),
    "sql": LangRecipe(
        lang="sql",
        extensions=(".sql",),
        test_globs=("t/*.sql", "*/t/*.sql", "*_test.sql"),
        root_markers=(),
        typecheck=ToolRecipe(
            argv=("sqlfluff", "lint", "--format", "sarif", "{files}"),
            report_format="sarif",
            available_probe=("sqlfluff", "--version"),
        ),
        # pg_prove's default output is the prove harness SUMMARY, not TAP
        # (conformance-proven); --verbose passes the raw TAP stream through,
        # which the ingester reads with the summary tolerated as noise. A
        # multi-file run emits one plan per file and lands in errored (plan/
        # points mismatch) — an honest abstention, one file per run is proven.
        test=ToolRecipe(
            argv=("pg_prove", "--verbose", "{files}"),
            report_format="tap",
            available_probe=("pg_prove", "--version"),
        ),
    ),
}


def _validate_registry(registry: dict[str, LangRecipe]) -> dict[str, LangRecipe]:
    """Import-time cross-check: a row naming a format with no ingester is a
    boot failure, never a KeyError mid-verification."""
    for lang, row in registry.items():
        if row.lang != lang:
            raise RuntimeError(
                f"registry key {lang!r} does not match row lang {row.lang!r}"
            )
        for slot in ("typecheck", "test", "adequacy"):
            recipe: ToolRecipe | None = getattr(row, slot)
            if recipe is not None and recipe.report_format not in INGESTERS:
                raise RuntimeError(
                    f"registry row {lang!r} {slot} names report_format "
                    f"{recipe.report_format!r} with no ingester"
                )
    return registry


_validate_registry(REGISTRY)

_BY_EXTENSION: dict[str, LangRecipe] = {
    ext: row for row in REGISTRY.values() for ext in row.extensions
}


def recipe_for_file(path: str) -> LangRecipe | None:
    """Resolve a repo-relative POSIX path to its language row by extension."""
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0:  # extensionless, or a dotfile like ".gitignore"
        return None
    return _BY_EXTENSION.get(name[dot:])


def all_test_globs() -> tuple[str, ...]:
    """The sorted union of every row's test globs — the registry is the single
    glob owner; callers pass this to ``matrix.blast_radius`` explicitly."""
    union: set[str] = set()
    for row in REGISTRY.values():
        union.update(row.test_globs)
    return tuple(sorted(union))
