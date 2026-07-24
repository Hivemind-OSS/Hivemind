"""ADD-2: fingerprint.compare(old_token, new_token) — pairwise drift classifier.

compare is the change-path's directional drift oracle: it holds TWO recorded
tokens (base + head) and classifies their relationship by delegating to the
existing _breaks_call. This pins the unchanged|additive|breaking|indeterminate
truth table, the directionality, and the "a version we cannot read is never a
break" rule (mirroring matches()'s incomparable).
"""

from __future__ import annotations

from hive.combdrift.fingerprint import Interface, compare, render


def _iface(**overrides) -> Interface:
    base = dict(
        category="func",
        is_generator=False,
        req_positional=0,
        max_positional=0,
        has_star=False,
        has_kw=False,
        req_kwonly=0,
        contract_decorators=frozenset(),
        base_count=0,
    )
    base.update(overrides)
    return Interface(**base)


def _tok(**overrides) -> str:
    return render(_iface(**overrides))


# --- unchanged: byte-identical / equal interfaces -----------------------------


def test_identical_tokens_unchanged():
    t = _tok(req_positional=2, max_positional=2)
    assert compare(t, t) == "unchanged"


def test_equal_interfaces_unchanged():
    assert (
        compare(
            _tok(req_positional=1, max_positional=3),
            _tok(req_positional=1, max_positional=3),
        )
        == "unchanged"
    )


# --- additive: parses, not breaking, shapes differ ----------------------------


def test_added_optional_positional_is_additive():
    assert (
        compare(
            _tok(req_positional=2, max_positional=2),
            _tok(req_positional=2, max_positional=3),
        )
        == "additive"
    )


def test_added_star_is_additive():
    assert (
        compare(
            _tok(req_positional=2, max_positional=2),
            _tok(req_positional=0, max_positional=0, has_star=True),
        )
        == "additive"
    )


def test_added_kwargs_is_additive():
    assert compare(_tok(has_kw=False), _tok(has_kw=True)) == "additive"


def test_added_base_is_additive():
    assert (
        compare(
            _tok(category="class", base_count=1), _tok(category="class", base_count=2)
        )
        == "additive"
    )


# --- breaking: _breaks_call(old, new) is True ---------------------------------


def test_required_arity_increase_is_breaking():
    assert (
        compare(
            _tok(req_positional=2, max_positional=2),
            _tok(req_positional=3, max_positional=3),
        )
        == "breaking"
    )


def test_capacity_drop_below_required_is_breaking():
    assert (
        compare(
            _tok(req_positional=3, max_positional=3),
            _tok(req_positional=2, max_positional=2),
        )
        == "breaking"
    )


def test_removed_kwargs_is_breaking():
    assert compare(_tok(has_kw=True), _tok(has_kw=False)) == "breaking"


def test_sync_to_async_is_breaking():
    assert compare(_tok(category="func"), _tok(category="async_func")) == "breaking"


def test_func_to_class_is_breaking():
    assert compare(_tok(category="func"), _tok(category="class")) == "breaking"


def test_generator_toggle_is_breaking():
    assert compare(_tok(is_generator=False), _tok(is_generator=True)) == "breaking"


def test_decorator_toggle_is_breaking():
    assert (
        compare(
            _tok(contract_decorators=frozenset()),
            _tok(contract_decorators=frozenset({"property"})),
        )
        == "breaking"
    )


def test_base_removed_is_breaking():
    assert (
        compare(
            _tok(category="class", base_count=2), _tok(category="class", base_count=1)
        )
        == "breaking"
    )


# --- directionality: compare is NOT symmetric ---------------------------------


def test_directionality_kwargs():
    # Removing kwargs breaks; adding it is additive — the same pair, swapped.
    with_kw = _tok(has_kw=True)
    without_kw = _tok(has_kw=False)
    assert compare(with_kw, without_kw) == "breaking"
    assert compare(without_kw, with_kw) == "additive"


# --- indeterminate: a version we cannot read is never a break -----------------

_FUTURE = "combdrift-fp/2:func(req=0,max=0,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"


def test_future_old_token_is_indeterminate():
    # Even against a wildly breaking-looking new token, a future old is abstain.
    assert compare(_FUTURE, _tok(req_positional=9)) == "indeterminate"


def test_future_new_token_is_indeterminate():
    assert compare(_tok(req_positional=9), _FUTURE) == "indeterminate"


def test_malformed_old_token_is_indeterminate():
    assert compare("not-a-fingerprint", _tok()) == "indeterminate"


def test_malformed_new_token_is_indeterminate():
    assert compare(_tok(), "not-a-fingerprint") == "indeterminate"


def test_empty_token_is_indeterminate():
    assert compare("", "") == "indeterminate"


# --- old v1 tokens stay comparable across the format's lifetime ----------------


def test_old_version_tokens_still_compare():
    old = "combdrift-fp/1:func(req=1,max=1,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
    assert compare(old, _tok(req_positional=1, max_positional=1)) == "unchanged"
    assert compare(old, _tok(req_positional=2, max_positional=2)) == "breaking"
