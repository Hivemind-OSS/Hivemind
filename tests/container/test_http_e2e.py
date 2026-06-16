"""AC1 whole-stack proof: a device with a valid token round-trips the REAL stack over HTTP.

Ties together build_container (FakeWarmProvider — no torch) → SqliteTokenStore.create →
run_http's handler (_build_handler) → the REAL HiveMCPServer → store. The piecewise chunk
tests prove identity threading (ch1), the HTTP transport with a spy (ch3), and container
wiring (ch4) SEPARATELY; this proves they compose: a valid token's hive_write is attributed
to the token's LABEL (not the process default), hive_recall returns it, all five tools answer
over HTTP, and a bad token never reaches the store (INV-1).
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from hive.app.config import Config
from hive.app.container import build_container
from hive.app.http_server import _build_handler
from tests.fakes._fakes import FakeWarmProvider


@pytest.fixture()
def daemon(tmp_path):
    """(url, token, container). Process identity is 'process-default'; the token's label is
    'alice-laptop' — so attribution to the label (not the process default) is observable."""
    cfg = Config.load(db_path=str(tmp_path / "shared.db"))
    c = build_container(cfg, tenant_id="default", agent_id="process-default",
                        embedder=FakeWarmProvider(d=256))
    c.warm_embedder()
    c.build_index()
    server = c.make_server()
    token = c.token_store.create("alice-laptop")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                _build_handler(server, c.token_store.verify, threading.Lock()))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    try:
        yield url, token, c
    finally:
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)


def _call(url, name, args, token, req_id=1):
    """POST a tools/call; return (http_status, parsed tool payload or None on 4xx)."""
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, None
    env = json.loads(raw)
    return status, json.loads(env["result"]["content"][0]["text"])


def test_valid_token_write_attributed_to_label_then_recallable(daemon):
    url, token, c = daemon
    text = "the WAL transaction runs under one BEGIN IMMEDIATE per producer tick"
    status, w = _call(url, "hive_write", {"text": text, "approved_by": "user"}, token)
    assert status == 200 and w["status"] == "approved"
    # ATTRIBUTION: proposed_by is the TOKEN LABEL, never the process default (the headline of AC1/INV-2)
    ep = c.store.get_episode(w["id"])
    assert ep.proposed_by == "alice-laptop"
    # round-trip: recall it back over HTTP (rebuild the index to include the fresh approved write)
    c.build_index()
    status, r = _call(url, "hive_recall", {"query": text}, token)
    assert status == 200 and r["abstained"] is False
    assert any(h["text"] == text for h in r["reference_context"])


def test_all_five_tools_answer_over_http(daemon, tmp_path):
    url, token, _ = daemon
    s_h, h = _call(url, "hive_health", {}, token)
    assert s_h == 200 and h["ok"] is True
    s_w, w = _call(url, "hive_write", {"text": "a durable insight about retries", "approved_by": "u"}, token)
    assert s_w == 200 and w["status"] == "approved"
    s_c, c = _call(url, "hive_capture", {"text": "a quarantined note about caching"}, token)
    assert s_c == 200 and c["status"] == "quarantined"
    s_r, r = _call(url, "hive_recall", {"query": "a durable insight about retries"}, token)
    assert s_r == 200
    s_i, i = _call(url, "hive_init", {"repo_path": str(tmp_path), "harness": "generic"}, token)
    assert s_i == 200 and i["phase"] == 1


def test_bad_token_writes_nothing_to_the_real_store(daemon):
    url, _, c = daemon
    before = c.store.counts()
    code, _ = _call(url, "hive_write",
                    {"text": "should never land", "approved_by": "u"}, "hive_badtoken")
    assert code == 401                                  # 401 at the transport
    assert c.store.counts() == before                   # INV-1: the store was never touched
