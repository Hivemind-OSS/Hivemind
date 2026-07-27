"""CT-15 — the repo self-contains every engine (plan §4 Intent 1, §7).

Post-move, the hivemind repo alone builds, checks, and ships: ``hive.matrix`` /
``hive.combdrift`` are first-party subpackages of the one ``hive``
dist, and every trace of the two-repo world — the vendored wheelhouse, the
``[tool.uv.sources]`` engine pins, the bare ``import matrix`` / ``combdrift`` /
``hive_edge`` names, the ``combdrift.*``/``matrix.*`` mypy overrides, and the
``vendor_edge`` / ``Hive-edge`` references — is gone. This module pins that world.

RED-FIRST MECHANICS (authored pre-move, then frozen): each check asserts a
POST-MOVE fact directly, so it fails as a clean assertion now and flips green as
its owning chunk lands — the engine move (importable subpackages), packaging
(sources/overrides/sync extra), and the deletions sweep (wheelhouse, bare
imports, machinery references). Never an import/collection error.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

ENGINE_SUBPACKAGES = ("hive.matrix", "hive.combdrift")
# The bare engine dist names that must never resolve from the repo again (they
# become `hive.matrix` / `hive.combdrift` — D2).
BARE_ENGINE_NAMES = ("matrix", "combdrift", "hive_edge")
BARE_ENGINE_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+(?:matrix|combdrift|hive_edge)(?:\.|\s|,|$)"
)
# The deleted two-repo machinery tokens (case-sensitive `Hive-edge` is the GitHub
# repo, NOT the kept lowercase `hive-edge` console script).
MACHINERY_TOKENS = ("vendor_edge", "Hive-edge")


def _module_exists(dotted: str) -> bool:
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def _pkg_name(requirement: str) -> str:
    """The bare distribution name of a requirement string (drops any version /
    marker / extras specifier): ``tree-sitter>=0.25,<0.26`` -> ``tree-sitter``."""
    return re.split(r"[<>=!~;\[\s]", requirement.strip(), maxsplit=1)[0]


def test_engine_subpackages_importable() -> None:
    """The three engines import as first-party subpackages of the ``hive`` dist.
    RED pre-move (none exist); green after the engine move (plan Step 1)."""
    missing = [name for name in ENGINE_SUBPACKAGES if not _module_exists(name)]
    assert not missing, (
        f"engine subpackages not first-party yet: {missing} — the engine move "
        "(plan Step 1) has not landed"
    )


def test_engine_subpackages_packaged() -> None:
    """The engine sources live under ``hive/`` with package markers AND setuptools
    discovers them via the ``hive*`` glob — so the built wheel carries
    ``hive/matrix`` / ``hive/combdrift`` (the BUG-040 no-explicit-
    list pin). RED pre-move (dirs absent); green after the move."""
    for rel in (
        "hive/matrix/__init__.py",
        "hive/combdrift/__init__.py",
    ):
        assert (REPO_ROOT / rel).is_file(), (
            f"{rel} is missing — the engine sources have not moved in-repo"
        )
    include = _pyproject()["tool"]["setuptools"]["packages"]["find"]["include"]
    assert any(glob == "hive*" or glob.startswith("hive") for glob in include), (
        f"setuptools packages.find must include a hive* glob so the engine "
        f"subpackages are auto-discovered into the wheel; got {include}"
    )


def test_no_vendor_wheels() -> None:
    """The vendored wheelhouse ceases to exist (plan §7). RED pre-move (present);
    green after the deletions sweep (plan Step 6)."""
    wheels = REPO_ROOT / "vendor" / "wheels"
    assert not wheels.exists(), (
        f"{wheels} still present — the vendored wheelhouse must be deleted once "
        "the engines are first-party (plan Step 6)"
    )


def test_no_uv_sources_engine_pins() -> None:
    """No ``[tool.uv.sources]`` pin resolves an engine name to a local wheel. RED
    pre-move (three pins present); green after packaging (plan Step 4)."""
    sources = _pyproject().get("tool", {}).get("uv", {}).get("sources", {})
    pinned = [name for name in ("hive-edge", "comb-drift", "matrix") if name in sources]
    assert not pinned, (
        f"[tool.uv.sources] still pins engine names {pinned} to vendored wheels — "
        "the pins must be deleted (plan §3.4)"
    )


def test_sync_extra_drops_bare_engine_names() -> None:
    """The ``sync`` optional-dependency extra no longer lists the bare engine dist
    names (they are first-party now; their third-party deps take their place). RED
    pre-move; green after packaging (plan Step 4)."""
    sync = _pyproject()["project"]["optional-dependencies"]["sync"]
    names = {_pkg_name(req) for req in sync}
    leaked = names & {"hive-edge", "comb-drift", "matrix"}
    assert not leaked, (
        f"the sync extra still carries bare engine names {sorted(leaked)} — they "
        "must leave the extra once the engines are first-party (plan §3.4)"
    )


def test_no_engine_mypy_overrides() -> None:
    """No first-party ``combdrift.*``/``matrix.*`` mypy override survives — strict
    typing extends to the engines with NO relaxation (plan §4 Intent 7 / D6). RED
    pre-move; green after packaging + the strict-typing pass (plan Step 4)."""
    overrides = _pyproject().get("tool", {}).get("mypy", {}).get("overrides", [])
    offenders = []
    for entry in overrides:
        modules = entry.get("module", [])
        modules = [modules] if isinstance(modules, str) else modules
        offenders.extend(m for m in modules if m in ("combdrift.*", "matrix.*"))
    assert not offenders, (
        f"mypy overrides still relax first-party engine modules {offenders} — the "
        "engine overrides must be deleted (the override rule is stub-only, D6)"
    )


def test_no_bare_engine_imports() -> None:
    """No bare ``import matrix`` / ``combdrift`` / ``hive_edge`` statement survives
    under ``hive/`` or ``tests/`` (they become ``import hive.matrix`` etc. — D2).
    The frozen contract suite is excluded: it names these tokens as the thing it
    forbids and imports no engine by construction. RED pre-move; green after the
    consumer rewrite (plan Step 3)."""
    offenders: list[str] = []
    scanned = 0
    for rel in _tracked_files():
        if not rel.endswith(".py"):
            continue
        if not (rel.startswith("hive/") or rel.startswith("tests/")):
            continue
        if rel.startswith("tests/contract/"):
            continue
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # tracked but deleted in the working tree — carries no import
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if BARE_ENGINE_IMPORT.match(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    # a scan that read nothing would pass while asserting nothing
    assert scanned > 10, f"the import scan read almost no files ({scanned})"
    assert not offenders, (
        "bare engine imports must be rewritten to hive.matrix / hive.combdrift / "
        "hive.edge (D2); still present:\n" + "\n".join(offenders)
    )


def test_no_deleted_machinery_references() -> None:
    """No committed file (outside the CHANGELOG / BUGS / docs-PLANS allowlist and
    the contract suite that names them) references the deleted ``vendor_edge``
    script or the ``Hive-edge`` GitHub repo. RED pre-move (Dockerfile / pyproject /
    docs / the script itself); green after the deletions + docs sweep (Steps 6-7)."""
    offenders: list[str] = []
    for rel in _tracked_files():
        base = rel.rsplit("/", 1)[-1]
        if base in ("CHANGELOG.md", "BUGS.md"):
            continue
        if rel.startswith("docs/PLANS/") or rel.startswith("CONTEXT/"):
            continue
        if rel.startswith("tests/contract/"):
            continue
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary / unreadable — cannot carry a text reference
        for token in MACHINERY_TOKENS:
            if token in text:
                offenders.append(f"{rel} references {token!r}")
    assert not offenders, (
        "deleted two-repo machinery is still referenced:\n" + "\n".join(offenders)
    )
