"""hive — the operator CLI for the Hivemind container (the M12 ops front door).

One stdlib file that ORCHESTRATES the landed substrate and reimplements none of it:
lifecycle rides `docker compose`, provisioning shells to the in-container
`hive.tools.authctl` (token crypto + the access_tokens schema stay single-sourced in
the adapter), the tunnel is the compose `tunnel` profile. Exposed as `hive` via
[project.scripts]; `python -m hive.tools.cli` works uninstalled. Imports no brain
runtime — it runs on a host with nothing but the repo + Docker.

Boundary (M11/M12): this CLI wires lifecycle + transport ONLY. `hive connect` prints
the MCP registration line; per-repo onboarding is agent-driven over MCP (the agent
self-installs the rules block surfaced in the `hive_health` tool description) and never
happens here.

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
from typing import Callable, Mapping, Optional, Sequence, TextIO

SERVICE = "hive-server"           # single source of the compose service name (mirrors compose.yaml)
HTTP_PORT = 8765                  # the daemon's loopback HTTP port (mirrors compose.yaml)
AUTHCTL = ("python", "-m", "hive.tools.authctl")    # the in-container admin tool
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
    `input` pipes text to the child's stdin (the credit NDJSON ingest rides this —
    a keyword-only, additive widening). // O(child)."""
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
        profile = TUNNEL_PROFILE
    # tunnel up is unnamed (starts the profile's sidecar too); default names the
    # daemon only — both are `up -d`, never an ephemeral cold-start `run --rm`.
    tail = ["up", "-d", "--build"] + ([] if profile else [SERVICE])
    if run(_compose(*tail, profile=profile), env, capture=False).returncode != 0:
        return EX_UNAVAILABLE
    return _wait_healthy(run, env)


def _nuke(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    answer = ask("hive nuke DESTROYS the data volume (hive-data). Type 'nuke' to confirm: ")
    if (answer or "").strip() != "nuke":
        print("hive: not confirmed — nothing destroyed", file=sys.stderr)
        return EX_USAGE
    return _rc(run(_compose("down", "-v"), env, capture=False))


def _status(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    ps = run(_compose("ps", SERVICE), env)
    if ps.returncode != 0 or "Up" not in (ps.stdout or ""):
        print("server: down", file=out)
        return EX_UNAVAILABLE
    hc = run(_compose("exec", "-T", SERVICE, "python", "-m", "hive.tools.healthcheck"), env)
    health = "healthy" if hc.returncode == 0 else "UNHEALTHY"
    tn = run(_compose("ps", "ngrok", profile=TUNNEL_PROFILE), env)
    tunnel_on = tn.returncode == 0 and "Up" in (tn.stdout or "")
    domain = (env.get("NGROK_DOMAIN") or "").strip()
    if tunnel_on:
        tunnel = f"on — https://{domain}/mcp" if domain else "on"
    else:
        tunnel = "off (loopback only)"
    tok = _exec_authctl(run, env, "list")
    if tok.returncode == 0:
        seats = str(len([ln for ln in (tok.stdout or "").splitlines() if ln.strip()]))
    else:
        seats = "unknown"
    print(f"server: up ({health})\ntunnel: {tunnel}\nseats:  {seats}", file=out)
    return EX_OK if hc.returncode == 0 else EX_UNAVAILABLE


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
    per-repo onboarding is agent-driven over MCP (the rules block in hive_health)."""
    domain = (env.get("NGROK_DOMAIN") or "").strip()
    if domain:
        url = f"https://{domain}/mcp"
    else:
        url = f"http://localhost:{HTTP_PORT}/mcp"
        print("hive: NGROK_DOMAIN not set — printed the local-loopback line "
              "(`hive up --tunnel` + NGROK_DOMAIN gives the public one)", file=sys.stderr)
    print(f'claude mcp add --transport http hive {url} '
          '--header "Authorization: Bearer ${HIVE_TOKEN}"', file=out)
    print(f"hive: the teammate exports HIVE_TOKEN first; {_SEAT_HINT}", file=sys.stderr)
    return EX_OK


def _logs(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    argv = ["logs", "-f"] + ([args.service] if args.service else [])
    return _rc(run(_compose(*argv), env, capture=False))       # streams; Ctrl-C detaches


def _backup(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """One-shot snapshot of the warm store + prune to retention.backup_keep — exec the
    in-container backupctl and forward the snapshot path. Manual (no scheduler); run it on
    whatever cadence you like — it keeps the backup_keep most-recent snapshots you take."""
    child = run(_compose("exec", "-T", SERVICE, "python", "-m", "hive.tools.backupctl"), env)
    if child.returncode != 0:
        sys.stderr.write(child.stderr or "")
        return EX_UNAVAILABLE
    out.write(child.stdout or "")                       # the snapshot dest path, verbatim
    return EX_OK


def _health(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """Print the convergence KPI snapshot (+ trends) off the warm store — exec the
    in-container healthcheck in report mode and forward its JSON. Read-only and needs no
    MCP seat. Gaps/contested stay on hive_health over MCP (they need the live servable
    index, which a read-only host-side read cannot build)."""
    child = run(_compose("exec", "-T", SERVICE, "python", "-m",
                         "hive.tools.healthcheck", "--trends"), env)
    if child.returncode != 0:
        sys.stderr.write(child.stderr or "")
        return EX_UNAVAILABLE
    out.write(child.stdout or "")                       # the KPI JSON, verbatim
    return EX_OK


_HANDLERS: dict[str, Callable[..., int]] = {
    "up": _up,
    "down": _down,
    "logs": _logs,
    "backup": _backup,
    "health": _health,
    "nuke": _nuke,
    "status": _status,
    "token": _token,
    "revoke": _revoke,
    "tokens": _tokens,
    "connect": _connect,
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
    sub.add_parser("nuke", help="DESTROY the stack incl. the data volume (asks to confirm)")
    sub.add_parser("backup", help="snapshot the store now (manual; keeps the backup_keep most-recent you take)")
    sub.add_parser("health", help="convergence KPI trends off the warm store "
                                  "(gaps/contested: hive_health over MCP)")
    sub.add_parser("status", help="server health, tunnel state + URL, seat count")
    p_token = sub.add_parser("token", help="mint a per-seat token (printed ONCE to stdout)")
    p_token.add_argument("seat", help="agent-seat identity, e.g. alice-laptop (one per seat)")
    p_revoke = sub.add_parser("revoke", help="revoke a seat's token (next request → 401)")
    p_revoke.add_argument("seat", help="the seat label to revoke")
    sub.add_parser("tokens", help="list provisioned seat labels (never the tokens)")
    sub.add_parser("connect", help="print the teammate's `claude mcp add` line (transport only)")
    args = parser.parse_args(argv)
    return _HANDLERS[args.cmd](args, run=run, out=out, env=env, ask=ask)


if __name__ == "__main__":  # pragma: no cover — module entry (`python -m hive.tools.cli`)
    raise SystemExit(main(sys.argv[1:]))
