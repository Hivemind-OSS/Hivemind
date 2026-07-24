"""Single source of truth for node-ID normalization.

Independent producers must agree on node IDs or the graph splits a single entity
into disconnected ghost nodes: the AST extractor (per language) and the graph
builder that reconciles edge endpoints. Keeping the recipe in one module is what
stops those producers from drifting — the historical ID-drift bug class (Unicode
collapse, same-filename collisions, extractor-vs-builder file-node mismatch) all
came from copy-pasted, separately-maintained normalization.

The recipe: NFKC-normalize (so composed/decomposed Unicode forms collapse),
replace runs of non-word characters with a single underscore (``re.UNICODE`` so
CJK/Cyrillic/Arabic/accented-Latin letters survive instead of collapsing to a
per-file node), collapse repeated underscores, strip leading/trailing
underscores, and casefold.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["normalize_id", "make_id"]


def normalize_id(s: str) -> str:
    r"""Normalize a single ID string to its canonical form.

    Idempotent: ``normalize_id(normalize_id(s)) == normalize_id(s)``.
    """
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[^\w]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s)
    return s.strip("_").casefold()


def make_id(*parts: str) -> str:
    """Build a canonical node ID from one or more name parts.

    Parts are joined with ``_`` (after stripping stray ``_``/``.`` edges from each
    part) and then run through :func:`normalize_id`, so the result is identical to
    what the builder produces from the joined string.
    """
    return normalize_id("_".join(p.strip("_.") for p in parts if p))
