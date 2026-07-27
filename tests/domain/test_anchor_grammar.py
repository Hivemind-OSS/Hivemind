"""`hive/domain/anchor_grammar.py` is the ONE owner of what an anchor IS; these pin
all three of its functions so its consumers (the write gate, the census join, the
staleness prober) cannot fork again.

The STRICT tokenizer is pinned as a total function — the whole table of spellings, plus
the invariant that an accepted anchor rides VERBATIM (never stripped, never
re-spelled), because the stored string is what the census join later matches on. The
LENIENT read-side twin (`probe_target`) is pinned against it row for row: it must agree
on every spelling the boundary admits and differ on exactly one shape, the historical
single-colon anchor the gate now refuses but the store still holds. The refusal half is
pinned message-for-message: the sentence is the only thing a refused writer gets, so a
clause that stops naming its violated spelling is a regression even though the refusal
itself still fires.

Two cross-copy tripwires ride along: the pattern the prober hands git may never carry a
`-L` field separator, and the module must stay import-free — it is loaded by the pure
domain and runs on the hostile write path, so it can depend on nothing and crash on
nothing.
"""

from __future__ import annotations

import ast
import inspect

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hive.domain import anchor_grammar
from hive.domain.anchor_grammar import (
    SYMBOL_SEP,
    anchor_grammar_error,
    probe_target,
    split_anchor,
)

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


# ── the read-side twin: same table, plus the one historical shape ─────────────


@pytest.mark.parametrize(
    "anchor, expected",
    [
        ("a.py::S", ("a.py", "S")),  # canonical symbol-scoped
        ("a.py", ("a.py", "")),  # file-scoped
        ("a.py:greet", ("a.py", "greet")),  # THE historical shape: split, not dropped
        ("a.py:42", ("a.py:42", "")),  # a line number is a span, not a name
        ("a:b/c.py::S", ("a:b/c.py", "S")),  # an explicit '::' owns the split
        ("a:b/c.py", ("a", "b/c.py")),  # no '::' at all ⇒ the first colon splits
        ("the auth refresh flow", ("the auth refresh flow", "")),  # prose
        ("", ("", "")),  # total over the empty string
        ("a.py::Ns::C.m", ("a.py", "Ns::C.m")),  # nested '::' stays in the symbol
        (":greet", (":greet", "")),  # empty path half ⇒ no split
        ("a.py:", ("a.py:", "")),  # empty symbol half ⇒ no split
    ],
)
def test_probe_target_tokenizes_the_whole_table(anchor, expected):
    assert probe_target(anchor) == expected


@pytest.mark.parametrize("anchor", LIVE_SPELLINGS)
def test_probe_target_agrees_with_the_gate_on_every_admitted_spelling(anchor):
    """The two tokenizers may differ ONLY on shapes the write boundary refuses. If
    they diverged on an admitted one, the prober would ask git about a different
    target than the census join matches on — the BUG-077 shape, re-opened from the
    other side."""
    assert probe_target(anchor) == split_anchor(anchor)


def test_probe_target_differs_from_the_gate_exactly_on_the_refused_shape():
    """The read-historical leniency, stated as the difference it IS: the single-colon
    spelling the gate refuses is the one shape the prober still resolves, so that
    population keeps a computed verdict and stays inside retirement coverage."""
    anchor = "app.py:greet"
    assert anchor_grammar_error(anchor) is not None, "the gate still refuses it"
    assert split_anchor(anchor) == ("app.py:greet", ""), "the strict half is unmoved"
    assert probe_target(anchor) == ("app.py", "greet")


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

    probe_path, probe_symbol = probe_target(anchor)  # never raises either
    assert anchor.startswith(probe_path), "the read side rewrote its input"
    assert anchor.endswith(probe_symbol)

    error = anchor_grammar_error(anchor)  # never raises
    if error is None:
        assert ":" not in anchor or SYMBOL_SEP in anchor
        assert path, "an admitted anchor always has a path component"
        if SYMBOL_SEP in anchor:
            assert symbol and not symbol.isdecimal()
    else:
        assert isinstance(error, str) and error


# ── the cross-copy tripwires ──────────────────────────────────────────────────


@pytest.mark.parametrize("anchor", LIVE_SPELLINGS + ["app.py:greet", "a.py:42"])
def test_the_prober_never_hands_git_a_pattern_carrying_a_field_separator(anchor):
    """Both tokenizers live in this module now, so the mint-vs-reader pair cannot
    fork across a package boundary the way it did before. What still has to hold at
    the seam: whatever symbol the prober derives must be expressible as a ``-L``
    lookup, including for the historical spelling the read side revived."""
    from hive.app.sync import _funcname_patterns
    from hive.domain.staleness import symbol_lookups

    path, symbol = probe_target(anchor)
    assert path, "the prober always has a path to ask git about"
    for lookup in symbol_lookups(symbol):
        for pattern in _funcname_patterns(lookup):
            assert ":" not in pattern, (
                "':' is git -L's own field separator — a pattern carrying one is "
                "parsed as a PATH and comes back as a false 'no path' (a false stale)"
            )


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
