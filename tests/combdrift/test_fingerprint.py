"""The pure format owner: Interface + render + directional matches().

This module has no parse or filesystem dependency; it is the entire 0-false-stale
surface for Layer B, so it is hardened in isolation. `render` mints the opaque,
version-tagged capture token; `matches` is a directional call-compatibility
compare — it returns "mismatch" (→ stale) only for a change that breaks a
previously-valid call, "incomparable" (→ unverifiable) for a version it cannot
parse, and "match" otherwise. Additive changes never mismatch.
"""

from __future__ import annotations

from hive.combdrift.fingerprint import FINGERPRINT_VERSION, Interface, matches, render


def _iface(**overrides) -> Interface:
    """Build an Interface with neutral defaults; override only what a test needs."""
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


# --- render: versioned, deterministic, canonical ------------------------------


def test_render_is_versioned_deterministic():
    a = render(_iface(req_positional=2, max_positional=3))
    b = render(_iface(req_positional=2, max_positional=3))
    assert a == b  # no clock, RNG, or set-ordering nondeterminism
    assert a.startswith("combdrift-fp/1:")  # version tag present
    assert FINGERPRINT_VERSION == "1"


def test_render_canonical_string():
    # Pin the exact format; this module is its single owner.
    s = render(_iface(req_positional=2, max_positional=3))
    assert s == "combdrift-fp/1:func(req=2,max=3,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"


def test_render_decorators_sorted():
    # Set membership rendered in a stable (sorted) order for determinism.
    s = render(_iface(contract_decorators=frozenset({"staticmethod", "property"})))
    assert "dec=property+staticmethod" in s


def test_render_roundtrips_through_matches():
    # Every field survives render -> internal parse -> compare against itself.
    a = _iface(
        category="async_func",
        is_generator=True,
        req_positional=1,
        max_positional=4,
        has_star=True,
        has_kw=True,
        req_kwonly=2,
        contract_decorators=frozenset({"classmethod", "property"}),
        base_count=0,
    )
    assert matches(render(a), a) == "match"


# --- matches: identical and additive -> match ---------------------------------


def test_matches_identical_is_match():
    a = _iface(req_positional=2, max_positional=2)
    assert matches(render(a), a) == "match"


def test_matches_added_optional_is_match():
    # A new optional positional (max grows, req unchanged) breaks no call.
    expected = render(_iface(req_positional=2, max_positional=2))
    assert matches(expected, _iface(req_positional=2, max_positional=3)) == "match"


def test_matches_added_star_absorbs_is_match():
    # Replacing fixed params with *args only widens what calls are accepted.
    expected = render(_iface(req_positional=2, max_positional=2))
    current = _iface(req_positional=0, max_positional=0, has_star=True)
    assert matches(expected, current) == "match"


def test_matches_added_kwargs_is_match():
    expected = render(_iface(has_kw=False))
    assert matches(expected, _iface(has_kw=True)) == "match"


def test_matches_base_added_is_match():
    expected = render(_iface(category="class", base_count=1))
    assert matches(expected, _iface(category="class", base_count=2)) == "match"


# --- matches: breaking changes -> mismatch ------------------------------------


def test_matches_required_arity_increase_is_mismatch():
    expected = render(_iface(req_positional=2, max_positional=2))
    assert matches(expected, _iface(req_positional=3, max_positional=3)) == "mismatch"


def test_matches_capacity_drop_below_required_is_mismatch():
    # A formerly-valid 3-arg call now overflows a 2-max signature with no *args.
    expected = render(_iface(req_positional=3, max_positional=3))
    assert matches(expected, _iface(req_positional=2, max_positional=2)) == "mismatch"


def test_matches_removed_kwargs_is_mismatch():
    expected = render(_iface(has_kw=True))
    assert matches(expected, _iface(has_kw=False)) == "mismatch"


def test_matches_sync_to_async_is_mismatch():
    expected = render(_iface(category="func"))
    assert matches(expected, _iface(category="async_func")) == "mismatch"


def test_matches_func_to_class_is_mismatch():
    expected = render(_iface(category="func"))
    assert matches(expected, _iface(category="class")) == "mismatch"


def test_matches_generator_change_is_mismatch():
    expected = render(_iface(is_generator=False))
    assert matches(expected, _iface(is_generator=True)) == "mismatch"


def test_matches_decorator_toggle_is_mismatch():
    expected = render(_iface(contract_decorators=frozenset()))
    current = _iface(contract_decorators=frozenset({"property"}))
    assert matches(expected, current) == "mismatch"


def test_matches_base_removed_is_mismatch():
    expected = render(_iface(category="class", base_count=2))
    assert matches(expected, _iface(category="class", base_count=1)) == "mismatch"


# --- matches: version handling -> incomparable (never mismatch) ---------------


def test_matches_future_version_is_incomparable():
    # An unknown (future) version must route to unverifiable, never stale, even
    # against a wildly different current interface.
    token = "combdrift-fp/2:func(req=0,max=0,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
    assert matches(token, _iface(req_positional=99)) == "incomparable"


def test_matches_malformed_is_incomparable():
    assert matches("not-a-fingerprint", _iface()) == "incomparable"
    assert matches("", _iface()) == "incomparable"


def test_matches_old_version_still_compares():
    # The established v1 format stays parseable and comparable; a later version
    # being added must not blind the existing corpus minted under v1.
    expected = "combdrift-fp/1:func(req=1,max=1,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
    assert matches(expected, _iface(req_positional=1, max_positional=1)) == "match"
    assert matches(expected, _iface(req_positional=2, max_positional=2)) == "mismatch"
