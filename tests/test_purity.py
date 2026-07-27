"""AST import-linter (P0.0). A blocking gate:
  - hive/domain/** may not import any I/O module (sqlite3|torch|subprocess|os|git|time).
  - hive/domain|adapters|app/** may not import the build-excluded scripts/ utilities.
  - hive/domain/** may not import the moved-in engines (hive.census|hive.verifier);
    hive.census|hive.verifier stay standalone of the server stack, importing only
    each other + the edge engines (hive.matrix|hive.combdrift).
  - the absorbed engines (hive.matrix|hive.combdrift) stay standalone too — they
    never import the hive server stack or the census/verifier consumers.
  - nothing on the hivemind side imports the pre-move top-level names
    (hive_census|hive_verifier|hive_edge|matrix|combdrift) — the still-installed
    old wheels would silently satisfy a missed rename.
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
        f"illegal imports: {imports - allowed}"
    )


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
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and (node.module == "scripts" or node.module.startswith("scripts."))
            ):
                hits.add(node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "scripts" or alias.name.startswith("scripts."):
                        hits.add(alias.name)
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert not offenders, (
        f"runtime must not import the build-excluded scripts/ utilities: {offenders}"
    )


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
    assert not offenders, (
        f"domain/ must not import the census/verifier engines: {offenders}"
    )


def test_engines_never_import_hive_domain_or_app() -> None:
    # The engines moved IN wholesale but stay standalone: hive.census/hive.verifier
    # depend only on each other + the first-party edge engine packages
    # (hive.matrix, hive.combdrift), never on the hive server stack — the
    # dependency arrow points one way.
    offenders: dict[str, set[str]] = {}
    for path in _full_module_paths("census", "verifier"):
        bad = _imports_under(
            path, ("hive.domain", "hive.app", "hive.adapters", "hive.tools")
        )
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad
    assert not offenders, (
        f"the engines must stay standalone of the server stack: {offenders}"
    )


def test_absorbed_engines_never_import_the_server_stack() -> None:
    # The edge engines moved IN as first-party subpackages (hive.matrix,
    # hive.combdrift) but stay standalone: they may import each other, never the
    # hive server stack (domain/app/adapters/tools) nor the census/verifier
    # engines that consume them — the dependency arrow points one way
    # (server -> engines), so the engines remain independently shippable. Their
    # only consumer now is the census; the drift path asks git instead.
    paths = _full_module_paths("matrix", "combdrift")
    assert paths, "the scan found no engine modules at all — it would pass vacuously"
    offenders: dict[str, set[str]] = {}
    for path in paths:
        bad = _imports_under(
            path,
            (
                "hive.domain",
                "hive.app",
                "hive.adapters",
                "hive.tools",
                "hive.census",
                "hive.verifier",
            ),
        )
        if bad:
            offenders[str(path.relative_to(ROOT))] = bad
    assert not offenders, (
        f"the absorbed edge engines must stay standalone of the server stack: "
        f"{offenders}"
    )


def test_no_module_imports_the_premove_engine_names() -> None:
    # The transition venv still carries the pre-move top-level engine packages —
    # hive-edge's `hive_census`/`hive_verifier` AND the vendored 0.9.0
    # `matrix`/`combdrift`/`hive_edge` wheels — so a missed import rename would
    # resolve SILENTLY against the wrong (old, external) copy instead of failing
    # loudly. Runtime and the moved test suites alike must name only the
    # first-party hive.census / hive.verifier / hive.matrix / hive.combdrift /
    # hive.edge.
    offenders: dict[str, set[str]] = {}
    scan_roots = [
        *ROOT.rglob("*.py"),
        *(ROOT.parent / "tests" / "census").rglob("*.py"),
        *(ROOT.parent / "tests" / "verifier").rglob("*.py"),
    ]
    for path in scan_roots:
        bad = _imports_under(
            path,
            ("hive_census", "hive_verifier", "hive_edge", "matrix", "combdrift"),
        )
        if bad:
            offenders[str(path)] = bad
    assert not offenders, f"pre-move engine names must not be imported: {offenders}"
