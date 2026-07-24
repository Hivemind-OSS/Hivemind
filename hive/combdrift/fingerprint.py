"""The single owner of the interface-fingerprint format: render + compare.

This module is pure — no parsing of source, no filesystem, no language knowledge.
`combdrift.symbols` extracts a language-neutral `Interface` descriptor; this module
turns it into an opaque, version-tagged string (`render`) and decides whether a
recorded string is still satisfied by a freshly-extracted interface (`matches`).
The string format, its version tag, and the round-trip parse live here and
nowhere else, so no other module ever inspects the token's shape.

`matches` is a *directional* compare: it returns "mismatch" only when a call that
was valid under the recorded interface would be invalid under the current one.
Additive changes (a new optional parameter, a new `*args`/`**kwargs`, a new base)
break no previously-valid call and return "match". A recorded token whose version
this module cannot parse returns "incomparable" — a future format never reads as
a breakage, and every historical version stays parseable so an upgrade never
blinds the existing corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Interface:
    """Language-neutral call-shape descriptor extracted from a declaration."""

    category: str  # "func" | "async_func" | "class"
    is_generator: bool
    req_positional: int  # required positional params (positional total − defaults)
    max_positional: int  # total positional params, excluding *args/rest
    has_star: bool  # *args / rest parameter present
    has_kw: bool  # **kwargs present (Python; always False for JS/TS v1)
    req_kwonly: int  # required keyword-only params (Python)
    contract_decorators: frozenset[str]  # call-convention decorators only
    base_count: int  # base classes / extends+implements targets (0 for funcs)


FINGERPRINT_VERSION = "1"

# Versions this module knows how to parse. The current version plus every prior
# one — adding a new version must never remove an old one, or tokens minted under
# the old format would become incomparable (a recall loss across upgrades).
_KNOWN_VERSIONS = frozenset({"1"})
_CATEGORIES = frozenset({"func", "async_func", "class"})

_PREFIX = "combdrift-fp/"
_MatchResult = Literal["match", "mismatch", "incomparable"]
_DriftResult = Literal["unchanged", "additive", "breaking", "indeterminate"]


def render(iface: Interface) -> str:
    """Mint the canonical, deterministic, version-tagged token for `iface`.

    This is the capture-side product a consumer persists opaquely. Determinism
    follows from fixed field order and sorted set rendering — no clock or RNG.
    """
    decorators = "+".join(sorted(iface.contract_decorators))
    return (
        f"{_PREFIX}{FINGERPRINT_VERSION}:{iface.category}("
        f"req={iface.req_positional},"
        f"max={iface.max_positional},"
        f"star={int(iface.has_star)},"
        f"kw={int(iface.has_kw)},"
        f"kwo={iface.req_kwonly},"
        f"gen={int(iface.is_generator)},"
        f"dec={decorators},"
        f"base={iface.base_count})"
    )


def matches(expected: str, current: Interface) -> _MatchResult:
    """Directional call-compatibility compare of a recorded token vs a fresh interface.

    "incomparable" when `expected`'s version is unparseable (future/unknown) or
    the token is malformed; "mismatch" when a call valid under `expected` would
    be invalid under `current`; "match" otherwise (including purely additive
    changes). Total — never raises.
    """
    parsed = _parse(expected)
    if parsed is None:
        return "incomparable"
    return "mismatch" if _breaks_call(parsed, current) else "match"


def compare(old_token: str, new_token: str) -> _DriftResult:
    """Directional drift between two recorded tokens. Total — never raises.

    The change-path holds two tokens (a base and a head capture); this is the
    token-to-token analogue of `matches` (which takes a token and a fresh
    Interface). Both delegate to the same `_breaks_call`, so the breaking-
    direction rule has exactly one owner and `compare` re-derives nothing.

    indeterminate  — either token is a future/unknown/malformed version (mirrors
                     `matches`'s "incomparable": a version we cannot read is never
                     read as a breakage);
    breaking       — a call valid under `old_token` would be invalid under
                     `new_token` (`_breaks_call` is True);
    additive       — both parse, nothing breaks, but the call shapes differ;
    unchanged      — the parsed interfaces are identical.
    """
    old = _parse(old_token)
    new = _parse(new_token)
    if old is None or new is None:
        return "indeterminate"
    if _breaks_call(old, new):
        return "breaking"
    if old == new:
        return "unchanged"
    return "additive"


def _breaks_call(expected: Interface, current: Interface) -> bool:
    """True iff some call valid under `expected` is invalid under `current`."""
    # Category folds in sync/async; a class/func/async swap changes invocation.
    if expected.category != current.category:
        return True
    if expected.is_generator != current.is_generator:
        return True
    # A higher required-arg floor strands the minimal previously-valid call.
    if current.req_positional > expected.req_positional:
        return True
    # The minimal previously-valid call (expected.req_positional args) now
    # overflows, and there is no *args to absorb the surplus.
    if current.max_positional < expected.req_positional and not current.has_star:
        return True
    # A keyword that **kwargs used to absorb now has nowhere to land.
    if expected.has_kw and not current.has_kw:
        return True
    # Any toggle of a call-convention decorator changes how the symbol is invoked.
    if expected.contract_decorators != current.contract_decorators:
        return True
    # Dropping a base may remove an inherited member a call relied on.
    if current.base_count < expected.base_count:
        return True
    return False


def _parse(token: str) -> Interface | None:
    """Parse a token back into an Interface, version-dispatched.

    Returns None for any version this module cannot parse and for any malformed
    token of a known version — both route to "incomparable", never a mismatch.
    """
    if not token.startswith(_PREFIX):
        return None
    version, sep, body = token[len(_PREFIX) :].partition(":")
    if not sep or version not in _KNOWN_VERSIONS:
        return None
    return _parse_v1(body)


def _parse_v1(body: str) -> Interface | None:
    if not body.endswith(")") or "(" not in body:
        return None
    category, _, rest = body.partition("(")
    if category not in _CATEGORIES:
        return None
    fields: dict[str, str] = {}
    for part in rest[:-1].split(","):
        key, sep, value = part.partition("=")
        if not sep:
            return None
        fields[key] = value
    try:
        return Interface(
            category=category,
            is_generator=fields["gen"] == "1",
            req_positional=int(fields["req"]),
            max_positional=int(fields["max"]),
            has_star=fields["star"] == "1",
            has_kw=fields["kw"] == "1",
            req_kwonly=int(fields["kwo"]),
            contract_decorators=frozenset(d for d in fields["dec"].split("+") if d),
            base_count=int(fields["base"]),
        )
    except (KeyError, ValueError):
        return None
