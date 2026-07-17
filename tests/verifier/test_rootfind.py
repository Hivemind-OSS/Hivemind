"""Contracts over config-dir discipline — the wrong-cwd footgun, closed.

A runner spawned from the repo root when the file's project config lives in a
nested directory reads the wrong (or no) config — the monorepo near-miss this
module exists to prevent. The nearest config dir wins, the walk is clamped to
the repo root (a marker above the repo can never pull a spawn outside it), and
no marker at all degrades honestly to the repo root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hive.verifier.rootfind import find_nearest_config_dir, group_by_config_dir

_MARKERS = ("pyproject.toml", "setup.cfg")


def _tree(root: Path, *paths: str) -> None:
    for rel in paths:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")


# --- nearest wins ----------------------------------------------------------------


def test_nested_config_dir_beats_repo_root(tmp_path: Path) -> None:
    _tree(tmp_path, "pyproject.toml", "pkg/pyproject.toml", "pkg/src/mod.py")
    found = find_nearest_config_dir(tmp_path / "pkg/src/mod.py", tmp_path, _MARKERS)
    assert found == tmp_path / "pkg"


def test_marker_in_the_files_own_dir_wins_over_parents(tmp_path: Path) -> None:
    _tree(tmp_path, "pkg/pyproject.toml", "pkg/src/pyproject.toml", "pkg/src/mod.py")
    found = find_nearest_config_dir(tmp_path / "pkg/src/mod.py", tmp_path, _MARKERS)
    assert found == tmp_path / "pkg" / "src"


def test_any_of_several_markers_matches(tmp_path: Path) -> None:
    _tree(tmp_path, "pkg/setup.cfg", "pkg/mod.py")
    found = find_nearest_config_dir(tmp_path / "pkg/mod.py", tmp_path, _MARKERS)
    assert found == tmp_path / "pkg"


def test_marker_at_repo_root_resolves_to_repo_root(tmp_path: Path) -> None:
    _tree(tmp_path, "pyproject.toml", "src/deep/mod.py")
    found = find_nearest_config_dir(tmp_path / "src/deep/mod.py", tmp_path, _MARKERS)
    assert found == tmp_path


# --- the clamp ---------------------------------------------------------------------


def test_marker_above_repo_root_is_ignored(tmp_path: Path) -> None:
    # The marker sits OUTSIDE the repo; the walk must clamp at repo_root and
    # never let a spawn cwd escape the repo being verified.
    _tree(tmp_path, "pyproject.toml", "repo/src/mod.py")
    repo = tmp_path / "repo"
    found = find_nearest_config_dir(repo / "src/mod.py", repo, _MARKERS)
    assert found == repo


# --- honest degradation ---------------------------------------------------------------


def test_no_marker_anywhere_degrades_to_repo_root(tmp_path: Path) -> None:
    _tree(tmp_path, "src/mod.py")
    assert find_nearest_config_dir(tmp_path / "src/mod.py", tmp_path, _MARKERS) == tmp_path


def test_empty_markers_degrade_to_repo_root(tmp_path: Path) -> None:
    # The sql row carries no root markers: the config dir IS the repo root.
    _tree(tmp_path, "pyproject.toml", "db/schema.sql")
    assert find_nearest_config_dir(tmp_path / "db/schema.sql", tmp_path, ()) == tmp_path


# --- input shapes -----------------------------------------------------------------------


def test_relative_file_path_is_resolved_against_repo_root(tmp_path: Path) -> None:
    _tree(tmp_path, "pkg/pyproject.toml", "pkg/mod.py")
    found = find_nearest_config_dir(Path("pkg/mod.py"), tmp_path, _MARKERS)
    assert found == tmp_path / "pkg"


def test_file_outside_repo_root_is_a_programmer_error(tmp_path: Path) -> None:
    _tree(tmp_path, "elsewhere/mod.py", "repo/pyproject.toml")
    with pytest.raises(ValueError):
        find_nearest_config_dir(tmp_path / "elsewhere/mod.py", tmp_path / "repo", _MARKERS)


# --- grouping -------------------------------------------------------------------------


def test_grouping_merges_same_config_and_separates_different(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        "a/pyproject.toml",
        "a/one.py",
        "a/two.py",
        "b/pyproject.toml",
        "b/three.py",
        "loose.py",
    )
    groups = group_by_config_dir(
        [
            tmp_path / "b/three.py",
            tmp_path / "a/two.py",
            tmp_path / "loose.py",
            tmp_path / "a/one.py",
        ],
        tmp_path,
        _MARKERS,
    )
    assert groups == {
        tmp_path: (tmp_path / "loose.py",),
        tmp_path / "a": (tmp_path / "a/one.py", tmp_path / "a/two.py"),
        tmp_path / "b": (tmp_path / "b/three.py",),
    }


def test_grouping_is_deterministic_regardless_of_input_order(tmp_path: Path) -> None:
    _tree(tmp_path, "a/pyproject.toml", "a/one.py", "a/two.py")
    files = [tmp_path / "a/two.py", tmp_path / "a/one.py"]
    forward = group_by_config_dir(files, tmp_path, _MARKERS)
    backward = group_by_config_dir(list(reversed(files)), tmp_path, _MARKERS)
    assert forward == backward
    assert list(forward) == sorted(forward)
    assert forward[tmp_path / "a"] == tuple(sorted(forward[tmp_path / "a"]))


def test_grouping_empty_input_is_empty(tmp_path: Path) -> None:
    assert group_by_config_dir([], tmp_path, _MARKERS) == {}
