"""run_http_dual — the two-door endpoint contract in front of HiveMCPServer.

Drives the REAL handler (``_build_handler``) on a real loopback ``ThreadingHTTPServer`` at
127.0.0.1:0, with a SpyServer standing in for HiveMCPServer so the transport obligations are
tested in isolation. Auth is a property of the door (``auth_required``): the TUNNEL door
(default ``True``) verifies the Bearer BEFORE handle (INV-1 401); the LOOPBACK door
(``False``) is tokenless and never 401s. AUTH is orthogonal to IDENTITY: on BOTH doors the
per-request identity is resolved ``X-Hive-Agent-Id`` → echoed ``Mcp-Session-Id`` → ``"local"``,
and the token is NEVER the identity. Also covered: ``Mcp-Session-Id`` mint at ``initialize``,
202 notification, 405 GET, the pre-auth ``GET /healthz`` probe (200 on both doors, and no
hole in the bearer gate), 403 Origin, INV-3 robustness, and channel separation (protocol
errors ride a 200). The mutation (do_POST skips the ``verify``-None → 401 guard) reds the
missing-token test.

Part S extends the contract with the two transport belts, both
keyword-only and DEFAULT-OFF so every pre-existing test here doubles as the AC6
backward-compat proof: a ``limiter`` throttles one resolved identity with 429+``Retry-After``
(handle untouched, other identities unaffected), and ``max_body_bytes`` rejects an oversized
body with 413 BEFORE any body byte is read (raw-socket proof: a declared-but-never-sent
body would hang a server that drained first). Mutations: drop the 429 guard, drop
the 413 return, flip the cap comparison.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from email.message import Message
from http.server import ThreadingHTTPServer

import pytest

from hive.app.http_server import (
    _AGENT_ID_HEADER,
    _SESSION_ID_HEADER,
    _asserted_agent_id,
    _bearer,
    _build_handler,
    _resolve_identity,
    _session_id,
)
from hive.app.mcp_server import MCPResponse, ServerIdentity
from hive.app.rate_limit import TokenBucketLimiter

_RPC = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "hive_health", "arguments": {}},
    }
)


class _SpyServer:
    """Minimal HiveMCPServer stand-in: records the (req, identity) each handle() sees and
    returns a canned response; ``raise_on`` makes handle() raise for one method (INV-3)."""

    def __init__(self, *, raise_on=None) -> None:
        self.identity = ServerIdentity(tenant_id="default", agent_id="server-default")
        self.calls: list = []
        self._raise_on = raise_on

    def handle(self, req, *, identity=None):
        self.calls.append((req, identity))
        if self._raise_on is not None and req.method == self._raise_on:
            raise RuntimeError("boom")
        return MCPResponse(id=req.id, result={"ok": True, "method": req.method})


def _serve(spy, verify, **belts):
    """Start a real loopback server for ``spy``; return (base_url, stop()). ``belts``
    (``limiter=`` / ``max_body_bytes=``) pass through to ``_build_handler``; existing
    callers pass none — exercising the default-OFF path (AC6)."""
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), _build_handler(spy, verify, threading.Lock(), **belts)
    )
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"

    def stop():
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)

    return url, stop


@pytest.fixture()
def live():
    """(url, spy). Token 'good-tok' → label 'alice-laptop'; everything else → reject."""
    spy = _SpyServer()
    url, stop = _serve(spy, lambda tok: {"good-tok": "alice-laptop"}.get(tok))
    try:
        yield url, spy
    finally:
        stop()


def _request(url, *, method="POST", body=None, token=None, headers=None):
    data = (
        (body.encode("utf-8") if isinstance(body, str) else body)
        if body is not None
        else None
    )
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8"), dict(r.headers)
    except urllib.error.HTTPError as e:  # 4xx/5xx surface here
        return e.code, e.read().decode("utf-8"), dict(e.headers)


# ── the bearer-auth contract ───────────────────────────────────────────────────
def test_valid_token_request_returns_200_json_result(live):
    url, spy = live
    status, body, hdrs = _request(url, body=_RPC, token="good-tok")
    assert status == 200
    assert hdrs.get("Content-Type") == "application/json"
    env = json.loads(body)
    assert env["id"] == 1 and env["result"]["ok"] is True
    assert len(spy.calls) == 1


def test_missing_token_is_401_and_handle_never_called(live):
    url, spy = live
    status, body, _ = _request(url, body=_RPC)  # no Authorization header
    assert status == 401
    assert json.loads(body)["error"] == "unauthorized"
    assert spy.calls == []  # INV-1: the write/recall path is untouched


def test_garbage_token_is_401_and_handle_never_called(live):
    url, spy = live
    status, _, _ = _request(url, body=_RPC, token="not-a-real-token")
    assert status == 401
    assert spy.calls == []


def test_tunnel_door_tenant_from_server_not_client(live):
    # the tenant is always the server's, never the caller's (single-tenant boundary). The
    # per-session identity model is pinned in test_tunnel_door_identity_is_session_not_token.
    url, spy = live
    _request(url, body=_RPC, token="good-tok")
    ((_, identity),) = spy.calls
    assert identity.tenant_id == "default"  # tenant from the server, NOT the client


def test_notification_without_id_is_202_no_body_and_no_handle(live):
    url, spy = live
    notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    status, body, _ = _request(url, body=notif, token="good-tok")
    assert status == 202 and body == ""
    assert spy.calls == []  # ack only — handle not called


def test_get_is_405(live):
    url, _ = live
    status, _, hdrs = _request(url, method="GET")
    assert status == 405
    assert "POST" in (hdrs.get("Allow") or "")


def test_delete_is_405(live):
    # X8: no sessions to tear down → DELETE is 405 with Allow: POST, decided at method
    # routing BEFORE verify/handle, so the recall/write path is never reached.
    url, spy = live
    status, _, hdrs = _request(url, method="DELETE")
    assert status == 405
    assert "POST" in (hdrs.get("Allow") or "")
    assert spy.calls == []  # rejected before handle


def test_healthz_is_200_pre_auth_on_both_doors(live):
    """The liveness signal a supervisor in FRONT of the socket can read — a reverse proxy,
    an orchestrator's readiness gate, an uptime monitor. It answers 200 on the
    token-required door with NO token (a prober holds no seat) and on the tokenless one,
    and never reaches handle(), so it carries nothing from the store."""
    url, spy = live
    base = url.rsplit("/mcp", 1)[0]
    status, body, _ = _request(f"{base}/healthz", method="GET")
    assert status == 200 and json.loads(body) == {"status": "ok"}
    assert spy.calls == []  # rejected/answered before handle — no store handle in reach

    loop_url, stop = _serve(_SpyServer(), lambda tok: None, auth_required=False)
    try:
        status, body, _ = _request(
            f"{loop_url.rsplit('/mcp', 1)[0]}/healthz", method="GET"
        )
        assert status == 200 and json.loads(body) == {"status": "ok"}
    finally:
        stop()


def test_healthz_opens_no_hole_in_the_bearer_gate(live):
    """The health route lives in do_GET alone: POSTing to it on the token-required door
    still 401s without a token. A probe path that bypassed auth for POST would be a
    tokenless write channel wearing a health check's name."""
    url, spy = live
    base = url.rsplit("/mcp", 1)[0]
    status, _, _ = _request(f"{base}/healthz", body=_RPC)
    assert status == 401
    assert spy.calls == []


def test_only_the_exact_health_path_answers_200(live):
    """Exact match, not a prefix: a supervisor is configured with one path, and a
    prefix-matched probe would answer for URLs the server never agreed to serve."""
    url, _ = live
    base = url.rsplit("/mcp", 1)[0]
    for path in ("/health", "/healthz/", "/healthz/extra", "/"):
        status, _, hdrs = _request(f"{base}{path}", method="GET")
        assert status == 405, path
        assert "POST" in (hdrs.get("Allow") or "")


def test_request_with_origin_header_is_403(live):
    url, spy = live
    status, body, _ = _request(
        url, body=_RPC, token="good-tok", headers={"Origin": "http://evil.example"}
    )
    assert status == 403
    assert json.loads(body)["error"] == "forbidden_origin"
    assert spy.calls == []  # rejected before handle (DNS-rebinding belt)


def test_malformed_json_body_is_jsonrpc_parse_error_in_200(live):
    url, _ = live
    status, body, _ = _request(url, body="{not json", token="good-tok")
    assert status == 200  # channel separation: protocol error in a 200
    assert json.loads(body)["error"]["code"] == -32700


def test_nonobject_body_is_jsonrpc_invalid_request(live):
    # X6a: a well-formed but non-OBJECT JSON-RPC body (a bare scalar or a batch array) is an
    # Invalid Request → -32600 INSIDE a 200 (channel separation), and handle() never runs.
    url, spy = live
    for body in ("5", "[1, 2, 3]"):
        status, resp, _ = _request(url, body=body, token="good-tok")
        assert status == 200  # protocol error rides a 200
        assert json.loads(resp)["error"]["code"] == -32600
    assert spy.calls == []  # never dispatched to handle


def test_handler_exception_is_jsonrpc_error_and_daemon_keeps_serving():
    # INV-3: a raising handler → JSON-RPC -32603 in a 200, and the daemon serves the NEXT request.
    spy = _SpyServer(raise_on="tools/call")
    url, stop = _serve(spy, lambda tok: "alice" if tok == "good-tok" else None)
    try:
        status, body, _ = _request(url, body=_RPC, token="good-tok")
        assert status == 200
        assert json.loads(body)["error"]["code"] == -32603
        ping = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
        s2, b2, _ = _request(
            url, body=ping, token="good-tok"
        )  # daemon survived the crash
        assert s2 == 200 and json.loads(b2)["result"]["method"] == "ping"
    finally:
        stop()


def test_bearer_parses_scheme_case_insensitively():
    def h(val):
        m = Message()
        if val is not None:
            m["Authorization"] = val
        return m

    assert _bearer(h("Bearer abc123")) == "abc123"
    assert _bearer(h("bearer xyz")) == "xyz"  # scheme is case-insensitive (RFC 7235)
    assert _bearer(h("Basic abc")) is None  # wrong scheme
    assert _bearer(h("Bearer   ")) is None  # empty token after the scheme
    assert _bearer(h(None)) is None  # no header at all


# ── Part S belt 1: per-identity rate limit (429, post-auth, per-label — AC4) ────
def test_rate_limited_identity_gets_429_and_other_identities_unaffected():
    # the limiter keys on the resolved per-session identity (X-Hive-Agent-Id), NOT the token
    # (the token only gates the tunnel door). A valid Bearer passes the gate; the identity
    # header partitions the buckets.
    spy = _SpyServer()
    # window_s=3600 ⇒ no refill can land mid-test (deterministic without an injected clock)
    limiter = TokenBucketLimiter(limit=2, window_s=3600.0)
    url, stop = _serve(
        spy, lambda tok: "ok" if tok == "good-tok" else None, limiter=limiter
    )
    a = {_AGENT_ID_HEADER: "alice"}
    try:
        for _ in range(2):  # the per-window budget
            status, _, _ = _request(url, body=_RPC, token="good-tok", headers=a)
            assert status == 200
        status, body, hdrs = _request(url, body=_RPC, token="good-tok", headers=a)
        assert status == 429  # the N+1th on the SAME identity
        assert json.loads(body)["error"] == "rate_limited"
        assert int(hdrs.get("Retry-After")) >= 1  # machine-actionable backoff
        assert len(spy.calls) == 2  # handle NOT called on the 429
        # independent buckets: a different identity is untouched by alice's flood (AC4)
        status, _, _ = _request(
            url, body=_RPC, token="good-tok", headers={_AGENT_ID_HEADER: "bob"}
        )
        assert status == 200 and len(spy.calls) == 3
        # an INVALID token still 401s — the limiter never runs pre-auth (D3)
        status, _, _ = _request(url, body=_RPC, token="not-a-token", headers=a)
        assert status == 401
    finally:
        stop()


def test_no_limiter_default_never_throttles(live):
    # AC6: the belt is default-OFF — an unmodified call site never sees a 429.
    url, spy = live
    for _ in range(25):
        status, _, _ = _request(url, body=_RPC, token="good-tok")
        assert status == 200
    assert len(spy.calls) == 25


# ── Part S belt 2: request body cap (413, pre-auth, pre-read — AC5) ─────────────
def test_oversized_body_is_413_handle_never_called_daemon_survives():
    spy = _SpyServer()
    url, stop = _serve(
        spy, lambda tok: "alice" if tok == "good-tok" else None, max_body_bytes=256
    )
    try:
        big = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"pad": "x" * 1000},
            }
        )
        status, body, _ = _request(url, body=big, token="good-tok")
        assert status == 413
        assert json.loads(body)["error"] == "payload_too_large"
        assert spy.calls == []  # rejected before verify/handle
        # INV-3: the daemon still serves the next well-sized request
        status, _, _ = _request(url, body=_RPC, token="good-tok")
        assert status == 200 and len(spy.calls) == 1
    finally:
        stop()


def test_oversized_body_rejected_before_any_byte_is_read():
    # Raw socket: DECLARE a huge Content-Length but send NO body byte. A server that
    # drained the body before checking the cap would block until the socket timeout;
    # the prompt 413 proves the reject happens at the header, pre-read (AC5).
    spy = _SpyServer()
    url, stop = _serve(spy, lambda tok: "alice", max_body_bytes=1024)
    host, port = url.split("/")[2].split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=5) as s:
            s.sendall(
                b"POST /mcp HTTP/1.1\r\nHost: t\r\nAuthorization: Bearer x\r\n"
                b"Content-Type: application/json\r\nContent-Length: 10000000\r\n\r\n"
            )
            first = s.recv(4096)
        assert b" 413 " in first.split(b"\r\n", 1)[0]  # status line, not a hung read
        assert spy.calls == []
    finally:
        stop()


def test_under_cap_body_passes_with_belts_on():
    # both belts armed ⇒ a normal request is untouched (the belts only bite their faults)
    spy = _SpyServer()
    limiter = TokenBucketLimiter(limit=100, window_s=3600.0)
    url, stop = _serve(
        spy,
        lambda tok: "alice" if tok == "good-tok" else None,
        limiter=limiter,
        max_body_bytes=4096,
    )
    try:
        status, body, _ = _request(url, body=_RPC, token="good-tok")
        assert status == 200
        assert json.loads(body)["result"]["ok"] is True
    finally:
        stop()


# ── identity extractors + the per-session resolver (both doors) ─────────────────
def test_asserted_agent_id_extracts_strips_and_floors_blank():
    def h(val):
        m = Message()
        if val is not None:
            m[_AGENT_ID_HEADER] = val
        return m

    assert _asserted_agent_id(h("alice")) == "alice"
    assert _asserted_agent_id(h("  bob  ")) == "bob"  # surrounding whitespace stripped
    assert _asserted_agent_id(h("   ")) is None  # blank/whitespace → None
    assert _asserted_agent_id(h("")) is None  # empty → None
    assert _asserted_agent_id(h(None)) is None  # absent → None


def test_session_id_extracts_strips_and_floors_blank():
    def h(val):
        m = Message()
        if val is not None:
            m[_SESSION_ID_HEADER] = val
        return m

    assert _session_id(h("sess-1")) == "sess-1"
    assert _session_id(h("  s  ")) == "s"
    assert _session_id(h("   ")) is None
    assert _session_id(h(None)) is None


def test_resolve_identity_precedence():
    # X-Hive-Agent-Id → echoed Mcp-Session-Id → "local"; explicit header wins over the session.
    def h(**vals):
        m = Message()
        for k, v in vals.items():
            m[k] = v
        return m

    assert _resolve_identity(h()) == "local"  # neither → the floor
    assert _resolve_identity(h(**{_SESSION_ID_HEADER: "S"})) == "S"  # session id alone
    assert (
        _resolve_identity(h(**{_AGENT_ID_HEADER: "alice"})) == "alice"
    )  # explicit alone
    assert (
        _resolve_identity(
            h(
                **{
                    _AGENT_ID_HEADER: "alice",  # both → explicit wins
                    _SESSION_ID_HEADER: "S",
                }
            )
        )
        == "alice"
    )
    assert (
        _resolve_identity(h(**{_SESSION_ID_HEADER: "   "})) == "local"
    )  # blank session floored


def _open_spy(verify_calls):
    """A SpyServer + a verify that RECORDS each call (so the loopback door can assert it
    NEVER runs). Returns (spy, verify)."""
    spy = _SpyServer()

    def verify(tok):
        verify_calls.append(tok)
        return {"good-tok": "alice-laptop"}.get(tok)

    return spy, verify


# ── the tunnel door (auth_required=True, the default): the bearer GATES only ─────
def test_tunnel_door_identity_is_session_not_token(live):
    # the model's core: the token GATES the door, it is NEVER the identity — identity is
    # per-session (X-Hive-Agent-Id, else the echoed Mcp-Session-Id), the SAME as loopback.
    url, spy = live
    _request(url, body=_RPC, token="good-tok", headers={_AGENT_ID_HEADER: "alice"})
    ((_, ident),) = spy.calls
    assert ident.agent_id == "alice"  # NOT "alice-laptop" (the token label)
    spy.calls.clear()
    _request(url, body=_RPC, token="good-tok", headers={_SESSION_ID_HEADER: "S-123"})
    ((_, ident),) = spy.calls
    assert ident.agent_id == "S-123"  # the echoed session, not the token label


def test_tunnel_door_bad_token_is_401_even_with_agent_id(live):
    # the gate is independent of identity: a bad token still 401s WITH an X-Hive-Agent-Id.
    url, spy = live
    status, body, _ = _request(
        url, body=_RPC, token="bad", headers={_AGENT_ID_HEADER: "alice"}
    )
    assert status == 401 and json.loads(body)["error"] == "unauthorized"
    assert spy.calls == []


# ── the loopback door (auth_required=False): tokenless, identity per-session ─────
def _loopback(spy, verify, **belts):
    return _serve(spy, verify, auth_required=False, **belts)


def test_loopback_door_serves_without_token():
    # tokenless: NO Bearer, NO header → 200, identity floors to "local" (NOT a 400),
    # and verify is NEVER consulted.
    verify_calls: list = []
    spy, verify = _open_spy(verify_calls)
    url, stop = _loopback(spy, verify)
    try:
        status, body, _ = _request(url, body=_RPC)
        assert status == 200
        assert json.loads(body)["result"]["ok"] is True
        ((_, ident),) = spy.calls
        assert ident.agent_id == "local"
        assert verify_calls == []
    finally:
        stop()


def test_loopback_door_uses_asserted_header_when_present():
    spy = _SpyServer()
    url, stop = _loopback(spy, lambda tok: "x")
    try:
        _request(url, body=_RPC, headers={_AGENT_ID_HEADER: "alice"})
        ((_, ident),) = spy.calls
        assert ident.agent_id == "alice"
    finally:
        stop()


def test_loopback_door_ignores_bearer():
    # a stray Bearer is IGNORED on the loopback door: identity is the header, verify is dark.
    verify_calls: list = []
    spy, verify = _open_spy(verify_calls)
    url, stop = _loopback(spy, verify)
    try:
        status, _, _ = _request(
            url, body=_RPC, token="good-tok", headers={_AGENT_ID_HEADER: "carol"}
        )
        assert status == 200
        ((_, ident),) = spy.calls
        assert ident.agent_id == "carol"  # the header, not the Bearer label
        assert verify_calls == []
    finally:
        stop()


def test_loopback_door_limiter_keys_on_label():
    spy = _SpyServer()
    limiter = TokenBucketLimiter(limit=2, window_s=3600.0)
    url, stop = _loopback(spy, lambda tok: None, limiter=limiter)
    try:
        for _ in range(2):
            status, _, _ = _request(url, body=_RPC, headers={_AGENT_ID_HEADER: "alice"})
            assert status == 200
        status, body, hdrs = _request(
            url, body=_RPC, headers={_AGENT_ID_HEADER: "alice"}
        )
        assert status == 429
        assert json.loads(body)["error"] == "rate_limited"
        assert int(hdrs.get("Retry-After")) >= 1
        assert len(spy.calls) == 2
        status, _, _ = _request(url, body=_RPC, headers={_AGENT_ID_HEADER: "bob"})
        assert (
            status == 200 and len(spy.calls) == 3
        )  # a different identity, its own bucket
    finally:
        stop()


def test_loopback_door_handler_exception_is_jsonrpc_error_in_200():
    # INV-3 holds on the loopback door too: a raising handler → JSON-RPC -32603 inside a 200.
    spy = _SpyServer(raise_on="tools/call")
    url, stop = _loopback(spy, lambda tok: None)
    try:
        status, body, _ = _request(url, body=_RPC, headers={_AGENT_ID_HEADER: "alice"})
        assert status == 200
        assert json.loads(body)["error"]["code"] == -32603
    finally:
        stop()


# ── Mcp-Session-Id: server-minted at initialize, echoed back as the identity ─────
_INIT = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})


def test_initialize_mints_session_id():
    # an initialize response carries a fresh Mcp-Session-Id; two initializes mint DISTINCT ids.
    spy = _SpyServer()
    url, stop = _loopback(spy, lambda tok: None)
    try:
        _, _, h1 = _request(url, body=_INIT)
        _, _, h2 = _request(url, body=_INIT)
        s1, s2 = h1.get(_SESSION_ID_HEADER), h2.get(_SESSION_ID_HEADER)
        assert s1 and s2 and s1 != s2  # fresh + distinct per initialize
    finally:
        stop()


def test_non_initialize_does_not_mint_session_id(live):
    # only initialize mints — a tools/call response carries no Mcp-Session-Id.
    url, _ = live
    _, _, hdrs = _request(url, body=_RPC, token="good-tok")
    assert hdrs.get(_SESSION_ID_HEADER) is None


def test_session_id_resolves_as_identity_when_no_header():
    # a tool call echoing Mcp-Session-Id (no X-Hive-Agent-Id) → that session is the identity;
    # X-Hive-Agent-Id wins when BOTH are present.
    spy = _SpyServer()
    url, stop = _loopback(spy, lambda tok: None)
    try:
        _request(url, body=_RPC, headers={_SESSION_ID_HEADER: "sess-9"})
        ((_, ident),) = spy.calls
        assert ident.agent_id == "sess-9"
        spy.calls.clear()
        _request(
            url,
            body=_RPC,
            headers={_SESSION_ID_HEADER: "sess-9", _AGENT_ID_HEADER: "alice"},
        )
        ((_, ident),) = spy.calls
        assert ident.agent_id == "alice"  # explicit header wins over the session
    finally:
        stop()
