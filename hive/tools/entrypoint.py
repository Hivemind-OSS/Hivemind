"""M12 — the container ENTRYPOINT: a fail-fast boot state machine (driving adapter).

`python -m hive.tools.entrypoint` is the image's `ENTRYPOINT`. It is a *driving
adapter* (hexagonal): it boots and supervises the domain, it contains none of it. The
one job is to bring up "a warm, migrated, indexed, embedder-loaded server on stdio" in a
STRICT, each-step-logged order and to FAIL FAST (a non-zero exit code, never stdout
text — stdout is the JSON-RPC channel) the instant any step cannot be guaranteed:

    config.loaded → migrate.done → index.built → embedder.warm → serve.ready

Exit codes are the contract (sysexits.h), because an orchestrator / `./hive up` gates on
them and on the healthcheck — prose in a log cannot lie about an exit code:
    0  EX_OK           — clean serve (run_stdio returned)
    69 EX_UNAVAILABLE  — the embedder is dead / never became resident (no recall at cold)
    70 EX_SOFTWARE     — an internal boot step failed (migrate / index / assembly)
    78 EX_CONFIG       — a required env var is missing OR Config validation failed

Boundary (M12 §5): the entrypoint OWNS the operator-facing env contract for the three
boot-critical values the ported server shape needs (`tenant_id` / `db_path` / `agent_id`,
the §6.2 compose keys `HIVE_TENANT_ID` / `HIVE_STORE__DB_PATH` / `HIVE_AGENT_ID`) and
bridges them into `Config.load`; the rest of the config tree is M-config's `HIVE_*__`
namespace, which the entrypoint does NOT re-parse. The REAL adapter assembly (store +
embedder + index + server) lands in P1.14 (`hive.app.container.build_container`) and is
INJECTED here as `build_boot` — so the boot ORDER, the fail-fast exit codes and the
embedder-resident gate are pinned by unit tests against a fake boot NOW, and the real
wiring drops in behind the same seam later with zero change to this state machine.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Callable, Mapping, Optional, Protocol

from hive.app.config import Config

_log = logging.getLogger("hive.entrypoint")

# sysexits.h — the boot contract (an exit code cannot lie the way a log line can).
EX_OK = 0
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_CONFIG = 78

_DEFAULT_DB_PATH = "/data/shared.db"
_DEFAULT_AGENT_ID = "default-agent"
_DEFAULT_LOG_LEVEL = logging.INFO


def _configure_logging(level: int) -> None:
    """Bind the structured-JSON-to-stderr handler onto the `hive` logger (M12 §6: all boot
    checkpoints are JSON on stderr; stdout is the JSON-RPC channel). Idempotent — safe to
    call twice (default level pre-config, then the operator's `cfg.obs.log_level`). Lazy
    import so module import stays light and torch-free. // O(1)."""
    try:
        from hive.app.observability import configure_json_logging  # noqa: PLC0415 — lazy
        configure_json_logging(level=int(level), stream=True)
    except Exception as exc:                            # noqa: BLE001 — logging setup never aborts boot
        _log.warning("entrypoint.log_config_failed kind=%s", type(exc).__name__)

# Readiness markers the SEPARATE healthcheck process reads (same /data DB, same PID ns).
# Namespaced per the strict-prefix store discipline so they never collide with producer
# (`last_producer_tick_ts`) or onboarding (`hive_init:link:*`) meta keys.
MARK_EMBEDDER_LOADED = "boot:embedder_loaded"
MARK_SERVE_PID = "boot:serve_pid"
MARK_SERVE_STARTTIME = "boot:serve_starttime"


def _proc_starttime(pid: int) -> int:
    """The serving process's start-time (field 22 of /proc/<pid>/stat). Recorded at
    serve.ready so the healthcheck can reject a STALE marker from a prior boot: under the
    exec-form ENTRYPOINT the server is PID 1 and a restart reuses PID 1, so a raw PID is
    not a unique identity across restart — the start-time is. 0 if unreadable (non-Linux/
    test); the healthcheck then refuses to go green, the safe direction. // O(1)."""
    try:
        with open(f"/proc/{int(pid)}/stat") as fh:
            data = fh.read()
        rest = data[data.rindex(")") + 1:].split()
        return int(rest[19])
    except (OSError, ValueError, IndexError, OverflowError):
        return 0


class Boot(Protocol):
    """The assembled bundle the entrypoint drives, in order. The real implementation is
    P1.14's `build_container`; tests inject a fake. Each method is a boot CHECKPOINT.

    `store` is exposed so the entrypoint can stamp the readiness markers itself (keeping
    the marker policy in the testable state machine, not buried in the assembler)."""
    store: Any

    def migrate(self) -> None: ...
    def build_index(self) -> None: ...
    def warm_embedder(self) -> Any: ...   # returns the embedder; raise ⇒ EX_UNAVAILABLE
    def make_server(self) -> Any: ...     # returns a HiveMCPServer (embedder resident)


def _default_build_boot(cfg: Config, *, tenant_id: str, agent_id: str) -> Boot:
    """The REAL assembler is the P1.14 keystone (`hive.app.container.build_container`).
    Lazy-imported so this module imports torch-free and `--help` never pulls the adapter
    stack. Until P1.14 lands, invoking the container at runtime raises a CLEAR error
    rather than a cryptic ImportError — unit tests never reach this path (they inject)."""
    try:
        from hive.app.container import build_container  # noqa: PLC0415 — lazy by design
    except ImportError as exc:                          # pragma: no cover — P1.14 not yet wired
        _log.error("entrypoint.boot_unwired error=%s", type(exc).__name__)
        raise RuntimeError(
            "real adapter assembly is not wired until P1.14 "
            "(hive.app.container.build_container); inject build_boot to boot earlier"
        ) from exc
    return build_container(cfg, tenant_id=tenant_id, agent_id=agent_id)  # pragma: no cover


def _serve_stdio(server: Any) -> None:                  # pragma: no cover — real stdio serve
    from hive.app.mcp_server import run_stdio            # noqa: PLC0415 — lazy
    run_stdio(server, sys.stdin, sys.stdout)


def _resolve_env(env: Mapping[str, str]) -> Optional[tuple[str, str, str]]:
    """Resolve the three boot-critical operator values. Returns None iff a REQUIRED var is
    missing (the caller maps that to EX_CONFIG). `HIVE_TENANT_ID` is the one hard-required
    var (the single-tenant boundary); db_path/agent_id have safe defaults."""
    tenant_id = (env.get("HIVE_TENANT_ID") or "").strip()
    if not tenant_id:
        # error path: name the missing var + EX_CONFIG; NEVER echo any env *value* (secret-safe)
        _log.error("entrypoint.missing_required_env var=HIVE_TENANT_ID code=%d", EX_CONFIG)
        return None
    db_path = (env.get("HIVE_STORE__DB_PATH") or "").strip() or _DEFAULT_DB_PATH
    agent_id = (env.get("HIVE_AGENT_ID") or "").strip() or _DEFAULT_AGENT_ID
    return tenant_id, db_path, agent_id


def _invalidate_ready(boot: Boot) -> None:
    """Clear the embedder-ready marker at boot START — BEFORE migrate/index/warm — so a
    restarted container (which reuses the persistent /data volume AND reuses PID 1) starts
    structurally RED and only flips green when THIS boot reaches serve.ready. Without this,
    a crash-looping or still-warming restart reads the prior boot's stale `1` as healthy.
    Best-effort; a clear failure leaves the (safe) red-on-its-own-merits to the start-time
    guard. // O(1)."""
    store = getattr(boot, "store", None)
    if store is None:
        return
    try:
        store.meta_set(MARK_EMBEDDER_LOADED, "0")
    except Exception as exc:                            # noqa: BLE001 — never abort boot on this
        _log.warning("entrypoint.invalidate_ready_failed kind=%s", type(exc).__name__)


def _mark_ready(boot: Boot, *, pid: int) -> None:
    """Persist the readiness markers the healthcheck reads. The PID + its start-time are
    written FIRST and the `embedder_loaded=1` flag LAST, so a healthcheck can never observe
    `loaded=1` without a matching (pid, start-time) identity. Best-effort: a marker-write
    failure must NOT abort an otherwise-warm serve (it only degrades the healthcheck to
    red, which is the safe direction). `store` may be absent on a minimal boot."""
    store = getattr(boot, "store", None)
    if store is None:
        return
    try:
        store.meta_set(MARK_SERVE_PID, str(pid))
        store.meta_set(MARK_SERVE_STARTTIME, str(_proc_starttime(pid)))
        store.meta_set(MARK_EMBEDDER_LOADED, "1")       # set LAST — identity is in place first
    except Exception as exc:                            # noqa: BLE001 — never abort serve on this
        _log.warning("entrypoint.mark_ready_failed kind=%s (healthcheck stays red)",
                     type(exc).__name__)


def main(argv: Optional[list[str]] = None, *, env: Optional[Mapping[str, str]] = None,
         build_boot: Optional[Callable[..., Boot]] = None,
         serve: Optional[Callable[[Any], None]] = None,
         pid: Optional[int] = None) -> int:
    """Boot the container in strict order; return a sysexits exit code. Never raises out
    of the boot path — every failure is logged to stderr and converted to an exit code so
    stdout (the JSON-RPC channel) stays clean. // O(1) control flow."""
    env = os.environ if env is None else env
    build_boot = build_boot or _default_build_boot
    serve = serve or _serve_stdio
    pid = os.getpid() if pid is None else pid

    # Structured JSON → stderr at a safe default BEFORE config loads, so even the missing-env
    # error surfaces in the §6 format (re-leveled to cfg.obs.log_level once config is known).
    _configure_logging(_DEFAULT_LOG_LEVEL)

    # ── config.loaded (EX_CONFIG on missing-env OR validation failure) ──
    resolved = _resolve_env(env)
    if resolved is None:                                # ← removing this guard is RULE-2 mut #2
        return EX_CONFIG
    tenant_id, db_path, agent_id = resolved
    try:
        cfg = Config.load(db_path=db_path, env=env, runtime={"tenant_id": tenant_id})
    except Exception as exc:                            # noqa: BLE001 — bad config is EX_CONFIG
        _log.error("entrypoint.config_invalid kind=%s code=%d", type(exc).__name__, EX_CONFIG)
        return EX_CONFIG
    _configure_logging(int(getattr(cfg.obs, "log_level", _DEFAULT_LOG_LEVEL)))  # operator level now live
    _log.info("entrypoint.config_loaded tenant_id=%s db_path=%s", tenant_id, db_path)

    try:
        boot = build_boot(cfg, tenant_id=tenant_id, agent_id=agent_id)
    except Exception as exc:                            # noqa: BLE001 — assembler failure
        _log.error("entrypoint.assemble_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE)
        return EX_SOFTWARE

    # Invalidate any STALE ready marker from a prior boot BEFORE migrate — a restarted
    # container (persistent volume + reused PID 1) must start red until THIS boot warms.
    _invalidate_ready(boot)

    # ── migrate.done (EX_SOFTWARE; the guard below makes serve UNREACHABLE on failure) ──
    try:
        boot.migrate()
    except Exception as exc:                            # noqa: BLE001
        _log.error("entrypoint.migrate_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE)
        return EX_SOFTWARE                              # ← deleting this return is RULE-2 mut #4
    _log.info("entrypoint.migrate_done")

    # ── index.built (EX_SOFTWARE) ──
    try:
        boot.build_index()
    except Exception as exc:                            # noqa: BLE001
        _log.error("entrypoint.index_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE)
        return EX_SOFTWARE
    _log.info("entrypoint.index_built")

    # ── embedder.warm (EX_UNAVAILABLE; healthy ≡ resident — a cold embedder cannot serve) ──
    try:
        embedder = boot.warm_embedder()
    except Exception as exc:                            # noqa: BLE001 — dead embedder
        _log.error("entrypoint.embedder_warm_failed kind=%s code=%d",
                   type(exc).__name__, EX_UNAVAILABLE)
        return EX_UNAVAILABLE                           # ← swallowing this is RULE-2 mut #3
    if not bool(getattr(embedder, "loaded", False)):
        _log.error("entrypoint.embedder_not_resident code=%d", EX_UNAVAILABLE)
        return EX_UNAVAILABLE
    _log.info("entrypoint.embedder_warm")

    # ── assemble the server (embedder now resident) ──
    try:
        server = boot.make_server()
    except Exception as exc:                            # noqa: BLE001
        _log.error("entrypoint.make_server_failed kind=%s code=%d",
                   type(exc).__name__, EX_SOFTWARE)
        return EX_SOFTWARE

    # ── serve.ready: stamp the readiness markers, THEN serve (run_stdio blocks) ──
    _mark_ready(boot, pid=pid)
    _log.info("entrypoint.serve_ready tenant_id=%s pid=%d", tenant_id, pid)
    serve(server)
    return EX_OK


if __name__ == "__main__":  # pragma: no cover — module entry (the image ENTRYPOINT)
    raise SystemExit(main(sys.argv[1:]))
