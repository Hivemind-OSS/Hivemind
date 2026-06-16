"""CV1 — HiveClient, tested against the REAL HTTP stack: the real HiveMCPServer
(real admission/recall/store) behind ``_build_handler`` on a real loopback
``ThreadingHTTPServer``, with a REAL SqliteTokenStore verifying the bearer (the
real-credential bearer-verification test idiom) — the client/server envelope contract cannot drift unseen.

The vendorability contract is enforced, not prose: importing ``hive.client``
pulls in nothing outside the stdlib (transitively, subprocess-asserted), and a
COPIED ``client.py`` imports standalone from a temp dir."""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hive.adapters.auth_store_sqlite import SqliteTokenStore
from hive.adapters.sqlite_db import connect
from hive.app.http_server import _build_handler
from hive.client import HiveClient, HiveError
from tests.mcp._helpers import build_real_server


@pytest.fixture()
def live():
    """(url, token, server): the real stack on an ephemeral port. The token is a
    real minted credential for seat label 'seat-alpha'."""
    server, _clock = build_real_server()
    tokens = SqliteTokenStore(connect(":memory:", check_same_thread=False))
    plaintext = tokens.create("seat-alpha")
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _build_handler(server, tokens.verify, threading.Lock()))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    try:
        yield url, plaintext, server
    finally:
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)


def _canned(body: bytes, *, ctype: str = "application/json", status: int = 200):
    """A one-reply HTTP server returning ``body`` verbatim — the malformed-server
    fixtures for the never-partial contract."""

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"

    def stop():
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)
    return url, stop


# ── the round-trip against the real stack ★ ────────────────────────────────────
def test_client_recall_capture_write_fetch_health(live):
    url, token, _server = live
    c = HiveClient(url, token)

    assert c.health()["ok"] is True
    assert c.recall("anything at all") == []                  # empty store ⇒ []

    cap = c.capture("dead-end: the flag --fast corrupts the cache")
    assert cap["status"] == "quarantined" and isinstance(cap["id"], int)

    w = c.write("the deploy needs DEPLOY_KEY set in the environment",
                approved_by="alice")
    assert w["status"] == "approved" and w["approved_by"] == "alice"

    hits = c.recall("the deploy needs DEPLOY_KEY set in the environment")
    assert hits and hits[0]["text"].startswith("the deploy needs DEPLOY_KEY")
    assert hits[0]["trust"] == "established"                  # envelope verbatim

    f = c.fetch(w["content_hash"])
    assert f["found"] is True and f["text"] == ("the deploy needs DEPLOY_KEY "
                                                "set in the environment")
    assert c.fetch("0" * 64)["found"] is False                # clean miss, no raise

    # 401 BEFORE the tool layer: a bad token raises, carrying the HTTP status
    bad = HiveClient(url, "not-a-real-token")
    with pytest.raises(HiveError) as ei:
        bad.health()
    assert ei.value.http_status == 401


def test_client_write_supersedes_via_replaces(live):
    url, token, _server = live
    c = HiveClient(url, token)
    old = c.write("use port 8080 for the dev server", approved_by="alice")
    new = c.write("use port 9090 for the dev server (8080 is taken)",
                  approved_by="alice", replaces=old["id"])
    assert new["superseded"] == old["id"]
    f = c.fetch(old["content_hash"])                          # retired row annotates
    assert f["superseded_by"]["episode_id"] == new["id"]


# ── never-partial on any failure layer ─────────────────────────────────────────
def test_client_never_partial_on_transport_error(live):
    url, token, _server = live
    # (a) connection refused — port 9 (discard) has no listener
    dead = HiveClient("http://127.0.0.1:9/", token, timeout_s=0.5)
    with pytest.raises(HiveError):
        dead.health()
    # (b) non-JSON 200 body
    url2, stop2 = _canned(b"<html>proxy says hi</html>", ctype="text/html")
    try:
        with pytest.raises(HiveError) as ei:
            HiveClient(url2, token).health()
        assert ei.value.http_status == 200
    finally:
        stop2()
    # (c) JSON-RPC protocol error envelope ⇒ HiveError carrying rpc_error ★
    url3, stop3 = _canned(json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "error": {"code": -32601, "message": "nope"}}).encode())
    try:
        with pytest.raises(HiveError) as ei:
            HiveClient(url3, token).recall("x")
        assert ei.value.rpc_error == {"code": -32601, "message": "nope"}
    finally:
        stop3()
    # (d) tool-level isError framing ⇒ HiveError, never a half-dict
    url4, stop4 = _canned(json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "result": {"content": [{"type": "text", "text": "invalid arguments"}],
                    "isError": True}}).encode())
    try:
        with pytest.raises(HiveError):
            HiveClient(url4, token).capture("y")
    finally:
        stop4()
    # (e) malformed result shape (no content[0].text)
    url5, stop5 = _canned(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"weird": True}}).encode())
    try:
        with pytest.raises(HiveError):
            HiveClient(url5, token).fetch("aa")
    finally:
        stop5()


# ── the vendorability fences ───────────────────────────────────────────────────
_STDLIB_PROBE = (
    "import sys, hive.client; "
    "bad = {'torch', 'sentence_transformers', 'numpy'} & "
    "{m.split('.')[0] for m in sys.modules}; "
    "print(sorted(bad))")


def test_client_is_stdlib_only():
    # TRANSITIVE: importing hive.client (including via hive/__init__) must pull
    # in no ML/array dependency — the fence is the import graph, not prose.
    out = subprocess.run([sys.executable, "-c", _STDLIB_PROBE],
                         capture_output=True, text=True, timeout=60,
                         cwd=str(pathlib.Path(__file__).resolve().parents[2]))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_vendored_copy_imports_standalone(tmp_path):
    # the vendoring contract: a COPIED client.py imports from a bare directory.
    # -E -s: PYTHONPATH and user-site are ignored (the repo is unreachable);
    # cwd stays on sys.path so the copy itself is what resolves.
    src = pathlib.Path(__file__).resolve().parents[2] / "hive" / "client.py"
    shutil.copy(src, tmp_path / "client.py")
    out = subprocess.run(
        [sys.executable, "-E", "-s", "-c",
         "import client; c = client.HiveClient('http://x', 't'); "
         "print(type(c).__name__)"],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "HiveClient"
