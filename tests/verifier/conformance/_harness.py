"""Shared conformance machinery — real spawns only, one honesty switch.

Every leg in this tier drives the public ``verify()`` with the default
hermetic spawn, so probe, tool, report, ingest, classify, and merge are all
the real path; the engine is a deterministic hand-built two-node graph
(touched file -> contained symbol <- test symbol) so the affected-set never
depends on a per-language extractor. The python e2e leg drops even that
and builds the graph with the real matrix extractor — zero doubles anywhere.

Degraded environments stay honest: a missing toolchain SKIPS the leg with the
tool named, except under ``HIVE_VERIFIER_CONFORMANCE_STRICT=1`` where absence
is a FAILURE — the strict gate proves all 8 languages with zero skips, so a
quietly-skipping leg can never masquerade as coverage.

Setup steps that belong to the TARGET repo (``npm ci`` from a committed
lockfile, the cmake configure+build a ctest run presupposes) run here as
bounded subprocesses; they are fixture preparation, not verifier behavior.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import networkx as nx
import pytest

from hive.verifier.registry import REGISTRY
from hive.verifier.result import VerifyResult
from hive.verifier.spawn import ToolPresence, probe_tool
from hive.verifier.touched import TouchedFile, TouchedSet
from hive.verifier.verify import VerifyOptions, verify

STRICT_ENV = "HIVE_VERIFIER_CONFORMANCE_STRICT"

FIXTURES = Path(__file__).parent / "fixtures"

_SETUP_TIMEOUT_S = 600

# Probe argvs mirror the registry rows (the row is the single owner of its
# probe); setup-only tools the rows never spawn are added explicitly.
PROBES: dict[str, tuple[str, ...]] = {
    recipe.argv[0]: recipe.available_probe
    for row in REGISTRY.values()
    for recipe in (row.typecheck, row.test)
    if recipe is not None
}
PROBES.update(
    {
        "npm": ("npm", "--version"),
        "cmake": ("cmake", "--version"),
    }
)

_PROBE_CACHE: dict[tuple[str, ...], ToolPresence] = {}


def is_strict() -> bool:
    return os.environ.get(STRICT_ENV) == "1"


def missing(reason: str) -> None:
    """One degraded-environment door: skip by default, fail under strict."""
    if is_strict():
        pytest.fail(f"strict conformance: {reason}", pytrace=False)
    pytest.skip(reason)


def require_tools(*tools: str) -> None:
    for tool in tools:
        presence = probe_tool(PROBES[tool], cache=_PROBE_CACHE)
        if not presence.available:
            missing(f"{tool} unavailable ({presence.reason})")


def _run_setup(argv: list[str], cwd: Path, what: str) -> None:
    """Run one bounded target-repo setup step; lack of success degrades the
    leg (skip / strict-fail), never errors the suite."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=_SETUP_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        missing(f"{what} could not run: {exc}")
        return
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-800:]
        missing(f"{what} failed (exit {proc.returncode}): {tail}")


# --- node family: committed lockfile + npm ci, bins ride PATH -----------------------

_NPM_CI_DONE: set[Path] = set()


def node_tools(
    monkeypatch: pytest.MonkeyPatch, lang: str, tools: tuple[str, ...]
) -> None:
    require_tools("node", "npm")
    lang_dir = FIXTURES / lang
    if lang_dir not in _NPM_CI_DONE:
        if not (lang_dir / "node_modules").is_dir():
            _run_setup(
                ["npm", "ci", "--no-audit", "--no-fund"],
                lang_dir,
                f"npm ci in {lang_dir}",
            )
        _NPM_CI_DONE.add(lang_dir)
    bin_dir = lang_dir / "node_modules" / ".bin"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for tool in tools:
        presence = probe_tool(PROBES[tool])  # uncached: PATH just changed
        if not presence.available:
            missing(f"{tool} unavailable after npm ci ({presence.reason})")


# --- c/cpp family: the ctest row presupposes a configured+built tree ----------------

_CMAKE_BUILT: set[Path] = set()


def ensure_cmake_build(variant_dir: Path) -> None:
    require_tools("cmake", "ctest")
    if variant_dir in _CMAKE_BUILT:
        return
    build = variant_dir / "build"
    if build.is_dir():
        shutil.rmtree(build)  # a half-built tree from a prior run must not lie
    _run_setup(
        [
            "cmake",
            "-S",
            ".",
            "-B",
            "build",
            "-DCMAKE_C_COMPILER=clang",
            "-DCMAKE_CXX_COMPILER=clang++",
        ],
        variant_dir,
        f"cmake configure in {variant_dir}",
    )
    _run_setup(
        ["cmake", "--build", "build"], variant_dir, f"cmake build in {variant_dir}"
    )
    _CMAKE_BUILT.add(variant_dir)


# --- sql family: the live-postgres leg needs a reachable service --------------------

PG_KEYS = ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")


def pg_connection_env() -> tuple[tuple[str, str], ...]:
    """PG* connection env for pg_prove, from the operator/container
    environment. The hermetic spawn env is minimal by design, so connection
    settings must ride the recipe's extra_env — ambient PG* never leaks in."""
    require_tools("pg_prove")
    if "PGHOST" not in os.environ:
        missing("no postgres service configured (PGHOST unset)")
    return tuple((key, os.environ[key]) for key in PG_KEYS if key in os.environ)


# --- matrix scratch pin (for the e2e legs that build a real graph) ------------------

_MATRIX_SCRATCH: Path | None = None


def ensure_scratch_matrix_out() -> Path:
    """Pin MATRIX_OUT to a per-run scratch dir before matrix can read it.

    MATRIX_OUT is import-time state in matrix (the census engines module owns
    the same discipline): setting the env var after ``matrix.paths`` has
    loaded is a silent no-op, and the relative default would litter a
    ``matrix-out/`` into whatever CWD the suite runs from. If another suite in
    this process already pinned an absolute scratch, adopt it; a relative pin
    is refused loudly instead of littering.
    """
    global _MATRIX_SCRATCH
    if _MATRIX_SCRATCH is not None:
        return _MATRIX_SCRATCH
    paths_module = sys.modules.get("hive.matrix.paths")
    if paths_module is not None:
        pinned = Path(str(paths_module.MATRIX_OUT))
        if not pinned.is_absolute():
            pytest.fail(
                "matrix.paths loaded with a relative MATRIX_OUT before the "
                "conformance scratch pin; graph artifacts would litter the CWD",
                pytrace=False,
            )
        _MATRIX_SCRATCH = pinned
        return pinned
    scratch = Path(tempfile.mkdtemp(prefix="hive-verifier-conformance-matrix-out-"))
    os.environ["MATRIX_OUT"] = str(scratch)
    _MATRIX_SCRATCH = scratch
    return scratch


# --- the verify() drivers ------------------------------------------------------------


def link_graph(touched: str, test_file: str | None) -> nx.DiGraph:
    """touched file --contains--> symbol <--calls-- test symbol (in test_file)."""
    g = nx.DiGraph()
    g.add_node(touched, label=touched, file_type="file", source_file=touched)
    g.add_node("sym.unit", label="unit()", file_type="code", source_file=touched)
    g.add_edge(touched, "sym.unit", relation="contains")
    if test_file is not None:
        g.add_node(
            "sym.test_unit",
            label="test_unit()",
            file_type="code",
            source_file=test_file,
        )
        g.add_edge("sym.test_unit", "sym.unit", relation="calls")
    return g


def conf_verify(
    repo: Path,
    touched: str,
    test_file: str | None = None,
    *,
    engine: object | None = None,
    **opt_kwargs: object,
) -> VerifyResult:
    """Run the real verify() on one fixture repo with real spawns."""
    return verify(
        repo,
        TouchedSet((TouchedFile(touched, frozenset()),)),
        base_sha="conformance-base",
        head_sha="conformance-head",
        engine=engine if engine is not None else link_graph(touched, test_file),
        opts=VerifyOptions(**opt_kwargs),  # type: ignore[arg-type]
    )
