"""Shared fixtures: hermetic git env + the constructed engine fixture repo.

The engine fixture repo is the hermetic-REAL substrate for the adapter and
join tests: a two-commit git repo whose head narrows a function signature
(breaking), narrows a method signature (breaking), deletes two functions
(removed — one with a caller, one without), adds a source file, edits a
comment-only banner line, and touches a non-source file. The real matrix and
combdrift engines run over it; nothing engine-shaped is faked.

The expensive artifacts (worktrees, graphs, verdict) are session-scoped: the
graphs are pure in-memory values and the worktrees stay provisioned until the
session ends.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from hive.census import open_change
from hive.census.diff import Change

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "census-test",
    "GIT_AUTHOR_EMAIL": "census@test.invalid",
    "GIT_COMMITTER_NAME": "census-test",
    "GIT_COMMITTER_EMAIL": "census@test.invalid",
}


@pytest.fixture(scope="session", autouse=True)
def hermetic_git_session() -> Iterator[None]:
    """Session-wide git isolation so worktree/diff subprocesses spawned from
    session-scoped fixtures never read user or system git config."""
    previous = {key: os.environ.get(key) for key in _GIT_ENV}
    os.environ.update(_GIT_ENV)
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def census_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_GIT_ENV},
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


_BASE_LIB = (
    "def greet(name, punct):\n"
    '    return "hi " + name + punct\n'
    "\n"
    "\n"
    "def farewell(name):\n"
    '    return "bye " + name\n'
    "\n"
    "\n"
    "def orphan():\n"
    '    return "lonely"\n'
    "\n"
    "\n"
    "class Greeter:\n"
    "    def wave(self, name):\n"
    '        return greet(name, "!")\n'
)

_HEAD_LIB = (
    "def greet(name):\n"
    '    return "hi " + name\n'
    "\n"
    "\n"
    "class Greeter:\n"
    "    def wave(self):\n"
    '        return greet("x")\n'
)

_BASE_APP = (
    "from lib import farewell, greet\n"
    "\n"
    "\n"
    "def caller():\n"
    '    return greet("world", "!")\n'
    "\n"
    "\n"
    "def bye():\n"
    '    return farewell("world")\n'
    "\n"
    "\n"
    "def outer():\n"
    "    return caller()\n"
)

_HEAD_APP = (
    "from lib import greet\n"
    "\n"
    "\n"
    "def caller():\n"
    '    return greet("world")\n'
    "\n"
    "\n"
    "def bye():\n"
    '    return "bye world"\n'
    "\n"
    "\n"
    "def outer():\n"
    "    return caller()\n"
)

_TEST_LIB = (
    "from lib import greet\n"
    "\n"
    "\n"
    "def test_greet():\n"
    '    assert greet("x", ".")\n'
)

# The banner edit changes only a pre-symbol comment line: the sole in-span
# graph node is the file's own node, which attribution must never read as a
# symbol.
_BASE_LIB2 = "# old banner\ndef gamma():\n    return 3\n"
_HEAD_LIB2 = "# new banner\ndef gamma():\n    return 3\n"

# Added whole-file: the span starts at line 1, before the first symbol node,
# so only the nodes-starting-within-span half of the union rule can attribute
# it.
_UTIL = (
    "# helpers\n"
    "\n"
    "def alpha():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def beta():\n"
    "    return 2\n"
)


@pytest.fixture(scope="session")
def engine_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("engine-fixture") / "repo"
    repo.mkdir()
    census_git(repo, "init", "-q")
    (repo / "lib.py").write_text(_BASE_LIB)
    (repo / "app.py").write_text(_BASE_APP)
    (repo / "test_lib.py").write_text(_TEST_LIB)
    (repo / "lib2.py").write_text(_BASE_LIB2)
    (repo / "notes.txt").write_text("alpha\n")
    census_git(repo, "add", "-A")
    census_git(repo, "commit", "-qm", "base")
    (repo / "lib.py").write_text(_HEAD_LIB)
    (repo / "app.py").write_text(_HEAD_APP)
    (repo / "lib2.py").write_text(_HEAD_LIB2)
    (repo / "notes.txt").write_text("beta\n")
    (repo / "util.py").write_text(_UTIL)
    census_git(repo, "add", "-A")
    census_git(repo, "commit", "-qm", "head")
    return repo


@pytest.fixture(scope="session")
def engine_change(engine_repo: Path) -> Iterator[Change]:
    with open_change(engine_repo, "HEAD~1", "HEAD") as change:
        yield change


@pytest.fixture(scope="session")
def matrix_scratch() -> Path:
    from hive.census.engines import ensure_scratch_matrix_out

    return ensure_scratch_matrix_out()


@pytest.fixture(scope="session")
def graph_pair(engine_change: Change, matrix_scratch: Path):
    from hive.census.engines import build_graphs

    return build_graphs(engine_change)


@pytest.fixture(scope="session")
def touched_symbols(engine_change: Change, graph_pair):
    from hive.census.engines import attribute_symbols

    return attribute_symbols(engine_change.touched, graph_pair)


@pytest.fixture(scope="session")
def change_verdict(engine_change: Change, touched_symbols):
    from hive.census.engines import node_change

    return node_change(engine_change, touched_symbols)
