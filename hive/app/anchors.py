"""``normalize_anchors`` / ``normalize_repos`` — the ONE boundary grammar gate for
the two repo-scope carriers an agent can send (the ``normalize_meta`` idiom).

Two vocabularies arrive at the write/recall boundary:

- ``anchors`` — an array of ``{"repo": name, "anchor": location}`` objects (the
  ``AnchorRef`` carrier shape): a code binding of a memory. ``repo`` must be a
  REGISTERED repo name; ``anchor`` is free text (``path`` or ``path::Symbol``).
- ``repos`` — an array of ``"name"`` / ``"name@branch"`` strings: repo-scope
  membership (write side) or the recall partition filter (read side). ``name``
  must be a REGISTERED repo name; ``branch`` is free text.

Each violation raises ``BadAnchors`` naming the violated clause — loud refusal,
never a silent drop; the MCP handler maps it to the clean ``status="refused"``
envelope (nothing stored, not a tool error). The absent forms ``None`` / ``""`` /
``[]`` normalize to ``()`` — no scope (a general memory / the global recall
partition).

Like ``normalize_meta`` these functions are TOTAL over ``object`` — isinstance-
driven, never indexing a non-dict (a hostile shape refuses, it cannot crash the
boundary) — and pure, with ONE injected read: whether a repo name is registered
arrives as ``known_repos`` (a ``name -> bool`` callable or a read-only collection
of names, e.g. ``{r.name for r in store.repo_registry()}``) — the gate never
imports the store.
"""

from __future__ import annotations

from collections.abc import Callable, Container

from hive.domain.models import AnchorRef


class BadAnchors(ValueError):
    """The loud grammar refusal — the message names the violated clause."""


# Bounded boundary (the META_MAX_KEYS analog): more bindings/scope entries than
# this in one call is a hostile or broken payload, not a real memory.
SCOPE_MAX_ENTRIES = 32

_ANCHOR_KEYS = frozenset({"repo", "anchor"})


def split_scope(entry: str) -> tuple[str, str]:
    """Lexical ``"name@branch"`` split → ``(name, branch)``; no ``@`` → ``(entry, "")``.

    Splits on the FIRST ``@`` only (a branch name may itself carry ``@``). Pure
    and lossy by design — validation/refusal belongs to the ``normalize_*`` gates;
    consumers (``hive.app.drift`` ref routing) treat an empty branch as canonical."""
    name, _sep, branch = entry.partition("@")
    return name, branch


def _known_fn(
    known_repos: Callable[[str], bool] | Container[str],
) -> Callable[[str], bool]:
    """The injected registry-membership read, normalized to a predicate."""
    if callable(known_repos):
        return known_repos
    return lambda name: name in known_repos


def normalize_anchors(
    raw: object, *, known_repos: Callable[[str], bool] | Container[str]
) -> tuple[AnchorRef, ...]:
    """Shape ``raw`` into the canonical ``AnchorRef`` tuple, or raise ``BadAnchors``.

    ``None`` / ``""`` / ``[]`` → ``()`` (absent — a general or scope-only memory).
    A conforming array of exactly-``{repo, anchor}`` objects → ``AnchorRef`` per
    entry, input order kept, ``fp_meta`` empty (the SERVER mints fingerprints —
    an agent can never send one). Anything else refuses. // O(entries)."""
    if raw is None or raw == "" or (isinstance(raw, (list, tuple)) and not raw):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise BadAnchors(
            "anchors must be an array of {repo, anchor} objects, got "
            f"{type(raw).__name__}"
        )
    if len(raw) > SCOPE_MAX_ENTRIES:
        raise BadAnchors(f"anchors has {len(raw)} entries (max {SCOPE_MAX_ENTRIES})")
    known = _known_fn(known_repos)
    out: list[AnchorRef] = []
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BadAnchors(
                f"anchors[{i}] must be a {{repo, anchor}} object, got "
                f"{type(item).__name__}"
            )
        extra = set(item) - _ANCHOR_KEYS
        if extra:
            raise BadAnchors(
                f"anchors[{i}] has unknown key(s) {sorted(map(repr, extra))} "
                "(want exactly repo, anchor)"
            )
        repo = item.get("repo")
        anchor = item.get("anchor")
        if not isinstance(repo, str) or not repo:
            raise BadAnchors(
                f"anchors[{i}].repo must be a non-empty registered repo name"
            )
        if not isinstance(anchor, str) or not anchor:
            raise BadAnchors(
                f"anchors[{i}].anchor must be a non-empty code location "
                "(path or path::Symbol)"
            )
        if not known(repo):
            raise BadAnchors(
                f"unknown repo {repo!r} in anchors[{i}] — not in the registry"
            )
        if (repo, anchor) in seen:
            raise BadAnchors(
                f"duplicate anchor binding ({repo!r}, {anchor!r}) at anchors[{i}]"
            )
        seen.add((repo, anchor))
        out.append(AnchorRef(repo=repo, anchor=anchor))
    return tuple(out)


def normalize_repos(
    raw: object, *, known_repos: Callable[[str], bool] | Container[str]
) -> tuple[tuple[str, str], ...]:
    """Shape ``raw`` into canonical ``(name, branch)`` pairs, or raise ``BadAnchors``.

    ``None`` / ``""`` / ``[]`` → ``()`` (absent — the global match-everything
    scope on recall, no repo membership on write). A conforming array of
    ``"name"`` / ``"name@branch"`` strings → one ``(name, branch)`` pair per
    entry, input order kept, ``branch == ""`` when unqualified (canonical).
    Anything else refuses. // O(entries)."""
    if raw is None or raw == "" or (isinstance(raw, (list, tuple)) and not raw):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise BadAnchors(
            "repos must be an array of 'name' / 'name@branch' strings, got "
            f"{type(raw).__name__}"
        )
    if len(raw) > SCOPE_MAX_ENTRIES:
        raise BadAnchors(f"repos has {len(raw)} entries (max {SCOPE_MAX_ENTRIES})")
    known = _known_fn(known_repos)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, str) or not entry:
            raise BadAnchors(
                f"repos[{i}] must be a non-empty 'name' / 'name@branch' string"
            )
        name, branch = split_scope(entry)
        if not name:
            raise BadAnchors(f"repos[{i}] {entry!r} has an empty repo name")
        if "@" in entry and not branch:
            raise BadAnchors(f"repos[{i}] {entry!r} has an empty branch after '@'")
        if not known(name):
            raise BadAnchors(
                f"unknown repo {name!r} in repos[{i}] — not in the registry"
            )
        if name in seen:
            raise BadAnchors(
                f"repo {name!r} named twice in repos (one scope entry per repo)"
            )
        seen.add(name)
        out.append((name, branch))
    return tuple(out)
