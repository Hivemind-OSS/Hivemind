"""Resolve a single anchor to a location in a repo, safely and read-only.

This module owns filesystem and path-containment knowledge; all language and
parsing knowledge lives in `combdrift.symbols`. A target file is read read-only and
handed to the symbol seam as text — never imported or executed — so the verifier
is safe against untrusted repos and against module-top-level side effects.
"""

from __future__ import annotations

import os
from typing import cast

from hive.combdrift import fingerprint, symbols
from hive.combdrift.symbols import SymbolLookup
from hive.combdrift.types import (
    REASON_FILE_MISSING,
    REASON_FINGERPRINT_VERSION_MISMATCH,
    REASON_NO_SYMBOL_REQUESTED,
    REASON_OK,
    REASON_PARSE_ERROR,
    REASON_PATH_OUTSIDE_REPO,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_INDIRECT,
    REASON_SYMBOL_MISSING,
    REASON_UNSUPPORTED_LANGUAGE,
    Anchor,
    AnchorResult,
    FingerprintDelta,
)


def _within_repo(repo_root: str, rel_path: str) -> str | None:
    """Return the absolute path of rel_path iff it is contained in repo_root.

    Containment is decided after resolving symlinks and `..` on both sides via
    realpath + commonpath, so traversal and symlink escapes are rejected.
    Returns None when the path escapes the repository.
    """
    root_abs = os.path.realpath(repo_root)
    candidate_abs = os.path.realpath(os.path.join(root_abs, rel_path))
    try:
        common = os.path.commonpath([root_abs, candidate_abs])
    except ValueError:
        # Different drives / mixed absolute-relative on some platforms.
        return None
    if common != root_abs:
        return None
    return candidate_abs


def resolve_anchor(repo_root: str, anchor: Anchor) -> AnchorResult:
    """Resolve one anchor against repo_root, reading the target as data only.

    Total function — never raises on expected conditions. Returns an AnchorResult
    whose `found` is True iff the anchor path is inside repo_root, the file exists,
    parses, and (if a symbol is given) the symbol resolves in that file's language.
    """
    abs_path = _within_repo(repo_root, anchor.path)
    if abs_path is None:
        return AnchorResult(
            path=anchor.path,
            symbol=anchor.symbol,
            found=False,
            location=None,
            reason=f"{REASON_PATH_OUTSIDE_REPO}: {anchor.path}",
        )

    if not os.path.isfile(abs_path):
        return AnchorResult(
            path=anchor.path,
            symbol=anchor.symbol,
            found=False,
            location=None,
            reason=f"{REASON_FILE_MISSING}: {anchor.path}",
        )

    with open(abs_path, "r", encoding="utf-8") as handle:
        source = handle.read()

    lookup = symbols.locate(source, anchor.path, anchor.symbol)
    lookup, source_path = _follow_reexport(repo_root, anchor.path, lookup)
    return _to_anchor_result(anchor, lookup, source_path)


def _first_existing_candidate(
    repo_root: str, candidates: tuple[str, ...]
) -> tuple[str, str] | None:
    """First candidate that is in-repo and a real file: (rel_path, abs_path).

    Containment is checked per candidate, so a candidate that escapes the repo
    is skipped — a re-export can never be followed out of the repository.
    """
    for rel in candidates:
        abs_path = _within_repo(repo_root, rel)
        if abs_path is not None and os.path.isfile(abs_path):
            return rel, abs_path
    return None


def _follow_reexport(
    repo_root: str, importer_path: str, lookup: SymbolLookup
) -> tuple[SymbolLookup, str]:
    """Follow ONE named relative re-export edge to its source declaration.

    If `lookup` carries a followable edge, resolve it to a single in-repo target
    and re-locate the original name there; otherwise return `lookup` and
    `importer_path` unchanged. Returns (possibly-upgraded lookup, path the
    location should reference). Total — never raises; read-only.

    indirect upgrades to found/missing only on a clean landing:
      - first in-repo module candidate that is a readable file is the target;
      - target 'found'   -> (target lookup, target path)   [interface flows on];
      - target 'missing' -> a submodule of the package -> stay indirect;
                            else provable absence -> (target lookup, target path);
      - target 'indirect' (2nd hop) / 'parse_error' / 'unsupported' -> stay indirect;
      - no candidate resolves / unreadable -> stay indirect.
    """
    edge = lookup.reexport
    if edge is None:
        return lookup, importer_path
    target = _first_existing_candidate(repo_root, edge.module_candidates)
    if target is None:
        return lookup, importer_path
    target_rel, target_abs = target
    try:
        with open(target_abs, "r", encoding="utf-8") as handle:
            source = handle.read()
    except (OSError, UnicodeDecodeError):
        return lookup, importer_path
    target_lookup = symbols.locate(source, target_rel, edge.name)
    if target_lookup.status == "found":
        return target_lookup, target_rel
    if target_lookup.status == "missing":
        if _first_existing_candidate(repo_root, edge.submodule_candidates) is not None:
            # The name is a submodule file of the package, not a deleted symbol.
            return lookup, importer_path
        return target_lookup, target_rel  # provable absence at the source -> stale
    # A second hop, an unparseable target, or an unsupported language is
    # uncertain: keep the original indirect, never stale.
    return lookup, importer_path


def _to_anchor_result(
    anchor: Anchor, lookup: SymbolLookup, source_path: str
) -> AnchorResult:
    """Map a structural SymbolLookup onto the reason-coded AnchorResult.

    `source_path` is the importer's own path, or — when a re-export was
    followed — the resolved source file, so `location` and a followed
    `symbol_missing` both point at the real declaration site.
    """
    if lookup.status == "unsupported":
        return AnchorResult(
            path=anchor.path,
            symbol=anchor.symbol,
            found=False,
            location=None,
            reason=f"{REASON_UNSUPPORTED_LANGUAGE}: {anchor.path}",
        )

    if lookup.status == "parse_error":
        return AnchorResult(
            path=anchor.path,
            symbol=anchor.symbol,
            found=False,
            location=None,
            reason=f"{REASON_PARSE_ERROR}: {lookup.detail or 'syntax error'}",
        )

    if lookup.status == "missing":
        # A followed re-export names its resolved source so the deletion is
        # locatable (a missing result otherwise carries no location).
        detail = anchor.symbol
        if source_path != anchor.path:
            detail = f"{anchor.symbol} (via {source_path})"
        return AnchorResult(
            path=anchor.path,
            symbol=anchor.symbol,
            found=False,
            location=None,
            reason=f"{REASON_SYMBOL_MISSING}: {detail}",
        )

    if lookup.status == "indirect":
        # Present but not a resolvable callable: absence is not provable, so this
        # is unverifiable, never stale. The detail names the indirection.
        return AnchorResult(
            path=anchor.path,
            symbol=anchor.symbol,
            found=False,
            location=None,
            reason=f"{REASON_SYMBOL_INDIRECT}: {lookup.detail}",
        )

    # status == "found"
    if anchor.symbol is None:
        return AnchorResult(
            path=anchor.path,
            symbol=None,
            found=True,
            location=f"{anchor.path}:1",
            reason=REASON_NO_SYMBOL_REQUESTED,
        )
    location = f"{source_path}:{lookup.lineno}"
    if anchor.fingerprint is None:
        return AnchorResult(
            path=anchor.path,
            symbol=anchor.symbol,
            found=True,
            location=location,
            reason=REASON_OK,
        )
    return _compare_fingerprint(anchor, lookup, location)


def _compare_fingerprint(
    anchor: Anchor, lookup: SymbolLookup, location: str
) -> AnchorResult:
    """Layer B: classify a found symbol against its recorded fingerprint.

    `found` stays True and `location` stays populated in every branch — the
    symbol exists; only its call shape is in question. Drift is the lone stale
    trigger; ambiguity or an unreadable version is unverifiable, never stale.
    """
    if lookup.interface is None:
        # Overloaded / ambiguous shape: no single interface to compare against.
        return AnchorResult(
            path=anchor.path,
            symbol=anchor.symbol,
            found=True,
            location=location,
            reason=f"{REASON_FINGERPRINT_VERSION_MISMATCH}: ambiguous_overload",
        )
    result = fingerprint.matches(cast(str, anchor.fingerprint), lookup.interface)
    if result == "mismatch":
        current = fingerprint.render(lookup.interface)
        return AnchorResult(
            path=anchor.path,
            symbol=anchor.symbol,
            found=True,
            location=location,
            reason=f"{REASON_SIGNATURE_CHANGED}: {anchor.fingerprint} -> {current}",
            delta=FingerprintDelta(
                old_token=anchor.fingerprint, new_token=current, breaking=True
            ),
        )
    if result == "incomparable":
        return AnchorResult(
            path=anchor.path,
            symbol=anchor.symbol,
            found=True,
            location=location,
            reason=REASON_FINGERPRINT_VERSION_MISMATCH,
        )
    # match: identical or purely additive — the symbol's call shape is intact, but
    # surface the (non-breaking) drift so a consumer sees the old/new tokens too.
    current = fingerprint.render(lookup.interface)
    return AnchorResult(
        path=anchor.path,
        symbol=anchor.symbol,
        found=True,
        location=location,
        reason=REASON_OK,
        delta=FingerprintDelta(
            old_token=anchor.fingerprint, new_token=current, breaking=False
        ),
    )


def fingerprint_anchor(repo_root: str, anchor: Anchor) -> str | None:
    """Capture seam: mint the interface fingerprint for an anchor, read-only.

    Returns the opaque token iff the anchor resolves to a single callable in the
    current working tree; returns None when there is no symbol, the symbol is not
    a single resolvable callable (absent, indirect, or overloaded), or the file
    is missing, unparseable, unsupported, or outside the repo. Total — never
    raises. The consumer MUST call this synchronously at capture, against the
    same working tree the agent saw, for the 0-false-stale guarantee to hold.
    """
    if anchor.symbol is None:
        return None
    abs_path = _within_repo(repo_root, anchor.path)
    if abs_path is None or not os.path.isfile(abs_path):
        return None
    try:
        with open(abs_path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    lookup = symbols.locate(source, anchor.path, anchor.symbol)
    # Follow the same hop verify does, so the baseline describes the SOURCE
    # declaration and capture/verify can never disagree on the compared shape.
    lookup, _source_path = _follow_reexport(repo_root, anchor.path, lookup)
    if lookup.status == "found" and lookup.interface is not None:
        return fingerprint.render(lookup.interface)
    return None
