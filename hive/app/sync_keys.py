"""The ONE per-repo sync meta-key grammar — the single definition shared by the daemon
that WRITES the keys (``hive.app.sync``) and the health reader that SERVES them
(``hive.app.census_health``).

It exists because the two sides drifted apart once and nothing caught it: the reader
carried its own string literals for the field names, so when the per-repo rewrite
deleted a writer the reader kept advertising the field — silently, forever, because a
string literal cannot break an import the way the shared constant it replaced would
have. Four of six served fields were structurally empty on a healthy feed, and an
operator runbook keyed off one of them reported a working connection as broken.

The coupling is therefore mechanical again, in both directions:

- every served field names its writer through ``KEY_BUILDERS`` — a field with no
  builder cannot be advertised at all;
- ``tests/app/test_sync_keys.py`` asserts STATICALLY (over ``hive.app.sync``'s AST)
  that the daemon still calls every builder in that map, so deleting a writer without
  dropping its field goes red at the seam rather than on a live server.

The key shape is ``sync:<repo>:<field>``. The 2-part ``sync:<field>`` globals are a
DIFFERENT namespace — the tick SHELL's own fleet-wide surface (no repo in scope), owned
by ``hive.app.sync`` and never served in a per-repo block.

Pure string construction: no I/O, no store, no config.
"""

from __future__ import annotations

from typing import Callable

_PREFIX = "sync:"


def _repo_key(repo: str, field: str) -> str:
    return f"{_PREFIX}{repo}:{field}"


def canonical_tip_key(repo: str) -> str:
    """The sync watermark — ``repo``'s canonical-ref tip SHA. The durable truth the
    drift materializer verifies against (store meta = truth, D4)."""
    return _repo_key(repo, "last_tip")


def last_error_key(repo: str) -> str:
    """``repo``'s last surfaced per-leg fault: redacted, sticky, advisory."""
    return _repo_key(repo, "last_error")


def tracked_ref_key(repo: str) -> str:
    """The branch the daemon RESOLVED for ``repo`` — the registry row's
    ``canonical_ref`` when set, else origin's default head. The resolved line is the
    operator-visible truth; the registry alone cannot answer it, since the common
    "track whatever the default branch is" registration stores no ref at all."""
    return _repo_key(repo, "tracked_ref")


def last_sync_ts_key(repo: str) -> str:
    """ts of ``repo``'s last fault-free tick — ITS OWN legs only. Per-repo on purpose:
    the fleet-wide "every repo ran clean" stamp is the tick shell's separate global
    key, and under it one faulted repo hides every healthy repo's freshness."""
    return _repo_key(repo, "last_sync_ts")


def backfilled_total_key(repo: str) -> str:
    """``repo``'s anchor carriers fingerprint-backfilled, ever — the positive proof
    the mint path works end to end (mirror cloned, anchor resolved against the real
    tree, ``hive-edge mint`` spawned and parsed)."""
    return _repo_key(repo, "backfilled_total")


#: Served field name → the builder for its key. This map IS the served field set: a
#: field absent here cannot be advertised, and a builder here that no writer in
#: ``hive.app.sync`` calls is caught statically by the seam test.
KEY_BUILDERS: dict[str, Callable[[str], str]] = {
    "tracked_ref": tracked_ref_key,
    "last_tip": canonical_tip_key,
    "last_sync_ts": last_sync_ts_key,
    "last_error": last_error_key,
    "backfilled_total": backfilled_total_key,
}

#: How each served field is typed on the wire: verbatim string-or-None…
STR_FIELDS = ("tracked_ref", "last_tip", "last_sync_ts", "last_error")
#: …vs parsed as a count (still None when there is no readable count to report).
COUNTER_FIELDS = ("backfilled_total",)
