"""run_http_dual — the warm HTTP daemon: a two-door endpoint in front of HiveMCPServer.

The natural generalization of ``run_stdio``: stdio is "one process, one identity"; this is
"one process, PER-AGENT-SESSION identity." Auth is a property of the listening SOCKET, not a
config mode: ``run_http_dual`` binds a tokenless LOOPBACK door and a token-required TUNNEL
door — each on its own bind address, because only the deployment knows how either is
reached — on ONE server / ONE lock / ONE limiter. On the tunnel door the Bearer is verified BEFORE ``handle()`` (an absent/unknown token
never touches recall/write — INV-1 → 401); on the loopback door there is no token. AUTH and
IDENTITY are orthogonal: on BOTH doors the per-request identity is resolved as
``X-Hive-Agent-Id`` → echoed ``Mcp-Session-Id`` → ``"local"`` and becomes the ``proposed_by``
via a per-request ``ServerIdentity`` threaded through ``handle(identity=…)``. The token GATES
the tunnel door but is NEVER the identity (so token/engineer count cannot leak into promotion);
the transport stays ignorant of tool internals (D1).

``GET /healthz`` answers 200 on both doors, pre-auth and content-free — the liveness signal
a supervisor in FRONT of the socket can read (the image HEALTHCHECK runs inside the container
and is invisible there). Every other GET stays a 405.

Channel separation: auth/transport outcomes are HTTP status codes (401/403/405/202);
protocol/handler errors are JSON-RPC errors INSIDE a 200 — the two never mix. The shared
``sqlite3.Connection`` and the embedder are not thread-safe, so ALL handler execution (incl.
the ``verify`` DB read) is serialized under one global lock; WAL read-concurrency is traded
for simplicity (escape valve: WAL read-concurrency). The daemon never crashes on a bad request
(INV-3) — the HTTP analog of "the stdio loop never crashes on a bad line".

Stdlib only (``http.server``) — zero new dependencies. The endpoint contract lives in
``_build_handler`` (unit-tested against a real loopback server on 127.0.0.1:0); ``run_http_dual``
is the thin, blocking bind+serve wrapper.

Part S adds the two self-defense belts a tunnel-exposed endpoint
needs, both keyword-only and DEFAULT-OFF (AC6 — every existing call site is unchanged):
a body cap that 413s on the DECLARED Content-Length before any body byte is read (the
cheapest flood dies first, pre-auth), and a per-token limiter that 429s a single verified
label post-auth (D3 — keyed on identity, never a forwarded IP) with ``Retry-After``.
Both are HTTP-status outcomes, consistent with the 401/403/405/202 channel doctrine.

The census-webhook nudge (D6) is the one path resolved before the bearer gate: with
``webhook_secret`` set — ``run_http_dual`` threads it to the TUNNEL door alone —
``POST /census-webhook`` is authenticated by a constant-time HMAC-SHA256 of the raw body
vs ``X-Hub-Signature-256`` (``sha256=<hex>``): 204 + ``webhook_nudge()`` on match, 401 on
mismatch. A wake-up, never a data channel: the payload is NEVER parsed and the branch
holds no server/store handle, so the route is structurally incapable of mutation — a
forged webhook buys at worst one extra fetch of true remote state. Unset secret ⇒ the
branch is dead code and the door is byte-identical to the pre-webhook build.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import uuid
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from hive.app.mcp_server import (
    HiveMCPServer,
    MCPRequest,
    MCPResponse,
    ServerIdentity,
    _err,
)
from hive.app.rate_limit import TokenBucketLimiter

_log = logging.getLogger("hive.app.http_server")

_JSON = "application/json"
_DEFAULT_MAX_BODY = 1 << 20  # 1 MiB — generous for JSON-RPC tool calls (gbrain parity)
_AGENT_ID_HEADER = (
    "X-Hive-Agent-Id"  # explicit per-session identity (highest precedence)
)
_SESSION_ID_HEADER = (
    "Mcp-Session-Id"  # server-minted at initialize; echoed by conforming clients
)
_LOCAL_AGENT_ID = "local"  # the residual identity bucket (no header, no session id)
_HEALTH_PATH = "/healthz"  # the pre-auth liveness probe (both doors)


def _bearer(headers: Message) -> Optional[str]:
    """Extract the bearer token from an ``Authorization`` header, or None. The scheme is
    matched case-insensitively (RFC 7235); the token is taken verbatim. // O(1)."""
    raw: str = headers.get("Authorization") or ""
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    tok = parts[1].strip()
    return tok or None


def _asserted_agent_id(headers: Message) -> Optional[str]:
    """The explicit per-session caller identity from ``X-Hive-Agent-Id``, or None if
    absent/blank. Transport-level (never a TOOL argument, so INV-2 holds) and the reliable
    override the bench harness / a client's env path populates. // O(1)."""
    val: str = (headers.get(_AGENT_ID_HEADER) or "").strip()
    return val or None


def _session_id(headers: Message) -> Optional[str]:
    """The echoed server-minted ``Mcp-Session-Id`` (the provider-agnostic per-session
    identity for a conforming client), or None. Best-effort by design: structurally absent
    on request #1 (the ``initialize`` that mints it) and on a client that does not echo it —
    such a caller falls through to ``"local"`` and is caught by the health hint. // O(1)."""
    val: str = (headers.get(_SESSION_ID_HEADER) or "").strip()
    return val or None


def _resolve_identity(headers: Message) -> str:
    """The per-agent-session identity, resolved the SAME way on BOTH doors:
    explicit ``X-Hive-Agent-Id`` → echoed ``Mcp-Session-Id`` → the ``"local"`` bucket.
    The token (tunnel door) only AUTHENTICATES — it is NEVER the identity, so the engineer /
    token count cannot leak into promotion (the solo ≡ team invariant). A missing identity is
    never a 400: it floors to ``"local"``. // O(1)."""
    return _asserted_agent_id(headers) or _session_id(headers) or _LOCAL_AGENT_ID


def _mint_session_id() -> str:
    """A fresh opaque session id (visible-ASCII per the MCP spec) the server assigns on the
    ``initialize`` response; conforming clients echo it on every later request. No session
    store — it is read back purely as the per-session identity. // O(1)."""
    return uuid.uuid4().hex


def _build_handler(
    server: HiveMCPServer,
    verify: Callable[[str], Optional[str]],
    lock: threading.Lock,
    *,
    limiter: Optional[TokenBucketLimiter] = None,
    max_body_bytes: int = _DEFAULT_MAX_BODY,
    auth_required: bool = True,
    webhook_secret: str = "",
    webhook_nudge: Optional[Callable[[], None]] = None,
) -> type:
    """Build the ``BaseHTTPRequestHandler`` subclass that enforces the endpoint contract.
    Factored out of ``run_http_dual`` so the contract is unit-testable against a real loopback
    ``ThreadingHTTPServer`` on an ephemeral port (``run_http_dual`` only binds + serve_forever).
    The Part S belts are keyword-only and defaulted OFF/generous (``limiter=None`` ⇒ no
    429 path exists; AC6). ``limiter.check`` runs under the SAME global lock as ``verify``
    — the limiter has no internal locking by contract. The deliberate mutations (skip the
    ``verify``-None → 401 guard; drop the 413/429 guards; invert the webhook HMAC gate)
    live in ``do_POST``.

    ``webhook_secret`` (default "" ⇒ the branch is dead code, the door byte-identical)
    arms the D6 census-webhook nudge: ``POST /census-webhook`` is resolved after
    body-drain + the Origin guard and BEFORE the bearer gate by a constant-time
    HMAC-SHA256 of the raw body vs ``X-Hub-Signature-256`` — 204 + ``webhook_nudge()``
    on match (the sync loop's wake ``Event.set``; ``None`` ⇒ the wake is a no-op), 401
    on mismatch. The payload is never parsed.

    ``auth_required`` picks the DOOR (auth is a property of the listening socket, not a config
    mode). ``True`` (default — the TUNNEL door): the Bearer is verified, an absent/unknown
    token never reaches ``handle()`` (INV-1 → 401). ``False`` (the LOOPBACK door): tokenless —
    no 401, ``verify`` is never consulted. AUTH and IDENTITY are orthogonal: on BOTH doors the
    per-request identity is ``_resolve_identity`` (``X-Hive-Agent-Id`` → echoed
    ``Mcp-Session-Id`` → ``"local"``); the token GATES the tunnel door but is NEVER the
    identity, and a missing identity is floored to ``"local"`` (never a 400)."""

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive; every body is drained so it is safe
        server_version = "hive-mcp/1.0"

        # ── response primitives (every reply sets Content-Length — HTTP/1.1 framing) ──
        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str = _JSON,
            extra: Optional[dict[str, str]] = None,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(
            self, code: int, obj: dict[str, Any], extra: Optional[dict[str, str]] = None
        ) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), _JSON, extra)

        def _declared_length(self) -> int:
            """The DECLARED Content-Length (0 on absent/malformed) — reads no body byte,
            so the body cap can reject on it before any allocation. // O(1)."""
            try:
                return int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                return 0

        def _read_body(self) -> bytes:
            """Drain exactly Content-Length bytes (keep-alive correctness even on a reject)."""
            n = self._declared_length()
            return self.rfile.read(n) if n > 0 else b""

        # ── method routing ───────────────────────────────────────────────────────
        def do_GET(self) -> None:
            # /healthz — the liveness signal an EXTERNAL supervisor can read: a reverse
            # proxy, an orchestrator's readiness gate, an uptime monitor. The image's
            # HEALTHCHECK runs INSIDE the container and is invisible to anything in front
            # of the socket, so a fronted deployment otherwise has no honest signal and
            # must guess from a raw TCP connect. Pre-auth on BOTH doors by construction
            # (a prober holds no seat token, and the bearer gate lives in do_POST), and
            # deliberately content-free: this branch holds no server/store handle, so it
            # cannot become a data channel. The only fact it discloses — that something
            # answers here — the 401 on POST already discloses.
            if self.path == _HEALTH_PATH:
                return self._json(200, {"status": "ok"})
            # anything else: no SSE stream offered → 405 (spec)
            self._json(405, {"error": "method_not_allowed"}, extra={"Allow": "POST"})

        def do_DELETE(self) -> None:  # no sessions to tear down → 405 (spec)
            self._json(405, {"error": "method_not_allowed"}, extra={"Allow": "POST"})

        def do_POST(self) -> None:
            try:
                # (0) body cap (413, AC5): reject on the DECLARED length BEFORE the body
                # is read into memory and before verify/parse — the cheapest flood dies
                # first. The unread body makes the connection unreusable, so the client
                # is told to close (send_header('Connection','close') also flags the
                # server side). ← dropping this return is a deliberate mutation.
                if self._declared_length() > max_body_bytes:
                    return self._json(
                        413,
                        {"error": "payload_too_large"},
                        extra={"Connection": "close"},
                    )
                body = self._read_body()  # drain first (keep-alive safe)
                # (1) Origin guard (spec MUST, DNS-rebinding): a browser is never legitimate.
                if self.headers.get("Origin") is not None:  # INV-4
                    return self._json(403, {"error": "forbidden_origin"})
                # (2) census-webhook nudge (D6; tunnel door only — run_http_dual threads the
                # secret there alone), resolved BEFORE the bearer gate so a forwarder needs
                # no hive token, but never ungated: constant-time HMAC-SHA256 of the raw
                # body vs X-Hub-Signature-256 (INV-1 — auth still precedes any action). A
                # wake-up, never a data channel: the body is never parsed (its bytes exist
                # only as HMAC input) and the branch holds no server/store handle — only
                # the sync loop's wake callable — so a forged signature buys a 401 and a
                # replayed valid one at most one extra fetch of true remote state. Unset
                # secret ⇒ dead branch (the door is byte-identical).
                if self.path == "/census-webhook" and webhook_secret:
                    provided = self.headers.get("X-Hub-Signature-256") or ""
                    expected = (
                        "sha256="
                        + hmac.new(
                            webhook_secret.encode("utf-8"), body, hashlib.sha256
                        ).hexdigest()
                    )
                    if not hmac.compare_digest(  # ← inverting/skipping this
                        expected.encode("utf-8"),  #   gate is a deliberate mutation
                        provided.encode("utf-8"),
                    ):
                        return self._json(401, {"error": "unauthorized"})
                    if webhook_nudge is not None:
                        webhook_nudge()
                    return self._send(204, b"")
                # (3) AUTH gate (per-door) vs IDENTITY (shared, per-agent-session) — orthogonal.
                # The token GATES the tunnel door; it is NEVER the identity (else token/engineer
                # count would leak into promotion, breaking solo≡team). Identity resolves the
                # SAME way on both doors: X-Hive-Agent-Id → echoed Mcp-Session-Id → "local"
                # (a missing identity floors to "local", never a 400). The 429 throttle keys on
                # that resolved label (D3 — no forwarded-IP trust), under the same lock (the
                # limiter has no internal lock).
                label = _resolve_identity(self.headers)
                rl = None
                if auth_required:  # tunnel door: verify the Bearer
                    tok = _bearer(self.headers)
                    with lock:
                        allowed = (verify(tok) is not None) if tok else False
                        rl = (
                            limiter.check(label)
                            if (limiter is not None and allowed)
                            else None
                        )
                    if not allowed:  # ← skipping this guard is the mutation
                        return self._json(401, {"error": "unauthorized"})
                else:  # loopback door: tokenless, never 401
                    if limiter is not None:
                        with lock:
                            rl = limiter.check(label)
                if rl is not None and not rl.allowed:
                    return self._json(
                        429,
                        {"error": "rate_limited"},
                        extra={"Retry-After": str(rl.retry_after_s)},
                    )
                # (4) parse JSON-RPC with the SAME guards as run_stdio
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as e:
                    return self._json(
                        200,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": f"parse error: {e}"},
                        },
                    )
                if not isinstance(payload, dict):  # batch array / bare scalar
                    return self._json(
                        200,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {
                                "code": -32600,
                                "message": "invalid request: expected a JSON object",
                            },
                        },
                    )
                # (5) notification / response (no id) → 202 Accepted, no body (spec MUST)
                if "id" not in payload:
                    return self._send(202, b"")
                # (6) request → handle under the VERIFIED identity; single JSON response
                raw_params = payload.get("params")
                params = raw_params if isinstance(raw_params, dict) else {}
                req = MCPRequest(
                    id=payload.get("id"),
                    method=payload.get("method", ""),
                    params=params,
                )
                ident = ServerIdentity(
                    tenant_id=server.identity.tenant_id, agent_id=label
                )
                # Mcp-Session-Id mint: an `initialize` response carries a fresh server-minted
                # session id; a conforming client echoes it on later requests, where
                # _resolve_identity reads it back as the per-session identity (no session store).
                extra = (
                    {_SESSION_ID_HEADER: _mint_session_id()}
                    if req.method == "initialize"
                    else None
                )
                try:
                    with lock:
                        resp = server.handle(req, identity=ident)
                except (
                    Exception
                ) as e:  # INV-3: handler fault → JSON-RPC error inside a 200
                    _log.error(
                        "http.handle_crash",
                        extra={
                            "event": "http.handle_crash",
                            "error_type": type(e).__name__,
                        },
                        exc_info=True,
                    )
                    resp = MCPResponse(
                        id=req.id,
                        error=_err(-32603, f"internal error: {type(e).__name__}"),
                    )
                self._send(200, resp.to_json().encode("utf-8"), _JSON, extra)
            except Exception as e:  # INV-3: transport fault → HTTP 500, daemon survives
                _log.error(
                    "http.transport_crash",
                    extra={
                        "event": "http.transport_crash",
                        "error_type": type(e).__name__,
                    },
                    exc_info=True,
                )
                try:
                    self._json(500, {"error": "internal_error"})
                except Exception:  # client already gone — swallow
                    pass

        def log_message(
            self, *args: Any
        ) -> None:  # silence the default stderr access log
            return

    return _Handler


def run_http_dual(
    server: HiveMCPServer,
    *,
    loopback_host: str,
    loopback_port: int,
    tunnel_host: str,
    tunnel_port: int,
    verify: Callable[[str], Optional[str]],
    lock: threading.Lock,
    limiter: Optional[TokenBucketLimiter] = None,
    max_body_bytes: int = _DEFAULT_MAX_BODY,
    webhook_secret: str = "",
    webhook_nudge: Optional[Callable[[], None]] = None,
) -> None:  # pragma: no cover — blocking serve
    """Bind BOTH doors on ONE server / ONE lock / ONE limiter: the tokenless LOOPBACK door
    (``auth_required=False``) and the token-required TUNNEL door (``auth_required=True``).
    The tunnel door serves on a daemon thread; the loopback door serves on THIS thread
    (blocking). Passing the SAME ``lock`` + SAME ``limiter`` into both ``_build_handler``
    calls is what keeps the single-writer serialization invariant (THEORY §5) holding across
    two listeners — no path constructs a per-listener lock, so ``check_same_thread=False``
    stays safe. The webhook nudge (D6) is threaded to the TUNNEL door ALONE — one port is
    forwarded, so that is the only door a forge can reach.

    Each door carries its OWN bind address. They are separate parameters because the two
    doors have opposite exposure requirements and only the deployment knows how they are
    reached: under compose the tokenless door must bind the container's interface for a
    host port map to reach it (its safety is the map's ``127.0.0.1:`` prefix), while a
    deployment with no such map — a container platform, a bare ``docker run``, a host
    process — has nothing to make an all-interfaces bind safe and must give the tokenless
    door a loopback address. A single shared ``host`` could not express both, so it silently
    exported whatever posture compose happened to need."""
    tunnel = ThreadingHTTPServer(
        (tunnel_host, tunnel_port),
        _build_handler(
            server,
            verify,
            lock,
            limiter=limiter,
            max_body_bytes=max_body_bytes,
            auth_required=True,
            webhook_secret=webhook_secret,
            webhook_nudge=webhook_nudge,
        ),
    )
    th = threading.Thread(target=tunnel.serve_forever, daemon=True)
    th.start()
    _log.info(
        "http.serving host=%s tunnel_port=%d auth_required=True",
        tunnel_host,
        tunnel_port,
    )
    loopback = ThreadingHTTPServer(
        (loopback_host, loopback_port),
        _build_handler(
            server,
            verify,
            lock,
            limiter=limiter,
            max_body_bytes=max_body_bytes,
            auth_required=False,
        ),
    )
    _log.info(
        "http.serving host=%s loopback_port=%d auth_required=False",
        loopback_host,
        loopback_port,
    )
    loopback.serve_forever()
