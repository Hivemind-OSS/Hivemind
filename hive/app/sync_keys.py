"""The ONE sync meta-key grammar — the single definition shared by the daemon that
WRITES the keys (``hive.app.sync``) and the health reader that SERVES them
(``hive.app.census_health``). TWO namespaces, one law:

- ``sync:<repo>:<field>`` — the PER-REPO surface, one set of fields per registry row;
- ``sync:<field>`` (2-part) — the tick SHELL's FLEET surface: the state of the daemon
  itself, which belongs to no repo. It cannot be smuggled into the repo-keyed map as a
  reserved sentinel, because the registry slug gate is ``[a-z0-9._-]+`` — ``_fleet``,
  ``daemon`` and ``sync`` are all legal repo names an operator could register — so the
  two namespaces get two separate homes in the served report as well.

It exists because the two sides drifted apart once and nothing caught it: the reader
carried its own string literals for the field names, so when the per-repo rewrite
deleted a writer the reader kept advertising the field — silently, forever, because a
string literal cannot break an import the way the shared constant it replaced would
have. Four of six served fields were structurally empty on a healthy feed, and an
operator runbook keyed off one of them reported a working connection as broken.

The coupling is therefore mechanical again, in both directions and on BOTH surfaces:

- every served field names its writer through ``KEY_BUILDERS`` / ``FLEET_KEY_BUILDERS``
  — a field with no builder cannot be advertised at all;
- ``tests/app/test_sync_keys.py`` asserts STATICALLY (over ``hive.app.sync``'s AST)
  that the daemon still calls every builder in those maps, so deleting a writer without
  dropping its field goes red at the seam rather than on a live server.

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
    """``repo``'s most recent completed tick's fault: redacted, advisory, CURRENT
    — re-stamped by every faulted tick, deleted by this repo's next fault-free
    tick, so non-null ⇔ the last completed tick of THIS repo faulted (BUG-099).
    Fault history lives in the ``sync.repo_leg_failed`` log lines, not here."""
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
    """``repo``'s anchor bindings the daemon has BASELINED, ever — the positive proof
    the staleness path works end to end (mirror cloned, watermark advanced, a binding
    that reached the server without one given the commit it is measured from). The
    field name predates the git-native ladder and is kept because it is a SERVED wire
    field: renaming it would break an operator runbook to gain nothing."""
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


# ── the tick SHELL's fleet surface: 2-part keys, no repo in scope ──────────────
def _fleet_key(field: str) -> str:
    return f"{_PREFIX}{field}"


def fleet_last_error_key() -> str:
    """The tick SHELL's most recent fault: the registry read failing, anything
    escaping a whole tick, or a mirror prune for an already-DEREGISTERED name (leg
    label ``prune[<name>]``). Redacted, advisory, CURRENT — the fleet twin of
    ``last_error_key``: re-stamped while the shell fault persists, deleted by the
    next tick whose SHELL ran fault-free (independent of per-repo results), so
    non-null ⇔ the daemon's own last completed tick faulted (BUG-099).

    It belongs to no repo BY CONSTRUCTION: on a registry fault the tick returns before
    any repo is even known, so no per-repo key is written OR cleared — the clear too
    sits after that return, which is why a dead daemon's diagnosis stands until the
    daemon itself completes a clean shell. Without a fleet home for this fault a fully
    dead daemon reads as N passing repos (BUG-062)."""
    return _fleet_key("last_error")


def fleet_last_sync_ts_key() -> str:
    """ts of the last tick in which the mirror prune AND every registered repo ran
    fault-free — "the whole fleet synced". An empty-registry tick is inert and stamps
    nothing, so a server with no registered repo serves this null forever — honestly:
    nothing has ever synced."""
    return _fleet_key("last_sync_ts")


#: Fleet field name → the builder for its key. This map IS the served fleet field set,
#: exactly as ``KEY_BUILDERS`` is the per-repo one, and carries the same guarantee: a
#: field here whose builder no writer in ``hive.app.sync`` calls is caught statically.
FLEET_KEY_BUILDERS: dict[str, Callable[[], str]] = {
    "last_sync_ts": fleet_last_sync_ts_key,
    "last_error": fleet_last_error_key,
}

#: Every fleet field is a verbatim string-or-None on the wire (no counters here yet).
FLEET_STR_FIELDS = ("last_sync_ts", "last_error")
