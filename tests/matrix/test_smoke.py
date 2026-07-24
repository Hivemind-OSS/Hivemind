"""Chunk 0 scaffold contract: the package imports and carries a version string."""

from __future__ import annotations

import hive.matrix as matrix


def test_package_imports():
    assert matrix is not None


def test_version_is_nonempty_string():
    assert isinstance(matrix.__version__, str)
    assert matrix.__version__
