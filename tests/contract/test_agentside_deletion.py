"""CT-17 — the agent-side vestiges die with the engine move (plan §4 Intent 3, D4).

U4 deleted the only callers of the client-side hooks; in-container nothing invokes
them. So the absorbed ``hive.edge`` CLI does NOT port ``hooks.py``, the ``hook``
verb group, or the ``worktree-delta`` verb: ``hook`` and ``worktree-delta`` join
``census`` / ``upgrade`` in the argparse-rejected exit-2 pinned set, and
``hive.edge.hooks`` is not importable. This module pins that deletion.

RED-FIRST MECHANICS (authored pre-move, then frozen): every check GUARDS on the
post-move CLI existing (``hive.edge.cli``) and fails as a clean assertion while it
is absent — never an import/collection error. The verb-rejection checks drive the
IN-REPO CLI deterministically via ``python -m hive.edge.cli`` (the same reason
CT-16 does: the external uv-tool ``hive-edge`` on PATH still answers ``hook`` with
exit 0, so only the in-repo module proves the deletion). The served-string sweep
for these verbs stays owned by the untouched ``tests/app/test_contract.py``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

EDGE_CLI_MODULE = "hive.edge.cli"
IN_REPO_LAUNCHER = [sys.executable, "-m", EDGE_CLI_MODULE]
# argparse rejects an unknown subcommand with exit 2 (the census/upgrade pin).
ARGPARSE_REJECT = 2


def _module_exists(dotted: str) -> bool:
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _run_verb(verb: str) -> subprocess.CompletedProcess:
    """Invoke ``python -m hive.edge.cli <verb>`` in a scratch cwd so no ambient
    git repo influences the parse — argparse choice-validation happens before any
    verb body, so a rejected verb exits 2 regardless of cwd."""
    return subprocess.run(
        IN_REPO_LAUNCHER + [verb],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent),
    )


def test_hook_verb_rejected() -> None:
    """``hive-edge hook`` is gone (D4): argparse rejects it with exit 2. RED
    pre-move via the guard (the in-repo CLI does not exist); green after the
    engine move drops the hook dispatch (plan Step 1)."""
    assert _module_exists(EDGE_CLI_MODULE), (
        f"in-repo {EDGE_CLI_MODULE!r} does not exist yet — the hook-verb deletion "
        "cannot be checked until the engine move (plan Step 1) lands"
    )
    proc = _run_verb("hook")
    assert proc.returncode == ARGPARSE_REJECT, (
        f"`hive-edge hook` must be argparse-rejected (exit {ARGPARSE_REJECT}); got "
        f"exit {proc.returncode} (stdout={proc.stdout!r} stderr={proc.stderr[:200]!r})"
    )


def test_worktree_delta_verb_rejected() -> None:
    """``hive-edge worktree-delta`` is gone (D4): argparse rejects it with exit 2.
    RED pre-move via the guard; green after the engine move (plan Step 1)."""
    assert _module_exists(EDGE_CLI_MODULE), (
        f"in-repo {EDGE_CLI_MODULE!r} does not exist yet — the worktree-delta "
        "deletion cannot be checked until the engine move (plan Step 1) lands"
    )
    proc = _run_verb("worktree-delta")
    assert proc.returncode == ARGPARSE_REJECT, (
        f"`hive-edge worktree-delta` must be argparse-rejected (exit "
        f"{ARGPARSE_REJECT}); got exit {proc.returncode} "
        f"(stdout={proc.stdout!r} stderr={proc.stderr[:200]!r})"
    )


def test_hooks_module_not_ported() -> None:
    """``hive.edge.hooks`` is not importable — the Claude-Code hook adapters are
    not ported (D4). Guarded on the ``hive.edge`` package existing so the check is
    RED pre-move (package absent) and meaningfully green post-move (package
    present, ``hooks`` deliberately absent)."""
    assert _module_exists("hive.edge"), (
        "the hive.edge package does not exist yet — the engine move (plan Step 1) "
        "has not landed"
    )
    assert not _module_exists("hive.edge.hooks"), (
        "hive/edge/hooks.py must NOT be ported (D4) — the agent-side hook adapters "
        "are dead post-U4 and join the removed set"
    )
