"""Passive census-feed health for ``hive_health(include_census_health=true)`` — v3
PER-REPO blocks, one per registry row, keyed by repo name (``{}`` on an empty registry).

Per repo, two questions: (1) how many days since the last SHA-bound ``change_outcome``
evidence row landed FOR THIS REPO — the cheap window into "has this repo's census feed
gone dark" (attribution reads the canonical payload's ``repo`` key; a legacy repo-less
row attributes to nobody — honest under-claim); (2) when the sync daemon has left
``sync:<name>:*`` meta for the repo, its observable state — the durable watermark, the
last surfaced fault, and the counters. With sync configured for a repo, darkness is
REINTERPRETED: census is server-automatic, so a dark feed reads ``status: "sync
stalled"`` (present-only-when-dark).

App-side, read-only, direct SQL over ``store.conn`` — like ``trends.py``/``gaps.py``.
``store`` is duck-typed: ``repo_registry()`` (the block keys) + ``conn``. The per-repo
meta key scheme is ``sync:<name>:<field>`` (the same family as the drift watermark
``sync:<name>:last_tip`` — ``hive.app.drift.canonical_tip_key``); the legacy 2-part
global keys (``sync:last_tip``) belong to no repo and are ignored. Keys are read raw
from the store, never from live config, so a field serves None exactly when its meta is
genuinely absent — honest absence over invented data (Law 6).

It serves the raw day-count with no invented "too stale" threshold (THEORY §9 #14 — no
magic number an operator could mis-set), and "dark" stays threshold-free the same way:
dark == no attributable ``change_outcome`` evidence at all. The error is defined out of
existence for the caller (NEVER raises — Law 6, side-channels fail open) and each leg
guards itself: an evidence fault can never break the sync leg, nor vice versa; a
registry fault degrades to ``{}``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME

_DAY_S = 86_400

_SYNC_PREFIX = "sync:"

# The per-repo sync fields served verbatim (string-or-None) and as tolerant counters.
_SYNC_STR_FIELDS = ("tracked_ref", "last_tip", "last_sync_ts", "last_error")
_SYNC_COUNTER_FIELDS = ("candidates_evaluated", "backfilled_total")


def _counter(raw: str | None) -> int:
    """A ``sync:*`` string-int counter, with the daemon's own bump tolerance: absent or
    non-digit reads 0 — a count is never invented from junk."""
    return int(raw) if raw and str(raw).isdigit() else 0


def _last_outcome_ts_by_repo(conn: sqlite3.Connection) -> dict[str, int]:
    """MAX(ts) of ``change_outcome`` evidence per attributed repo (the canonical
    payload's ``repo`` key). Malformed / repo-less payloads attribute to nobody
    (under-claim). Fail-open: any fault reads as an empty map."""
    out: dict[str, int] = {}
    try:
        for row in conn.execute(
            "SELECT ts, payload FROM evidence_events WHERE kind = ?",
            (EK_CHANGE_OUTCOME,),
        ):
            try:
                doc = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            repo = doc.get("repo") if isinstance(doc, dict) else None
            if not isinstance(repo, str) or not repo:
                continue
            ts = int(row["ts"])
            if repo not in out or ts > out[repo]:
                out[repo] = ts
    except Exception:
        return {}
    return out


def _sync_meta_by_repo(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """Every per-repo ``sync:<name>:<field>`` meta row, grouped by repo name. The
    legacy 2-part global keys carry no repo name and are skipped. Fail-open: any
    fault (e.g. a store predating the ``meta`` table) reads as an empty map."""
    out: dict[str, dict[str, str]] = {}
    try:
        for key, value in conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'sync:%'"
        ):
            body = str(key)[len(_SYNC_PREFIX) :]
            repo, sep, field = body.partition(":")
            if not sep or not repo or not field:
                continue  # legacy global key — belongs to no repo
            out.setdefault(repo, {})[field] = str(value)
    except Exception:
        return {}
    return out


def census_health_report(store: Any) -> dict[str, Any]:
    """``{repo_name: block}`` for every REGISTERED repo (``{}`` on an empty registry
    or any registry fault). Each block: ``{"days_since_last_change_outcome": int |
    None}`` plus a ``sync`` sub-block iff the daemon has left ``sync:<name>:*`` meta
    for that repo (the key-present-only-when-true idiom). NEVER raises (fail-open —
    Law 6, this is a side-channel and never breaks ``hive_health``)."""
    try:
        names = [row.name for row in store.repo_registry()]
    except Exception:
        return {}
    if not names:
        return {}
    conn = store.conn
    last_ts = _last_outcome_ts_by_repo(conn)
    sync_meta = _sync_meta_by_repo(conn)
    now = int(time.time())
    report: dict[str, Any] = {}
    for name in names:
        block: dict[str, Any] = {"days_since_last_change_outcome": None}
        ts = last_ts.get(name)
        if ts is not None:
            block["days_since_last_change_outcome"] = (now - int(ts)) // _DAY_S
        meta = sync_meta.get(name)
        if meta:
            sync_block: dict[str, Any] = {"configured": True}
            for field in _SYNC_STR_FIELDS:
                sync_block[field] = meta.get(field)
            for field in _SYNC_COUNTER_FIELDS:
                sync_block[field] = _counter(meta.get(field))
            if block["days_since_last_change_outcome"] is None:
                # marker: an unconditional attach reds
                # test_sync_configured_and_live_carries_no_stalled_status.
                sync_block["status"] = "sync stalled"
            block["sync"] = sync_block
        report[name] = block
    return report
