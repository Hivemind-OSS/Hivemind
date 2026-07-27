"""ADD-1: change.verify_change — the change/diff-oriented front-end.

Verifies a commit's touched symbols by minting the call shape at a base worktree
and a head worktree and feeding the existing directional compare. The roll-up
reuses verdict precedence (unverifiable > stale > current). Two refinements over
the spec sketch are pinned here: a deletion is "removed" (provable -> stale), not
"indeterminate"; and a touched symbol that resolves at NEITHER tree abstains
(unverifiable), never false-stale (D2).
"""

from __future__ import annotations

from hive.combdrift.change import ChangeVerdict, SymbolChange, verify_change
from hive.combdrift.version import VerifierVersion


def _sc(cv: ChangeVerdict, symbol: str) -> SymbolChange:
    [hit] = [s for s in cv.symbols if s.symbol == symbol]
    return hit


# --- drift on a persisting symbol ---------------------------------------------


def test_breaking_drift_is_stale(make_repo):
    base = make_repo({"mod.py": "def f(a, b):\n    return (a, b)\n"}, name="base")
    head = make_repo({"mod.py": "def f(a, b, c):\n    return (a, b, c)\n"}, name="head")
    cv = verify_change(base, head, (("mod.py", "f"),), base_sha="b0", head_sha="h0")
    sc = _sc(cv, "f")
    assert sc.existed_before is True and sc.exists_after is True
    assert sc.drift == "breaking"
    assert sc.old_fingerprint is not None and sc.new_fingerprint is not None
    assert sc.old_fingerprint != sc.new_fingerprint
    assert cv.verdict == "stale"


def test_additive_drift_is_current(make_repo):
    base = make_repo({"mod.py": "def f(a, b):\n    return (a, b)\n"}, name="base")
    head = make_repo(
        {"mod.py": "def f(a, b, c=1):\n    return (a, b, c)\n"}, name="head"
    )
    cv = verify_change(base, head, (("mod.py", "f"),), base_sha="b", head_sha="h")
    assert _sc(cv, "f").drift == "additive"
    assert cv.verdict == "current"


def test_unchanged_is_current(make_repo):
    src = {"mod.py": "def f(a, b):\n    return (a, b)\n"}
    base = make_repo(src, name="base")
    head = make_repo(dict(src), name="head")
    cv = verify_change(base, head, (("mod.py", "f"),), base_sha="b", head_sha="h")
    assert _sc(cv, "f").drift == "unchanged"
    assert cv.verdict == "current"


# --- existence transitions -----------------------------------------------------


def test_deletion_is_removed_and_stale(make_repo):
    # f present at base, gone at head (file still there) -> provable deletion (D1).
    base = make_repo({"mod.py": "def f(a):\n    return a\n"}, name="base")
    head = make_repo({"mod.py": "def g(a):\n    return a\n"}, name="head")
    cv = verify_change(base, head, (("mod.py", "f"),), base_sha="b", head_sha="h")
    sc = _sc(cv, "f")
    assert sc.existed_before is True and sc.exists_after is False
    assert sc.drift == "removed"
    assert sc.reason.startswith("symbol_missing")
    assert cv.verdict == "stale"


def test_new_symbol_is_additive_and_current(make_repo):
    base = make_repo({"mod.py": "def g(a):\n    return a\n"}, name="base")
    head = make_repo({"mod.py": "def f(a):\n    return a\n"}, name="head")
    cv = verify_change(base, head, (("mod.py", "f"),), base_sha="b", head_sha="h")
    sc = _sc(cv, "f")
    assert sc.existed_before is False and sc.exists_after is True
    assert sc.drift == "additive"
    assert cv.verdict == "current"


# --- D2: a symbol that resolves at NEITHER tree must abstain, never false-stale -


def test_never_existed_symbol_abstains_not_stale(make_repo):
    base = make_repo({"mod.py": "def g():\n    return 1\n"}, name="base")
    head = make_repo({"mod.py": "def g():\n    return 2\n"}, name="head")
    cv = verify_change(base, head, (("mod.py", "ghost"),), base_sha="b", head_sha="h")
    sc = _sc(cv, "ghost")
    assert sc.existed_before is False and sc.exists_after is False
    assert sc.drift == "indeterminate"
    assert cv.verdict == "unverifiable"  # the load-bearing 0-FP guard: NOT stale


# --- indeterminate: shape uncomparable (overload at head) ----------------------


def test_overload_at_head_is_indeterminate(make_repo):
    base = make_repo({"mod.py": "def f(a):\n    return a\n"}, name="base")
    head = make_repo(
        {"mod.py": "def f(a):\n    return a\n\n\ndef f(a, b):\n    return b\n"},
        name="head",
    )
    cv = verify_change(base, head, (("mod.py", "f"),), base_sha="b", head_sha="h")
    sc = _sc(cv, "f")
    assert sc.exists_after is True
    assert sc.new_fingerprint is None  # overload -> no single token to compare
    assert sc.drift == "indeterminate"
    assert cv.verdict == "unverifiable"


# --- blast radius: an absent BASE token must not leak the head's uncompared reason -
#
# verify_change resolves head against the base token, so whenever the base mints no token
# the head resolution runs with no fingerprint — and resolution now calls that
# `no_fingerprint` (unverifiable) instead of `ok`. The change path decides those cases from
# existence and the token pair, never from that reason, so every SymbolChange below must
# read exactly as it did before resolution changed.


def test_added_symbol_keeps_ok_reason_despite_absent_base_token(make_repo):
    # The census path's hottest case: a symbol ADDED in the range. It is resolvable at head
    # and absent at base, so head resolves against a None base token — yet an add breaks no
    # previously-valid call, so it stays additive/ok, and a range of only adds still rolls
    # up current. Adopting the head resolution's reason here would report `no_fingerprint`
    # and flip every add-only commit's receipt from current to unverifiable.
    base = make_repo({"mod.py": "def g(a):\n    return a\n"}, name="base")
    head = make_repo({"mod.py": "def f(a):\n    return a\n"}, name="head")
    cv = verify_change(base, head, (("mod.py", "f"),), base_sha="b", head_sha="h")
    sc = _sc(cv, "f")
    assert sc.old_fingerprint is None  # nothing for the head shape to compare against
    assert sc.new_fingerprint is not None
    assert sc.drift == "additive"
    assert sc.reason == "ok"
    assert cv.verdict == "current"


def test_overloaded_base_stays_indeterminate(make_repo):
    # Overloaded at base: present both sides, but the base has no single call shape to mint,
    # so the pair is uncomparable -> indeterminate / fingerprint_version_mismatch, rolling up
    # unverifiable. The head resolution ran without a token here too; its reason stays out.
    base = make_repo(
        {"mod.py": "def f(a):\n    return a\n\n\ndef f(a, b):\n    return b\n"},
        name="base",
    )
    head = make_repo({"mod.py": "def f(a):\n    return a\n"}, name="head")
    cv = verify_change(base, head, (("mod.py", "f"),), base_sha="b", head_sha="h")
    sc = _sc(cv, "f")
    assert sc.existed_before is True and sc.exists_after is True
    assert sc.old_fingerprint is None  # overload -> no single token to compare
    assert sc.drift == "indeterminate"
    assert sc.reason == "fingerprint_version_mismatch"
    assert cv.verdict == "unverifiable"


def test_indirect_base_binding_stays_indeterminate(make_repo):
    # An indirect binding (a data alias, not a top-level callable) at base and still indirect
    # at head: absence is unprovable at either end, so the underlying implementation drifting
    # decides nothing -> indeterminate / symbol_indirect, rolling up unverifiable.
    base = make_repo(
        {"mod.py": "def _impl(a):\n    return a\n\n\nf = _impl\n"}, name="base"
    )
    head = make_repo(
        {"mod.py": "def _impl(a, b):\n    return (a, b)\n\n\nf = _impl\n"}, name="head"
    )
    cv = verify_change(base, head, (("mod.py", "f"),), base_sha="b", head_sha="h")
    sc = _sc(cv, "f")
    assert sc.existed_before is False and sc.exists_after is False
    assert sc.drift == "indeterminate"
    assert sc.reason == "symbol_indirect"
    assert cv.verdict == "unverifiable"


def test_no_change_reason_reports_no_fingerprint(make_repo):
    # The sweep: one range carrying every shape whose base token is absent (an add, an
    # indirect base promoted to a real callable, an overloaded base, a still-indirect base)
    # alongside the shapes that do compare (unchanged, breaking, deleted). No SymbolChange
    # may surface `no_fingerprint` — that reason belongs to the memory-verification path,
    # where an uncompared shape is a withheld claim; on a change receipt the base token's
    # absence is already spoken for by the drift.
    base = make_repo(
        {
            "mod.py": (
                "def kept(a, b):\n    return (a, b)\n\n\n"
                "def broke(a, b):\n    return (a, b)\n\n\n"
                "def gone(a):\n    return a\n\n\n"
                "def over(a):\n    return a\n\n\n"
                "def over(a, b):\n    return b\n\n\n"
                "def _impl(a):\n    return a\n\n\n"
                "promoted = _impl\n\n\n"
                "aliased = _impl\n"
            )
        },
        name="base",
    )
    head = make_repo(
        {
            "mod.py": (
                "def kept(a, b):\n    return (a, b)\n\n\n"
                "def broke(a, b, c):\n    return (a, b, c)\n\n\n"
                "def added(a):\n    return a\n\n\n"
                "def over(a):\n    return a\n\n\n"
                "def _impl(a, b):\n    return (a, b)\n\n\n"
                "def promoted(a):\n    return a\n\n\n"
                "aliased = _impl\n"
            )
        },
        name="head",
    )
    touched = tuple(
        ("mod.py", sym)
        for sym in ("kept", "broke", "gone", "added", "over", "promoted", "aliased")
    )
    cv = verify_change(base, head, touched, base_sha="b", head_sha="h")
    assert {s.symbol: s.drift for s in cv.symbols} == {
        "kept": "unchanged",
        "broke": "breaking",
        "gone": "removed",
        "added": "additive",
        "over": "indeterminate",
        "promoted": "additive",  # indirect at base -> a real callable at head
        "aliased": "indeterminate",
    }
    for sc in cv.symbols:
        assert "no_fingerprint" not in sc.reason, (sc.symbol, sc.reason)
    assert cv.verdict == "unverifiable"  # the indeterminate pair dominates


# --- roll-up precedence reuses verdict._classify -------------------------------


def test_breaking_plus_unchanged_rolls_up_stale(make_repo):
    base = make_repo(
        {"mod.py": "def f(a, b):\n    return (a, b)\n\n\ndef g(a):\n    return a\n"},
        name="base",
    )
    head = make_repo(
        {
            "mod.py": "def f(a, b, c):\n    return (a, b, c)\n\n\ndef g(a):\n    return a\n"
        },
        name="head",
    )
    cv = verify_change(
        base, head, (("mod.py", "f"), ("mod.py", "g")), base_sha="b", head_sha="h"
    )
    assert _sc(cv, "f").drift == "breaking"
    assert _sc(cv, "g").drift == "unchanged"
    assert cv.verdict == "stale"  # stale dominates current


def test_unverifiable_dominates_stale(make_repo):
    base = make_repo({"mod.py": "def f(a, b):\n    return (a, b)\n"}, name="base")
    head = make_repo({"mod.py": "def f(a, b, c):\n    return (a, b, c)\n"}, name="head")
    # f breaks (stale) but ghost is unverifiable -> unverifiable dominates.
    cv = verify_change(
        base, head, (("mod.py", "f"), ("mod.py", "ghost")), base_sha="b", head_sha="h"
    )
    assert _sc(cv, "f").drift == "breaking"
    assert cv.verdict == "unverifiable"


# --- shape of the output -------------------------------------------------------


def test_sha_and_version_stamp_present(make_repo):
    base = make_repo({"mod.py": "def f():\n    return 1\n"}, name="base")
    head = make_repo({"mod.py": "def f():\n    return 1\n"}, name="head")
    cv = verify_change(base, head, (("mod.py", "f"),), base_sha="BASE", head_sha="HEAD")
    assert cv.base_sha == "BASE" and cv.head_sha == "HEAD"
    assert isinstance(cv.verifier_version, VerifierVersion)
    assert cv.verifier_version.base_sha == "BASE"
    assert cv.verifier_version.head_sha == "HEAD"


def test_none_symbol_entries_are_skipped(make_repo):
    base = make_repo({"mod.py": "def f():\n    return 1\n"}, name="base")
    head = make_repo({"mod.py": "def f():\n    return 1\n"}, name="head")
    cv = verify_change(
        base, head, (("mod.py", None), ("mod.py", "f")), base_sha="b", head_sha="h"
    )
    assert [s.symbol for s in cv.symbols] == [
        "f"
    ]  # file-level touch produced no SymbolChange


def test_empty_touched_is_unverifiable(make_repo):
    base = make_repo({"mod.py": "def f():\n    return 1\n"}, name="base")
    head = make_repo({"mod.py": "def f():\n    return 1\n"}, name="head")
    cv = verify_change(base, head, (), base_sha="b", head_sha="h")
    assert cv.symbols == ()
    assert cv.verdict == "unverifiable"  # nothing verified -> abstain


# --- safety: verify_change parses, never imports/executes ----------------------


def test_verify_change_does_not_execute_target(make_repo):
    # A top-level sys.exit(1) would kill this process if imported; AST resolution
    # through verify_change must be unaffected.
    code = "import sys\nsys.exit(1)\n\n\ndef handler():\n    return 'ok'\n"
    base = make_repo({"boom.py": code}, name="base")
    head = make_repo({"boom.py": code}, name="head")
    cv = verify_change(
        base, head, (("boom.py", "handler"),), base_sha="b", head_sha="h"
    )
    assert _sc(cv, "handler").exists_after is True
    assert cv.verdict == "current"
