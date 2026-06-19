"""Whole-stack proof of the TWO-DOOR endpoint: both doors bound on ONE server / ONE lock.

Ties together build_container (FakeWarmProvider — no torch) → SqliteTokenStore.create →
``_build_handler`` (auth_required True/False) → the REAL HiveMCPServer → store. The piecewise
chunk tests prove identity resolution (test_http_server), the dual-serve wiring (test_entrypoint),
and container wiring (test_http_e2e historically) SEPARATELY; this proves they compose under the
MODE-COLLAPSE model:

- the TUNNEL door (auth_required=True) gates on the bearer but attributes to the per-SESSION
  identity (an explicit X-Hive-Agent-Id), NEVER the token label;
- the LOOPBACK door (auth_required=False) is tokenless and floors to ``"local"``, or takes the
  header / echoed Mcp-Session-Id as identity;
- BOTH doors share ONE HiveMCPServer + ONE lock, so a tokenless loopback write is visible to a
  token-authed tunnel recall, and concurrent traffic across both doors never corrupts the single
  writer (the shared-lock invariant — separate locks would race the one sqlite connection).

All conns are built via the prod ``connect()`` (check_same_thread=False) inside build_container,
never a hand-rolled sqlite conn (the deferred-isolation tx trap).
"""
from __future__ import annotations

import concurrent.futures as cf
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
def stack(tmp_path):
    """(loopback_url, tunnel_url, token, container). Both doors are bound on ONE server + ONE
    lock (mirroring run_http_dual on ephemeral ports): a tokenless loopback door and a
    token-required tunnel door. Process identity 'process-default'; the token's label is
    'alice-laptop' (distinct from any X-Hive-Agent-Id the tests send, so attribution to the
    per-session identity — not the token label — is observable)."""
    cfg = Config.load(db_path=str(tmp_path / "shared.db"))
    c = build_container(cfg, tenant_id="default", agent_id="process-default",
                        embedder=FakeWarmProvider(d=256))
    c.warm_embedder()
    c.build_index()
    server = c.make_server()
    token = c.token_store.create("alice-laptop")
    lock = threading.Lock()                                  # ONE lock shared across BOTH doors
    loop_httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _build_handler(server, c.token_store.verify, lock, auth_required=False))
    tun_httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _build_handler(server, c.token_store.verify, lock, auth_required=True))
    threads = [threading.Thread(target=h.serve_forever, daemon=True)
               for h in (loop_httpd, tun_httpd)]
    for t in threads:
        t.start()
    loop_url = f"http://127.0.0.1:{loop_httpd.server_address[1]}/mcp"
    tun_url = f"http://127.0.0.1:{tun_httpd.server_address[1]}/mcp"
    try:
        yield loop_url, tun_url, token, c
    finally:
        for h in (loop_httpd, tun_httpd):
            h.shutdown()
            h.server_close()
        for t in threads:
            t.join(timeout=5)


def _call(url, name, args, *, token=None, headers=None, req_id=1):
    """POST a tools/call; return (http_status, parsed tool payload or None on 4xx)."""
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, None
    env = json.loads(raw)
    return status, json.loads(env["result"]["content"][0]["text"])


# ── the tunnel door: bearer GATES, identity is the per-session header (not the token) ──
def test_tunnel_door_write_attributed_to_session_identity_then_recallable(stack):
    _loop, tun, token, c = stack
    text = "the WAL transaction runs under one BEGIN IMMEDIATE per producer tick"
    status, w = _call(tun, "hive_write", {"text": text, "approved_by": "user"},
                      token=token, headers={"X-Hive-Agent-Id": "alice-session-1"})
    assert status == 200 and w["status"] == "approved"
    # ATTRIBUTION: proposed_by is the per-SESSION identity, NOT the token label ("alice-laptop")
    # and NOT the process default ("process-default") — the headline of the collapsed model.
    ep = c.store.get_episode(w["id"])
    assert ep.proposed_by == "alice-session-1"
    # round-trip: recall it back over HTTP (rebuild the index to include the fresh approved write)
    c.build_index()
    status, r = _call(tun, "hive_recall", {"query": text}, token=token)
    assert status == 200 and r["abstained"] is False
    assert any(h["text"] == text for h in r["reference_context"])


def test_all_four_tools_answer_over_the_tunnel_door(stack):
    _loop, tun, token, _c = stack
    s_h, h = _call(tun, "hive_health", {}, token=token)
    assert s_h == 200 and h["ok"] is True
    s_w, w = _call(tun, "hive_write", {"text": "a durable insight about retries",
                                       "approved_by": "u"}, token=token)
    assert s_w == 200 and w["status"] == "approved"
    s_c, cap = _call(tun, "hive_capture", {"text": "a quarantined note about caching"},
                     token=token)
    assert s_c == 200 and cap["status"] == "quarantined"
    s_r, _r = _call(tun, "hive_recall", {"query": "a durable insight about retries"},
                    token=token)
    assert s_r == 200


def test_bad_token_writes_nothing_to_the_real_store(stack):
    _loop, tun, _token, c = stack
    before = c.store.counts()
    code, _ = _call(tun, "hive_write",
                    {"text": "should never land", "approved_by": "u"}, token="hive_badtoken")
    assert code == 401                                  # 401 at the tunnel door
    assert c.store.counts() == before                   # INV-1: the store was never touched


# ── the loopback door: tokenless, identity per-session (header / session id / "local") ──
def test_loopback_door_tokenless_write_attributed_to_local_then_recallable(stack):
    loop, _tun, _token, c = stack
    text = "the index rebuild reads the servable snapshot at boot"
    status, w = _call(loop, "hive_write", {"text": text, "approved_by": "user"})  # NO token
    assert status == 200 and w["status"] == "approved"
    ep = c.store.get_episode(w["id"])
    assert ep.proposed_by == "local"                    # the loopback residual bucket
    c.build_index()
    status, r = _call(loop, "hive_recall", {"query": text})
    assert status == 200 and r["abstained"] is False
    assert any(h["text"] == text for h in r["reference_context"])


def test_loopback_door_uses_header_identity(stack):
    loop, _tun, _token, c = stack
    status, w = _call(loop, "hive_write",
                      {"text": "a header-attributed note", "approved_by": "u"},
                      headers={"X-Hive-Agent-Id": "alice"})
    assert status == 200
    assert c.store.get_episode(w["id"]).proposed_by == "alice"


def test_distinct_session_ids_are_distinct_identities(stack):
    # the per-session identity round-trip through the REAL server: two writes echoing distinct
    # Mcp-Session-Id headers (no explicit X-Hive-Agent-Id) land under two distinct proposed_by.
    loop, _tun, _token, c = stack
    _, w1 = _call(loop, "hive_write", {"text": "session-bound note one", "approved_by": "u"},
                  headers={"Mcp-Session-Id": "sess-A"})
    _, w2 = _call(loop, "hive_write", {"text": "session-bound note two", "approved_by": "u"},
                  headers={"Mcp-Session-Id": "sess-B"})
    assert c.store.get_episode(w1["id"]).proposed_by == "sess-A"
    assert c.store.get_episode(w2["id"]).proposed_by == "sess-B"


# ── both doors share ONE server + ONE lock (the single-writer invariant across listeners) ──
def test_two_listeners_share_one_lock_and_one_server(stack):
    loop, tun, token, c = stack
    # (a) cross-door visibility: a tokenless loopback write is recallable over the token door —
    # ONE server / ONE store sits behind both listeners.
    text = "cross-door visibility: one server behind both listeners"
    status, _w = _call(loop, "hive_write", {"text": text, "approved_by": "user"})
    assert status == 200
    c.build_index()
    status, r = _call(tun, "hive_recall", {"query": text}, token=token)
    assert status == 200 and any(h["text"] == text for h in r["reference_context"])
    # (b) concurrency: drive captures (the write path) across BOTH doors at once. The SHARED
    # lock serializes every handler, so the one sqlite conn is never raced — all 200, counts()
    # stays coherent. Separate locks would surface a cross-thread "transaction within a
    # transaction" / "database is locked" here.
    def hit(i):
        url, tok = (loop, None) if i % 2 == 0 else (tun, token)
        s, _ = _call(url, "hive_capture", {"text": f"concurrent note {i}"},
                     token=tok, req_id=100 + i)
        return s
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(hit, range(24)))
    assert all(s == 200 for s in results)
    n_episodes, _n_pending = c.store.counts()           # no cross-thread corruption
    assert isinstance(n_episodes, int)
