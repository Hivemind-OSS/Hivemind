"""hive — the operator CLI for the Hivemind container (the M12 ops front door).

One stdlib file that ORCHESTRATES the landed substrate and reimplements none of it:
lifecycle rides `docker compose`, provisioning shells to the in-container
`hive.tools.authctl` (token crypto + the access_tokens schema stay single-sourced in
the adapter), the tunnel is the compose `tunnel` profile. Exposed as `hive` via
[project.scripts]; `python -m hive.tools.cli` works uninstalled. Imports no brain
runtime — it runs on a host with nothing but the repo + Docker.

Boundary (M11/M12): this CLI wires lifecycle + transport ONLY. `hive connect` prints
the MCP registration line; per-repo onboarding is served over MCP (the usage contract
reaches every agent via the always-on `initialize` instructions — the CLI installs nothing,
and the optional versioned rules block an agent MAY install is the agent's own act over MCP,
never the CLI's) and never happens here.

Injection seams (`run` / `out` / `env` / `ask`) keep every verb unit-testable without
Docker — authctl's `connect_fn`/`out` idiom. `run` receives the FULL argv (program
included): the health-wait polls `docker inspect`, which is not a compose verb, so a
compose-prefixing runner could not carry it. Secret-safe: no token and no env value
is ever logged; a child's stdout is forwarded verbatim only where the verb's contract
is exactly that (token/tokens).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, TextIO

SERVICE = "hive-server"           # single source of the compose service name (mirrors compose.yaml)
IMAGE = "hive:vmin"               # the built image tag (mirrors compose.yaml `image:`) — `reset`
                                  # runs backupctl in a throwaway container off the data volume
HTTP_PORT = 8765                  # the daemon's loopback HTTP port (mirrors compose.yaml)
AUTHCTL = ("python", "-m", "hive.tools.authctl")    # the in-container admin tool
CENSUSCTL = ("python", "-m", "hive.tools.censusctl")  # the in-container ingest tool
TUNNEL_PROFILE = "tunnel"         # mirrors the compose `profiles: ["tunnel"]` gate

# sysexits.h — mirror authctl/entrypoint (an exit code cannot lie like a log can)
EX_OK = 0
EX_USAGE = 64
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_CONFIG = 78

# A runner takes the full child argv (+ the CLI's env mapping) and returns the
# completed process; `capture=False` streams the child's output to the operator.
Run = Callable[..., "subprocess.CompletedProcess"]


def default_run(argv: Sequence[str], env: Optional[Mapping[str, str]] = None, *,
                capture: bool = True,
                input: Optional[str] = None) -> subprocess.CompletedProcess:
    """The one place that actually spawns a child. `env` is passed through whole (the
    prod caller hands os.environ), so compose interpolation sees the operator's vars.
    `input`, when given, pipes text to the child's stdin (a keyword-only, additive seam
    for verbs that feed a child over stdin rather than argv). // O(child)."""
    return subprocess.run(
        list(argv), env=None if env is None else dict(env),
        capture_output=capture, text=True, input=input)


def _compose(*args: str, profile: Optional[str] = None) -> list[str]:
    """Build a `docker compose …` argv; `profile` injects the global --profile flag
    (it must precede the subcommand). // O(1)."""
    head = ["docker", "compose"]
    if profile:
        head += ["--profile", profile]
    return head + list(args)


_DOTENV = ".env"            # repo-root operator config; the SAME file compose auto-loads (cwd-relative)


def _load_dotenv(path: str, base: Mapping[str, str]) -> dict:
    """Merge ``KEY=VALUE`` lines from a dotenv file UNDER ``base`` so the CLI's own env
    sees the operator's ``.env`` — the single source compose already reads — and a verb's
    pre-flight check (e.g. ``up --tunnel``'s NGROK secret gate) cannot disagree with what
    compose will interpolate. ``base`` (the shell environment) WINS on conflict, mirroring
    docker compose precedence; a missing/unreadable file leaves ``base`` untouched (returned
    as a fresh dict). Blank lines, ``#`` comments, and lines with no ``=`` are skipped; the
    value is taken verbatim after the first ``=`` (surrounding whitespace trimmed, an inline
    ``#`` is NOT a comment — matching compose). // O(lines)."""
    merged = dict(base)
    try:
        with open(path, encoding="utf-8") as fh:
            raw_lines = fh.readlines()
    except OSError:
        return merged
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in merged:        # shell env wins; never override a real var
            merged[key] = value.strip()
    return merged


def _rc(proc: subprocess.CompletedProcess) -> int:
    """Map a compose child's exit to sysexits: the docker layer failing is a service
    problem (EX_UNAVAILABLE), not a usage one. // O(1)."""
    return EX_OK if proc.returncode == 0 else EX_UNAVAILABLE


HEALTH_POLL_S = 3                 # poll cadence of the bounded health-wait
DEFAULT_HEALTH_TIMEOUT_S = 180    # override via HIVE_HEALTH_TIMEOUT


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """The single-owner shape of `hive status` (THEORY §6): server up/down, health tri-state,
    tunnel state + URL, seat count — probed ONCE by `_probe_status`, consumed by BOTH the CLI
    text path (`_status`) and the UI JSON path (`/api/status`). Frozen + slots; the defaults
    UNDER-claim (Law 2): an unreachable stack floors to down / None / False, never optimistically
    up, so neither consumer can render a healthier picture than the probe actually saw."""
    server: str = "down"                    # "up" | "down"
    healthy: Optional[bool] = None          # True | False when up; None when down/unknown
    tunnel_on: bool = False
    tunnel_url: Optional[str] = None        # the public /mcp URL when the tunnel is up + a domain is set
    seats: Optional[int] = None             # provisioned seat count; None when the stack is down / list fails


# ── verbs ────────────────────────────────────────────────────────────────────────


def _dump_logs(run: Run, env: Mapping[str, str]) -> None:
    run(_compose("logs", "--tail=200", SERVICE), env, capture=False)


def _wait_healthy(run: Run, env: Mapping[str, str]) -> int:
    """Bounded wait until the daemon's healthcheck reports healthy; on unhealthy or
    timeout, dump recent logs and return EX_UNAVAILABLE. The status source is
    `docker inspect` on the container compose resolved — exactly what the healthcheck
    gate in compose.yaml drives. // O(timeout / poll)."""
    timeout_s = int((env.get("HIVE_HEALTH_TIMEOUT") or "").strip()
                    or DEFAULT_HEALTH_TIMEOUT_S)
    cid = (run(_compose("ps", "-q", SERVICE), env).stdout or "").strip()
    if not cid:
        print("hive: server container not found", file=sys.stderr)
        return EX_UNAVAILABLE
    elapsed = 0
    while True:
        probe = run(["docker", "inspect", "-f", "{{.State.Health.Status}}", cid], env)
        status = (probe.stdout or "").strip() or "starting"
        if status == "healthy":
            print("hive: healthy", file=sys.stderr)
            return EX_OK
        if status == "unhealthy":
            print("hive: UNHEALTHY", file=sys.stderr)
            _dump_logs(run, env)
            return EX_UNAVAILABLE
        if elapsed >= timeout_s:
            print(f"hive: health-wait timeout after {timeout_s}s", file=sys.stderr)
            _dump_logs(run, env)
            return EX_UNAVAILABLE
        time.sleep(HEALTH_POLL_S)
        elapsed += HEALTH_POLL_S


def _up(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    profile = None
    if args.tunnel:
        # public exposure is a conscious flag; its secrets fail-fast HERE, before any
        # child call (compose `:-` defaults them precisely so the CLI owns this gate).
        missing = [k for k in ("NGROK_AUTHTOKEN", "NGROK_DOMAIN")
                   if not (env.get(k) or "").strip()]
        if missing:
            print(f"hive: --tunnel requires {' + '.join(missing)} "
                  "(set in .env — see .env.example)", file=sys.stderr)
            return EX_CONFIG
        # The tunnel door is STRUCTURALLY token-gated (the daemon binds the tunnel listener
        # with auth_required=True; ngrok forwards only to it), so there is no posture to
        # refuse here — the public endpoint cannot be unauthenticated by construction.
        profile = TUNNEL_PROFILE
    # tunnel up is unnamed (starts the profile's sidecar too); default names the
    # daemon only — both are `up -d`, never an ephemeral cold-start `run --rm`.
    tail = ["up", "-d", "--build"] + ([] if profile else [SERVICE])
    if run(_compose(*tail, profile=profile), env, capture=False).returncode != 0:
        return EX_UNAVAILABLE
    return _wait_healthy(run, env)


_DEFAULT_RESET_OUT = "hive-backups"   # host dir (cwd-relative) for reset's + upgrade's snapshot


def _copy_snapshot_into_volume(run: Run, env: Mapping[str, str],
                               src: str) -> subprocess.CompletedProcess:
    """Copy a HOST snapshot `.db` onto `/data/shared.db` inside the `hive-data` volume, clearing the
    stale WAL sidecars, in a THROWAWAY `--entrypoint sh` container (the image ENTRYPOINT is the
    daemon, not a shell). Single-sourced so `restore` and `upgrade`'s auto-rollback share ONE copy
    line. The source dir is bind-mounted at `/in`; a `reset`/`upgrade`-produced snapshot is
    world-readable, so it reads regardless of owner. // O(db size)."""
    name = os.path.basename(src)
    return run(["docker", "run", "--rm", "--entrypoint", "sh",
                "-v", "hive-data:/data", "-v", f"{os.path.dirname(src)}:/in", IMAGE,
                "-c", f"cp /in/{name} /data/shared.db && "
                      "rm -f /data/shared.db-wal /data/shared.db-shm"], env)


def _snapshot_to_host(run: Run, env: Mapping[str, str],
                      abs_out: str) -> tuple[subprocess.CompletedProcess, str]:
    """Snapshot the store OUT of the volume to a host dir as a self-contained, recoverable `.db` —
    the safety net `reset` and `upgrade` both take BEFORE anything destructive. Runs `backupctl` in
    a THROWAWAY container as ROOT (`--user 0:0` reads the image-user-owned live WAL store and writes
    the operator's host bind), with `--entrypoint python` overriding the image's daemon ENTRYPOINT so
    the args reach backupctl — a pure file-level SQLite backup that works even when the daemon CANNOT
    boot against an incompatible store. On success a second root `chown` hands the snapshot back to
    the operator. Returns (the backupctl proc, the host-path snapshot dest — `abs_out` itself if the
    backup failed or printed no path). // O(db size)."""
    snap = run(["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "python",
                "-v", "hive-data:/data", "-v", f"{abs_out}:/out",
                "-e", "HIVE_RETENTION__BACKUP_DIR=/out",
                IMAGE, "-m", "hive.tools.backupctl"], env)
    if snap.returncode != 0:
        return snap, abs_out
    snap_name = os.path.basename((snap.stdout or "").strip())
    host_dest = os.path.join(abs_out, snap_name) if snap_name else abs_out
    # hand the root-written snapshot back to the operator (best-effort — it is already safe and
    # world-readable, so restore works regardless; this just makes it the operator's to manage).
    # POSIX-ownership only: native Windows has no os.getuid/getgid and Docker Desktop bind mounts
    # are already host-owned, so the chown-back is both impossible and unnecessary there — skip it.
    if hasattr(os, "getuid"):
        run(["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "chown",
             "-v", f"{abs_out}:/out", IMAGE, "-R", f"{os.getuid()}:{os.getgid()}", "/out"], env)
    return snap, host_dest


def _reset(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """Recoverable clean-start: snapshot the store OUT of the volume to the host, THEN destroy +
    recreate it empty. The snapshot is the safety net — `reset` ABORTS before any destructive
    `down -v` if it fails (fail-safe toward preservation, Law 6), so an accidental reset costs a
    restart, not the memory.

    The snapshot runs `backupctl` in a THROWAWAY container, as ROOT (`--user 0:0`) so it can read
    the image-user-owned live WAL store and write the operator's host dir, with `--entrypoint
    python` overriding the image's daemon ENTRYPOINT so the args actually reach backupctl — a pure
    file-level SQLite backup that never constructs the store, so it works even when the daemon
    CANNOT boot against a schema/geometry-incompatible store (the very case reset exists for). A
    second root `chown` hands the snapshot back to the operator. Unlike a bare `down -v`, the prior
    store survives on the host. // O(db size) + a clean restart."""
    abs_out = os.path.abspath(args.out or _DEFAULT_RESET_OUT)
    if not args.yes:
        answer = ask(f"hive reset DESTROYS the data volume (a snapshot is saved to {abs_out} "
                     "first). Type 'reset' to confirm: ")
        if (answer or "").strip() != "reset":
            print("hive: not confirmed — nothing destroyed", file=sys.stderr)
            return EX_USAGE
    # 1. snapshot OUT of the volume FIRST — the recoverable safety net (shared with `upgrade`).
    #    Works even when the daemon can't boot against an incompatible store (the very case reset
    #    exists for), and hands the snapshot back to the operator on success.
    snap, host_dest = _snapshot_to_host(run, env, abs_out)
    if snap.returncode != 0:
        sys.stderr.write(snap.stderr or "")
        print("hive: pre-reset snapshot FAILED — nothing destroyed "
              "(run `hive up` if you meant to start a fresh store)", file=sys.stderr)
        return EX_UNAVAILABLE
    # 2. only now is it safe to destroy the volume + recreate it empty + warm.
    if run(_compose("down", "-v"), env, capture=False).returncode != 0:
        return EX_UNAVAILABLE
    if run(_compose("up", "-d", "--build", SERVICE), env, capture=False).returncode != 0:
        return EX_UNAVAILABLE
    rc = _wait_healthy(run, env)
    print(f"hive: reset complete — prior store saved to {host_dest}\n"
          f"      roll back with: hive restore {host_dest}", file=sys.stderr)
    return rc


def _restore(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """Replace the live store with a host snapshot — the inverse of `reset`. Stops the daemon to
    release the WAL locks, copies the snapshot into the volume as `/data/shared.db` (clearing stale
    WAL sidecars), then rebuilds + restarts. The copy runs as the default image user (which owns
    `/data`); a `reset`-produced snapshot is world-readable, so it reads regardless of owner. The
    `--entrypoint sh` override is required — the image's ENTRYPOINT is the daemon, not a shell.
    // O(db size) + a clean restart."""
    src = os.path.abspath(args.file)
    if not os.path.isfile(src):
        print(f"hive: no such snapshot: {args.file}", file=sys.stderr)
        return EX_USAGE
    if not args.yes:
        answer = ask(f"hive restore OVERWRITES the live store with {src}. Type 'restore' to confirm: ")
        if (answer or "").strip() != "restore":
            print("hive: not confirmed — store unchanged", file=sys.stderr)
            return EX_USAGE
    # stop the daemon to release WAL locks before overwriting the db file; preserves the volume.
    if run(_compose("down"), env, capture=False).returncode != 0:
        return EX_UNAVAILABLE
    cp = _copy_snapshot_into_volume(run, env, src)
    if cp.returncode != 0:
        sys.stderr.write(cp.stderr or "")
        print("hive: restore copy FAILED — the daemon is stopped; run `hive up`", file=sys.stderr)
        return EX_UNAVAILABLE
    if run(_compose("up", "-d", "--build", SERVICE), env, capture=False).returncode != 0:
        return EX_UNAVAILABLE
    rc = _wait_healthy(run, env)
    print(f"hive: restore complete — store replaced from {src}", file=sys.stderr)
    return rc


_DEFAULT_UPGRADE_REF = "release"   # the maintainer's vetted-release pin; roll back with an older tag


def _rollback(run: Run, env: Mapping[str, str], *, prev: str, snap: str) -> int:
    """Bounded auto-revert for a failed `upgrade`: restore the code (`git checkout <prev>`) AND the
    store (the just-taken host snapshot copied back into the volume), then rebuild + wait. Every step
    is guarded; if ANY step of the revert ITSELF fails, print the EXACT manual recovery and exit
    EX_SOFTWARE — the operator is never left without a printed way back (Law 6). Returns EX_UNAVAILABLE
    when the revert SUCCEEDS (the upgrade still failed, but the server is back on `prev`). // O(db size)."""
    def _manual(reason: str) -> int:
        print(f"hive: AUTOMATIC ROLLBACK FAILED ({reason}) — recover manually:\n"
              f"      git checkout {prev}\n"
              f"      hive restore {snap}", file=sys.stderr)
        return EX_SOFTWARE
    if run(["git", "checkout", prev], env).returncode != 0:
        return _manual("git checkout")
    if run(_compose("down"), env, capture=False).returncode != 0:   # release WAL locks before the copy
        return _manual("compose down")
    cp = _copy_snapshot_into_volume(run, env, snap)
    if cp.returncode != 0:
        sys.stderr.write(cp.stderr or "")
        return _manual("snapshot restore")
    if run(_compose("up", "-d", "--build", SERVICE), env, capture=False).returncode != 0:
        return _manual("rebuild")
    if _wait_healthy(run, env) != EX_OK:
        return _manual("post-rollback health")
    print(f"hive: rolled back to {prev[:12] or 'HEAD'} — store restored from {snap}", file=sys.stderr)
    return EX_UNAVAILABLE


def _upgrade(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """Move the server to a vetted `release` ref: snapshot-gated, health-verified, auto-rolling-back.
    Ordered like `_reset` (snapshot-first / abort-before-destroy):

    1. a DIRTY tree aborts (no magic stash — the operator resolves it);
    2. `git fetch` + assert the target ref RESOLVES, before touching anything;
    3. capture HEAD as the rollback target;
    4. take a HOST snapshot that MUST succeed before the checkout (the recoverable safety net);
    5. checkout the ref → rebuild → bounded health-wait → app status gate;
    6. on ANY post-checkout failure, auto-revert (code + store) via `_rollback`.

    Failures fall toward the recoverable direction (Law 6): before the snapshot nothing has changed;
    after it, the pre-upgrade store is always on the host. // O(db size) + up to two rebuilds."""
    ref = args.ref
    if (run(["git", "status", "--porcelain"], env).stdout or "").strip():
        print("hive: working tree is dirty — commit or stash before `hive upgrade` "
              "(nothing changed)", file=sys.stderr)
        return EX_USAGE
    if run(["git", "fetch", "--tags"], env).returncode != 0:
        print("hive: `git fetch` failed — cannot reach the remote (nothing changed)", file=sys.stderr)
        return EX_UNAVAILABLE
    if run(["git", "rev-parse", "--verify", "--quiet", ref], env).returncode != 0:
        print(f"hive: ref {ref!r} not found after fetch (nothing changed)", file=sys.stderr)
        return EX_USAGE
    prev = (run(["git", "rev-parse", "HEAD"], env).stdout or "").strip()
    if not args.yes:
        answer = ask(f"hive upgrade snapshots the store, checks out {ref!r}, rebuilds, and "
                     f"health-gates (auto-rollback to {prev[:12] or 'HEAD'} on failure). "
                     "Type 'upgrade' to confirm: ")
        if (answer or "").strip() != "upgrade":
            print("hive: not confirmed — nothing changed", file=sys.stderr)
            return EX_USAGE
    abs_out = os.path.abspath(_DEFAULT_RESET_OUT)
    snap, host_dest = _snapshot_to_host(run, env, abs_out)
    if snap.returncode != 0:
        sys.stderr.write(snap.stderr or "")
        print("hive: pre-upgrade snapshot FAILED — nothing changed", file=sys.stderr)
        return EX_UNAVAILABLE
    if run(["git", "checkout", ref], env).returncode != 0:
        print(f"hive: `git checkout {ref}` failed — server unchanged; snapshot at {host_dest}",
              file=sys.stderr)
        return EX_UNAVAILABLE
    healthy = (run(_compose("up", "-d", "--build", SERVICE), env, capture=False).returncode == 0
               and _wait_healthy(run, env) == EX_OK
               and _probe_status(run, env).healthy is True)
    if healthy:
        print(f"hive: upgrade complete — now at {ref} (snapshot saved to {host_dest})", file=sys.stderr)
        return EX_OK
    print(f"hive: {ref} did not come up healthy — rolling back to {prev[:12] or 'HEAD'}", file=sys.stderr)
    return _rollback(run, env, prev=prev, snap=host_dest)


def _probe_status(run: Run, env: Mapping[str, str]) -> StatusSnapshot:
    """Probe the live stack ONCE into the single-owner `StatusSnapshot`. A DOWN server
    short-circuits to the fail-safe floor (`StatusSnapshot()`) running NO in-container exec —
    an unreachable stack is a safe, cheap, non-mutating read (Law 6: side-channels fail OPEN,
    never spawning a child against a stack that is down). When up, it execs the in-container
    healthcheck (health tri-state), reads the tunnel `ps` + NGROK_DOMAIN (state + public URL),
    and lists seats via authctl. // O(1) + the child probes."""
    ps = run(_compose("ps", SERVICE), env)
    if ps.returncode != 0 or "Up" not in (ps.stdout or ""):
        return StatusSnapshot()                            # down: the safe floor, ZERO exec
    hc = run(_compose("exec", "-T", SERVICE, "python", "-m", "hive.tools.healthcheck"), env)
    tn = run(_compose("ps", "ngrok", profile=TUNNEL_PROFILE), env)
    tunnel_on = tn.returncode == 0 and "Up" in (tn.stdout or "")
    domain = (env.get("NGROK_DOMAIN") or "").strip()
    tunnel_url = f"https://{domain}/mcp" if (tunnel_on and domain) else None
    tok = _exec_authctl(run, env, "list")
    seats = (len([ln for ln in (tok.stdout or "").splitlines() if ln.strip()])
             if tok.returncode == 0 else None)
    return StatusSnapshot(server="up", healthy=hc.returncode == 0, tunnel_on=tunnel_on,
                          tunnel_url=tunnel_url, seats=seats)


def _status(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """Format the single-owner `StatusSnapshot` into the operator's byte-identical status text.
    The probe logic lives ONCE in `_probe_status` (shared with the UI /api/status JSON path);
    this is only the text projection. // O(1) + the probe."""
    snap = _probe_status(run, env)
    if snap.server == "down":
        print("server: down", file=out)
        return EX_UNAVAILABLE
    health = "healthy" if snap.healthy else "UNHEALTHY"
    if snap.tunnel_on:
        tunnel = f"on — {snap.tunnel_url}" if snap.tunnel_url else "on"
    else:
        tunnel = "off (loopback only)"
    seats = str(snap.seats) if snap.seats is not None else "unknown"
    print(f"server: up ({health})\ntunnel: {tunnel}\nseats:  {seats}", file=out)
    return EX_OK if snap.healthy else EX_UNAVAILABLE


def _down(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    return _rc(run(_compose("down"), env, capture=False))      # PRESERVES the volume


# the seat contract, surfaced at every provisioning touchpoint
_SEAT_HINT = "mint one token per seat (`hive token <seat>`) — never share across agents"


def _exec_authctl(run: Run, env: Mapping[str, str], *args: str) -> subprocess.CompletedProcess:
    """All token verbs ride the in-container authctl — crypto + schema stay there.
    `-T` (no TTY) keeps the child's stdout a clean pipe for the credential."""
    return run(_compose("exec", "-T", SERVICE, *AUTHCTL, *args), env)


def _token(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    child = _exec_authctl(run, env, "create", args.seat)
    if child.returncode != 0:
        sys.stderr.write(child.stderr or "")
        return child.returncode                        # authctl already speaks sysexits
    print((child.stdout or "").strip(), file=out)      # the credential — stdout ONLY
    print(f"hive: token for seat {args.seat!r} shown once above — hand it over via a "
          f"secret manager; {_SEAT_HINT}", file=sys.stderr)
    return EX_OK


def _revoke(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    child = _exec_authctl(run, env, "revoke", args.seat)
    sys.stderr.write(child.stderr or "")
    return child.returncode if child.returncode != 0 else EX_OK


def _tokens(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    child = _exec_authctl(run, env, "list")
    if child.returncode != 0:
        sys.stderr.write(child.stderr or "")
        return child.returncode
    out.write(child.stdout or "")                      # seat labels, one per line, verbatim
    return EX_OK


def _connect(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """Print the teammate's transport-registration one-liner. Transport ONLY:
    per-repo onboarding is agent-driven over MCP (the rules block in hive_health).

    Identity is per-agent-SESSION — the server-minted ``Mcp-Session-Id`` any conforming client
    echoes, or an explicit ``X-Hive-Agent-Id`` for readable provenance — so ``connect`` bakes no
    static id. The remote (tunnel) door is token-gated; the bearer only AUTHENTICATES, it is
    never the identity. The local (loopback) door is tokenless."""
    domain = (env.get("NGROK_DOMAIN") or "").strip()
    if domain:
        # remote: the token-required tunnel door. The Bearer AUTHENTICATES; identity is still
        # the agent's per-session Mcp-Session-Id (or an explicit X-Hive-Agent-Id), same as local.
        # This line is copy-pasted by a teammate on an unknown OS/shell and `claude mcp add` bakes
        # the header at add-time, so print a literal placeholder — never shell-expansion syntax
        # (bash ${VAR} / PowerShell $env:VAR / cmd %VAR% would each be wrong on the other two).
        print(f'claude mcp add --transport http hive https://{domain}/mcp '
              '--header "Authorization: Bearer <seat-token>"', file=out)
        print(f"hive: replace <seat-token> with the seat's token; {_SEAT_HINT}", file=sys.stderr)
    else:
        # local: the tokenless loopback door. No Bearer, no baked id — per-session identity is
        # automatic via the server-minted Mcp-Session-Id a conforming client echoes.
        print(f'claude mcp add --transport http hive http://localhost:{HTTP_PORT}/mcp',
              file=out)
        print("hive: NGROK_DOMAIN not set — printed the tokenless local-loopback line "
              "(`hive up --tunnel` + NGROK_DOMAIN gives the token-gated public one).",
              file=sys.stderr)
        print("hive: identity is per-agent-session (the server-minted Mcp-Session-Id a "
              "conforming client echoes). For a readable/controlled id set a per-session "
              "X-Hive-Agent-Id — Claude Code's headersHelper (fresh UUID per session), a "
              "harness/CI header per agent, ${env:VAR} on Cursor/Windsurf/Cline, ${input:..} "
              "on VS Code, or env_http_headers on Codex.", file=sys.stderr)
    # edge-tools breadcrumb (transport-only boundary preserved — this is a cross-reference, not a
    # merged verb): the teammate also installs the edge CLI; census evidence is server-side (sync).
    print("hive: edge tools — install `hive-edge` (see HIVE-ADMIN.md §8); census evidence is "
          "computed server-side once sync is configured (HIVE_SYNC__REPO_URL, HIVE-ADMIN.md §4) "
          "— nothing to wire per device.", file=sys.stderr)
    return EX_OK


def _logs(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    argv = ["logs", "-f"] + ([args.service] if args.service else [])
    return _rc(run(_compose(*argv), env, capture=False))       # streams; Ctrl-C detaches


def _exec_backup(run: Run, env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """The one-shot in-container snapshot child (backupctl); its stdout is the snapshot dest
    path. Single-sourced so the CLI `backup` verb and the UI `/api/backup` route shell the SAME
    argv (never a second, drifting copy of the exec line). // O(db size)."""
    return run(_compose("exec", "-T", SERVICE, "python", "-m", "hive.tools.backupctl"), env)


def _backup(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """One-shot snapshot of the warm store + prune to retention.backup_keep — exec the
    in-container backupctl and forward the snapshot path. Manual (no scheduler); run it on
    whatever cadence you like — it keeps the backup_keep most-recent snapshots you take."""
    child = _exec_backup(run, env)
    if child.returncode != 0:
        sys.stderr.write(child.stderr or "")
        return EX_UNAVAILABLE
    out.write(child.stdout or "")                       # the snapshot dest path, verbatim
    return EX_OK


def _ingest(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """Feed an unsigned census receipt's change outcome into the evidence ledger — exec
    the in-container censusctl with the receipt piped over stdin (the documented
    `input=` seam): a host path does not exist in-container, so the bytes travel, not
    the name. Post-merge outcomes ride flags; the tag stays server-derived. Forwards
    the child's one-line JSON report verbatim; censusctl already speaks sysexits, so
    its exit code passes through."""
    if args.post_merge and not args.verdict:
        print("hive: --post-merge requires --verdict {pass,fail}", file=sys.stderr)
        return EX_USAGE
    try:
        with open(args.receipt, encoding="utf-8") as fh:
            receipt_text = fh.read()
    except OSError as error:
        print(f"hive: cannot read receipt {args.receipt!r}: {error}", file=sys.stderr)
        return EX_USAGE
    flags = (["--post-merge", "--verdict", args.verdict, "--signal", args.signal]
             if args.post_merge else [])
    child = run(_compose("exec", "-T", SERVICE, *CENSUSCTL, "ingest", "-", *flags),
                env, input=receipt_text)
    sys.stderr.write(child.stderr or "")
    if child.returncode != 0:
        return child.returncode                        # censusctl already speaks sysexits
    out.write(child.stdout or "")                      # the one-line JSON report, verbatim
    return EX_OK


def _ui(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """Serve the loopback operator dashboard (`hive ui`) and open it in the native browser.
    The `ui` module — and its http.server / webbrowser transport imports — is imported LAZILY
    HERE so that heavy import is deferred off every other verb's path (THEORY §6). // O(1) + serve."""
    from hive.tools import ui
    return ui.serve_ui(host=args.host, port=args.port, run=run, env=env,
                       open_browser=not args.no_open)


_HANDLERS: dict[str, Callable[..., int]] = {
    "up": _up,
    "down": _down,
    "logs": _logs,
    "backup": _backup,
    "reset": _reset,
    "restore": _restore,
    "upgrade": _upgrade,
    "status": _status,
    "token": _token,
    "revoke": _revoke,
    "tokens": _tokens,
    "connect": _connect,
    "ingest": _ingest,
    "ui": _ui,
}

def _ask_stderr(prompt: str) -> str:
    """Default `ask`: prompt on STDERR (stdout stays machine-parseable), read stdin."""
    print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip()


def main(argv: Optional[list[str]] = None, *, run: Optional[Run] = None,
         out: Optional[TextIO] = None, env: Optional[Mapping[str, str]] = None,
         ask: Optional[Callable[[str], str]] = None) -> int:
    """Dispatch one admin verb; return a sysexits code. `run` (default: a subprocess
    wrapper), `out`, `env`, and `ask` are injection seams — every verb is unit-testable
    without Docker. // O(1) + the child's work."""
    argv = sys.argv[1:] if argv is None else argv
    # prod path: fold the repo-root .env UNDER the shell env so the CLI and compose read
    # ONE source. An explicit env (tests) is taken verbatim — no .env side-load.
    env = _load_dotenv(_DOTENV, os.environ) if env is None else env
    run = run or default_run
    out = out if out is not None else sys.stdout
    ask = ask or _ask_stderr

    parser = argparse.ArgumentParser(
        prog="hive",
        description="Operate the Hivemind container: lifecycle, per-seat tokens, tunnel.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_up = sub.add_parser("up", help="build + start the daemon; bounded wait until healthy")
    p_up.add_argument("--tunnel", action="store_true",
                      help="also start the public ngrok tunnel "
                           "(requires NGROK_AUTHTOKEN + NGROK_DOMAIN)")
    sub.add_parser("down", help="stop the stack (the data volume is preserved)")
    p_logs = sub.add_parser("logs", help="follow logs (optionally one service)")
    p_logs.add_argument("service", nargs="?", default=None,
                        help=f"compose service (e.g. {SERVICE}, ngrok)")
    p_reset = sub.add_parser(
        "reset", help="snapshot the store out of the volume, then destroy + recreate it empty "
                      "(recoverable clean-start; asks to confirm)")
    p_reset.add_argument("--out", default=None,
                         help=f"host dir for the pre-reset snapshot (default: ./{_DEFAULT_RESET_OUT})")
    p_reset.add_argument("--yes", action="store_true",
                         help="skip the typed confirmation (for scripted dev resets)")
    p_restore = sub.add_parser(
        "restore", help="replace the live store with a snapshot .db (inverse of reset; asks to confirm)")
    p_restore.add_argument("file", help="path to a snapshot .db (e.g. ./hive-backups/hive-<stamp>.db)")
    p_restore.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    p_upgrade = sub.add_parser(
        "upgrade", help="move the server to a vetted release ref: snapshot → checkout → rebuild → "
                        "health-gate → auto-rollback on failure (backup-gated)")
    p_upgrade.add_argument("--ref", default=_DEFAULT_UPGRADE_REF,
                           help=f"git ref to move to (default: {_DEFAULT_UPGRADE_REF}; "
                                "roll back with an older tag)")
    p_upgrade.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    sub.add_parser("backup", help="snapshot the store now (manual; keeps the backup_keep most-recent you take)")
    sub.add_parser("status", help="server health, tunnel state + URL, seat count")
    p_token = sub.add_parser("token", help="mint a per-seat token (printed ONCE to stdout)")
    p_token.add_argument("seat", help="agent-seat identity, e.g. alice-laptop (one per seat)")
    p_revoke = sub.add_parser("revoke", help="revoke a seat's token (next request → 401)")
    p_revoke.add_argument("seat", help="the seat label to revoke")
    sub.add_parser("tokens", help="list provisioned seat labels (never the tokens)")
    sub.add_parser("connect", help="print the teammate's `claude mcp add` line (transport only)")
    p_ui = sub.add_parser(
        "ui", help="serve the loopback operator dashboard and open it in your browser")
    p_ui.add_argument("--host", default="127.0.0.1",
                      help="loopback bind host (default: 127.0.0.1; a routable host is refused)")
    p_ui.add_argument("--port", type=int, default=4173, help="loopback port (default: 4173)")
    p_ui.add_argument("--no-open", action="store_true",
                      help="serve without opening the native browser (headless / remote host)")
    p_ingest = sub.add_parser(
        "ingest", help="feed an unsigned census receipt's outcome into the evidence ledger")
    p_ingest.add_argument("receipt", help="path to the receipt JSON "
                                          "(piped to the in-container censusctl)")
    p_ingest.add_argument("--post-merge", action="store_true",
                          help="record a post-merge outcome (requires --verdict; the "
                               "server derives the tag from --signal)")
    p_ingest.add_argument("--verdict", choices=("pass", "fail"), default=None,
                          help="the post-merge outcome (pre-merge verdicts are derived "
                               "from the receipt, never asserted)")
    p_ingest.add_argument("--signal", choices=("randomized", "canary", "none"),
                          default="none",
                          help="post-merge evidence signal — machine-checked requires "
                               "randomized/canary")
    args = parser.parse_args(argv)
    return _HANDLERS[args.cmd](args, run=run, out=out, env=env, ask=ask)


if __name__ == "__main__":  # pragma: no cover — module entry (`python -m hive.tools.cli`)
    raise SystemExit(main(sys.argv[1:]))
