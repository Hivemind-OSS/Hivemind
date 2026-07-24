"""Python extractor: thin wrapper binding the Python config to the generic walker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .configs import _PYTHON_CONFIG
from .generic import _extract_generic


# ── Public API ────────────────────────────────────────────────────────────────


def extract_python(path: Path) -> dict[str, Any]:
    """Extract classes, functions, and imports from a .py file via tree-sitter AST."""
    return _extract_generic(path, _PYTHON_CONFIG)
