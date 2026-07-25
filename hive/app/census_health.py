"""Passive census-feed health for ``hive_health(include_census_health=true)``:
``{"repos": {<name>: block}, "fleet": {…}}`` — the repo-keyed map alongside a slot for
the facts that belong to NO repo.

Two homes because there are two kinds of fact. ``repos`` answers, per registry row: (1)
how many days since the last SHA-bound ``change_outcome`` evidence row landed FOR THIS
REPO — the cheap window into "has this repo's census feed gone dark" (attribution reads
the canonical payload's ``repo`` key; a legacy repo-less row attributes to nobody —
honest under-claim); (2) when the sync daemon has left ``sync:<name>:*`` meta for the
repo, its observable state — the durable watermark, the last surfaced fault, and the
counters. With sync configured for a repo, darkness is REINTERPRETED: census is
server-automatic, so a dark feed reads ``status: "sync stalled"``
(present-only-when-dark).

``fleet`` answers the question the repo-keyed map structurally could not (BUG-062): is
the DAEMON alive? A tick-shell fault — the registry read failing, anything escaping a
whole tick, a mirror prune stuck under a deregistered name — returns before any
per-repo key is written or cleared, so every block keeps its last-healthy values and a
week-dead daemon read as N passing repos while the store held the diagnosis the whole
time. The fault could not live under a reserved key INSIDE the map either: the registry
slug gate admits ``_fleet``/``daemon``/``sync`` as legal repo names, so any sentinel
could collide with a real block. Hence a sibling slot, always present, carrying the
tick shell's 2-part ``sync:<field>`` keys.

App-side, read-only, direct SQL over ``store.conn`` — like ``trends.py``/``gaps.py``.
``store`` is duck-typed: ``repo_registry()`` (the block keys) + ``conn``. Neither served
field set nor either key scheme is restated here: both come from ``hive.app.sync_keys``,
the one grammar the writing daemon also imports, so a field can only be advertised when
a writer exists for it (BUG-059 — the reader's own string literals once outlived four
writers, and nothing broke). Keys are read raw from the store, never from live config,
so a field serves None exactly when its meta is genuinely absent — honest absence over
invented data (Law 6): that applies to the counters too, which serve None rather than a
confident ``0`` when there is no readable count, since ``0`` reads as a measurement.

It serves the raw day-count with no invented "too stale" threshold (THEORY §9 #14 — no
magic number an operator could mis-set), and "dark" stays threshold-free the same way:
dark == no attributable ``change_outcome`` evidence at all. The error is defined out of
existence for the caller (NEVER raises — Law 6, side-channels fail open) and each leg
guards itself: an evidence fault can never break the sync leg, a fleet fault can never
break the per-repo legs nor the reverse, and a registry fault degrades ``repos`` to
``{}`` while ``fleet`` — which is exactly where that fault is recorded — still serves.
"""

from __future__ import annotations

import json
import time
from typing import Any

from hive.app.sync_keys import (
    COUNTER_FIELDS,
    FLEET_KEY_BUILDERS,
    FLEET_STR_FIELDS,
    STR_FIELDS,
)
from hive.domain.evidence_kinds import EK_CHANGE_OUTCOME

_DAY_S = 86_400

_SYNC_PREFIX = "sync:"


def _counter(raw: str | None) -> int | None:
    """A ``sync:*`` string-int counter, or None when there is no readable count —
    absent meta (the daemon has not bumped it yet) and junk (a corrupt row) alike. A
    ``0`` here would read as a measured zero and be indistinguishable from an unwired
    counter, which is precisely how BUG-059 stayed invisible on a live server."""
    return int(raw) if raw and str(raw).isdigit() else None


def _last_outcome_ts_by_repo(conn: Any) -> dict[str, int]:
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


def _sync_meta_by_repo(conn: Any) -> dict[str, dict[str, str]]:
    """Every per-repo ``sync:<name>:<field>`` meta row, grouped by repo name. The
    2-part tick-SHELL keys carry no repo name and are skipped here — they are the
    fleet block's, not any repo's. Fail-open: any fault (e.g. a store predating the
    ``meta`` table) reads as an empty map."""
    out: dict[str, dict[str, str]] = {}
    try:
        for key, value in conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'sync:%'"
        ):
            body = str(key)[len(_SYNC_PREFIX) :]
            repo, sep, field = body.partition(":")
            if not sep or not repo or not field:
                continue  # a 2-part tick-SHELL key — belongs to no repo
            out.setdefault(repo, {})[field] = str(value)
    except Exception:
        return {}
    return out


def _fleet_meta(conn: Any) -> dict[str, str]:
    """The tick SHELL's 2-part ``sync:<field>`` meta rows, keyed by FIELD — read by
    exact key through the grammar's own builders, never by pattern, so a per-repo key
    can never be mistaken for a fleet one. Fail-open: any fault reads as an empty
    map, which serves as honest absence rather than as a healthy daemon."""
    out: dict[str, str] = {}
    try:
        for field, build in FLEET_KEY_BUILDERS.items():
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (build(),)
            ).fetchone()
            if row is not None:
                out[field] = str(row[0])
    except Exception:
        return {}
    return out


def _fleet_block(conn: Any) -> dict[str, Any]:
    """The daemon's OWN state — the facts belonging to no repo. Always present (an
    absent block would itself be an unreadable signal), every field served verbatim or
    None, and thresholdless: "how far behind is `last_sync_ts`" is the operator's
    judgment, not an invented staleness constant (THEORY §9 #14)."""
    meta = _fleet_meta(conn)
    return {field: meta.get(field) for field in FLEET_STR_FIELDS}


def _repo_blocks(store: Any, conn: Any) -> dict[str, Any]:
    """``{repo_name: block}`` for every REGISTERED repo (``{}`` on an empty registry
    or any registry fault). Each block: ``{"days_since_last_change_outcome": int |
    None}`` plus a ``sync`` sub-block iff the daemon has left ``sync:<name>:*`` meta
    for that repo (the key-present-only-when-true idiom)."""
    try:
        names = [row.name for row in store.repo_registry()]
    except Exception:
        return {}
    if not names:
        return {}
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
            for field in STR_FIELDS:
                sync_block[field] = meta.get(field)
            for field in COUNTER_FIELDS:
                sync_block[field] = _counter(meta.get(field))
            if block["days_since_last_change_outcome"] is None:
                # marker: an unconditional attach reds
                # test_sync_configured_and_live_carries_no_stalled_status.
                sync_block["status"] = "sync stalled"
            block["sync"] = sync_block
        report[name] = block
    return report


def census_health_report(store: Any) -> dict[str, Any]:
    """``{"repos": {<name>: block}, "fleet": {…}}`` — the per-repo blocks alongside the
    tick shell's own state. ``repos`` is ``{}`` on an empty registry or a registry
    fault; ``fleet`` is always present, and on a registry fault it is the slot carrying
    the very fault that emptied ``repos``.

    The two legs are INDEPENDENT by construction (Law 6, and the reason a fleet fact
    needed its own home rather than a sentinel key inside the map): each reads its own
    keys under its own guard, so neither can break the other and NEITHER can raise —
    this is a side-channel and never breaks ``hive_health``."""
    try:
        conn = store.conn
    except Exception:  # a store with no readable conn at all
        conn = None
    return {"repos": _repo_blocks(store, conn), "fleet": _fleet_block(conn)}
