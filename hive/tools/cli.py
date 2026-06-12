"""hive — the operator CLI for the Hivemind container (the M12 ops front door).

One stdlib file that ORCHESTRATES the landed substrate and reimplements none of it:
lifecycle rides `docker compose`, provisioning shells to the in-container
`hive.tools.authctl` (token crypto + the access_tokens schema stay single-sourced in
the adapter), the tunnel is the compose `tunnel` profile. Exposed as `hive` via
[project.scripts]; `python -m hive.tools.cli` works uninstalled. Imports no brain
runtime — it runs on a host with nothing but the repo + Docker.

Boundary (M11/M12): this CLI wires lifecycle + transport ONLY. `hive connect` prints
the MCP registration line; the per-repo handshake (`hive_init`) stays agent-driven
over MCP and never happens here.

Injection seams (`run` / `out` / `env` / `ask`) keep every verb unit-testable without
Docker — authctl's `connect_fn`/`out` idiom. `run` receives the FULL argv (program
included): the health-wait polls `docker inspect`, which is not a compose verb, so a
compose-prefixing runner could not carry it. Secret-safe: no token and no env value
is ever logged; a child's stdout is forwarded verbatim only where the verb's contract
is exactly that (token/tokens).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
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
                capture: bool = True) -> subprocess.CompletedProcess:
    """The one place that actually spawns a child. `env` is passed through whole (the
    prod caller hands os.environ), so compose interpolation sees the operator's vars.
    // O(child)."""
    return subprocess.run(
        list(argv), env=None if env is None else dict(env),
        capture_output=capture, text=True)


def _compose(*args: str, profile: Optional[str] = None) -> list[str]:
    """Build a `docker compose …` argv; `profile` injects the global --profile flag
    (it must precede the subcommand). // O(1)."""
    head = ["docker", "compose"]
    if profile:
        head += ["--profile", profile]
    return head + list(args)


def _rc(proc: subprocess.CompletedProcess) -> int:
    """Map a compose child's exit to sysexits: the docker layer failing is a service
    problem (EX_UNAVAILABLE), not a usage one. // O(1)."""
    return EX_OK if proc.returncode == 0 else EX_UNAVAILABLE


# ── verbs ────────────────────────────────────────────────────────────────────────


def _down(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    return _rc(run(_compose("down"), env, capture=False))      # PRESERVES the volume


def _logs(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    argv = ["logs", "-f"] + ([args.service] if args.service else [])
    return _rc(run(_compose(*argv), env, capture=False))       # streams; Ctrl-C detaches


_HANDLERS: dict[str, Callable[..., int]] = {
    "down": _down,
    "logs": _logs,
}

# Verbs that never touch compose (pure local prints) skip the tenant gate.
_LOCAL_VERBS: frozenset[str] = frozenset()


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
    import os                                       # local: env default without a module global

    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    run = run or default_run
    out = out if out is not None else sys.stdout
    ask = ask or _ask_stderr

    parser = argparse.ArgumentParser(
        prog="hive",
        description="Operate the Hivemind container: lifecycle, per-seat tokens, tunnel.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("down", help="stop the stack (the data volume is preserved)")
    p_logs = sub.add_parser("logs", help="follow logs (optionally one service)")
    p_logs.add_argument("service", nargs="?", default=None,
                        help=f"compose service (e.g. {SERVICE}, ngrok)")
    args = parser.parse_args(argv)

    # Every compose-touching verb needs the tenant id — compose interpolation
    # fail-fasts on it; surface the config error HERE, before any child call.
    if args.cmd not in _LOCAL_VERBS and not (env.get("HIVE_TENANT_ID") or "").strip():
        print("hive: HIVE_TENANT_ID is required (set it in .env or the environment)",
              file=sys.stderr)
        return EX_CONFIG

    return _HANDLERS[args.cmd](args, run=run, out=out, env=env, ask=ask)


if __name__ == "__main__":  # pragma: no cover — module entry (`python -m hive.tools.cli`)
    raise SystemExit(main(sys.argv[1:]))
