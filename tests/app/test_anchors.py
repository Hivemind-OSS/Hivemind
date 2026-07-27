"""Unit suite for the boundary grammar gate ``hive.app.anchors`` — the engine of
the CT-1/CT-3 unknown-repo refusal scenarios (a write/capture/recall naming an
unregistered repo refuses cleanly, nothing stored; the handler maps ``BadAnchors``
to the ``status="refused"`` envelope), and of the anchor-grammar refusal it
delegates to ``hive.domain.anchor_grammar`` (a spelling the census subject feed
could never join refuses at the boundary rather than being stored unjoinable)."""

from __future__ import annotations

import pytest

from hive.app.anchors import (
    SCOPE_MAX_ENTRIES,
    BadAnchors,
    normalize_anchors,
    normalize_repos,
    split_scope,
)
from hive.domain.models import AnchorRef

KNOWN = {"alpha", "beta"}


# ── refusal discipline ────────────────────────────────────────────────────────


def test_bad_anchors_is_a_value_error():
    assert issubclass(BadAnchors, ValueError)


# ── split_scope (the one owner of the name@branch notation) ───────────────────


@pytest.mark.parametrize(
    "entry, expected",
    [
        ("alpha", ("alpha", "")),
        ("alpha@feature", ("alpha", "feature")),
        ("alpha@a@b", ("alpha", "a@b")),  # first @ only — branches may carry @
        ("@feature", ("", "feature")),
        ("alpha@", ("alpha", "")),  # lossy: refusal is the gates' job
        ("", ("", "")),
    ],
)
def test_split_scope_is_lexical(entry, expected):
    assert split_scope(entry) == expected


# ── normalize_anchors: absent forms ───────────────────────────────────────────


@pytest.mark.parametrize("absent", [None, "", [], ()])
def test_anchors_absent_forms_normalize_to_empty(absent):
    assert normalize_anchors(absent, known_repos=KNOWN) == ()


# ── normalize_anchors: conforming shapes ──────────────────────────────────────


def test_single_anchor_normalizes_to_anchor_ref():
    out = normalize_anchors(
        [{"repo": "alpha", "anchor": "hive/app/x.py::fn"}], known_repos=KNOWN
    )
    assert out == (AnchorRef(repo="alpha", anchor="hive/app/x.py::fn"),)
    assert out[0].fp_meta == ""  # the server mints fingerprints, never the agent


def test_multi_anchor_keeps_input_order():
    out = normalize_anchors(
        [{"repo": "beta", "anchor": "b.py::g"}, {"repo": "alpha", "anchor": "a.py::f"}],
        known_repos=KNOWN,
    )
    assert [(a.repo, a.anchor) for a in out] == [
        ("beta", "b.py::g"),
        ("alpha", "a.py::f"),
    ]


def test_injected_read_accepts_a_callable():
    out = normalize_anchors(
        [{"repo": "alpha", "anchor": "a.py"}], known_repos=lambda name: name == "alpha"
    )
    assert out[0].repo == "alpha"
    with pytest.raises(BadAnchors, match="unknown repo 'beta'"):
        normalize_anchors(
            [{"repo": "beta", "anchor": "b.py"}],
            known_repos=lambda name: name == "alpha",
        )


def test_colon_free_anchor_is_free_text():
    """The grammar constrains only colon-BEARING spellings; the rest is free text.

    Renamed from the wider claim it used to make: an anchor is no longer free text
    outright, but every non-empty string without a ``':'`` still admits verbatim —
    an anchor need not be code, so unicode, spaces and no path shape at all remain
    a legal binding."""
    weird = "some anchor // with spaces, ünïcode and no path shape"
    out = normalize_anchors([{"repo": "alpha", "anchor": weird}], known_repos=KNOWN)
    assert out[0].anchor == weird


@pytest.mark.parametrize(
    "anchor",
    [
        "a.py",  # file-scoped
        "a.py::S",  # symbol-scoped
        "a.py::Ns::C.m",  # nested '::' belongs to the SYMBOL, not the split
        "a:b/c.py::S",  # a colon-bearing path an explicit '::' makes unambiguous
        "a.py::f2",  # a digit INSIDE a symbol is not a line number
        "some anchor with no path shape",  # colon-free prose
    ],
)
def test_grammatical_anchors_are_stored_verbatim(anchor):
    """An admitted anchor rides through untouched — the gate never rewrites a
    spelling, so what the census join later matches is exactly what was sent."""
    out = normalize_anchors([{"repo": "alpha", "anchor": anchor}], known_repos=KNOWN)
    assert out == (AnchorRef(repo="alpha", anchor=anchor),)


# ── normalize_anchors: refusals name the violated clause ──────────────────────


def test_unknown_repo_refuses_and_names_the_repo():
    with pytest.raises(BadAnchors, match="unknown repo 'ghost'"):
        normalize_anchors([{"repo": "ghost", "anchor": "x.py::f"}], known_repos=KNOWN)


@pytest.mark.parametrize(
    "raw, clause",
    [
        ({"repo": "alpha", "anchor": "x"}, "must be an array"),  # bare object
        ("alpha", "must be an array"),  # bare string
        (42, "must be an array"),
        ([["alpha", "x.py"]], "must be a {repo, anchor} object"),  # non-dict entry
        (["alpha"], "must be a {repo, anchor} object"),
        ([{"repo": "alpha"}], r"anchors\[0\].anchor"),  # missing anchor
        ([{"anchor": "x.py"}], r"anchors\[0\].repo"),  # missing repo
        ([{"repo": "alpha", "anchor": ""}], "non-empty code location"),
        ([{"repo": "", "anchor": "x.py"}], "non-empty registered repo name"),
        ([{"repo": 3, "anchor": "x.py"}], "non-empty registered repo name"),
        ([{"repo": "alpha", "anchor": 3}], "non-empty code location"),
        ([{"repo": "alpha", "anchor": "x.py", "fp_meta": "t"}], "unknown key"),
        ([{"repo": "alpha", "anchor": "x.py", "evil": 1}], "unknown key"),
    ],
)
def test_malformed_anchors_refuse_with_the_clause(raw, clause):
    with pytest.raises(BadAnchors, match=clause):
        normalize_anchors(raw, known_repos=KNOWN)


@pytest.mark.parametrize(
    "anchor, clause",
    [
        ("a.py:S", "the symbol separator is '::', not ':'"),
        ("a.py:42", "the symbol separator is '::', not ':'"),
        # a colon-bearing path with no '::' is lexically indistinguishable from the
        # dead spelling, so the boundary refuses it too rather than guess
        ("a:b/c.py", "the symbol separator is '::', not ':'"),
        ("a.py::", "names no symbol after '::'"),
        ("a.py::42", "is a line number, not a symbol"),
        ("::S", "has an empty path component"),
    ],
)
def test_ungrammatical_anchors_refuse_with_the_clause(anchor, clause):
    """A spelling the census subject feed can never join refuses at the boundary.

    The single-colon rows are the load-bearing ones: that spelling used to be
    stored, resolve healthily in the drift engine, and match no census subject —
    so the memory could never be outcome-verified and died at the provisional TTL."""
    with pytest.raises(BadAnchors, match=clause) as excinfo:
        normalize_anchors([{"repo": "alpha", "anchor": anchor}], known_repos=KNOWN)
    # the refusal is addressed: which entry of the array violated the grammar
    assert "anchors[0].anchor" in str(excinfo.value)


def test_single_colon_refusal_names_the_canonical_spelling():
    """The refusal is actionable — it spells the '::' form that should have been sent."""
    with pytest.raises(BadAnchors, match=r"write 'hive/app/x\.py::fn'"):
        normalize_anchors(
            [{"repo": "alpha", "anchor": "hive/app/x.py:fn"}], known_repos=KNOWN
        )


def test_ungrammatical_anchor_refuses_the_whole_array():
    """A late bad spelling refuses every binding in the call, not just its own entry."""
    with pytest.raises(BadAnchors, match=r"anchors\[1\].anchor"):
        normalize_anchors(
            [
                {"repo": "alpha", "anchor": "ok.py::f"},
                {"repo": "beta", "anchor": "bad.py:g"},
            ],
            known_repos=KNOWN,
        )


def test_hostile_shapes_refuse_never_crash():
    """Total over object — a hostile shape refuses, it cannot crash the boundary."""
    hostile = [
        object(),
        {1: 2},
        [{b"repo": b"x"}],
        [{"repo": None, "anchor": None}],
        [None],
        [True],
        0.5,
        {"a"},
    ]
    for raw in hostile:
        with pytest.raises(BadAnchors):
            normalize_anchors(raw, known_repos=KNOWN)


def test_duplicate_binding_refuses():
    with pytest.raises(BadAnchors, match="duplicate anchor binding"):
        normalize_anchors(
            [
                {"repo": "alpha", "anchor": "x.py::f"},
                {"repo": "alpha", "anchor": "x.py::f"},
            ],
            known_repos=KNOWN,
        )


def test_entry_cap_refuses():
    # the generated anchors are colon-free, so they clear the grammar — the cap is
    # what refuses, and it refuses before any entry is inspected
    raw = [
        {"repo": "alpha", "anchor": f"f{i}.py"} for i in range(SCOPE_MAX_ENTRIES + 1)
    ]
    with pytest.raises(BadAnchors, match="max"):
        normalize_anchors(raw, known_repos=KNOWN)


def test_refusal_leaves_no_partial_output():
    """First violation refuses the WHOLE array — all-or-nothing, nothing stored."""
    with pytest.raises(BadAnchors):
        normalize_anchors(
            [
                {"repo": "alpha", "anchor": "ok.py"},
                {"repo": "ghost", "anchor": "bad.py"},
            ],
            known_repos=KNOWN,
        )


# ── normalize_repos: absent forms ─────────────────────────────────────────────


@pytest.mark.parametrize("absent", [None, "", [], ()])
def test_repos_absent_forms_normalize_to_empty(absent):
    assert normalize_repos(absent, known_repos=KNOWN) == ()


# ── normalize_repos: conforming shapes ────────────────────────────────────────


def test_plain_names_and_branch_refs_normalize():
    out = normalize_repos(["alpha", "beta@feature"], known_repos=KNOWN)
    assert out == (("alpha", ""), ("beta", "feature"))


def test_branch_is_free_text_split_on_first_at():
    out = normalize_repos(["alpha@release/v2@rc1"], known_repos=KNOWN)
    assert out == (("alpha", "release/v2@rc1"),)


# ── normalize_repos: refusals ─────────────────────────────────────────────────


def test_unknown_repo_name_refuses():
    with pytest.raises(BadAnchors, match="unknown repo 'ghost'"):
        normalize_repos(["ghost"], known_repos=KNOWN)


def test_unknown_repo_name_refuses_even_with_branch():
    with pytest.raises(BadAnchors, match="unknown repo 'ghost'"):
        normalize_repos(["ghost@main"], known_repos=KNOWN)


@pytest.mark.parametrize(
    "raw, clause",
    [
        ("alpha", "must be an array"),  # bare string, not an array
        ({"repo": "alpha"}, "must be an array"),
        ([42], r"repos\[0\]"),
        ([""], r"repos\[0\]"),
        ([None], r"repos\[0\]"),
        (["@feature"], "empty repo name"),
        (["alpha@"], "empty branch"),
    ],
)
def test_malformed_repos_refuse_with_the_clause(raw, clause):
    with pytest.raises(BadAnchors, match=clause):
        normalize_repos(raw, known_repos=KNOWN)


def test_duplicate_repo_name_refuses():
    with pytest.raises(BadAnchors, match="named twice"):
        normalize_repos(["alpha", "alpha@feature"], known_repos=KNOWN)


def test_repos_entry_cap_refuses():
    known = {f"r{i}" for i in range(SCOPE_MAX_ENTRIES + 1)}
    raw = sorted(known)
    with pytest.raises(BadAnchors, match="max"):
        normalize_repos(raw, known_repos=known)
