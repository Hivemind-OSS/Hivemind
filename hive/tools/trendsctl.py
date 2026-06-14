"""hive trends — the operator's read of the convergence KPIs (the agent-config-tuning
observation surface).

`python -m hive.tools.trendsctl` runs INSIDE the container (the `hive trends` CLI verb
execs it, exactly as `hive status` execs `hive.tools.healthcheck`). It opens the shared
store READ-ONLY (`mode=ro` — it can never mutate or migrate as a side effect) and prints
the EXISTING `compute_trends` report — current vs previous 14d window + deltas — as JSON
on stdout. No daemon call, no token, no embedder: a separate read path beside the warm
server.

Boundary: this COMPUTES nothing new. It reuses `hive.app.trends.compute_trends` (the same
function `hive_health(include_trends=true)` serves) and the `hive.app.gaps` clusterer,
clustered at the LIVE `autonomy.demand_tau` (resolved through `Config.load`, so the CLI's
view matches the server's). `compute_trends` touches only `store.conn`, so the store
stand-in is a one-attribute view over the read-only connection — building the real store
would run the schema `executescript` (a write) on a read path.

Never raises out of `main`: a missing/unreadable store is logged to stderr and mapped to a
non-zero exit with NO stdout (an empty read must not look like an empty-but-valid report).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Callable, Mapping, Optional

from hive.app.config import AutonomyConfig, Config
from hive.app.gaps import cluster_misses
from hive.app.trends import compute_trends

_log = logging.getLogger("hive.trendsctl")

_DEFAULT_DB_PATH = "/data/shared.db"
EX_OK = 0
EX_FAIL = 1


class _ConnView:
    """The minimal store stand-in `compute_trends` needs: it reads ONLY `.conn`. A
    one-attribute view keeps the trends read on a read-only connection (the real store's
    __init__ runs a schema `executescript`, which a `mode=ro` connection cannot do). The
    `tests/container/test_trendsctl.py` end-to-end test against a seeded real DB is the
    enforced contract that this stays sufficient if `compute_trends` ever grows."""
    __slots__ = ("conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn


def _resolve_tau(env: Mapping[str, str], db_path: str) -> float:
    """The clustering τ the server's demand rule uses, so the CLI's demand_entropy matches
    `hive_health`'s. Resolved through the SAME 4-layer Config stack (TOML + env pins);
    degrades to the AutonomyConfig default on any load failure — a stale τ must never break
    the read."""
    try:
        return float(Config.load(db_path=db_path, env=env).autonomy.demand_tau)
    except Exception as exc:                              # noqa: BLE001 — never break the read
        _log.warning("trendsctl.config_load_failed kind=%s — using default demand_tau",
                     type(exc).__name__)
        return float(AutonomyConfig().demand_tau)


def _default_probe(env: Mapping[str, str], *, now: Optional[int] = None) -> dict:
    """Open the shared store read-only and compute the trends report. Raises on a
    missing/unreadable DB (the caller maps that to a non-zero exit) — the read-only URI
    guarantees no write/migrate side effect. // report-time SQL only."""
    db_path = (env.get("HIVE_STORE__DB_PATH") or "").strip() or _DEFAULT_DB_PATH
    tau = _resolve_tau(env, db_path)
    when = int(now) if now is not None else int(time.time())
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        return compute_trends(_ConnView(conn),
                              lambda rows: cluster_misses(rows, tau=tau), now=when)
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None, *, env: Optional[Mapping[str, str]] = None,
         probe: Optional[Callable[[Mapping[str, str]], dict[str, Any]]] = None) -> int:
    """Print the trends JSON to stdout and exit 0; on ANY probe failure, log to stderr,
    print nothing, exit 1. `probe` is the injection seam (unit-testable without Docker or a
    real DB). // O(1) + the probe's report-time SQL."""
    env = os.environ if env is None else env
    probe = probe or _default_probe
    try:
        snap = probe(env)
    except Exception as exc:                              # noqa: BLE001 — main never raises
        _log.warning("trendsctl.probe_failed kind=%s", type(exc).__name__)
        return EX_FAIL
    print(json.dumps(snap, sort_keys=True))               # stdout = the report (CLI forwards it)
    return EX_OK


if __name__ == "__main__":  # pragma: no cover — module entry (`hive trends` execs this)
    raise SystemExit(main())
