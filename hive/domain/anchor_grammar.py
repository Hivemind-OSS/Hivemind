"""The ONE server-side owner of the anchor grammar — what an anchor IS, and how it
tokenizes into ``(path, symbol)``.

Three owners used to answer that question differently: the write gate accepted any
non-empty string, the census join partitioned on ``"::"`` alone, and the drift
engine's tokenizer fell back to a single ``":"``. The disagreement was silent and
one-directional — a ``path/file.py:Symbol`` anchor was ACCEPTED, resolved correctly
by the engine (so its drift verdict looked healthy), and matched NO census subject,
so no ``change_outcome`` row could ever land and the memory died at the provisional
TTL while still being right (BUG-077). This module is that single owner; the write
gate (``hive.app.anchors``), the census join (``hive.domain.change_evidence``) and
the sync backfill (``hive.app.sync``) all read it, so acceptance and matching cannot
fork again.

The grammar, stated once::

    path                 file-scoped        (the whole string is the path)
    path::Symbol         symbol-scoped      (partition on the FIRST '::')
    path::Ns::Cls.m      symbol-scoped      (nested '::' belongs to the SYMBOL)

and the four refusals ``anchor_grammar_error`` names: an empty path component, a
``":"`` with no ``"::"`` (the dead spelling — lexically ambiguous, and a
colon-bearing path is indistinguishable from it), a ``"::"`` naming no symbol, and
a ``"::"`` whose symbol is a bare line number. Colon-free prose still admits: an
anchor need not be code.

MINT-CURRENT, READ-HISTORICAL (the meta-envelope posture, THEORY §5). The two halves
live HERE, adjacent, because they forked once before when they lived in different
packages and nothing caught it:

- the MINT side (``anchor_grammar_error``) is the write boundary — canonical spelling
  only, from now on, and it never rewrites what is already stored;
- the READ side (``probe_target``) keeps the historical ``path/file.py:Symbol``
  population RESOLVABLE against a tree, so a memory stored before the write gate
  landed still earns a computed drift verdict instead of dropping out of machine-gated
  retirement coverage (BUG-083).

``split_anchor`` stays the STRICT tokenizer and is what the census join reads: a
single-colon anchor matches no census subject, and loosening that side would re-open
BUG-077. Old formats age out by refresh, never by rewrite.

PURE: stdlib only, no imports at all. All three functions are TOTAL over any ``str``
and never raise — a hostile anchor refuses, it cannot crash the write boundary. The
purity gate (tests/test_purity.py) forbids sqlite3 | torch | subprocess | os | git |
time anywhere in hive/domain/.
"""

from __future__ import annotations

#: The symbol separator. A single ``":"`` is NOT it — that is the whole bug.
SYMBOL_SEP = "::"


def split_anchor(anchor: str) -> tuple[str, str]:
    """``(path, symbol)`` — partition on the FIRST ``"::"``.

    ``symbol == ""`` means file-scoped: both ``"a.py"`` and the degenerate
    ``"a.py::"`` read that way, matching what the census join does today. Nested
    ``"::"`` stays inside the symbol (``"a.py::Ns::C.m"`` → ``("a.py", "Ns::C.m")``),
    which is what C++/Rust receipt subjects spell. TOTAL over any ``str``; never
    raises, never strips, never rewrites — the stored anchor rides verbatim
    everywhere. // O(len)."""
    path, _sep, symbol = anchor.partition(SYMBOL_SEP)
    return path, symbol


def probe_target(anchor: str) -> tuple[str, str]:
    """``(path, symbol)`` for asking a TREE about a stored anchor — the read-historical
    twin of ``split_anchor``.

    Identical to ``split_anchor`` for every spelling the write gate accepts. It differs
    only for the dead single-colon spelling the gate now refuses but the store still
    holds: ``"app.py:greet"`` resolves to ``("app.py", "greet")`` rather than to a path
    no tree contains. Without that fallback the whole historical population reads
    ``unverifiable`` and leaves machine-gated retirement coverage — a real loss in the
    safe direction, which is why the leniency is a deliberate read-side decision rather
    than an accident of tokenizing (THEORY §5).

    A colon-bearing anchor is read as a path (not split) when the tail is a bare line
    number — a span is not a name, so no funcname resolver can match it — or when
    either half is empty. Colon-free prose is unaffected: it is its own path, which no
    tree contains, and the ladder calls that ``unverifiable``.

    NOT the census join's tokenizer: that side stays ``split_anchor``, because a
    single-colon anchor matches no census subject and pretending otherwise re-opens
    BUG-077. TOTAL over any ``str``; never raises, never rewrites. // O(len)."""
    path, symbol = split_anchor(anchor)
    if symbol or SYMBOL_SEP in anchor:
        return path, symbol
    head, sep, tail = anchor.partition(":")
    if sep and head and tail and not tail.isdecimal():
        return head, tail
    return path, symbol


def anchor_grammar_error(anchor: str) -> str | None:
    """``None`` when ``anchor`` is structurally joinable; else the violated clause.

    The message is the sentence the refusal envelope carries AFTER its
    ``anchors[i].anchor`` prefix (the caller owns the index), so a refused writer is
    told which spelling to use instead of guessing. TOTAL over any ``str``; never
    raises. // O(len).

    The clauses, in order — each closes a spelling that is dead in the census join,
    unresolvable in the drift engine, or a redundant form of one that already works:

    1. no path component at all;
    2. a ``":"`` with no ``"::"`` — the dead spelling. The guard is deliberately
       ``"::" not in anchor``, not "the path holds no colon": once an explicit
       ``"::"`` is present the split is unambiguous and the path may contain
       anything (``"a:b/c.py::S"`` joins at the symbol tier and resolves in the
       engine, so refusing it would be over-refusal). The accepted cost is that a
       colon-bearing path with NO ``"::"`` is refused — it is lexically
       indistinguishable from the bug shape, so no boundary rule can admit one and
       refuse the other;
    3. a ``"::"`` naming no symbol — a redundant spelling of the bare path, whose
       likeliest origin is a truncated symbol (a canonical-form decision, not an
       evidence-driven one);
    4. a ``"::"`` whose symbol is a bare line number — a span, not a name, so no
       funcname resolver and no census subject can ever match it.

    Symbol shape is deliberately NOT constrained beyond the bare-number case: an
    identifier regex would false-refuse exotic-language spellings the census
    legitimately emits, and only the bare-number shape is evidence-backed as dead."""
    path, symbol = split_anchor(anchor)
    if not path:
        return "has an empty path component"
    if SYMBOL_SEP not in anchor:
        if ":" in anchor:
            head, _sep, tail = anchor.partition(":")
            return (
                f"{anchor!r} — the symbol separator is '::', not ':' (a single-colon "
                f"anchor matches no census subject, so the memory can never be "
                f"outcome-verified); write '{head}::{tail}'"
            )
        return None
    if not symbol:
        return f"{anchor!r} names no symbol after '::' — drop the separator to bind the file"
    if symbol.isdecimal():
        return (
            f"{anchor!r} — {symbol!r} is a line number, not a symbol; bind the file "
            f"as '{path}'"
        )
    return None
