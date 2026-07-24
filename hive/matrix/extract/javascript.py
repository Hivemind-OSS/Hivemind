"""JavaScript / TypeScript extractor: binds the JS/TS/TSX configs to the generic walker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .configs import _JS_CONFIG, _TS_CONFIG, _TSX_CONFIG
from .generic import _extract_generic


def extract_js(path: Path) -> dict[str, Any]:
    """Extract classes, functions, arrow functions, and imports from a .js/.ts/.tsx file."""
    if path.suffix == ".tsx":
        config = _TSX_CONFIG
    elif path.suffix == ".ts":
        config = _TS_CONFIG
    else:
        config = _JS_CONFIG
    return _extract_generic(path, config)
