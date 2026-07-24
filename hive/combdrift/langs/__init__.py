"""Per-language tree-sitter backends behind a uniform `LangSpec`.

`REGISTRY` maps a file extension to the `LangSpec` the generic driver in
`combdrift.symbols` parses it with — one adapter module per language, so a
language can be added or pruned without touching the driver. Python is NOT here:
it resolves through the stdlib `ast` path in `symbols`, not tree-sitter.

`symbols.locate` owns the found/missing/indirect/parse_error/ambiguous tri-state
over every entry; a LangSpec only supplies observations (Law 1, written once).
"""

from __future__ import annotations

from hive.combdrift.langs.base import LangSpec, ReexportEdge
from hive.combdrift.langs.c import C
from hive.combdrift.langs.cpp import CPP
from hive.combdrift.langs.go import GO
from hive.combdrift.langs.javascript import JAVASCRIPT, TYPESCRIPT, TSX
from hive.combdrift.langs.rust import RUST

# Extension -> LangSpec. The lowercased path extension selects the backend and is
# the single source of which tree-sitter file types the verifier can resolve.
REGISTRY: dict[str, LangSpec] = {
    ".js": JAVASCRIPT,
    ".jsx": JAVASCRIPT,
    ".mjs": JAVASCRIPT,
    ".cjs": JAVASCRIPT,
    ".ts": TYPESCRIPT,
    ".mts": TYPESCRIPT,
    ".cts": TYPESCRIPT,
    ".tsx": TSX,
    ".go": GO,
    ".rs": RUST,
    ".c": C,
    ".h": C,
    ".cc": CPP,
    ".cpp": CPP,
    ".cxx": CPP,
    ".hpp": CPP,
    ".hh": CPP,
}

__all__ = ["REGISTRY", "LangSpec", "ReexportEdge"]
