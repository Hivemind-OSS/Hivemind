"""ui — the operator-console control plane: a pure router + (Part 2) a loopback socket shell.

`handle_request(method, path, headers, body, *, run, env) -> Response` holds ALL endpoint logic
and touches no socket, so the entire same-origin JSON API is unit-testable through the injected
`run`/`env` seams with a FakeRun — exactly the split `hive/app/http_server.py` uses to factor
`_build_handler` out of `run_http_dual`. The router ORCHESTRATES the landed CLI seams
(`cli._probe_status` / `cli._exec_authctl` / `cli._exec_backup` / `cli._compose`) and reimplements
no docker/authctl logic; the status shape stays single-owned in `cli.StatusSnapshot`.

Security posture — the inverted MCP door doctrine. `hive/app/http_server` 403s any browser
(a browser is never a legitimate MCP client). Here the browser IS the legitimate client, so the
door ADMITS the same-origin browser and instead enforces a Host-header allowlist (DNS-rebinding
defense, all methods) + a same-origin Origin check (CSRF defense, state-changing POST). Auth
stays a property of the listening socket — loopback-only, tokenless — never a token/config mode.
Answer paths fail CLOSED (a foreign Host / cross-origin POST / oversized body is refused before
any child runs); the daemon never raises on a bad request (INV-3).

tools/ is operator-surface, not domain (THEORY Law 4), so http.server/webbrowser/threading are
permitted here. Stdlib only — zero new dependencies.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping, Optional

from hive.tools import cli, ui_page

_JSON = "application/json"
_HTML = "text/html; charset=utf-8"

# 64 KiB — generous for a seat name / {"action":…} JSON, tiny enough that a flood dies cheap.
# The declared-length cap has ONE owner here; the socket shell reads it to avoid draining a
# huge body, and the router returns the 413 on it.
MAX_BODY_BYTES = 64 * 1024

# The loopback hostnames the browser's URL bar (and thus its Host/Origin) may carry. A Host whose
# hostname is not one of these is a DNS-rebinding attempt against our loopback socket → refused.
_ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]")


@dataclass(frozen=True)
class Response:
    """The transport-agnostic reply the pure router returns; the socket shell renders it verbatim
    (status line + Content-Type + Content-Length, always). A frozen carrier — no transport
    headers leak into it; Allow/Connection are the shell's concern, derived from the status."""
    status: int
    body: bytes
    content_type: str = _JSON


def _json(status: int, obj: dict) -> Response:
    """A JSON Response with the body encoded once (its length is the Content-Length). // O(1)."""
    return Response(status, json.dumps(obj).encode("utf-8"), _JSON)


# ── header helpers (work on a plain dict in tests and on a case-insensitive Message live) ──


def _declared_length(headers: Mapping[str, str]) -> int:
    """The DECLARED Content-Length (0 on absent/malformed) — reads no body byte. // O(1)."""
    try:
        return int(headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return 0


def _hostname(headers: Mapping[str, str]) -> str:
    """The Host header's hostname with any :port stripped (bracketed IPv6 kept whole). // O(1)."""
    host = (headers.get("Host") or "").strip()
    if host.startswith("["):                               # [::1]:4173 → [::1]
        return host.split("]", 1)[0] + "]" if "]" in host else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def _host_ok(headers: Mapping[str, str]) -> bool:
    """The Host allowlist: its hostname must be a loopback name. A missing/foreign Host fails
    CLOSED (rebinding defense). // O(1)."""
    return _hostname(headers) in _ALLOWED_HOSTNAMES


def _origin_ok(headers: Mapping[str, str]) -> bool:
    """Same-origin check for a state-changing POST: an absent Origin is allowed (a same-origin
    fetch / curl); a present Origin must equal our own origin (`http://<Host>`), so a cross-origin
    browser POST is refused. Admits the legitimate same-origin browser. // O(1)."""
    origin = headers.get("Origin")
    if origin is None:
        return True
    return origin == "http://" + (headers.get("Host") or "").strip()


# ── request-body arg parsing (fail-fast, before any child) ─────────────────────


def _load_obj(body: bytes) -> Optional[dict]:
    """Parse a JSON object body, or None on malformed JSON / a non-object. // O(len)."""
    try:
        obj = json.loads(body or b"")
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _seat_arg(body: bytes) -> Optional[str]:
    """The non-blank `seat` string, or None (→ 400 before any authctl child)."""
    obj = _load_obj(body)
    if obj is None:
        return None
    seat = obj.get("seat")
    return seat.strip() if isinstance(seat, str) and seat.strip() else None


_LIFECYCLE_ACTIONS = ("up", "down", "tunnel-up", "tunnel-down")


def _action_arg(body: bytes) -> Optional[str]:
    """The lifecycle action, constrained to the four SAFE actions (start/stop the daemon or the
    tunnel sidecar). Anything else — incl. `reset` / `restore` — is None (→ 400): reset is absent
    by construction and restore is its OWN guarded route, neither reachable through this channel."""
    obj = _load_obj(body)
    if obj is None:
        return None
    action = obj.get("action")
    return action if action in _LIFECYCLE_ACTIONS else None


def _name_arg(body: bytes) -> Optional[str]:
    """The non-blank `name` string from a JSON-object body, else None (→ 400 before any child)."""
    obj = _load_obj(body)
    if obj is None:
        return None
    name = obj.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


# A restorable backup name's shell-inert charset: letters / digits / dot / underscore / hyphen with
# a `.db` suffix (the machine backup-name shape, `hive-<stamp>.db`). It admits NO shell metacharacter
# (`; | & $ ( ) < > * ?`, whitespace, quotes …), so a validated name cannot break out of the
# `cp … && rm …` sh -c on the destructive restore path — the belt behind the shlex.quote suspenders.
_SAFE_BACKUP_NAME = re.compile(r"[A-Za-z0-9._-]+\.db")


def _is_safe_backup_name(name: str) -> bool:
    """A restorable name must be a BARE BASENAME with a shell-inert charset: no path separator, no
    `..`, not absolute, equal to its own basename, AND matching `^[A-Za-z0-9._-]+\\.db$` so no shell
    metacharacter survives into the destructive `cp` sh -c. Path traversal ("../shared.db", "/etc/x",
    "a/b") and injection ("a;b.db", "a b.db", "$(x).db", "a|b.db") are refused HERE — before the
    whitelist membership check and before any child touches the shared data volume. ← widening this
    charset to admit a shell metacharacter is a deliberate mutation (shell injection on the restore
    cp). // O(len)."""
    if "/" in name or "\\" in name or ".." in name or os.path.isabs(name):
        return False
    if not _SAFE_BACKUP_NAME.fullmatch(name):
        return False
    return name == os.path.basename(name)


def _upstream_failed() -> Response:
    """A docker/authctl/backup child exited nonzero → 502. Deliberately carries NO child stderr,
    so a token/secret/env value can never ride an error body (secret-safe). // O(1)."""
    return _json(502, {"error": "upstream_failed"})


# ── single-flight lifecycle + detached workers ────────────────────────────────
# The slow docker work (up / down / tunnel / restore) must NEVER block the loopback response: the
# browser hung once because the lifecycle handler ran `docker compose up -d --build` SYNCHRONOUSLY,
# rebuilding + bouncing a healthy stack while the request waited. So the discipline is — VALIDATE
# synchronously (fail fast to 400/409), then run the child on a DETACHED worker and return at once
# ({"status":"starting"|"stopping"|"restoring"}); the page's /api/status poll reflects the outcome.
# A single module-level flag, guarded by a lock, admits ONE such op at a time (a shared data volume
# must not see concurrent lifecycle/restore ops); a second concurrent request is refused 409.

_LIFECYCLE_LOCK = threading.Lock()   # guards _op_in_flight — the check-and-set must be atomic
_op_in_flight = False                # True while a lifecycle/tunnel/restore worker holds the slot


def _try_claim() -> bool:
    """Atomically claim the single lifecycle slot; return False WITHOUT claiming when an op already
    holds it (the caller then returns 409). ← always returning True (dropping the non-blocking
    claim) is a deliberate mutation: concurrent docker ops would race the data volume. // O(1)."""
    global _op_in_flight
    with _LIFECYCLE_LOCK:
        if _op_in_flight:
            return False
        _op_in_flight = True
        return True


def _release() -> None:
    """Free the slot — the worker calls this in its finally. Idempotent (set-False). // O(1)."""
    global _op_in_flight
    with _LIFECYCLE_LOCK:
        _op_in_flight = False


def _spawn(target: Callable[[], None]) -> None:
    """Launch `target` on a detached daemon thread so the slow docker child runs OFF the request
    thread and the loopback response returns at once. A module-level seam a test runs inline. // O(1)."""
    threading.Thread(target=target, daemon=True).start()


def _dispatch(work: Callable[[], None]) -> None:
    """Run `work` on a detached worker that RELEASES the claimed slot when it finishes (success or
    not). The caller MUST already hold the slot (via `_try_claim`). If the spawn itself fails, the
    slot is freed here so it can never strand every later op at 409. // O(1)."""
    def _guarded() -> None:
        try:
            work()
        finally:
            _release()
    try:
        _spawn(_guarded)
    except Exception:
        _release()
        raise


def _lifecycle_plan(action: str) -> tuple[list[str], str]:
    """Map a validated lifecycle action to its (compose argv, status word). Plain Start does NOT
    --build — it must not rebuild + bounce a healthy stack (the bug). down preserves the volume
    (no -v). tunnel-up starts ONLY the sidecar behind the tunnel profile; tunnel-down stops only
    it. // O(1)."""
    if action == "up":
        return cli._compose("up", "-d", cli.SERVICE), "starting"
    if action == "down":
        return cli._compose("down"), "stopping"
    if action == "tunnel-up":
        return cli._compose("up", "-d", "ngrok", profile=cli.TUNNEL_PROFILE), "starting"
    return cli._compose("stop", "ngrok"), "stopping"       # tunnel-down (hive-server untouched)


# ── the endpoint handlers (each returns a Response; body is the raw POST bytes) ──


def _h_page(body, *, run, env) -> Response:
    return Response(200, ui_page.PAGE_HTML.encode("utf-8"), _HTML)


def _h_status(body, *, run, env) -> Response:
    # serialize the SINGLE-OWNER StatusSnapshot — never scraped from `hive status` text.
    snap = cli._probe_status(run, env)
    return _json(200, {"server": snap.server, "healthy": snap.healthy,
                       "tunnel_on": snap.tunnel_on, "tunnel_url": snap.tunnel_url,
                       "seats": snap.seats})


def _h_tokens_list(body, *, run, env) -> Response:
    child = cli._exec_authctl(run, env, "list")
    if child.returncode != 0:
        return _upstream_failed()
    seats = [ln for ln in (child.stdout or "").splitlines() if ln.strip()]
    return _json(200, {"seats": seats})                    # LABELS only, never a token value


def _h_tokens_mint(body, *, run, env) -> Response:
    seat = _seat_arg(body)
    if seat is None:
        return _json(400, {"error": "bad_request"})        # fail-fast: no child on a blank seat
    child = cli._exec_authctl(run, env, "create", seat)
    if child.returncode != 0:
        return _upstream_failed()
    # the plaintext token — its ONLY appearance, in its own one-time response body.
    return _json(200, {"seat": seat, "token": (child.stdout or "").strip()})


def _h_tokens_revoke(body, *, run, env) -> Response:
    seat = _seat_arg(body)
    if seat is None:
        return _json(400, {"error": "bad_request"})
    child = cli._exec_authctl(run, env, "revoke", seat)
    if child.returncode != 0:
        return _upstream_failed()
    return _json(200, {"seat": seat, "revoked": True})


def _h_backup(body, *, run, env) -> Response:
    child = cli._exec_backup(run, env)
    if child.returncode != 0:
        return _upstream_failed()
    return _json(200, {"path": (child.stdout or "").strip()})


# The in-container lister for the in-volume backups dir: emits ONE JSON array (name/size/mtime per
# *.db), newest-first. Run via `python -c` so we never parse `ls` fragilely; json/os/glob are stdlib.
_BACKUPS_LISTER = "\n".join((
    "import json, os, glob",
    "d = '/data/backups'",
    "rows = []",
    "for p in glob.glob(os.path.join(d, '*.db')):",
    "    try:",
    "        st = os.stat(p)",
    "    except OSError:",
    "        continue",
    "    rows.append({'name': os.path.basename(p), 'size': st.st_size, 'mtime': int(st.st_mtime)})",
    "rows.sort(key=lambda r: r['mtime'], reverse=True)",
    "print(json.dumps(rows))",
))


def _list_backups(run, env) -> Optional[list]:
    """The in-volume backups listing (newest-first) via the in-container lister, or None on any
    child/parse failure. SINGLE-OWNED so /api/backups and the restore whitelist read the SAME source
    — a name is restorable IFF it is a member of THIS listing. // O(1) + the child."""
    child = run(cli._compose("exec", "-T", cli.SERVICE, "python", "-c", _BACKUPS_LISTER), env)
    if child.returncode != 0:
        return None
    try:
        rows = json.loads(child.stdout or "")
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return rows if isinstance(rows, list) else None


def _h_backups(body, *, run, env) -> Response:
    rows = _list_backups(run, env)
    if rows is None:
        return _upstream_failed()
    return _json(200, {"backups": rows})                   # newest-first, as the lister emits


def _restore_worker(name: str, run, env) -> None:
    """The detached restore mechanism: stop the daemon (release WAL locks), copy the chosen IN-VOLUME
    snapshot over /data/shared.db (clearing stale WAL sidecars) in a throwaway container, then restart
    WITHOUT rebuilding. Mirrors cli._restore, but the source is already inside the volume. `name` is a
    whitelisted bare basename (validated before dispatch) AND shell-quoted into the cp below, so it
    cannot break out of the sh -c on this destructive path even if validation is ever loosened
    (defense-in-depth). A failed step leaves the daemon down for the operator to see in /api/status.
    // O(db size) + a restart."""
    if run(cli._compose("down"), env, capture=False).returncode != 0:
        return                                             # stop failed → nothing overwritten
    safe = shlex.quote(name)                               # never interpolate a raw name into the sh -c
    cp = run(["docker", "run", "--rm", "--entrypoint", "sh", "-v", "hive-data:/data", cli.IMAGE,
              "-c", f"cp /data/backups/{safe} /data/shared.db && "
                    "rm -f /data/shared.db-wal /data/shared.db-shm"], env)
    if cp.returncode != 0:
        return                                             # copy failed → daemon stays down
    run(cli._compose("up", "-d", cli.SERVICE), env, capture=False)


def _h_restore(body, *, run, env) -> Response:
    name = _name_arg(body)
    if name is None:
        return _json(400, {"error": "bad_request"})        # blank/missing name → 400, no child
    # (1) PATH-TRAVERSAL + INJECTION GUARD — pure, BEFORE any child: a non-basename / traversal /
    #     absolute / shell-metacharacter name is refused with zero children. ← weakening this to
    #     accept such a name is a deliberate mutation (traversal / shell injection into the volume).
    if not _is_safe_backup_name(name):
        return _json(400, {"error": "unknown_backup"})
    # (2) WHITELIST — the name must be a member of the CURRENT in-volume listing; a well-formed but
    #     unknown name is refused before the destructive path. ← accepting a non-member is a
    #     deliberate mutation (restore an arbitrary / nonexistent file).
    rows = _list_backups(run, env)
    if rows is None:
        return _upstream_failed()
    if name not in {r.get("name") for r in rows if isinstance(r, dict)}:
        return _json(400, {"error": "unknown_backup"})
    # (3) single-flight: a concurrent lifecycle/restore op → 409 (no snapshot, no overwrite).
    if not _try_claim():
        return _json(409, {"error": "operation_in_progress"})
    # (4) AUTO SAFETY-SNAPSHOT FIRST (synchronous): the pre-restore store must stay recoverable. If
    #     it fails, ABORT — do NOT overwrite. ← overwriting before a SUCCESSFUL snapshot is a
    #     deliberate mutation (an un-snapshotted store is irrecoverably replaced).
    if cli._exec_backup(run, env).returncode != 0:
        _release()
        return _json(500, {"error": "safety_snapshot_failed"})
    # (5) the destructive down→cp→up runs on a DETACHED worker; /api/status shows the health return.
    _dispatch(lambda: _restore_worker(name, run, env))
    return _json(202, {"action": "restore", "status": "restoring"})


def _h_lifecycle(body, *, run, env) -> Response:
    # VALIDATE synchronously — an unknown/absent action is 400 before any claim or child (reset is
    # absent by construction; restore is its OWN guarded route, never a lifecycle action here).
    action = _action_arg(body)
    if action is None:
        return _json(400, {"error": "bad_request"})
    if action == "tunnel-up":
        # public exposure: gate the NGROK secrets from ENV ONLY (never the request body), before
        # any claim or child — mirrors cli._up's fail-fast. ← dropping this gate is a deliberate
        # mutation (a public tunnel could start with no auth env configured).
        missing = [k for k in ("NGROK_AUTHTOKEN", "NGROK_DOMAIN")
                   if not (env.get(k) or "").strip()]
        if missing:
            return _json(400, {"error": "missing_secrets", "missing": missing})
    argv, status = _lifecycle_plan(action)
    # single-flight: a second concurrent lifecycle/tunnel/restore op is refused 409, no child.
    if not _try_claim():
        return _json(409, {"error": "operation_in_progress"})
    # the slow docker child runs on a DETACHED worker; return 202 at once (the status poll shows the
    # outcome). The browser never hangs on `docker compose` again.
    _dispatch(lambda: run(argv, env, capture=False))
    return _json(202, {"action": action, "status": status})


def _h_logs(body, *, run, env) -> Response:
    # bounded, NON-blocking: --tail=200 and NO -f (a follow would hang the request forever).
    child = run(cli._compose("logs", "--tail=200", cli.SERVICE), env)
    if child.returncode != 0:
        return _upstream_failed()
    lines = (child.stdout or "").splitlines()
    return _json(200, {"lines": lines[-200:]})             # honor the ≤200-line budget


_Handler = Callable[..., Response]

# the (method, path) → handler dispatch table — EXACTLY the ten pinned routes, mirroring the
# one-registry-entry-per-option idiom of cli._HANDLERS. RESET is absent by construction; restore is
# present but guarded (bare-basename path-traversal whitelist + a safety snapshot before overwrite).
_ROUTES: dict[tuple[str, str], _Handler] = {
    ("GET", "/"): _h_page,
    ("GET", "/api/status"): _h_status,
    ("GET", "/api/tokens"): _h_tokens_list,
    ("POST", "/api/tokens"): _h_tokens_mint,
    ("POST", "/api/tokens/revoke"): _h_tokens_revoke,
    ("POST", "/api/backup"): _h_backup,
    ("GET", "/api/backups"): _h_backups,
    ("POST", "/api/restore"): _h_restore,
    ("POST", "/api/lifecycle"): _h_lifecycle,
    ("GET", "/api/logs"): _h_logs,
}


def _methods_for_path(path: str) -> list[str]:
    """The methods a known path answers (for the 405 Allow header). Single-owned off _ROUTES so
    the router's 405 decision and the shell's Allow header can never disagree. // O(routes)."""
    return sorted({m for (m, p) in _ROUTES if p == path})


def handle_request(method: str, path: str, headers: Mapping[str, str], body: bytes,
                   *, run: cli.Run, env: Mapping[str, str]) -> Response:
    """The whole endpoint contract as a pure function of the request + the injected seams. Every
    guard fails CLOSED and is a NAMED mutation marker pinned by a test in test_ui.py. // O(1) +
    the child's work."""
    try:
        # (1) Host allowlist — all methods. A foreign/missing Host (DNS-rebinding) is refused
        #     before anything else. ← dropping this return is a deliberate mutation (rebinding open).
        if not _host_ok(headers):
            return _json(403, {"error": "forbidden_host"})
        # (2) body cap — reject on the DECLARED Content-Length before parse/child (the flood cap).
        #     ← dropping this return is a deliberate mutation (unbounded body accepted).
        if _declared_length(headers) > MAX_BODY_BYTES:
            return _json(413, {"error": "payload_too_large"})
        # (3) same-origin Origin on a state-changing POST — a cross-origin browser POST is refused;
        #     the same-origin browser is admitted (the inverted doctrine).
        #     ← dropping this return is a deliberate mutation (CSRF / cross-origin open).
        if method == "POST" and not _origin_ok(headers):
            return _json(403, {"error": "forbidden_origin"})
        # (4) dispatch — unknown path 404, known path + wrong method 405 (Allow rides the shell).
        handler = _ROUTES.get((method, path))
        if handler is None:
            if not _methods_for_path(path):
                return _json(404, {"error": "not_found"})   # ← mutation: wrong code on an unknown path
            return _json(405, {"error": "method_not_allowed"})  # ← mutation: wrong code on a bad method
        return handler(body, run=run, env=env)
    except Exception:
        # never-raise (INV-3): a handler/parse fault becomes a 500 answer; the daemon survives.
        # ← dropping this catch is a deliberate mutation (a bad request would kill the server).
        return _json(500, {"error": "internal_error"})


# ── the transport shell: adapt the pure router to a loopback BaseHTTPRequestHandler ──
# Factored out exactly as hive/app/http_server.py factors _build_handler out of run_http_dual,
# so the endpoint contract stays unit-testable against the pure function AND a real socket.

_LOOPBACK_BINDS = ("127.0.0.1", "localhost", "::1")   # the only hosts serve_ui will bind


def _build_handler(run: cli.Run, env: Mapping[str, str]) -> type:
    """Build the `BaseHTTPRequestHandler` subclass that reads a request, delegates the WHOLE
    decision to the pure `handle_request`, and renders the returned `Response` verbatim
    (status + Content-Type + Content-Length always; Allow on a 405, Connection: close on a 413).
    The body cap's declared-length check is reused here to AVOID draining an over-cap body — the
    413 response itself is still the router's (single owner of the cap). All methods route through
    one dispatch, so a wrong method on a known path is the router's 405, not a stdlib 501."""

    class _UIHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"                  # keep-alive; every reply frames its length
        server_version = "hive-ui/1.0"

        def _emit(self, resp: Response) -> None:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            if resp.status == 405:                     # advertise the methods the path answers
                self.send_header("Allow", ", ".join(_methods_for_path(self.path.split("?", 1)[0])))
            if resp.status == 413:                     # the unread body makes the conn unreusable
                self.send_header("Connection", "close")
            self.end_headers()
            if resp.body and self.command != "HEAD":
                self.wfile.write(resp.body)

        def _dispatch(self) -> None:
            try:
                path = self.path.split("?", 1)[0]      # ignore any query string
                declared = _declared_length(self.headers)
                # over-cap: do NOT read the flood body; the router still 413s on the declared length.
                body = b"" if declared > MAX_BODY_BYTES else (
                    self.rfile.read(declared) if declared > 0 else b"")
                resp = handle_request(self.command, path, self.headers, body, run=run, env=env)
                self._emit(resp)
            except Exception:
                # transport-level never-raise (INV-3): the daemon survives any transport fault.
                # ← dropping this catch is a deliberate mutation (a bad request would kill it).
                try:
                    self._emit(_json(500, {"error": "internal_error"}))
                except Exception:
                    pass                               # client already gone — swallow

        do_GET = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch
        do_DELETE = _dispatch
        do_HEAD = _dispatch
        do_PATCH = _dispatch
        do_OPTIONS = _dispatch

        def log_message(self, *args) -> None:          # silence the access log (secret-safe, quiet)
            return

    return _UIHandler


def serve_ui(*, host: str = "127.0.0.1", port: int = 4173, run: Optional[cli.Run] = None,
             env: Optional[Mapping[str, str]] = None, open_browser: bool = True,
             server_factory: Callable[..., ThreadingHTTPServer] = ThreadingHTTPServer) -> int:
    """Bind the LOOPBACK socket and serve the operator console; return a sysexits code. A routable
    (non-loopback) host is REFUSED fail-FAST (EX_USAGE, NO bind) — the console exposes token
    mint/revoke, so it must never bind a routable interface (Law 6: boot fails fast). A bind
    OSError → EX_UNAVAILABLE; opening the browser is a best-effort side-channel that fails OPEN
    (a headless host just reads the printed URL); Ctrl-C → EX_OK (clean shutdown). // O(1) + serve."""
    run = run or cli.default_run
    env = cli._load_dotenv(cli._DOTENV, os.environ) if env is None else env
    if host not in _LOOPBACK_BINDS:
        print(f"hive ui: refusing to bind non-loopback host {host!r} — the operator console is "
              "loopback-only (127.0.0.1). No socket was opened.", file=sys.stderr)
        return cli.EX_USAGE                            # fail-fast: no bind on a routable host
    try:
        httpd = server_factory((host, port), _build_handler(run, env))
    except OSError as error:
        print(f"hive ui: cannot bind {host}:{port} — {error} "
              "(is the console already running, or the port in use?)", file=sys.stderr)
        return cli.EX_UNAVAILABLE
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"hive ui: operator console at {url} (loopback-only, tokenless; Ctrl-C to stop)",
          file=sys.stderr)
    if open_browser:
        try:
            webbrowser.open(url)                       # side-channel: fail OPEN (headless is fine)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("hive ui: stopped", file=sys.stderr)
    finally:
        httpd.server_close()
    return cli.EX_OK
