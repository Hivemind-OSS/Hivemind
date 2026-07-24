"""Shared fixtures for the hive-edge CLI verb tests.

`edge_home` (autouse) redirects the cli state/error-log home to a throwaway tmp
dir, so no test ever writes to the real ``~/.hive-edge``. Real git trees are
minted on demand (the branch-scope verify tests resolve the current branch);
plain Python trees suffice for mint/verify (combdrift needs a directory, never a repo).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hive.edge import cli


@pytest.fixture(autouse=True)
def edge_home(tmp_path, monkeypatch):
    """Redirect the cli state/error-log home to a per-test tmp dir."""
    home = tmp_path / "dot-hive-edge"
    monkeypatch.setattr(cli, "CONFIG_DIR", home)
    return home


@pytest.fixture
def py_repo(tmp_path):
    """A plain (non-git) tree with one resolvable callable: pkg/f.py:foo(a, b)."""
    root = tmp_path / "pyrepo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "f.py").write_text(
        "def foo(a, b):\n    return a + b\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def git_repo(tmp_path):
    """Builder: git_repo(name) -> a committed, clean git repo root Path."""

    def _make(name: str = "gitrepo") -> Path:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "t@t"], check=True
        )
        subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
        (root / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "c0"], check=True)
        return root

    return _make
