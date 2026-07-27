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


def test_the_edge_cli_is_gone_whole() -> None:
    """The agent-side deletion completed: the CLI those two verbs were deleted FROM
    no longer exists either. A verb cannot be re-added to a module that is not there,
    so the guard that pinned their individual absence is now redundant — this pins the
    stronger fact instead."""
    for dotted in (EDGE_CLI_MODULE, "hive.edge", "hive.edge.hooks"):
        assert not _module_exists(dotted), (
            f"{dotted!r} is importable again — the in-image engine CLI was the last "
            "reader of the fingerprint carriers and was deleted with them"
        )
