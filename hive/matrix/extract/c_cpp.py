"""C and C++ extractors: bind the C/C++ configs to the generic walker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .configs import _C_CONFIG, _CPP_CONFIG
from .generic import _extract_generic


def extract_c(path: Path) -> dict[str, Any]:
    """Extract functions and includes from a .c/.h file."""
    return _extract_generic(path, _C_CONFIG)


def extract_cpp(path: Path) -> dict[str, Any]:
    """Extract functions, classes, and includes from a .cpp/.cc/.cxx/.hpp file."""
    return _extract_generic(path, _CPP_CONFIG)
