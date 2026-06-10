"""The mechanical memory-lifecycle core: trust states, the ONE servability
predicate, and lazy TTL decay.

A captured memory is born ``quarantined`` (embedded but structurally unservable);
measured demand promotes it to ``provisional`` (served WITH its label); a human
``hive_write`` lands ``established``; supersession or TTL decay retires rows to
``deprecated``. ``is_servable`` is the single source every serving layer
re-evaluates (store scan predicate, index membership sync, per-hit recall belt);
``decayed`` is the lazy death rule the sweep materializes.

PURE: stdlib only. The purity gate (tests/test_purity.py) forbids
sqlite3 | torch | subprocess | os | git | time imports anywhere in hive/domain/.
"""
from __future__ import annotations

QUARANTINED, PROVISIONAL, ESTABLISHED, DEPRECATED = (
    "quarantined", "provisional", "established", "deprecated")
TRUST_STATES = (QUARANTINED, PROVISIONAL, ESTABLISHED, DEPRECATED)


def is_servable(*, status: str, trust: str, last_active_ts: int,
                now: int, provisional_ttl_s: int) -> bool:
    """THE single servability rule. Used by the store's servable scan (index
    rebuild), the promotion/demotion index sync, and the mcp_server recall belt.

    servable ≡ status='approved' AND (
        trust='established'
        OR (trust='provisional' AND last_active_ts > now − provisional_ttl_s))

    The provisional freshness compare is STRICT — at exactly the TTL boundary the
    row is no longer served (fail-closed). An unknown trust label is NOT servable.
    Pure, total, never raises.  // O(1)."""
    if status != "approved":
        return False
    if trust == ESTABLISHED:
        return True
    if trust == PROVISIONAL:
        return last_active_ts > now - provisional_ttl_s
    return False


def decayed(*, trust: str, last_active_ts: int, created_ts: int, now: int,
            quarantine_ttl_s: int, provisional_ttl_s: int) -> bool:
    """Lazy death rule (the sweep materializes what this reads):

    quarantined  — dead iff ``now − max(created_ts, last_active_ts) > quarantine_ttl``
                   (any touch refreshes the clock).
    provisional  — dead iff ``now − last_active_ts > provisional_ttl`` (exposure
                   refreshes it; promotion stamps it, so a fresh promotion is never
                   instantly dead).
    established / deprecated — never (supersession is established's only retirement).

    Both compares STRICT, mirroring ``is_servable``: at exactly the boundary a
    provisional row is unservable-but-not-yet-swept (a benign one-tick gap in the
    fail-closed direction). Pure, total, never raises.  // O(1)."""
    if trust == QUARANTINED:
        return now - max(created_ts, last_active_ts) > quarantine_ttl_s
    if trust == PROVISIONAL:
        return now - last_active_ts > provisional_ttl_s
    return False
