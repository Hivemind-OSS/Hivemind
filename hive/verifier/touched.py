"""Composer-facing input contract: which files (and post-image lines) a change touched.

Pure module. The census composer projects its diff-native carrier into this narrow
shape; this package never sees diff text, spans, or file statuses — it consumes a
validated touched-set and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TouchedFile:
    """One touched file: repo-root-relative POSIX path + post-image touched lines.

    ``lines`` may be empty — renames and deletes ride with no post-image lines,
    kept for provenance.
    """

    path: str
    lines: frozenset[int]

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("TouchedFile.path must be non-empty")
        if self.path.startswith("/"):
            raise ValueError(
                f"TouchedFile.path must be repo-root-relative, got {self.path!r}"
            )
        if ".." in self.path.split("/"):
            raise ValueError(
                f"TouchedFile.path must not contain '..' segments, got {self.path!r}"
            )
        if any(line < 1 for line in self.lines):
            raise ValueError("TouchedFile.lines must all be >= 1")


@dataclass(frozen=True, slots=True)
class TouchedSet:
    """The complete touched-set for one change, unique by path."""

    files: tuple[TouchedFile, ...]

    def __post_init__(self) -> None:
        paths = [f.path for f in self.files]
        if len(paths) != len(set(paths)):
            dupes = sorted({p for p in paths if paths.count(p) > 1})
            raise ValueError(f"TouchedSet paths must be unique; duplicates: {dupes}")
