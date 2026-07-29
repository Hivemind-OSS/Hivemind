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
    78 EX_CONFIG       — Config validation failed (or a malformed boot knob)

Boundary: the entrypoint OWNS the operator-facing env contract for the two
boot-critical operator values (`tenant_id` / `db_path`, the compose keys
`HIVE_TENANT_ID` / `HIVE_STORE__DB_PATH`) and bridges them into `Config.load`;
`agent_id` carries a process default and is not operator-facing in the HTTP build
(per-request attribution is the transport-resolved per-session identity). It also owns the
flat `HIVE_HTTP_*` transport knobs — `MAX_BODY_BYTES` plus the two doors' addresses
(`LOOPBACK_HOST`/`LOOPBACK_PORT`, `TUNNEL_HOST`/`TUNNEL_PORT`) — which are properties of the
DEPLOYMENT rather than of the memory system, so they stop at this adapter and never enter
`Config`. The rest of the config tree is M-config's `HIVE_*__`
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
import threading
import time
from typing import Any, Callable, Mapping, Optional, Protocol

from hive.app.config import Config
from hive.app.rate_limit import TokenBucketLimiter  # stdlib-light, torch-free

_log = logging.getLogger("hive.entrypoint")

# sysexits.h — the boot contract (an exit code cannot lie the way a log line can).
EX_OK = 0
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_CONFIG = 78

_DEFAULT_DB_PATH = "/data/shared.db"
_DEFAULT_AGENT_ID = "default-agent"
_DEFAULT_TENANT_ID = "default"
_DEFAULT_LOG_LEVEL = logging.INFO
_DEFAULT_HTTP_PORT = (
    8765  # the loopback door — host-published (compose maps 127.0.0.1:8765)
)
_DEFAULT_TUNNEL_PORT = (
    8766  # the tunnel door — compose-internal only (ngrok forwards to it)
)
# Both doors default to the container's own interface, because that is what the compose
# port map REQUIRES: `127.0.0.1:8765:8765` publishes to the HOST's loopback and docker's
# proxy reaches the container by its container IP, so a process bound to 127.0.0.1 INSIDE
# the container is unreachable through the map. The tokenless door's safety under compose
# is therefore the map's `127.0.0.1:` prefix, NOT this bind address. A deployment with no
# such map — a container platform, a bare `docker run -p`, a host process — has nothing
# supplying that safety and must set HIVE_HTTP_LOOPBACK_HOST=127.0.0.1, which is the whole
# reason the address is a knob rather than a constant.
_DEFAULT_BIND_HOST = "0.0.0.0"
_MEMORY_DB = ":memory:"  # an ephemeral store has no directory to probe
# Part S hardening: the rate-limit belt is fixed (no operator knob — disabling a DoS belt
# on a tunnel-reachable daemon is a footgun). The 1 MiB body cap mirrors
# http_server._DEFAULT_MAX_BODY (kept separate so http_server stays importable without it
# and is lazy-imported at serve time).
_DEFAULT_RATE_LIMIT = 120
_DEFAULT_RATE_WINDOW_S = 60.0
_DEFAULT_MAX_BODY_BYTES = 1 << 20


def _configure_logging(level: int) -> None:
    """Bind the structured-JSON-to-stderr handler onto the `hive` logger (all boot
    checkpoints are JSON on stderr; stdout is the JSON-RPC channel). Idempotent — safe to
    call twice (default level pre-config, then the operator's `cfg.obs.log_level`). Lazy
    import so module import stays light and torch-free. // O(1)."""
    try:
        from hive.app.observability import configure_json_logging  # noqa: PLC0415 — lazy

        configure_json_logging(level=int(level), stream=True)
    except Exception as exc:  # noqa: BLE001 — logging setup never aborts boot
        _log.warning("entrypoint.log_config_failed kind=%s", type(exc).__name__)


# Readiness markers the SEPARATE healthcheck process reads (same /data DB, same PID ns).
# Namespaced (`boot:`) per the strict-prefix store discipline so they never collide with
# any other meta key family.
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
        rest = data[data.rindex(")") + 1 :].split()
        return int(rest[19])
    except (OSError, ValueError, IndexError, OverflowError):
        return 0


class Boot(Protocol):
    """The assembled bundle the entrypoint drives, in order. The real implementation is
    P1.14's `build_container`; tests inject a fake. Each method is a boot CHECKPOINT.

    `store` is exposed so the entrypoint can stamp the readiness markers itself (keeping
    the marker policy in the testable state machine, not buried in the assembler).
    `token_store` is the HTTP daemon's per-device verify seam (`.verify(token) -> label|None`);
    widening this Protocol requires the REAL adapter (Container) to carry it too, not just the
    fake — see test_build_container_is_boot_conformant."""

    store: Any
    token_store: Any

    def migrate(self) -> None: ...
    def build_index(self) -> None: ...
    def warm_embedder(self) -> Any: ...  # returns the embedder; raise ⇒ EX_UNAVAILABLE
    def make_server(self) -> Any: ...  # returns a HiveMCPServer (embedder resident)


def _default_build_boot(cfg: Config, *, tenant_id: str, agent_id: str) -> Boot:
    """The REAL assembler is P1.14's composition root (`hive.app.container.build_container`).
    Lazy-imported so this module imports torch-free and `--help` never pulls the adapter
    stack. Until P1.14 lands, invoking the container at runtime raises a CLEAR error
    rather than a cryptic ImportError — unit tests never reach this path (they inject)."""
    try:
        from hive.app.container import build_container  # noqa: PLC0415 — lazy by design
    except ImportError as exc:  # pragma: no cover — P1.14 not yet wired
        _log.error("entrypoint.boot_unwired error=%s", type(exc).__name__)
        raise RuntimeError(
            "real adapter assembly is not wired until P1.14 "
            "(hive.app.container.build_container); inject build_boot to boot earlier"
        ) from exc
    return build_container(
        cfg, tenant_id=tenant_id, agent_id=agent_id
    )  # pragma: no cover


def _make_http_serve(
    boot: Boot,
    loopback_port: int,
    tunnel_port: int,
    *,
    loopback_host: str = _DEFAULT_BIND_HOST,
    tunnel_host: str = _DEFAULT_BIND_HOST,
    rate_limit: int = _DEFAULT_RATE_LIMIT,
    rate_window_s: float = _DEFAULT_RATE_WINDOW_S,
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES,
    run_http_dual: Optional[Callable[..., None]] = None,
    lock: Optional[threading.Lock] = None,
    webhook_secret: str = "",
    webhook_nudge: Optional[Callable[[], None]] = None,
) -> Callable[[Any], None]:
    """The DEFAULT serve step (replacing stdio): a warm HTTP daemon binding BOTH doors — the
    tokenless LOOPBACK door (`loopback_host:loopback_port`) and the token-required TUNNEL door
    (`tunnel_host:tunnel_port`). Auth is a property of the listening socket, not a config mode;
    the addresses default to the compose posture and are resolved from the environment by
    `main()`, so what fronts the daemon can decide them without a rebuild. The tunnel door's bearer gate depends on a `verify` CALLABLE
    (`boot.token_store.verify`) — never the concrete SQLite class. ONE `threading.Lock` (passed
    by `main()`, which shares the SAME lock with the sync daemon thread; created here only for
    a standalone caller) + ONE `TokenBucketLimiter` are threaded into BOTH listeners, so the
    single-writer serialization invariant (the shared conn + embedder are not thread-safe)
    holds ACROSS the two doors AND the sync side-channel. `webhook_secret`/`webhook_nudge`
    (default inert: "" ⇒ dead branch, byte-identical door) pass through verbatim and arm the
    census-webhook nudge on the TUNNEL door only — the nudge is the sync loop's wake Event's
    `set`, never a store/server handle. `run_http_dual` is injectable ONLY so this default
    path is unit-testable; the real one is lazy-imported (torch-free) so module import stays
    light."""
    if run_http_dual is None:
        from hive.app.http_server import run_http_dual as impl  # noqa: PLC0415 — lazy, torch-free

        run_http_dual = impl
    lock = lock if lock is not None else threading.Lock()  # ONE lock, both doors
    limiter = TokenBucketLimiter(
        limit=rate_limit, window_s=rate_window_s
    )  # ONE limiter, both doors

    def serve(server: Any) -> None:
        run_http_dual(
            server,
            loopback_host=loopback_host,
            loopback_port=loopback_port,
            tunnel_host=tunnel_host,
            tunnel_port=tunnel_port,
            verify=boot.token_store.verify,
            lock=lock,
            limiter=limiter,
            max_body_bytes=max_body_bytes,
            webhook_secret=webhook_secret,
            webhook_nudge=webhook_nudge,
        )

    return serve


def _resolve_max_body(env: Mapping[str, str]) -> Optional[int]:
    """Resolve the request body cap: `$HIVE_HTTP_MAX_BODY_BYTES` (default 1 MiB). A
    zero/negative cap would reject EVERY body, so non-positive is malformed → None →
    EX_CONFIG (disable by setting it large, not zero). NEVER echoes the value. // O(1)."""
    raw = (env.get("HIVE_HTTP_MAX_BODY_BYTES") or "").strip()
    if not raw:
        return _DEFAULT_MAX_BODY_BYTES
    try:
        max_body = int(raw)
    except ValueError:
        _log.error(
            "entrypoint.invalid_max_body var=HIVE_HTTP_MAX_BODY_BYTES code=%d",
            EX_CONFIG,
        )
        return None
    if max_body <= 0:
        _log.error(
            "entrypoint.invalid_max_body var=HIVE_HTTP_MAX_BODY_BYTES code=%d",
            EX_CONFIG,
        )
        return None
    return max_body


def _resolve_port(env: Mapping[str, str], var: str, default: int) -> Optional[int]:
    """Resolve one door's port from ``var`` (blank/absent ⇒ ``default``). A non-numeric or
    out-of-range value is malformed → None → EX_CONFIG; port 0 is rejected with the rest
    because an ephemeral port is one no operator could ever reach. NEVER echoes the value —
    a malformed env value is arbitrary text and may be a mis-pasted credential. // O(1)."""
    raw = (env.get(var) or "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        _log.error("entrypoint.invalid_port var=%s code=%d", var, EX_CONFIG)
        return None
    if not (1 <= port <= 65535):
        _log.error("entrypoint.invalid_port var=%s code=%d", var, EX_CONFIG)
        return None
    return port


def _resolve_http_bind(
    env: Mapping[str, str],
) -> Optional[tuple[str, int, str, int]]:
    """Resolve ``(loopback_host, loopback_port, tunnel_host, tunnel_port)`` from the four
    ``HIVE_HTTP_*`` knobs; every default reproduces the compose posture exactly, so an
    unset environment binds what it always bound.

    The knobs exist because the addresses are a property of the DEPLOYMENT, not of the
    software: what fronts the daemon (a compose port map, a platform router, a reverse
    proxy, an SSH forward) decides which port must be public and which address the
    tokenless door may safely answer on. Baking them in exported one deployment's posture
    to every other.

    None ⇒ EX_CONFIG. Two doors on the SAME port is refused whatever their addresses,
    because the second bind would fail with EADDRINUSE *after* the readiness markers are
    stamped — a container that looks healthy while serving one door. Failing at boot is the
    difference between a wrong value and a silent half-daemon. // O(1)."""
    loopback_host = (
        env.get("HIVE_HTTP_LOOPBACK_HOST") or ""
    ).strip() or _DEFAULT_BIND_HOST
    tunnel_host = (env.get("HIVE_HTTP_TUNNEL_HOST") or "").strip() or _DEFAULT_BIND_HOST
    loopback_port = _resolve_port(env, "HIVE_HTTP_LOOPBACK_PORT", _DEFAULT_HTTP_PORT)
    tunnel_port = _resolve_port(env, "HIVE_HTTP_TUNNEL_PORT", _DEFAULT_TUNNEL_PORT)
    if loopback_port is None or tunnel_port is None:
        return None
    if loopback_port == tunnel_port:
        # A port number is not a credential, so naming it is safe and is the whole
        # diagnostic: the operator set one knob to the other door's port.
        _log.error(
            "entrypoint.bind_port_collision port=%d code=%d (the tokenless loopback door "
            "and the token-required tunnel door cannot share a port — one of "
            "HIVE_HTTP_LOOPBACK_PORT / HIVE_HTTP_TUNNEL_PORT must change)",
            loopback_port,
            EX_CONFIG,
        )
        return None
    return loopback_host, loopback_port, tunnel_host, tunnel_port


def _probe_db_dir(db_path: str) -> Optional[str]:
    """Return the store's directory iff it EXISTS but this process cannot write it, else
    None. That state is the mounted-volume ownership mismatch — a volume whose files belong
    to a different uid than the image's runtime user — and without this probe it surfaces
    three steps later as sqlite's ``unable to open database file``, an EX_SOFTWARE that
    names neither the path nor the cause.

    A MISSING directory is a different failure and is only WARNed: on the shipped image
    ``/data`` always exists (VOLUME), so an absent directory means a deliberately custom
    ``db_path``, and creating it is not this probe's call to make. ``:memory:`` has no
    directory at all. // O(1) stat."""
    if db_path == _MEMORY_DB:
        return None
    directory = os.path.dirname(os.path.abspath(db_path)) or "."
    if not os.path.isdir(directory):
        _log.warning(
            "entrypoint.db_dir_missing path=%s (migrate cannot create the store under a "
            "directory that does not exist)",
            directory,
        )
        return None
    return None if os.access(directory, os.W_OK | os.X_OK) else directory


# The env vars whose VALUES are credentials: a value must never ride any log line
# (the config-invalid detail included). Variable NAMES may appear; values never.
_SECRET_ENV_VARS = ("HIVE_SYNC__TOKEN", "HIVE_SYNC__WEBHOOK_SECRET")


def _scrub_secret_values(text: str, env: Mapping[str, str]) -> str:
    """Replace any configured credential VALUE with ``***`` in an outbound message.
    Config's own error messages are name-only by construction — this belt makes the
    never-echo-a-secret invariant MECHANICAL at the escape path (the same direction
    as sync's ``_redact``), surviving any upstream message regression. // O(#secrets)."""
    for var in _SECRET_ENV_VARS:
        value = env.get(var) or ""
        if value:
            text = text.replace(value, "***")
    return text


def _resolve_env(env: Mapping[str, str]) -> tuple[str, str, str]:
    """Resolve the three boot-critical operator values, all with safe defaults so the
    container boots zero-config. `tenant_id` is a constant label (never a query filter —
    the single-tenant boundary), defaulting to `"default"` when unset; db_path/agent_id
    likewise default. NEVER echoes an env *value* (secret-safe)."""
    tenant_id = (env.get("HIVE_TENANT_ID") or "").strip() or _DEFAULT_TENANT_ID
    db_path = (env.get("HIVE_STORE__DB_PATH") or "").strip() or _DEFAULT_DB_PATH
    agent_id = (env.get("HIVE_AGENT_ID") or "").strip() or _DEFAULT_AGENT_ID
    return tenant_id, db_path, agent_id


def _probe_token_env(boot: Boot, env: Mapping[str, str]) -> list[str]:
    """The §5 secrets-row boot probe: every env var NAME a repo-registry row's
    ``token_env`` declares must be PRESENT in ``env`` — the names of the absent ones
    are returned so ``main()`` can fail fast EX_CONFIG NAMING them (no silent
    default). Presence is the bar (the sync tick resolves ``os.environ[var]``, so an
    absent var would KeyError there every tick); rows with an UNSET ``token_env``
    fall to the fleet-default var at tick time and may run anonymous — never probed.
    A store without a registry surface (minimal fakes) or an unreadable registry
    probes clean: the probe only ever reports a genuinely registered, genuinely
    absent var — real store faults surface at migrate. NEVER reads or echoes an env
    *value* (secret-safe). // O(rows)."""
    registry = getattr(getattr(boot, "store", None), "repo_registry", None)
    if registry is None:
        return []
    try:
        rows = registry()
    except Exception:  # noqa: BLE001 — the probe never invents a config fault
        return []
    missing: list[str] = []
    for row in rows:
        var = str(getattr(row, "token_env", "") or "")
        if var and var not in env and var not in missing:
            missing.append(var)
    return missing


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
    except Exception as exc:  # noqa: BLE001 — never abort boot on this
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
        store.meta_set(
            MARK_EMBEDDER_LOADED, "1"
        )  # set LAST — identity is in place first
    except Exception as exc:  # noqa: BLE001 — never abort serve on this
        _log.warning(
            "entrypoint.mark_ready_failed kind=%s (healthcheck stays red)",
            type(exc).__name__,
        )


def main(
    argv: Optional[list[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    build_boot: Optional[Callable[..., Boot]] = None,
    serve: Optional[Callable[[Any], None]] = None,
    pid: Optional[int] = None,
) -> int:
    """Boot the container in strict order; return a sysexits exit code. Never raises out
    of the boot path — every failure is logged to stderr and converted to an exit code so
    stdout (the JSON-RPC channel) stays clean. // O(1) control flow."""
    env = os.environ if env is None else env
    build_boot = build_boot or _default_build_boot
    pid = os.getpid() if pid is None else pid

    # Structured JSON → stderr at a safe default BEFORE config loads, so even the missing-env
    # error surfaces in the structured-JSON format (re-leveled to cfg.obs.log_level once config is known).
    _configure_logging(_DEFAULT_LOG_LEVEL)

    # ── config.loaded (EX_CONFIG on Config validation failure) ──
    tenant_id, db_path, agent_id = _resolve_env(
        env
    )  # all default; tenant is a label, not required
    max_body_bytes = _resolve_max_body(env)
    if max_body_bytes is None:
        return EX_CONFIG
    bind = _resolve_http_bind(env)
    if bind is None:
        return EX_CONFIG
    loopback_host, loopback_port, tunnel_host, tunnel_port = bind
    # The store's directory is probed BEFORE assembly: a volume whose ownership does not
    # match this process is a config fault the operator can fix, and it must not reach
    # migrate — where it becomes an EX_SOFTWARE naming neither the path nor the cause.
    unwritable = _probe_db_dir(db_path)
    if unwritable is not None:
        _log.error(
            "entrypoint.db_dir_not_writable path=%s uid=%d code=%d (the store's directory "
            "exists but this process cannot write it — typically a mounted volume owned by "
            "a different uid than the image's runtime user; chown the mount to that uid or "
            "run the container as its owner)",
            unwritable,
            os.geteuid() if hasattr(os, "geteuid") else -1,
            EX_CONFIG,
        )
        return EX_CONFIG
    try:
        cfg = Config.load(db_path=db_path, env=env, runtime={"tenant_id": tenant_id})
    except Exception as exc:  # noqa: BLE001 — bad config is EX_CONFIG
        # detail carries the exception message, which NAMES the offending variable —
        # an operator staring at exit 78 must know which var to fix; the scrub belt
        # guarantees no credential VALUE can ride the line.
        _log.error(
            "entrypoint.config_invalid kind=%s code=%d detail=%s",
            type(exc).__name__,
            EX_CONFIG,
            _scrub_secret_values(str(exc), env),
        )
        return EX_CONFIG
    _configure_logging(
        int(getattr(cfg.obs, "log_level", _DEFAULT_LOG_LEVEL))
    )  # operator level now live
    _log.info("entrypoint.config_loaded tenant_id=%s db_path=%s", tenant_id, db_path)

    try:
        boot = build_boot(cfg, tenant_id=tenant_id, agent_id=agent_id)
    except Exception as exc:  # noqa: BLE001 — assembler failure
        _log.error(
            "entrypoint.assemble_failed kind=%s code=%d",
            type(exc).__name__,
            EX_SOFTWARE,
        )
        return EX_SOFTWARE

    # THE global write lock, owned by main(): the two HTTP doors and the sync daemon
    # thread all serialize store/embedder access through this ONE object.
    lock = threading.Lock()

    # Invalidate any STALE ready marker from a prior boot BEFORE migrate — a restarted
    # container (persistent volume + reused PID 1) must start red until THIS boot warms.
    _invalidate_ready(boot)

    # ── token_env probe (EX_CONFIG): every REGISTERED credential var must exist ──
    # A repo-registry row stores the NAME of its git-token env var (D2 indirection —
    # never a secret byte); a row-named var absent from the boot env would fail that
    # repo's sync leg every tick, so it fails fast HERE, naming the var(s).
    missing_vars = _probe_token_env(boot, env)
    if missing_vars:
        _log.error(
            "entrypoint.token_env_missing vars=%s code=%d (a repo-registry row "
            "names this env var — set it, or re-register the repo without "
            "--token-env)",
            ",".join(missing_vars),
            EX_CONFIG,
        )
        return EX_CONFIG

    # ── migrate.done (EX_SOFTWARE; the guard below makes serve UNREACHABLE on failure) ──
    try:
        boot.migrate()
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "entrypoint.migrate_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE
        )
        return EX_SOFTWARE  # ← deleting this return is mutation #4
    _log.info("entrypoint.migrate_done")

    # ── index.built (EX_SOFTWARE) ──
    try:
        boot.build_index()
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "entrypoint.index_failed kind=%s code=%d", type(exc).__name__, EX_SOFTWARE
        )
        return EX_SOFTWARE
    _log.info("entrypoint.index_built")

    # ── embedder.warm (EX_UNAVAILABLE; healthy ≡ resident — a cold embedder cannot serve) ──
    try:
        embedder = boot.warm_embedder()
    except Exception as exc:  # noqa: BLE001 — dead embedder
        _log.error(
            "entrypoint.embedder_warm_failed kind=%s code=%d",
            type(exc).__name__,
            EX_UNAVAILABLE,
        )
        return EX_UNAVAILABLE  # ← swallowing this is mutation #3
    if not bool(getattr(embedder, "loaded", False)):
        _log.error("entrypoint.embedder_not_resident code=%d", EX_UNAVAILABLE)
        return EX_UNAVAILABLE
    _log.info("entrypoint.embedder_warm")

    # ── assemble the server (embedder now resident) ──
    try:
        server = boot.make_server()
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "entrypoint.make_server_failed kind=%s code=%d",
            type(exc).__name__,
            EX_SOFTWARE,
        )
        return EX_SOFTWARE

    # ── serve.ready: stamp the readiness markers, THEN serve (run_stdio blocks) ──
    _mark_ready(boot, pid=pid)
    _log.info("entrypoint.serve_ready tenant_id=%s pid=%d", tenant_id, pid)

    # The sync side-channel — ALWAYS started: WHICH repos to feed is the store's
    # durable registry, re-read every tick (an empty registry is an inert tick, never
    # an unarmed daemon, so registering the first repo needs no restart). Started
    # strictly AFTER the ready markers so sync can never delay readiness, and guarded
    # whole: a start failure is logged and the daemon serves anyway — the side-channel
    # fails open, the serve never does. The Container's own LifecycleService rides
    # through as the post-ingest promotion sweep (the established rung runs behind a
    # canonical ingest); a minimal boot without one degrades to no sweep, not a crash.
    sync_thread = None
    try:
        from hive.app.sync import start_sync  # noqa: PLC0415 — lazy
        from hive.domain.change_evidence import ChangeEvidenceService  # noqa: PLC0415 — lazy

        evidence = ChangeEvidenceService(
            reader=boot.store,
            appender=boot.store,
            now=lambda: int(time.time()),
            ranges=boot.store,
        )
        sync_thread = start_sync(
            cfg, boot.store, evidence, lock, lifecycle=getattr(boot, "lifecycle", None)
        )
    except Exception as exc:  # noqa: BLE001 — side-channel start must never abort serve
        _log.warning(
            "entrypoint.sync_start_failed kind=%s (serving without sync)",
            type(exc).__name__,
        )

    # Default the serve step to the warm HTTP daemon binding BOTH doors. Built only now —
    # after assembly (it needs boot.token_store.verify) and after start_sync, so the tunnel
    # door's webhook nudge is the LIVE sync thread's wake Event's `set` (no thread ⇒ nudge
    # None: a webhook 204, armed by cfg.sync.webhook_secret, degrades to a no-op wake).
    # The addresses come from `_resolve_http_bind` (defaults = the compose posture: the
    # loopback door at 8765, which the compose host map and `hive connect` assume, and the
    # tunnel door at 8766). Auth is a property of the socket — no posture to resolve. The
    # rate-limit belt uses its fixed defaults. An injected `serve` (every unit test) wins.
    if serve is None:
        nudge = getattr(sync_thread, "sync_nudge", None)
        serve = _make_http_serve(
            boot,
            loopback_port,
            tunnel_port,
            loopback_host=loopback_host,
            tunnel_host=tunnel_host,
            max_body_bytes=max_body_bytes,
            lock=lock,
            webhook_secret=cfg.sync.webhook_secret,
            webhook_nudge=(nudge.set if nudge is not None else None),
        )

    serve(server)
    return EX_OK


if __name__ == "__main__":  # pragma: no cover — module entry (the image ENTRYPOINT)
    raise SystemExit(main(sys.argv[1:]))
