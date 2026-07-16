"""Contract tests for the composer-facing touched-set input carriers."""

from __future__ import annotations

import dataclasses

import pytest

from hive.verifier.touched import TouchedFile, TouchedSet


def tf(path: str = "src/app.py", lines: frozenset[int] = frozenset({3, 7})) -> TouchedFile:
    return TouchedFile(path=path, lines=lines)


class TestTouchedFile:
    def test_valid_file_constructs(self) -> None:
        f = tf()
        assert f.path == "src/app.py"
        assert f.lines == frozenset({3, 7})

    def test_empty_lines_allowed(self) -> None:
        # Renames/deletes ride with no post-image lines.
        assert tf(lines=frozenset()).lines == frozenset()

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            tf(path="")

    def test_absolute_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="repo-root-relative"):
            tf(path="/etc/passwd")

    def test_parent_segment_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\.\."):
            tf(path="pkg/../secrets.py")

    def test_bare_parent_path_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\.\."):
            tf(path="..")

    def test_dotted_filename_allowed(self) -> None:
        # Only a whole ".." segment is banned, not dots inside a name.
        assert tf(path="pkg/a..b.py").path == "pkg/a..b.py"

    def test_line_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            tf(lines=frozenset({0}))

    def test_negative_line_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            tf(lines=frozenset({-4}))

    def test_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            tf().path = "other.py"  # type: ignore[misc]


class TestTouchedSet:
    def test_valid_set_constructs(self) -> None:
        s = TouchedSet(files=(tf(), tf(path="src/other.py")))
        assert len(s.files) == 2

    def test_empty_set_allowed(self) -> None:
        assert TouchedSet(files=()).files == ()

    def test_duplicate_paths_rejected(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            TouchedSet(files=(tf(), tf(lines=frozenset({9}))))

    def test_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            TouchedSet(files=()).files = ()  # type: ignore[misc]
