"""Single source of truth for the engine's output-directory name and artifacts.

The output directory is ``matrix-out`` by default and overridable with the
``MATRIX_OUT`` env var (worktrees or shared-output setups). It accepts a relative
name (``"matrix-out-feature"``) or an absolute path (``"/shared/matrix-out"``).
Centralising it here keeps the name in one place; the value is read once at import
time, so set ``MATRIX_OUT`` before the process starts and every reader honours it.

Two artifacts live under it: ``graph.json`` (the serialized graph) and
``manifest.json`` (the incremental content-hash manifest).
"""

from __future__ import annotations

import os
from pathlib import Path

MATRIX_OUT = os.environ.get("MATRIX_OUT", "matrix-out")

# Bare directory name even when MATRIX_OUT is an absolute path. Used by the scan
# exclude so a custom output dir is never re-ingested as source.
MATRIX_OUT_NAME = os.path.basename(os.path.normpath(MATRIX_OUT))


def out_path(*parts: str) -> Path:
    """A path inside the configured output dir, e.g. ``out_path("cache")``.

    ``Path(MATRIX_OUT) / ...`` resolves correctly for both a relative name
    ("matrix-out") and an absolute override ("/shared/matrix-out").
    """
    return Path(MATRIX_OUT, *parts)


def default_graph_json() -> str:
    """Default ``graph.json`` path under the configured output dir."""
    return str(out_path("graph.json"))


def default_manifest() -> str:
    """Default ``manifest.json`` path under the configured output dir."""
    return str(out_path("manifest.json"))
