"""AST import-linter (P0.0). A blocking gate:
  - hive/domain/** may not import any I/O module (sqlite3|torch|subprocess|os|git|time).
  - hive/domain|adapters|app/** may not import the build-excluded scripts/ utilities.
The gate has teeth: the mutation (add `import sqlite3` to a domain file)
must turn test_domain_imports_no_io red.
"""
from __future__ import annotations

import ast
import pathlib

import hive

ROOT = pathlib.Path(hive.__file__).resolve().parent
FORBIDDEN_IN_DOMAIN = {"sqlite3", "torch", "subprocess", "os", "git", "time"}


def _module_imports(path: pathlib.Path) -> set[str]:
    """Top-level module names imported by a .py file (first dotted segment)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import, not a top-level module dependency
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _full_module_paths(*subdirs: str) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for sub in subdirs:
        out.extend((ROOT / sub).rglob("*.py"))
    return out


def test_domain_imports_no_io() -> None:
    offenders: dict[str, set[str]] = {}
    for path in (ROOT / "domain").rglob("*.py"):
        bad = _module_imports(path) & FORBIDDEN_IN_DOMAIN
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad
    assert not offenders, f"domain/ must not import I/O modules: {offenders}"


def test_client_is_stdlib_only_by_ast() -> None:
    # hive/client.py is the VENDORABLE single-file client: its import set is an
    # explicit stdlib allowlist (the static half of the vendoring fence; the
    # transitive/runtime half lives in tests/clients/test_hive_client.py).
    allowed = {"__future__", "json", "urllib", "typing"}
    imports = _module_imports(ROOT / "client.py")
    assert imports <= allowed, (
        f"hive/client.py must stay stdlib-only/vendorable; "
        f"illegal imports: {imports - allowed}")


def test_scripts_not_imported_by_runtime() -> None:
    # The repo-root ``scripts/`` utilities are dev/eval helpers EXCLUDED from the built wheel
    # (pyproject ``packages.find`` ships only ``hive*``/``tests*``). A runtime module importing them
    # would crash an operator's installed package at import time — so the runtime never depends on
    # ``scripts``; the dependency points one way only (scripts may import hive, never the reverse).
    offenders: dict[str, set[str]] = {}
    for path in _full_module_paths("domain", "adapters", "app"):
        # detect `import scripts...` / `from scripts import ...`
        tree = ast.parse(path.read_text(), filename=str(path))
        hits = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and (
                    node.module == "scripts" or node.module.startswith("scripts.")):
                hits.add(node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "scripts" or alias.name.startswith("scripts."):
                        hits.add(alias.name)
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert not offenders, f"runtime must not import the build-excluded scripts/ utilities: {offenders}"
