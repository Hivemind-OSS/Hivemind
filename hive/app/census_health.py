"""Passive census-feed health for ``hive_health(include_census_health=true)``.

Two questions, one flag: (1) how many days since the last SHA-bound ``change_outcome``
evidence row landed — the cheap window into "has the census feed gone dark"; (2) when the
server-side sync daemon has run against this store (any ``sync:*`` meta key present), its
observable state — the durable watermark, the last surfaced fault, and the counters — so an
operator reads sync health from the SAME flag with no new CLI verb (U1 intent 12). With sync
configured, darkness is REINTERPRETED: census is server-automatic, so a dark feed no longer
suggests unwired hooks — the block carries ``status: "sync stalled"``.

App-side, read-only, direct SQL over ``store.conn`` — like ``trends.py``/``gaps.py``.
Unlike those, this is a SELF-CONTAINED report with its OWN fail-open guards rather than a
pure core plus a wrapper method in ``mcp_server``: single aggregate reads with no multi-step
pure computation worth unit-testing apart from their I/O, so a ``_census_health_report``
wrapper would be a near-pass-through. The error is defined out of existence for the caller
(it NEVER raises — Law 6, side-channels fail open), and each leg guards itself: a meta fault
(e.g. a store predating the ``meta`` table) can never break the day-count, nor vice versa.

It serves the raw day-count with no invented "too stale" threshold (THEORY §9 #14 — no magic
number an operator could mis-set), and "dark" stays threshold-free the same way: dark == no
``change_outcome`` evidence at all. The ``sync`` block is PRESENT only when the store carries
sync evidence (the key-present-only-when-true idiom) — a non-sync deployment's payload keeps
today's exact shape. ``tracked_ref`` and ``last_sync_ts`` are served as None: the daemon
persists no meta key for either and this module reads ONLY the store (live config never
reaches a bare conn) — honest absence over invented data (Law 6).
"""
from __future__ import annotations

import time

from hive.app.sync import (META_BACKFILLED_TOTAL, META_CANDIDATES_EVALUATED,
                           META_LAST_ERROR, META_LAST_TIP)
from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME

_DAY_S = 86_400


def _counter(raw) -> int:
    """A ``sync:*`` string-int counter, with exactly ``_bump_counter_locked``'s tolerance:
    absent or non-digit reads 0 — a count is never invented from junk."""
    return int(raw) if raw and str(raw).isdigit() else 0


def census_health_report(conn) -> dict:
    """``{"days_since_last_change_outcome": <int> | None}`` plus a ``sync`` block iff the
    sync daemon has left ``sync:*`` meta in this store. None / an absent block on an empty
    feed OR any fault (fail-open — Law 6, this is a side-channel and never breaks
    ``hive_health``)."""
    report: dict = {"days_since_last_change_outcome": None}
    try:
        row = conn.execute(
            "SELECT MAX(ts) FROM evidence_events WHERE kind = ?",
            (EK_CHANGE_OUTCOME,)).fetchone()
        last_ts = row[0] if row else None
        if last_ts is not None:
            report["days_since_last_change_outcome"] = (
                (int(time.time()) - int(last_ts)) // _DAY_S)
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'sync:%'").fetchall()
        sync_meta = {str(key): str(value) for key, value in rows}
        if sync_meta:
            block: dict = {
                "configured": True,
                # tracked_ref / last_sync_ts: NO sync:* meta key backs either (the daemon
                # persists neither; the resolved branch and tick time live only in the
                # running service) — served as None, never invented.
                "tracked_ref": None,
                "last_tip": sync_meta.get(META_LAST_TIP),
                "last_sync_ts": None,
                "last_error": sync_meta.get(META_LAST_ERROR),
                "candidates_evaluated": _counter(sync_meta.get(META_CANDIDATES_EVALUATED)),
                "backfilled_total": _counter(sync_meta.get(META_BACKFILLED_TOTAL)),
            }
            if report["days_since_last_change_outcome"] is None:
                # marker: an unconditional attach reds
                # test_sync_configured_and_live_carries_no_stalled_status.
                block["status"] = "sync stalled"
            report["sync"] = block
    except Exception:
        pass
    return report
