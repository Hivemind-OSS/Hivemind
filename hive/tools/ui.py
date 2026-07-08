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
from dataclasses import dataclass
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


def _action_arg(body: bytes) -> Optional[str]:
    """The lifecycle action, constrained to the two SAFE actions — anything else (incl. `reset`
    / `restore`) is None (→ 400). reset/restore are unreachable by construction, not hidden."""
    obj = _load_obj(body)
    if obj is None:
        return None
    action = obj.get("action")
    return action if action in ("up", "down") else None


def _upstream_failed() -> Response:
    """A docker/authctl/backup child exited nonzero → 502. Deliberately carries NO child stderr,
    so a token/secret/env value can never ride an error body (secret-safe). // O(1)."""
    return _json(502, {"error": "upstream_failed"})


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


def _h_lifecycle(body, *, run, env) -> Response:
    action = _action_arg(body)
    if action not in ("up", "down"):
        return _json(400, {"error": "bad_request"})        # unknown/absent action → 400, no child
    if action == "up":
        # loopback-only: the argv carries NEITHER --tunnel NOR --profile tunnel — a browser panel
        # must never open a public tunnel. (No health-wait: non-blocking; the status poll shows it.)
        proc = run(cli._compose("up", "-d", "--build", cli.SERVICE), env, capture=False)
    else:
        proc = run(cli._compose("down"), env, capture=False)   # PRESERVES the volume (no -v)
    if proc.returncode != 0:
        return _json(502, {"action": action, "ok": False, "error": "upstream_failed"})
    return _json(200, {"action": action, "ok": True})


def _h_logs(body, *, run, env) -> Response:
    # bounded, NON-blocking: --tail=200 and NO -f (a follow would hang the request forever).
    child = run(cli._compose("logs", "--tail=200", cli.SERVICE), env)
    if child.returncode != 0:
        return _upstream_failed()
    lines = (child.stdout or "").splitlines()
    return _json(200, {"lines": lines[-200:]})             # honor the ≤200-line budget


_Handler = Callable[..., Response]

# the (method, path) → handler dispatch table — EXACTLY the eight pinned routes, mirroring the
# one-registry-entry-per-option idiom of cli._HANDLERS. reset/restore are absent by construction.
_ROUTES: dict[tuple[str, str], _Handler] = {
    ("GET", "/"): _h_page,
    ("GET", "/api/status"): _h_status,
    ("GET", "/api/tokens"): _h_tokens_list,
    ("POST", "/api/tokens"): _h_tokens_mint,
    ("POST", "/api/tokens/revoke"): _h_tokens_revoke,
    ("POST", "/api/backup"): _h_backup,
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
