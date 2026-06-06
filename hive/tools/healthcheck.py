"""M12 — the container HEALTHCHECK: exit 0 IFF the server is up AND the embedder is RESIDENT.

`python -m hive.tools.healthcheck` is the image `HEALTHCHECK` CMD. It runs as a SEPARATE
process from the serving entrypoint (Docker spawns it inside the container), so it cannot
observe the server's in-RAM embedder directly. The contract — *healthy ≡ embedder
resident* — is therefore made structural via a readiness marker the entrypoint persists
to the shared `/data` DB at `serve.ready`: this process reads that marker (and verifies
the recorded serve PID is still alive, same PID namespace) instead of trusting that "the
model is probably loaded by now". The whole point (M12 §2, "define errors out of
existence"): an orchestrator / `./hive up` gates on health, so an unloaded server is
structurally invisible as "ready" — it can never be routed a recall.

Touches NO network (HF is offline anyway). Returns a sysexits-style code:
    0  → ok AND embedder_loaded   (the ONLY green)
    1  → unhealthy                (db unreachable, OR the embedder is not resident yet)
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Callable, Mapping, Optional

_log = logging.getLogger("hive.healthcheck")

_DEFAULT_DB_PATH = "/data/shared.db"
HEALTHY = 0
UNHEALTHY = 1

# Mirror the entrypoint's marker keys (kept here verbatim rather than imported so the
# healthcheck has NO dependency on the boot module — a smaller, independently-loadable
# probe surface). A drift between the two is caught by test_healthcheck_marker_keys_match.
MARK_EMBEDDER_LOADED = "boot:embedder_loaded"
MARK_SERVE_PID = "boot:serve_pid"
MARK_SERVE_STARTTIME = "boot:serve_starttime"


def _pid_alive(pid: int) -> bool:
    """True iff a process with `pid` exists (same PID namespace as the container). A stale
    marker left by a crashed server must NOT read as healthy — `os.kill(pid, 0)` is the
    cheap liveness probe (signal 0 = existence check, no signal delivered)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                            # exists but not ours — still alive
        return True
    except OSError:
        return False
    return True


def _proc_starttime(pid: int) -> Optional[int]:
    """The start-time of `pid` (field 22 of /proc/<pid>/stat, clock-ticks since system boot).
    This is the load-bearing PID-REUSE guard: under the image's exec-form ENTRYPOINT the
    serving process is ALWAYS PID 1, and a container restart resets the PID namespace, so the
    NEW (still-cold) boot is PID 1 too — `os.kill(1,0)` cannot tell it from the prior warm
    process. The start-time, however, is set at process creation and DIFFERS across restart,
    so a stale ready-marker whose recorded start-time ≠ the live PID's start-time is rejected.
    None if the process is gone or /proc is unavailable. // O(1)."""
    if pid <= 0:
        return None
    try:
        with open(f"/proc/{int(pid)}/stat") as fh:
            data = fh.read()
        # comm (field 2) is parenthesized and may contain spaces/')'; parse after the LAST ')'.
        rest = data[data.rindex(")") + 1:].split()
        return int(rest[19])                           # field 22 == index 19 once pid+comm dropped
    except (OSError, ValueError, IndexError, OverflowError):
        return None


def _starttime_matches(pid: int, recorded: Optional[str]) -> bool:
    """The recorded ready-marker belongs to the CURRENTLY-LIVE incarnation of `pid` iff its
    recorded start-time equals the live process's start-time. A missing/mismatched record ⇒
    the marker is from a prior (restarted/crashed) boot ⇒ NOT this server ⇒ unhealthy."""
    if recorded is None:
        return False
    cur = _proc_starttime(pid)
    return cur is not None and str(cur) == str(recorded)


def _default_probe(env: Mapping[str, str]) -> dict[str, Any]:
    """Open the shared DB read-only and derive (ok, embedder_loaded) WITHOUT any network
    and without loading the model. `ok` ⇐ the DB answers a trivial query; `embedder_loaded`
    ⇐ the readiness marker is set "1" AND the recorded serve PID is alive AND its recorded
    start-time matches the live PID (so a stale marker from a prior boot — same reused PID 1,
    different incarnation — cannot read green). Any failure degrades to unhealthy (the safe
    direction) — the healthcheck itself never raises."""
    db_path = (env.get("HIVE_STORE__DB_PATH") or "").strip() or _DEFAULT_DB_PATH
    conn: Optional[sqlite3.Connection] = None
    try:
        # read-only URI so the healthcheck can never create/migrate the store as a side effect
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1").fetchone()            # db reachable ⇒ ok
        marker = _meta(conn, MARK_EMBEDDER_LOADED)
        pid = _as_int(_meta(conn, MARK_SERVE_PID))
        recorded_start = _meta(conn, MARK_SERVE_STARTTIME)
        embedder_loaded = (marker == "1" and _pid_alive(pid)
                           and _starttime_matches(pid, recorded_start))
        return {"ok": True, "embedder_loaded": bool(embedder_loaded), "db_path": db_path}
    except Exception as exc:                            # noqa: BLE001 — probe never raises
        _log.warning("healthcheck.probe_failed kind=%s db_path=%s", type(exc).__name__, db_path)
        return {"ok": False, "embedder_loaded": False, "db_path": db_path}
    finally:
        if conn is not None:
            conn.close()


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def _as_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def main(argv: Optional[list[str]] = None, *, env: Optional[Mapping[str, str]] = None,
         probe: Optional[Callable[[Mapping[str, str]], dict[str, Any]]] = None) -> int:
    """Exit 0 IFF `ok` AND `embedder_loaded`. The conjunction IS the contract: dropping the
    `embedder_loaded` term (RULE-2 mutation) lets a cold server report healthy. // O(1)."""
    env = os.environ if env is None else env
    probe = probe or _default_probe
    snap = probe(env)
    ok = snap.get("ok") is True
    embedder_loaded = snap.get("embedder_loaded") is True
    if ok and embedder_loaded:                          # ← dropping the 2nd term is RULE-2 mut #1
        return HEALTHY
    # warn WHICH conjunct failed (the §6 boundary log) — db_path is an identifier, not a secret
    _log.warning("healthcheck.unhealthy ok=%s embedder_loaded=%s db_path=%s",
                 ok, embedder_loaded, snap.get("db_path"))
    return UNHEALTHY


if __name__ == "__main__":  # pragma: no cover — module entry (the image HEALTHCHECK CMD)
    raise SystemExit(main())
