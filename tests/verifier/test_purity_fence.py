"""Structural fences over the package source tree.

Fence 1 (no-diff): the census composer owns the constellation's single diff parse;
nothing diff-shaped may ever exist in this package — not a module, not a pattern.

Fence 2 (purity): the pure core (touched, result, ingest/*, registry, evidence) does
no I/O — no process, filesystem, clock, or network imports. All I/O is confined to
the spawn/rootfind/verify modules.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import hive.verifier

PACKAGE_DIR = Path(hive.verifier.__file__).resolve().parent

_DIFF_CONTENT = re.compile(r"parse_diff|unidiff|whatthepatch|@@ -")

_PURE_MODULE_GLOBS = (
    "touched.py",
    "result.py",
    "registry.py",
    "evidence.py",
    "ingest/*.py",
)
_BANNED_IMPORT_ROOTS = frozenset(
    {"subprocess", "os", "sys", "socket", "shutil", "time"}
)


def _package_sources() -> list[Path]:
    return sorted(PACKAGE_DIR.rglob("*.py"))


def test_no_diff_module_exists() -> None:
    offenders = [
        str(path)
        for path in _package_sources()
        if any(part.startswith("diff") for part in path.relative_to(PACKAGE_DIR).parts)
    ]
    assert offenders == [], (
        f"diff-shaped modules are banned in this package: {offenders}"
    )


def test_no_diff_parsing_patterns_in_source() -> None:
    offenders: dict[str, str] = {}
    for path in _package_sources():
        match = _DIFF_CONTENT.search(path.read_text(encoding="utf-8"))
        if match:
            offenders[str(path)] = match.group(0)
    assert offenders == {}, (
        f"diff-parsing patterns are banned in this package: {offenders}"
    )


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) stay inside the package and are fine.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_pure_modules_do_no_io() -> None:
    offenders: dict[str, list[str]] = {}
    for pattern in _PURE_MODULE_GLOBS:
        for path in sorted(PACKAGE_DIR.glob(pattern)):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            banned = _import_roots(tree) & _BANNED_IMPORT_ROOTS
            if banned:
                offenders[str(path)] = sorted(banned)
    assert offenders == {}, f"pure modules must not import I/O modules: {offenders}"
