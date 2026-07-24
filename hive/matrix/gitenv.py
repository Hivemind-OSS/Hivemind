"""The seven git repository-discovery env vars, per git's own documented behavior.

An inherited (not `-C`-derived) GIT_DIR silently overrides `-C <path>` targeting, so
any code that shells `git -C <other-path>` from inside a git hook subprocess (which
git populates with these vars) must strip them first or it silently targets the WRONG
repository. matrix is the dependency floor every consumer in the workspace already
stands on, so this module is the ONE owner of the denylist — census and the edge CLI
import it; nothing keeps a byte-identical copy anymore.
"""

from __future__ import annotations

import os

_GIT_REPO_DISCOVERY_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


def clean_git_env() -> dict[str, str]:
    """A copy of the current environment with the repo-discovery vars stripped, so a
    `git -C <path>` child targets `<path>` and not an inherited GIT_DIR."""
    return {k: v for k, v in os.environ.items() if k not in _GIT_REPO_DISCOVERY_VARS}
