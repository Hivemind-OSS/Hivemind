"""Chunk 3 — run_http: per-device bearer auth in front of HiveMCPServer.

Drives the REAL handler (``_build_handler``) on a real loopback ``ThreadingHTTPServer`` at
127.0.0.1:0, with a SpyServer standing in for HiveMCPServer so the transport obligations are
tested in isolation: 200/result on a valid token, 401-BEFORE-handle (INV-1), identity
threading (token label → ``identity.agent_id``), 202 notification, 405 GET, 403 Origin,
INV-3 robustness, and channel separation (protocol errors ride a 200). The mutation
(do_POST skips the ``verify``-None → 401 guard) makes the missing-token test red.

Part S extends the contract with the two transport belts, both
keyword-only and DEFAULT-OFF so every pre-existing test here doubles as the AC6
backward-compat proof: a ``limiter`` throttles one verified label with 429+``Retry-After``
(handle untouched, other labels unaffected), and ``max_body_bytes`` rejects an oversized
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

from hive.app.http_server import _bearer, _build_handler
from hive.app.mcp_server import MCPResponse, ServerIdentity
from hive.app.rate_limit import TokenBucketLimiter

_RPC = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "hive_health", "arguments": {}}})


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
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                _build_handler(spy, verify, threading.Lock(), **belts))
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
    data = (body.encode("utf-8") if isinstance(body, str) else body) if body is not None else None
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
    except urllib.error.HTTPError as e:                # 4xx/5xx surface here
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
    status, body, _ = _request(url, body=_RPC)              # no Authorization header
    assert status == 401
    assert json.loads(body)["error"] == "unauthorized"
    assert spy.calls == []                                   # INV-1: the write/recall path is untouched


def test_garbage_token_is_401_and_handle_never_called(live):
    url, spy = live
    status, _, _ = _request(url, body=_RPC, token="not-a-real-token")
    assert status == 401
    assert spy.calls == []


def test_identity_reaching_handle_is_the_token_label(live):
    url, spy = live
    _request(url, body=_RPC, token="good-tok")
    (_, identity), = spy.calls
    assert identity.agent_id == "alice-laptop"               # the verified device label
    assert identity.tenant_id == "default"                   # tenant from the server, NOT the client


def test_notification_without_id_is_202_no_body_and_no_handle(live):
    url, spy = live
    notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    status, body, _ = _request(url, body=notif, token="good-tok")
    assert status == 202 and body == ""
    assert spy.calls == []                                   # ack only — handle not called


def test_get_is_405(live):
    url, _ = live
    status, _, hdrs = _request(url, method="GET")
    assert status == 405
    assert "POST" in (hdrs.get("Allow") or "")


def test_request_with_origin_header_is_403(live):
    url, spy = live
    status, body, _ = _request(url, body=_RPC, token="good-tok",
                               headers={"Origin": "http://evil.example"})
    assert status == 403
    assert json.loads(body)["error"] == "forbidden_origin"
    assert spy.calls == []                                   # rejected before handle (DNS-rebinding belt)


def test_malformed_json_body_is_jsonrpc_parse_error_in_200(live):
    url, _ = live
    status, body, _ = _request(url, body="{not json", token="good-tok")
    assert status == 200                                     # channel separation: protocol error in a 200
    assert json.loads(body)["error"]["code"] == -32700


def test_handler_exception_is_jsonrpc_error_and_daemon_keeps_serving():
    # INV-3: a raising handler → JSON-RPC -32603 in a 200, and the daemon serves the NEXT request.
    spy = _SpyServer(raise_on="tools/call")
    url, stop = _serve(spy, lambda tok: "alice" if tok == "good-tok" else None)
    try:
        status, body, _ = _request(url, body=_RPC, token="good-tok")
        assert status == 200
        assert json.loads(body)["error"]["code"] == -32603
        ping = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
        s2, b2, _ = _request(url, body=ping, token="good-tok")   # daemon survived the crash
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
    assert _bearer(h("bearer xyz")) == "xyz"                 # scheme is case-insensitive (RFC 7235)
    assert _bearer(h("Basic abc")) is None                   # wrong scheme
    assert _bearer(h("Bearer   ")) is None                   # empty token after the scheme
    assert _bearer(h(None)) is None                          # no header at all


# ── Part S belt 1: per-token rate limit (429, post-auth, per-label — AC4) ───────
def test_rate_limited_token_gets_429_and_other_tokens_unaffected():
    spy = _SpyServer()
    # window_s=3600 ⇒ no refill can land mid-test (deterministic without an injected clock)
    limiter = TokenBucketLimiter(limit=2, window_s=3600.0)
    url, stop = _serve(spy, lambda tok: {"tok-a": "alice", "tok-b": "bob"}.get(tok),
                       limiter=limiter)
    try:
        for _ in range(2):                                   # the per-window budget
            status, _, _ = _request(url, body=_RPC, token="tok-a")
            assert status == 200
        status, body, hdrs = _request(url, body=_RPC, token="tok-a")
        assert status == 429                                 # the N+1th on the SAME label
        assert json.loads(body)["error"] == "rate_limited"
        assert int(hdrs.get("Retry-After")) >= 1             # machine-actionable backoff
        assert len(spy.calls) == 2                           # handle NOT called on the 429
        # independent buckets: a different device is untouched by alice's flood (AC4)
        status, _, _ = _request(url, body=_RPC, token="tok-b")
        assert status == 200 and len(spy.calls) == 3
        # an INVALID token still 401s — the limiter never runs pre-auth (D3)
        status, _, _ = _request(url, body=_RPC, token="not-a-token")
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
    url, stop = _serve(spy, lambda tok: "alice" if tok == "good-tok" else None,
                       max_body_bytes=256)
    try:
        big = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"pad": "x" * 1000}})
        status, body, _ = _request(url, body=big, token="good-tok")
        assert status == 413
        assert json.loads(body)["error"] == "payload_too_large"
        assert spy.calls == []                               # rejected before verify/handle
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
            s.sendall(b"POST /mcp HTTP/1.1\r\nHost: t\r\nAuthorization: Bearer x\r\n"
                      b"Content-Type: application/json\r\nContent-Length: 10000000\r\n\r\n")
            first = s.recv(4096)
        assert b" 413 " in first.split(b"\r\n", 1)[0]        # status line, not a hung read
        assert spy.calls == []
    finally:
        stop()


def test_under_cap_body_passes_with_belts_on():
    # both belts armed ⇒ a normal request is untouched (the belts only bite their faults)
    spy = _SpyServer()
    limiter = TokenBucketLimiter(limit=100, window_s=3600.0)
    url, stop = _serve(spy, lambda tok: "alice" if tok == "good-tok" else None,
                       limiter=limiter, max_body_bytes=4096)
    try:
        status, body, _ = _request(url, body=_RPC, token="good-tok")
        assert status == 200
        assert json.loads(body)["result"]["ok"] is True
    finally:
        stop()
