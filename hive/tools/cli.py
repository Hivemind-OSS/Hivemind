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
import json
import os
import subprocess
import sys
import time
from typing import Callable, Mapping, Optional, Sequence, TextIO

from hive.tools import originctl

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


# ── origin: the GitHub-API credit loop (link / sync / ls / rm) ──────────────────
# Config lives at $XDG_CONFIG_HOME|~/.config/hive/origins.json, 0600, token INLINE —
# the documented deviation from the env-var rule: the hourly cron has no operator env,
# so the token must rest on disk. $GITHUB_TOKEN at sync time overrides the stored one;
# no verb ever prints a token.

_CRON_MARKER = "# hive-origin"
_PAT_HINT = ("provide a PAT (classic `repo` scope, or fine-grained Contents:Read + "
             "Pull requests:Read) via --token-stdin, $GITHUB_TOKEN, or `gh auth login`")


def _origins_path(env: Mapping[str, str]) -> str:
    base = (env.get("XDG_CONFIG_HOME") or "").strip() \
        or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "hive", "origins.json")


def _read_origins(env: Mapping[str, str]) -> dict:
    """The repo→{token} map. Missing file ⇒ {} (nothing linked); an unreadable blob
    degrades to {} with a warning (re-link rebuilds it — never a crash path)."""
    try:
        with open(_origins_path(env), encoding="utf-8") as fh:
            return dict(json.load(fh).get("origins", {}))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        print(f"hive origin: {_origins_path(env)} unreadable — treating as unlinked",
              file=sys.stderr)
        return {}


def _write_origins(env: Mapping[str, str], origins: Mapping[str, dict]) -> None:
    """0600 from the first byte: the token never transits a world-readable window
    (os.open with the mode, not write-then-chmod); chmod covers pre-existing files."""
    path = _origins_path(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps({"version": 1, "origins": dict(origins)},
                         indent=2, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.chmod(path, 0o600)


def _resolve_token(args, env: Mapping[str, str], run: Run) -> Optional[str]:
    """The link-time token ladder: --token-stdin > $GITHUB_TOKEN/$GH_TOKEN >
    `gh auth token` > None (public-repo mode). Never echoes a token anywhere."""
    if getattr(args, "token_stdin", False):
        return sys.stdin.readline().strip() or None
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        tok = (env.get(var) or "").strip()
        if tok:
            return tok
    try:
        gh = run(["gh", "auth", "token"], env)
    except OSError:                                    # gh not installed
        return None
    if gh.returncode == 0 and (gh.stdout or "").strip():
        return (gh.stdout or "").strip()
    return None


def _install_cron(run: Run, env: Mapping[str, str], *, checkout: str) -> None:
    """ONE marker-tagged @hourly line per host (strip-then-append: idempotent;
    re-link moves the checkout). `cd` first so `python -m` resolves installed or
    not; `set -a && . ./.env` because cron's env is bare — the .env file is the
    only HIVE_TENANT_ID / compose-interpolation source. Failure warns, never
    blocks the link (`origin sync` stays manually runnable)."""
    log_path = os.path.join(os.path.dirname(_origins_path(env)), "origin-sync.log")
    line = (f"@hourly cd {checkout} && set -a && . ./.env && "
            f"{sys.executable} -m hive.tools.cli origin sync "
            f">> {log_path} 2>&1 {_CRON_MARKER}")
    try:
        current = run(["crontab", "-l"], env)
        base = (current.stdout or "") if current.returncode == 0 else ""
        kept = [ln for ln in base.splitlines()
                if not ln.rstrip().endswith(_CRON_MARKER)]
        written = run(["crontab", "-"], env, input="\n".join(kept + [line]) + "\n")
        if written.returncode != 0:
            raise OSError(written.stderr or "crontab write failed")
    except OSError as e:
        print(f"hive origin: warning — could not install the cron line ({e}); "
              "run `hive origin sync` manually or re-link", file=sys.stderr)


def _remove_cron(run: Run, env: Mapping[str, str]) -> None:
    try:
        current = run(["crontab", "-l"], env)
        if current.returncode != 0:
            return                                     # no crontab — nothing to strip
        kept = [ln for ln in (current.stdout or "").splitlines()
                if not ln.rstrip().endswith(_CRON_MARKER)]
        run(["crontab", "-"], env, input="\n".join(kept) + "\n")
    except OSError:
        print("hive origin: warning — could not edit the crontab", file=sys.stderr)


def _sync_one(repo: str, token: Optional[str], lookback_days: int, *,
              run: Run, out: TextIO, env: Mapping[str, str]) -> int:
    """One repo's stateless sweep: GitHub scan host-side (summary → stderr), then
    the win/loss rows ride NDJSON over stdin into the in-container ingest. Never
    load-bearing: skipping a sync changes nothing but credit freshness."""
    try:
        rows, summary = originctl.scan_github(
            repo, token=token, now=int(time.time()), lookback_days=lookback_days)
    except originctl.GithubApiError as e:
        if e.status in (401, 403, 404):
            print(f"hive origin: cannot access {repo} ({e.status}) — {_PAT_HINT}",
                  file=sys.stderr)
            return EX_CONFIG
        print(f"hive origin: GitHub API failure on {repo}: {e}", file=sys.stderr)
        return EX_UNAVAILABLE
    except OSError as e:
        print(f"hive origin: network failure on {repo}: {e}", file=sys.stderr)
        return EX_UNAVAILABLE
    print(f"hive origin: {repo} scan {json.dumps(summary, sort_keys=True)}",
          file=sys.stderr)
    if not rows:
        print(f"hive origin: {repo}: nothing to ingest", file=sys.stderr)
        return EX_OK
    ndjson = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    child = run(_compose("exec", "-T", SERVICE, "python", "-m",
                         "hive.tools.originctl", "ingest", "--ndjson", "-"),
                env, input=ndjson)
    if child.returncode != 0:
        sys.stderr.write(child.stderr or "")
        return EX_UNAVAILABLE
    out.write(child.stdout or "")                      # the ingest JSON report
    return EX_OK


def _origin_link(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """End-to-end link: resolve token → validate repo access → persist 0600 config
    → install ONE hourly cron line → first 90-day backfill sync."""
    try:
        repo = originctl.parse_repo_arg(args.target)
    except ValueError as e:
        print(f"hive origin: {e}", file=sys.stderr)
        return EX_USAGE
    if not os.path.exists("compose.yaml"):
        print("hive origin: compose.yaml not found in the current directory — run "
              "the link from the hive checkout (the cron line will cd here)",
              file=sys.stderr)
        return EX_CONFIG
    env_file_has_tenant = False
    try:
        with open(".env", encoding="utf-8") as fh:
            env_file_has_tenant = any(ln.strip().startswith("HIVE_TENANT_ID")
                                      for ln in fh)
    except OSError:
        pass
    if not env_file_has_tenant and not (env.get("HIVE_TENANT_ID") or "").strip():
        # behind the tenant gate this cannot fire; kept as the verb's own contract
        print("hive: HIVE_TENANT_ID is required (set it in .env)", file=sys.stderr)
        return EX_CONFIG
    if not env_file_has_tenant:
        print("hive origin: warning — .env does not set HIVE_TENANT_ID; the hourly "
              "cron sources ONLY ./.env, add it there", file=sys.stderr)
    token = _resolve_token(args, env, run)
    try:
        originctl.probe_repo(repo, token=token)
    except originctl.GithubApiError as e:
        if e.status in (401, 403, 404):
            print(f"hive origin: cannot access {repo} ({e.status}) — {_PAT_HINT}",
                  file=sys.stderr)
            return EX_CONFIG
        print(f"hive origin: GitHub API failure: {e}", file=sys.stderr)
        return EX_UNAVAILABLE
    except OSError as e:
        print(f"hive origin: network failure: {e}", file=sys.stderr)
        return EX_UNAVAILABLE
    origins = _read_origins(env)
    origins[repo] = {"token": token}
    _write_origins(env, origins)
    if args.no_cron:
        print(f"hive origin: linked {repo} (--no-cron: install the hourly sync "
              "yourself or run `hive origin sync`)", file=sys.stderr)
    else:
        _install_cron(run, env, checkout=os.getcwd())
        print(f"hive origin: linked {repo} — hourly sync installed", file=sys.stderr)
    lookback = args.lookback or originctl.FIRST_SYNC_LOOKBACK_DAYS
    return _sync_one(repo, token, lookback, run=run, out=out, env=env)


def _origin_sync(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    origins = _read_origins(env)
    if not origins:
        print("hive origin: no origins linked — link one with "
              "`hive origin <owner/repo>`", file=sys.stderr)
        return EX_CONFIG
    lookback = args.lookback or originctl.DEFAULT_LOOKBACK_DAYS
    env_token = (env.get("GITHUB_TOKEN") or "").strip() or None
    worst = EX_OK
    for repo, rec in origins.items():
        token = env_token or rec.get("token")          # env OVERRIDES stored
        rc = _sync_one(repo, token, lookback, run=run, out=out, env=env)
        if rc != EX_OK and worst == EX_OK:
            worst = rc                                 # keep syncing the rest
    return worst


def _origin_ls(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    for repo, rec in _read_origins(env).items():       # labels only — NEVER tokens
        print(f"{repo}\ttoken={'stored' if rec.get('token') else 'none'}", file=out)
    return EX_OK


def _origin_rm(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    if not args.extra:
        print("hive origin rm: name the repo — `hive origin rm <owner/repo>`",
              file=sys.stderr)
        return EX_USAGE
    try:
        repo = originctl.parse_repo_arg(args.extra)
    except ValueError as e:
        print(f"hive origin: {e}", file=sys.stderr)
        return EX_USAGE
    origins = _read_origins(env)
    if repo not in origins:
        print(f"hive origin rm: {repo} is not linked "
              f"(linked: {', '.join(sorted(origins)) or 'none'})", file=sys.stderr)
        return EX_USAGE
    del origins[repo]
    _write_origins(env, origins)
    if not origins:                                    # last origin gone ⇒ cron too
        _remove_cron(run, env)
    print(f"hive origin: removed {repo}", file=sys.stderr)
    return EX_OK


def _origin(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """Verb dispatch: a target carrying `/` or `github.com` is a repo ⇒ LINK; else
    the reserved words sync|ls|rm. Collision-free by construction — repo args
    always carry `/`."""
    target = args.target
    if "/" in target or "github.com" in target.lower():
        return _origin_link(args, run=run, out=out, env=env, ask=ask)
    if target == "sync":
        return _origin_sync(args, run=run, out=out, env=env, ask=ask)
    if target == "ls":
        return _origin_ls(args, run=run, out=out, env=env, ask=ask)
    if target == "rm":
        return _origin_rm(args, run=run, out=out, env=env, ask=ask)
    print(f"hive origin: {target!r} is neither owner/repo (link) nor one of "
          "sync|ls|rm — repo args always carry '/'", file=sys.stderr)
    return EX_USAGE


def _connect(args, *, run: Run, out: TextIO, env: Mapping[str, str], ask) -> int:
    """Print the teammate's transport-registration one-liner. Transport ONLY: the
    per-repo handshake (hive_init) stays agent-driven over MCP."""
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


_HANDLERS: dict[str, Callable[..., int]] = {
    "up": _up,
    "down": _down,
    "logs": _logs,
    "nuke": _nuke,
    "status": _status,
    "token": _token,
    "revoke": _revoke,
    "tokens": _tokens,
    "connect": _connect,
    "origin": _origin,
}

# Verbs that never touch compose (pure local prints) skip the tenant gate.
_LOCAL_VERBS: frozenset[str] = frozenset({"connect"})


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
    env = os.environ if env is None else env
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
    sub.add_parser("status", help="server health, tunnel state + URL, seat count")
    p_token = sub.add_parser("token", help="mint a per-seat token (printed ONCE to stdout)")
    p_token.add_argument("seat", help="agent-seat identity, e.g. alice-laptop (one per seat)")
    p_revoke = sub.add_parser("revoke", help="revoke a seat's token (next request → 401)")
    p_revoke.add_argument("seat", help="the seat label to revoke")
    sub.add_parser("tokens", help="list provisioned seat labels (never the tokens)")
    sub.add_parser("connect", help="print the teammate's `claude mcp add` line (transport only)")
    p_origin = sub.add_parser(
        "origin",
        help="GitHub credit loop: `origin <owner/repo>` links (config + hourly cron "
             "+ 90d backfill); `origin sync|ls|rm` operate on linked origins")
    p_origin.add_argument(
        "target",
        help="owner/repo or github.com URL to LINK (repo args always carry '/'); "
             "or one of the reserved words sync|ls|rm")
    p_origin.add_argument("extra", nargs="?", default=None,
                          help="the repo for `origin rm <owner/repo>`")
    p_origin.add_argument("--lookback", type=int, default=None,
                          help=f"scan window in days (sync default "
                               f"{originctl.DEFAULT_LOOKBACK_DAYS}; link backfill "
                               f"{originctl.FIRST_SYNC_LOOKBACK_DAYS})")
    p_origin.add_argument("--token-stdin", action="store_true",
                          help="read the GitHub token from stdin (outranks env/gh)")
    p_origin.add_argument("--no-cron", action="store_true",
                          help="skip installing the hourly cron line")
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
