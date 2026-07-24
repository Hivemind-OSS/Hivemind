"""CT-16 — engine-parity goldens: the in-repo engines reproduce the pre-move CLI.

The decisive anti-fork evidence for U5-EDGE-ABSORB (plan §4 Intent 2). The
``combdrift/fp`` + ``matrix/subgraph_fp`` tokens and the full verify verdict
matrix were minted from the CURRENTLY-INSTALLED 0.9.0 ``hive-edge`` console
script (the pre-move engines) into ``fixtures/engine_parity/goldens.json``. This
module re-runs the SAME fixture scenarios against the POST-MOVE in-repo engine
CLI (``python -m hive.edge.cli`` over ``hive.matrix`` / ``hive.combdrift``) and
asserts byte/field parity — if the byte-preserving move silently forks any
engine (the BUG-022/035/037/038/048 corners are exactly where a re-port drifts),
these goldens go red.

RED-FIRST MECHANICS (this chunk authors the suite pre-move): every parity test
GUARDS on the post-move module existing (``hive.edge.cli``) and FAILS with a
clean assertion — never a collection/import error — while it is absent. It flips
green once the engine move + the ``hive-edge = hive.edge.cli:main`` console-
script re-point land. tests/contract/** is frozen after this chunk; no later
chunk edits it.

Launcher note (a recorded deviation from the plan's console-script prose): the
goldens were minted via the ``hive-edge`` console script, but the parity replay
drives ``[sys.executable, "-m", "hive.edge.cli"]`` — the SAME ``main(argv)`` over
the SAME engines, so output is launcher-independent (proven at mint time). The
``-m`` form is deterministic (it cannot be shadowed on PATH by the external
uv-tool ``hive-edge``), so it pins the IN-REPO engines specifically. The
console-script re-point itself is pinned separately by
``test_console_script_repointed``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
FIXTURES = HERE / "fixtures" / "engine_parity"
GOLDENS_PATH = FIXTURES / "goldens.json"

# The post-move engine CLI this whole module is a contract for. Its ABSENCE is
# the clean red every parity test reports pre-move.
EDGE_CLI_MODULE = "hive.edge.cli"
# The deterministic in-repo launcher (see the module docstring's launcher note).
IN_REPO_LAUNCHER = [sys.executable, "-m", EDGE_CLI_MODULE]

# The scenario inputs — the ONE source shared by the minting script and this
# replay, so an argv can never disagree between capture and verify. Each token an
# input needs (a stored memory's fp / subgraph_fp) is sourced from a prior mint
# scenario's PINNED golden, keyed by ``fp_from`` / ``subgraph_from``; ``bump_fp``
# rewrites the fp's version envelope to an incomparable future value (BUG-037);
# ``git`` materializes a real one-commit repo on ``branch`` (only the off-branch
# scenario needs a resolvable current branch). Repo paths are per-scenario tmp
# dirs — the tokens are path-independent by construction, so a fresh checkout at
# replay reproduces the pinned bytes.
SCENARIOS: list[dict] = [
    {
        "id": "mint_single_root",
        "verb": "mint",
        "fixture": "single_root",
        "anchor": "src/core.py:compute",
    },
    {
        "id": "mint_multi_root",
        "verb": "mint",
        "fixture": "multi_root",
        "anchor": "alpha/a.py:run",
    },
    {
        "id": "mint_sig_before",
        "verb": "mint",
        "fixture": "sig_change/before",
        "anchor": "mod.py:transform",
    },
    # fresh: the minted fp + subgraph_fp verify current against the same tree
    # (the radius tier reads `current`).
    {
        "id": "verify_fresh",
        "verb": "verify",
        "fixture": "single_root",
        "anchor": "src/core.py:compute",
        "fp_from": "mint_single_root",
        "subgraph_from": "mint_single_root",
    },
    # stale/signature_changed (+ delta): the BEFORE fp against the AFTER tree.
    {
        "id": "verify_signature_changed",
        "verb": "verify",
        "fixture": "sig_change/after",
        "anchor": "mod.py:transform",
        "fp_from": "mint_sig_before",
    },
    # stale/symbol_missing: an anchor whose symbol no longer exists.
    {
        "id": "verify_symbol_missing",
        "verb": "verify",
        "fixture": "single_root",
        "anchor": "src/core.py:does_not_exist",
    },
    # unverifiable/not_a_code_anchor (BUG-022): a prose anchor (no file extension)
    # whose missing file must NOT read stale.
    {
        "id": "verify_prose_not_a_code_anchor",
        "verb": "verify",
        "fixture": "single_root",
        "anchor": "the auth refresh flow:refresh",
    },
    # unverifiable/fingerprint_version_mismatch (BUG-037): a future-version fp is
    # incomparable, never a false stale/fresh.
    {
        "id": "verify_future_version_fp",
        "verb": "verify",
        "fixture": "single_root",
        "anchor": "src/core.py:compute",
        "fp_from": "mint_single_root",
        "bump_fp": True,
    },
    # branch_scoped/off_branch: a would-be-stale anchor scoped to a branch the
    # consumer is not on.
    {
        "id": "verify_branch_scoped",
        "verb": "verify",
        "fixture": "sig_change/after",
        "anchor": "mod.py:transform",
        "fp_from": "mint_sig_before",
        "branches": "git-branches/1:feature-x",
        "git": True,
        "branch": "main",
    },
]


def _module_exists(dotted: str) -> bool:
    """True iff ``dotted`` is importable in this env — the RED-FIRST guard idiom
    (mirrors conftest's ``load_module``): a missing parent package yields False,
    never a ModuleNotFoundError that would ERROR the test at run rather than fail
    it cleanly."""
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _bump_token_version(token: str, to: int = 999) -> str:
    """Rewrite a ``name/N:body`` fingerprint envelope to ``name/<to>:body`` so a
    version-gated comparator treats it as incomparable (BUG-037). Inlined from
    conftest's ``bump_token_version`` to keep this module importable independent
    of the frozen conftest."""
    m = re.match(r"^([A-Za-z0-9_.-]+/)(\d+)(:.*)$", token, re.DOTALL)
    assert m is not None, f"token {token[:40]!r} carries no version envelope"
    return f"{m.group(1)}{to}{m.group(3)}"


def _git_init_commit(repo: Path, branch: str) -> None:
    """A hermetic one-commit git repo on ``branch`` — the off-branch scenario
    needs ``git rev-parse --abbrev-ref HEAD`` to resolve a stable branch name.
    ``symbolic-ref`` sets the unborn branch regardless of the host's
    ``init.defaultBranch``; identity/gpg are forced inline so the commit never
    reads host git config."""

    def g(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        )

    subprocess.run(
        ["git", "init", "-q", str(repo)], check=True, capture_output=True, text=True
    )
    g("symbolic-ref", "HEAD", f"refs/heads/{branch}")
    g("add", "-A")
    g(
        "-c",
        "user.email=ct@example.com",
        "-c",
        "user.name=ct",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "engine-parity fixture",
    )


def materialize_repo(
    fixture_rel: str, dest: Path, *, git: bool = False, branch: str | None = None
) -> Path:
    """Copy a committed fixture subtree into ``dest`` (a throwaway repo), byte for
    byte, optionally standing up a one-commit git repo on ``branch``. The ONE
    materializer both the minting script and the replay share, so capture and
    verify can never diverge on the tree under test."""
    dest = Path(dest)
    src = FIXTURES / fixture_rel
    assert src.is_dir(), f"fixture {fixture_rel!r} is missing under {FIXTURES}"
    for path in sorted(src.rglob("*")):
        if path.is_file():
            rel = path.relative_to(src)
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest / rel)
    if git:
        _git_init_commit(dest, branch or "main")
    return dest


def build_argv_tail(scenario: dict, repo: Path, resolve_token) -> list[str]:
    """The CLI argv (after the launcher) for ``scenario`` against ``repo``.
    ``resolve_token(scenario_id, key)`` sources a stored memory's fp/subgraph_fp
    from a prior mint result — identically at mint (prior run results) and replay
    (pinned goldens)."""
    argv = [scenario["verb"], "--repo", str(repo), "--anchor", scenario["anchor"]]
    if scenario["verb"] != "verify":
        return argv
    fp_from = scenario.get("fp_from")
    if fp_from:
        fp = resolve_token(fp_from, "combdrift/fp")
        if fp and scenario.get("bump_fp"):
            fp = _bump_token_version(fp)
        if fp:
            argv += ["--fp", fp]
    subgraph_from = scenario.get("subgraph_from")
    if subgraph_from:
        subgraph = resolve_token(subgraph_from, "matrix/subgraph_fp")
        if subgraph:
            argv += ["--subgraph-fp", subgraph]
    if scenario.get("branches"):
        argv += ["--branches", scenario["branches"]]
    return argv


def run_scenario(
    launcher: list[str], scenario: dict, resolve_token, workroot: Path
) -> dict:
    """Materialize + run ONE scenario, returning ``{argv, exit, stdout}`` where
    ``stdout`` is the parsed JSON map (None when the process produced no JSON —
    e.g. a pre-move ``-m hive.edge.cli`` that cannot import). ``launcher`` is the
    argv prefix (the external ``["hive-edge"]`` at mint, ``[python, "-m",
    hive.edge.cli]`` at replay). HIVE_EDGE_HOME is isolated per scenario so the
    matrix graph cache is a clean full build, never a stale-manifest incremental."""
    repo = workroot / scenario["id"]
    materialize_repo(
        scenario["fixture"],
        repo,
        git=scenario.get("git", False),
        branch=scenario.get("branch"),
    )
    home = workroot / f"{scenario['id']}-home"
    home.mkdir(parents=True, exist_ok=True)
    argv_tail = build_argv_tail(scenario, repo, resolve_token)
    env = dict(os.environ, HIVE_EDGE_HOME=str(home))
    proc = subprocess.run(
        list(launcher) + argv_tail, capture_output=True, text=True, env=env
    )
    out = (proc.stdout or "").strip()
    try:
        parsed = json.loads(out) if out else None
    except ValueError:
        parsed = None
    return {
        "argv_tail": argv_tail,
        "exit": proc.returncode,
        "stdout": parsed,
        "raw": out,
        "stderr": (proc.stderr or "").strip(),
    }


def load_goldens() -> dict | None:
    """The pinned goldens, or None when unminted (keeps collection error-free)."""
    if not GOLDENS_PATH.is_file():
        return None
    return json.loads(GOLDENS_PATH.read_text(encoding="utf-8"))


def _resolver_from(scenario_results: dict):
    """A ``resolve_token(id, key)`` that reads a token from already-known scenario
    stdout maps (pinned goldens at replay; live results at mint)."""

    def resolve(scenario_id: str, key: str):
        stdout = (scenario_results.get(scenario_id) or {}).get("stdout") or {}
        return stdout.get(key)

    return resolve


# ── the contract ─────────────────────────────────────────────────────────────


def test_edge_cli_importable() -> None:
    """The absorbed engine CLI exists as an in-repo module with a ``main`` entry.
    RED pre-move (``hive.edge.cli`` absent); green after the engine move."""
    assert _module_exists(EDGE_CLI_MODULE), (
        f"post-move engine CLI {EDGE_CLI_MODULE!r} does not exist yet — the "
        "engine move (plan Step 1) has not landed"
    )
    module = importlib.import_module(EDGE_CLI_MODULE)
    assert callable(getattr(module, "main", None)), (
        f"{EDGE_CLI_MODULE}.main is not a callable entry point"
    )


def test_console_script_repointed() -> None:
    """Packaging re-points the KEPT ``hive-edge`` console script to the in-repo
    ``hive.edge.cli:main`` (plan D3), so ``sync.py``'s subprocess discovery is
    byte-untouched. RED pre-move (no ``hive-edge`` script entry); green after the
    packaging chunk."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data.get("project", {}).get("scripts", {})
    assert scripts.get("hive-edge") == "hive.edge.cli:main", (
        "pyproject [project.scripts] must re-point hive-edge = "
        f"'hive.edge.cli:main'; found {scripts.get('hive-edge')!r}"
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
def test_engine_parity(scenario: dict, tmp_path: Path) -> None:
    """Each fixture scenario, replayed against the in-repo CLI, reproduces its
    pinned golden field-for-field (tokens byte-exact via dict equality). The
    stored-memory fp/subgraph_fp inputs are sourced from the PINNED mint goldens,
    so every scenario is independently checkable and order-free.

    RED pre-move via the ``_module_exists`` guard (clean assertion, no import
    error); green once the in-repo engines reproduce the pre-move bytes."""
    assert _module_exists(EDGE_CLI_MODULE), (
        f"post-move engine CLI {EDGE_CLI_MODULE!r} does not exist yet — engine "
        "parity cannot be checked until the move (plan Step 1) lands"
    )
    goldens = load_goldens()
    assert goldens is not None, f"parity goldens not minted at {GOLDENS_PATH}"
    pinned = goldens["scenarios"]
    assert scenario["id"] in pinned, f"no golden for scenario {scenario['id']!r}"

    resolve = _resolver_from(pinned)
    result = run_scenario(IN_REPO_LAUNCHER, scenario, resolve, tmp_path)
    expected = pinned[scenario["id"]]

    assert result["exit"] == expected["exit"], (
        f"{scenario['id']}: exit {result['exit']} != golden {expected['exit']} "
        f"(stderr: {result['stderr'][:200]})"
    )
    assert result["stdout"] == expected["stdout"], (
        f"{scenario['id']}: in-repo engine output forked from the pre-move "
        f"golden.\n  got:      {result['stdout']}\n  expected: {expected['stdout']}"
    )
