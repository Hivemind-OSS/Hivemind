"""AST import-linter (P0.0). A blocking gate:
  - hive/domain/** may not import any I/O module (sqlite3|torch|subprocess|os|git|time).
  - hive/domain|adapters|app/** may not import the build-excluded scripts/ utilities.
  - hive/domain|adapters|app/** may not import hive.research (dev-time only).
  - hive/domain/** may not import the moved-in engines (hive.census|hive.verifier);
    the engines stay standalone — they never import hive.domain or hive.app.
  - nothing on the hivemind side imports the pre-move top-level names
    (hive_census|hive_verifier) — the hive-edge transition install would silently
    satisfy a missed rename.
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


def _dotted_imports(path: pathlib.Path) -> set[str]:
    """FULL dotted module names imported by a .py file (absolute imports only)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module:
                names.add(node.module)
    return names


def _imports_under(path: pathlib.Path, roots: tuple[str, ...]) -> set[str]:
    return {
        name
        for name in _dotted_imports(path)
        if any(name == root or name.startswith(root + ".") for root in roots)
    }


def test_domain_never_imports_census_or_verifier() -> None:
    # The moved-in engines are driving-adapter territory (subprocess spawns, git,
    # jsonschema): the pure domain may never grow a dependency on them.
    offenders: dict[str, set[str]] = {}
    for path in (ROOT / "domain").rglob("*.py"):
        bad = _imports_under(path, ("hive.census", "hive.verifier"))
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad
    assert not offenders, f"domain/ must not import the census/verifier engines: {offenders}"


def test_engines_never_import_hive_domain_or_app() -> None:
    # The engines moved IN wholesale but stay standalone: hive.census/hive.verifier
    # depend only on each other + the edge engine packages (matrix, combdrift),
    # never on the hive server stack — the dependency arrow points one way.
    offenders: dict[str, set[str]] = {}
    for path in _full_module_paths("census", "verifier"):
        bad = _imports_under(path, ("hive.domain", "hive.app", "hive.adapters", "hive.tools"))
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad
    assert not offenders, f"the engines must stay standalone of the server stack: {offenders}"


def test_no_module_imports_the_premove_engine_names() -> None:
    # The transition venv still carries hive-edge's `hive_census`/`hive_verifier`
    # top-level packages, so a missed import rename would resolve SILENTLY against
    # the wrong (agent-side) copy instead of failing loudly. Runtime and the moved
    # test suites alike must name only hive.census / hive.verifier.
    offenders: dict[str, set[str]] = {}
    scan_roots = [
        *(path for path in ROOT.rglob("*.py") if "research" not in path.parts),
        *(ROOT.parent / "tests" / "census").rglob("*.py"),
        *(ROOT.parent / "tests" / "verifier").rglob("*.py"),
    ]
    for path in scan_roots:
        bad = _imports_under(path, ("hive_census", "hive_verifier"))
        if bad:
            offenders[str(path)] = bad
    assert not offenders, f"pre-move engine names must not be imported: {offenders}"


def test_research_not_imported_by_runtime() -> None:
    # hive/research is the dev-time benchmark harness: wheel-excluded (pyproject
    # ``packages.find`` excludes ``hive.research*``) and .dockerignore'd out of the image.
    # It sits ON TOP of the runtime and may import it; the reverse direction would crash an
    # operator's installed package at import time, so it is forbidden by AST.
    offenders: dict[str, set[str]] = {}
    for path in _full_module_paths("domain", "adapters", "app"):
        # detect `import hive.research...` / `from hive.research import ...`
        tree = ast.parse(path.read_text(), filename=str(path))
        hits = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("hive.research"):
                hits.add(node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("hive.research"):
                        hits.add(alias.name)
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert not offenders, f"runtime must not import hive.research: {offenders}"
