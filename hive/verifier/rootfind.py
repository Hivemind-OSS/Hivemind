"""Config-dir discipline — where a tool must be spawned from (I/O, read-only).

A language's runner reads its project config from the working directory, so a
spawn from the wrong dir silently checks the wrong project — the monorepo
footgun. The rule: walk UP from the file toward the repo root and the first
directory holding any of the row's root markers wins. Safe direction: the walk
is CLAMPED to the repo root — a marker above the repo can never pull a spawn
cwd outside the repo being verified — and no marker degrades to the repo root,
never to a guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def find_nearest_config_dir(
    file: Path, repo_root: Path, markers: tuple[str, ...]
) -> Path:
    """Nearest ancestor of ``file`` (up to and including ``repo_root``) holding
    any marker; no marker anywhere -> ``repo_root``.

    ``file`` may be absolute (must lie under ``repo_root``) or repo-relative.
    Raises ``ValueError`` for a file outside the repo — programmer error, not a
    verification outcome.
    """
    file_abs = file if file.is_absolute() else repo_root / file
    # A bounded candidate list, never an unbounded upward while-loop: the
    # file's own dir, then each ancestor toward the filesystem root.
    candidates = [file_abs.parent, *file_abs.parent.parents]
    if repo_root not in candidates:
        raise ValueError(f"{file} is not under repo_root {repo_root}")
    for directory in candidates:
        if directory == repo_root:
            # The clamp: never consider anything above the repo root. A marker
            # at the root itself changes nothing — the answer is already root.
            break
        if any((directory / marker).exists() for marker in markers):
            return directory
    return repo_root


def group_by_config_dir(
    files: Sequence[Path], repo_root: Path, markers: tuple[str, ...]
) -> dict[Path, tuple[Path, ...]]:
    """Partition files by their nearest config dir, deterministically:
    keys in sorted order, members sorted within each group."""
    groups: dict[Path, list[Path]] = {}
    for file in files:
        config_dir = find_nearest_config_dir(file, repo_root, markers)
        groups.setdefault(config_dir, []).append(file)
    return {
        config_dir: tuple(sorted(members))
        for config_dir, members in sorted(groups.items())
    }
