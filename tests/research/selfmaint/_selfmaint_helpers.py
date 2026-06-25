"""Shared loopback-server plumbing for the self-maintenance harness tests: a real
``ThreadingHTTPServer`` fronting either ``build_real_server`` (the true Hivemind stack) or a
recording spy, via the shipped ``_build_handler`` — the same way ``tests/mcp/test_http_server.py``
drives the endpoint contract."""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

from hive.app.http_server import _build_handler
from tests.mcp._helpers import build_real_server


def loopback_server(handler_server, *, auth_required: bool = False, verify=None):
    """Start a real loopback server for ``handler_server``; return ``(url, stop)``."""
    verify = verify or (lambda tok: None)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _build_handler(handler_server, verify, threading.Lock(), auth_required=auth_required))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"

    def stop():
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)
    return url, stop


def live_daemon(**server_kw):
    """``(url, server, clock, stop)`` — the REAL Hivemind MCP server behind real HTTP."""
    server, clock = build_real_server(**server_kw)
    url, stop = loopback_server(server)
    return url, server, clock, stop
