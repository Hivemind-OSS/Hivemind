"""The incremental content-hash manifest algorithm (detect_incremental).

Exercises each branch of the change rule on a real temp tree:
  * a file with no stored hash ⇒ changed (brand new);
  * mtime bumped + different md5 ⇒ changed;
  * mtime bumped + same md5 ⇒ NOT changed (false-touch);
  * a manifest key that vanished ⇒ deleted;
  * prune_sources is the deleted set (changed files are replaced, not pruned).
"""

from __future__ import annotations

import os
from pathlib import Path

from hive.matrix import detect


def _write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8")


def _names(paths: list[Path]) -> set[str]:
    return {Path(p).name for p in paths}


def _manifest_path(tmp_path: Path) -> Path:
    return tmp_path / "out" / "manifest.json"


def test_save_then_load_round_trips_mtime_and_single_hash(tmp_path):
    f = tmp_path / "a.py"
    _write(f, "def a():\n    return 1\n")
    mp = _manifest_path(tmp_path)
    detect.save_manifest([f], mp)

    loaded = detect.load_manifest(mp)
    key = str(f.resolve())
    assert set(loaded) == {key}
    # Single content hash (matrix is AST-only — no dual ast/semantic pair).
    assert sorted(loaded[key]) == ["hash", "mtime"]
    assert loaded[key]["hash"]


def test_missing_hash_means_changed(tmp_path):
    """A discovered file absent from the manifest is changed (brand new)."""
    f = tmp_path / "a.py"
    _write(f, "def a():\n    return 1\n")
    mp = _manifest_path(tmp_path)
    # No save → empty manifest → everything is new.
    change = detect.detect_incremental(tmp_path, mp)
    assert "a.py" in _names(change["changed"])


def test_mtime_bump_with_different_md5_is_changed(tmp_path):
    f = tmp_path / "a.py"
    _write(f, "def a():\n    return 1\n")
    mp = _manifest_path(tmp_path)
    detect.save_manifest([f], mp)

    # Real content change + a guaranteed-later mtime.
    _write(f, "def a():\n    return 2\n")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))

    change = detect.detect_incremental(tmp_path, mp)
    assert "a.py" in _names(change["changed"])


def test_mtime_bump_with_same_md5_is_not_changed(tmp_path):
    """A touched-but-unmodified file must NOT count as changed (the md5 guard)."""
    f = tmp_path / "a.py"
    content = "def a():\n    return 1\n"
    _write(f, content)
    mp = _manifest_path(tmp_path)
    detect.save_manifest([f], mp)

    # Bump mtime without changing bytes.
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))
    _write(f, content)  # identical bytes
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 20))

    change = detect.detect_incremental(tmp_path, mp)
    assert "a.py" not in _names(change["changed"])


def test_removed_file_is_deleted(tmp_path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    _write(a, "def a():\n    return 1\n")
    _write(b, "def b():\n    return 2\n")
    mp = _manifest_path(tmp_path)
    detect.save_manifest([a, b], mp)

    b.unlink()  # delete b
    change = detect.detect_incremental(tmp_path, mp)
    assert "b.py" in _names(change["deleted"])
    assert "a.py" not in _names(change["deleted"])


def test_prune_sources_is_the_deleted_set(tmp_path):
    """prune_sources must equal deleted — a changed file is replaced, not pruned.

    Pruning a changed file would delete its freshly re-extracted nodes inside
    build_merge, so the change set must keep the two disjoint.
    """
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    _write(a, "def a():\n    return 1\n")
    _write(b, "def b():\n    return 2\n")
    mp = _manifest_path(tmp_path)
    detect.save_manifest([a, b], mp)

    # a changes, b is deleted.
    _write(a, "def a():\n    return 99\n")
    os.utime(a, (a.stat().st_atime, a.stat().st_mtime + 10))
    b.unlink()

    change = detect.detect_incremental(tmp_path, mp)
    assert "a.py" in _names(change["changed"])
    assert "b.py" in _names(change["deleted"])
    # prune_sources is exactly the deleted set: b yes, a (changed) no.
    assert _names(change["prune_sources"]) == {"b.py"}


def test_manifest_keys_are_pruned_when_file_vanishes_on_save(tmp_path):
    """save_manifest records only files that still exist at write time."""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    _write(a, "def a():\n    return 1\n")
    _write(b, "def b():\n    return 2\n")
    mp = _manifest_path(tmp_path)

    b.unlink()
    detect.save_manifest([a, b], mp)  # b passed but gone
    loaded = detect.load_manifest(mp)
    assert _names([Path(k) for k in loaded]) == {"a.py"}
