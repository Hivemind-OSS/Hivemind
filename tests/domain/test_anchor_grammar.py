"""`hive/domain/anchor_grammar.py` is the ONE owner of what an anchor IS; these pin
both halves of it so the three consumers (the write gate, the census join, the sync
backfill) cannot fork again.

The tokenizer half is pinned as a total function — the whole table of spellings, plus
the invariant that an accepted anchor rides VERBATIM (never stripped, never
re-spelled), because the stored string is what the census join later matches on. The
refusal half is pinned message-for-message: the sentence is the only thing a refused
writer gets, so a clause that stops naming its violated spelling is a regression even
though the refusal itself still fires.

Two cross-copy tripwires ride along: the reader-side tokenizer
(`hive.edge.cli._split_anchor`, deliberately lenient about the historical single-colon
spelling) must still agree with this one on every spelling the boundary ADMITS, and
the module must stay import-free — it is loaded by the pure domain and runs on the
hostile write path, so it can depend on nothing and crash on nothing.
"""

from __future__ import annotations

import ast
import inspect

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hive.domain import anchor_grammar
from hive.domain.anchor_grammar import SYMBOL_SEP, anchor_grammar_error, split_anchor

# Every spelling the boundary admits — the canonical grammar from the module
# docstring plus the colon-free prose tail (an anchor need not be code).
LIVE_SPELLINGS = [
    "a.py",  # file-scoped
    "a.py::S",  # symbol-scoped
    "a.py::Ns::C.m",  # nested '::' belongs to the SYMBOL
    "hive/app/sync.py::SyncService.tick",  # dotted method
    "a:b/c.py::S",  # colon-bearing path made unambiguous by an explicit '::'
    "a.py::f2",  # digits in a symbol that is not ONLY digits
    "the auth refresh flow",  # colon-free prose
]


def _separator_message(anchor: str, corrected: str) -> str:
    """An independent copy of the single-colon clause's sentence — the refusal a
    writer of the dead spelling reads. Rebuilt here rather than pasted three times;
    drift between this and the module's f-string is what these rows catch."""
    return (
        f"{anchor!r} — the symbol separator is '::', not ':' (a single-colon anchor "
        f"matches no census subject, so the memory can never be outcome-verified); "
        f"write '{corrected}'"
    )


# ── the tokenizer: the whole table ────────────────────────────────────────────


@pytest.mark.parametrize(
    "anchor, expected",
    [
        ("a.py", ("a.py", "")),  # file-scoped: the whole string is the path
        ("a.py::S", ("a.py", "S")),  # symbol-scoped
        ("a.py::Ns::C.m", ("a.py", "Ns::C.m")),  # the FIRST separator wins
        ("a:b/c.py::S", ("a:b/c.py", "S")),  # a single ':' is never a separator
        ("a.py::", ("a.py", "")),  # degenerate: file-scoped, same as bare 'a.py'
        ("", ("", "")),  # total over any str, including the empty one
        ("::S", ("", "S")),  # an empty path component still tokenizes
        ("the auth refresh flow", ("the auth refresh flow", "")),  # prose
    ],
)
def test_split_anchor_tokenizes_the_whole_table(anchor, expected):
    assert split_anchor(anchor) == expected


def test_split_anchor_never_strips_or_rewrites():
    # the stored anchor is what the census join matches on later, so the tokenizer
    # must hand back the input's own bytes — surrounding whitespace and all.
    assert split_anchor("  a.py :: S  ") == ("  a.py ", " S  ")
    assert split_anchor(" a.py ") == (" a.py ", "")
    assert split_anchor("ünïcode/f.py::Cläss.m") == ("ünïcode/f.py", "Cläss.m")


@pytest.mark.parametrize(
    "anchor",
    ["", "a.py", "a.py::S", "a.py::Ns::C.m", "a:b/c.py::S", "a.py::", "::S", "::"],
)
def test_split_anchor_rejoins_losslessly(anchor):
    path, symbol = split_anchor(anchor)
    rejoined = path + SYMBOL_SEP + symbol if SYMBOL_SEP in anchor else path
    assert rejoined == anchor


def test_the_symbol_separator_is_the_double_colon():
    assert SYMBOL_SEP == "::"


# ── the refusals: each clause names its own violated spelling ─────────────────


@pytest.mark.parametrize(
    "anchor, message",
    [
        # 1. no path component at all
        ("", "has an empty path component"),
        ("::S", "has an empty path component"),
        ("::", "has an empty path component"),  # clause 1 wins over the empty symbol
        # 2. a ':' with no '::' — the dead spelling that joins no census subject
        ("a.py:S", _separator_message("a.py:S", "a.py::S")),
        ("a.py:42", _separator_message("a.py:42", "a.py::42")),
        # the corrected spelling is mechanically re-split at the FIRST colon, which
        # for a colon-bearing path is not the fix; the clause it names is what
        # carries the information here.
        ("a:b/c.py", _separator_message("a:b/c.py", "a::b/c.py")),
        # 3. a '::' naming no symbol — a redundant spelling of the bare path
        (
            "a.py::",
            "'a.py::' names no symbol after '::' — drop the separator to bind the file",
        ),
        # 4. a '::' whose symbol is a bare line number
        (
            "a.py::42",
            "'a.py::42' — '42' is a line number, not a symbol; bind the file as 'a.py'",
        ),
    ],
)
def test_refused_spellings_name_their_violated_clause(anchor, message):
    assert anchor_grammar_error(anchor) == message


@pytest.mark.parametrize("anchor", LIVE_SPELLINGS)
def test_admitted_spellings_are_not_refused(anchor):
    assert anchor_grammar_error(anchor) is None


@pytest.mark.parametrize("digits", ["42", "0", "007", "١٢"])
def test_a_bare_line_number_is_refused_in_any_script(digits):
    # the clause is `str.isdecimal`, not an ascii regex: a non-ascii digit string is
    # just as unresolvable a symbol as '42'.
    assert anchor_grammar_error(f"a.py::{digits}") is not None


@pytest.mark.parametrize("symbol", ["f2", "v2beta", "_1", "C.m2"])
def test_a_symbol_merely_containing_digits_still_admits(symbol):
    # symbol shape is deliberately unconstrained beyond the bare-number case — an
    # identifier regex would false-refuse spellings the census legitimately emits.
    assert anchor_grammar_error(f"a.py::{symbol}") is None


def test_the_single_colon_refusal_suggests_the_corrected_spelling():
    """The whole point of the clause: the writer is told which spelling to use, not
    merely that theirs is wrong."""
    message = anchor_grammar_error("app.py:greet")
    assert message is not None
    assert "write 'app.py::greet'" in message
    assert anchor_grammar_error("app.py::greet") is None  # the suggestion admits


@pytest.mark.parametrize(
    "anchor", ["", "::S", "a.py:S", "a:b/c.py", "a.py::", "a.py::42"]
)
def test_a_refusal_reads_as_one_sentence_after_the_callers_prefix(anchor):
    # the caller owns the index and prefixes `anchors[i].anchor `; the clause is the
    # rest of that sentence, so it may not open with whitespace or close a period.
    message = anchor_grammar_error(anchor)
    assert message is not None
    assert message == message.strip()
    assert not message.endswith(".")


# ── the properties: total over any str, and the admitted set is colon-safe ────

_ANCHOR_CHARS = "::.a/S0_ ü"  # colons, a dot, a slash, digits, space, non-ascii

anchors = st.one_of(
    st.text(alphabet=_ANCHOR_CHARS, max_size=12),
    st.text(max_size=40),
)


@settings(max_examples=500, deadline=None)
@given(anchor=anchors)
def test_both_functions_are_total_and_the_admitted_set_is_joinable(anchor: str) -> None:
    """Generated rather than tabulated because the input is HOSTILE: an anchor is
    free text off the wire, so the write boundary must refuse it, never crash on it.
    The joinability implication is the clause that matters — anything admitted either
    carries no colon at all or carries an explicit '::', which is exactly the set the
    census join can tokenize."""
    path, symbol = split_anchor(anchor)  # never raises
    rejoined = path + SYMBOL_SEP + symbol if SYMBOL_SEP in anchor else path
    assert rejoined == anchor, "the tokenizer rewrote its input"

    error = anchor_grammar_error(anchor)  # never raises
    if error is None:
        assert ":" not in anchor or SYMBOL_SEP in anchor
        assert path, "an admitted anchor always has a path component"
        if SYMBOL_SEP in anchor:
            assert symbol and not symbol.isdecimal()
    else:
        assert isinstance(error, str) and error


# ── the cross-copy tripwires ──────────────────────────────────────────────────


@pytest.mark.parametrize("anchor", LIVE_SPELLINGS)
def test_both_owners_tokenize_every_admitted_spelling_identically(anchor):
    """The mint side (this module) and the reader side (`hive.edge.cli._split_anchor`)
    are allowed to disagree only about spellings the boundary REFUSES — that
    asymmetry is the read-historical leniency, kept on purpose. Over the admitted set
    they must agree exactly, or an anchor would be stored under one reading and
    resolved under another (`""` and `None` are the same file-scoped meaning)."""
    from hive.edge.cli import _split_anchor

    path, symbol = split_anchor(anchor)
    edge_path, edge_symbol = _split_anchor(anchor)
    assert path == edge_path
    assert (symbol or None) == edge_symbol


def test_the_module_imports_nothing():
    """PURE to the letter: an import-free module cannot drag I/O into the domain, and
    cannot fail to load on the write path. The `__future__` pragma is a compiler
    directive, not a dependency — it is the one statement allowed here."""
    tree = ast.parse(inspect.getsource(anchor_grammar))
    statements = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert statements == ["from __future__ import annotations"]
