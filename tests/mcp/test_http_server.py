"""Chunk 3 — run_http: per-device bearer auth in front of HiveMCPServer (AUTH-PLAN §4).

Drives the REAL handler (``_build_handler``) on a real loopback ``ThreadingHTTPServer`` at
127.0.0.1:0, with a SpyServer standing in for HiveMCPServer so the transport obligations are
tested in isolation: 200/result on a valid token, 401-BEFORE-handle (INV-1), identity
threading (token label → ``identity.agent_id``), 202 notification, 405 GET, 403 Origin,
INV-3 robustness, and channel separation (protocol errors ride a 200). The RULE-2 mutation
(do_POST skips the ``verify``-None → 401 guard) makes the missing-token test red.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from email.message import Message
from http.server import ThreadingHTTPServer

import pytest

from hive.app.http_server import _bearer, _build_handler
from hive.app.mcp_server import MCPResponse, ServerIdentity

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


def _serve(spy, verify):
    """Start a real loopback server for ``spy``; return (base_url, stop())."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _build_handler(spy, verify, threading.Lock()))
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


# ── the §4 contract ────────────────────────────────────────────────────────────
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
