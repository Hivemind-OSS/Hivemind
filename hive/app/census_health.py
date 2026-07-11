"""Passive census-feed staleness signal for ``hive_health(include_census_health=true)``.

One question: how many days since the last SHA-bound ``change_outcome`` evidence row
landed? That is the only cheap window into "has the post-merge census feed gone dark"
without per-repo CI — the hook is fail-open by design, so a dead feed is otherwise silent.

App-side, read-only, direct SQL over ``store.conn`` — like ``trends.py``/``gaps.py``.
Unlike those, this is a SELF-CONTAINED report with its OWN fail-open try/except rather
than a pure core plus a wrapper method in ``mcp_server``: it is a single aggregate query
with no multi-step pure computation worth unit-testing apart from its I/O, so a
``_census_health_report`` wrapper would be a near-pass-through. The error is defined out of
existence for the caller (it NEVER raises — Law 6, side-channels fail open), so
``_handle_health`` calls it directly. It serves the raw day-count with no invented
"too stale" threshold (THEORY §9 #14 — no magic number an operator could mis-set).
"""
from __future__ import annotations

import time

from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME

_DAY_S = 86_400


def census_health_report(conn) -> dict:
    """``{"days_since_last_change_outcome": <int> | None}``. None on an empty feed OR any
    fault (fail-open — Law 6, this is a side-channel and never breaks ``hive_health``)."""
    try:
        row = conn.execute(
            "SELECT MAX(ts) FROM evidence_events WHERE kind = ?",
            (EK_CHANGE_OUTCOME,)).fetchone()
        last_ts = row[0] if row else None
        if last_ts is None:
            return {"days_since_last_change_outcome": None}
        days = (int(time.time()) - int(last_ts)) // _DAY_S
        return {"days_since_last_change_outcome": days}
    except Exception:
        return {"days_since_last_change_outcome": None}
