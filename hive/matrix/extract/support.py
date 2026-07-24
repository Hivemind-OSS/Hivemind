"""Shared low-level helpers for the extractors: id wrapper, text reads,
metadata sanitisation, recursion guard, reference-edge primitives, and the
tree-sitter version check."""

from __future__ import annotations

import html
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from ..ids import make_id

if TYPE_CHECKING:
    from tree_sitter import Node


# Language built-in globals that AST may classify as call targets when used as
# constructors or coercion functions (e.g. String(x), Number(x), Boolean(x)).
# Without this filter they become god-nodes accumulating spurious edges from
# every call site. Filter applied at same-file and cross-file resolution.
_LANGUAGE_BUILTIN_GLOBALS: frozenset[str] = frozenset(
    {
        # JavaScript / TypeScript ECMAScript built-ins
        "String",
        "Number",
        "Boolean",
        "Object",
        "Array",
        "Symbol",
        "BigInt",
        "Date",
        "RegExp",
        "Error",
        "TypeError",
        "RangeError",
        "SyntaxError",
        "ReferenceError",
        "EvalError",
        "URIError",
        "Promise",
        "Map",
        "Set",
        "WeakMap",
        "WeakSet",
        "JSON",
        "Math",
        "Reflect",
        "Proxy",
        "Intl",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "encodeURIComponent",
        "decodeURIComponent",
        "encodeURI",
        "decodeURI",
        # Browser / Node common globals
        "URL",
        "URLSearchParams",
        "FormData",
        "Blob",
        "File",
        "Headers",
        "Request",
        "Response",
        "AbortController",
        "AbortSignal",
        "TextEncoder",
        "TextDecoder",
        "console",
        # Python built-in callables
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "bytes",
        "len",
        "range",
        "enumerate",
        "zip",
        "map",
        "filter",
        "sum",
        "min",
        "max",
        "print",
        "open",
        "isinstance",
        "type",
        "super",
        "sorted",
        "reversed",
        "any",
        "all",
        "abs",
        "round",
        "next",
        "iter",
        "hash",
        "id",
        "repr",
        "callable",
        "getattr",
        "setattr",
        "hasattr",
        "delattr",
        "vars",
        "dir",
    }
)


def _make_id(*parts: str) -> str:
    return make_id(*parts)


def _file_stem(path: Path) -> str:
    parent = path.parent.name
    if parent and parent not in (".", ""):
        return f"{parent}.{path.stem}"
    return path.stem


def _read_text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


# ── Metadata sanitisation (recursive, bounded, HTML-safe) ─────────────────────
# Keeps node metadata JSON-compatible: strips control characters, HTML-escapes
# strings, and caps string/list size before the value reaches the graph.

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


_METADATA_MAX_VALUE_LEN = 512


_METADATA_MAX_LIST_ITEMS = 50


def _sanitize_metadata_string(value: object) -> str:
    """Return a control-character-free, HTML-escaped, bounded string."""
    text = _CONTROL_CHAR_RE.sub("", str(value))
    text = html.escape(text, quote=True)
    if len(text) > _METADATA_MAX_VALUE_LEN:
        text = text[:_METADATA_MAX_VALUE_LEN]
    return text


def _sanitize_metadata_value(value: object) -> object:
    """Sanitize a metadata value while preserving simple JSON-compatible types."""
    if isinstance(value, bool):
        # bool is a subclass of int — must be checked first to avoid coercion.
        return value
    if isinstance(value, str):
        return _sanitize_metadata_string(value)
    if isinstance(value, dict):
        return sanitize_metadata(value)
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_metadata_value(item) for item in value[:_METADATA_MAX_LIST_ITEMS]
        ]
    if isinstance(value, (int, float)) or value is None:
        return value
    return _sanitize_metadata_string(value)


def sanitize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, object]:
    """Sanitize metadata keys and values before graph export.

    Keeps the data JSON-compatible, strips control characters, escapes
    HTML-sensitive characters in strings, caps long strings/lists, and drops
    entries whose key becomes empty after sanitization.
    """
    if metadata is None:
        return {}
    result: dict[str, object] = {}
    for key, value in metadata.items():
        clean_key = _sanitize_metadata_string(key)
        if not clean_key:
            continue
        result[clean_key] = _sanitize_metadata_value(value)
    return result


_RECURSION_LIMIT = 10_000


# Language built-in globals that AST may classify as call targets when used as
# constructors or coercion functions (e.g. String(x), Number(x), Boolean(x)).
# Without this filter they become god-nodes accumulating spurious edges from
# every call site. Filter applied at same-file and cross-file resolution.
# See issue #726.


def _raise_recursion_limit() -> None:
    if sys.getrecursionlimit() < _RECURSION_LIMIT:
        sys.setrecursionlimit(_RECURSION_LIMIT)


def _safe_extract(
    extractor: Callable[[Path], dict[str, Any]], path: Path
) -> dict[str, Any]:
    try:
        return extractor(path)
    except RecursionError:
        print(
            f"  warning: skipped {path} (recursion limit exceeded)",
            file=sys.stderr,
            flush=True,
        )
        return {"nodes": [], "edges": [], "error": "recursion_limit_exceeded"}
    except Exception as e:
        if os.environ.get("MATRIX_DEBUG"):
            import traceback

            traceback.print_exc(file=sys.stderr)
        print(
            f"  warning: skipped {path} ({type(e).__name__}: {e})",
            file=sys.stderr,
            flush=True,
        )
        return {"nodes": [], "edges": [], "error": f"{type(e).__name__}: {e}"}


def _file_node_id(rel_path: Path) -> str:
    """File-level node ID: ``{parent_dir}_{stem}`` — one parent directory level,
    no extension. ``rel_path`` MUST be relative to the project root so top-level
    files collapse to a bare stem (``setup.py`` -> ``setup``) instead of picking
    up the root directory name. Every producer of a file node ID must agree on
    this recipe, or a file splits into two disconnected ghost nodes."""
    return _make_id(_file_stem(rel_path))


REFERENCE_CONTEXTS = frozenset(
    {
        "field",
        "parameter_type",
        "return_type",
        "generic_arg",
        "attribute",
        "value",
        "type",
    }
)


def _source_location(line: int | str | None) -> str | None:
    if line is None:
        return None
    if isinstance(line, str):
        return line if line.startswith("L") else f"L{line}"
    return f"L{line}"


def _semantic_reference_edge(
    source: str,
    target: str,
    context: str,
    source_file: str,
    line: int | str | None,
) -> dict[str, Any]:
    if context not in REFERENCE_CONTEXTS:
        raise ValueError(f"unknown reference context: {context}")
    return {
        "source": source,
        "target": target,
        "relation": "references",
        "context": context,
        "confidence": "EXTRACTED",
        "source_file": source_file,
        "source_location": _source_location(line),
        "weight": 1.0,
    }


# ── tree-sitter version gate, top-level extract(), and collect_files ──────────


def _check_tree_sitter_version() -> None:
    """Raise a clear error if tree-sitter is too old for the new Language API."""
    try:
        from tree_sitter import LANGUAGE_VERSION
    except ImportError:
        raise ImportError(
            "tree-sitter is not installed. Run: pip install 'tree-sitter>=0.23.0'"
        )
    # Language API v2 starts at LANGUAGE_VERSION 14
    if LANGUAGE_VERSION < 14:
        import tree_sitter as _ts

        raise RuntimeError(
            f"tree-sitter {getattr(_ts, '__version__', 'unknown')} is too old. "
            f"matrix requires tree-sitter >= 0.23.0 (Language API v2). "
            f"Run: pip install --upgrade tree-sitter"
        )
