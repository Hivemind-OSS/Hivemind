"""CV1 — HiveClient, pinned at the CLIENT boundary: the exact ``tools/call`` arguments each
verb sends (the v3 surface — no approver anywhere; structured anchors/repos; scoped recall)
and the never-partial parse of every failure layer, via one-reply canned HTTP servers
(stdlib http.server only).

This suite deliberately builds NO hive server: the served envelope semantics (write lands
provisional, the replaces gate, scoped recall behavior) are the frozen contract suite's
territory (tests/contract/**) — here the contract under test is what BYTES the vendorable
client puts on the wire and that no failure layer ever yields a partial dict.

The vendorability contract is enforced, not prose: importing ``hive.client`` pulls in
nothing outside the stdlib (transitively, subprocess-asserted), and a COPIED ``client.py``
imports standalone from a temp dir."""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hive.client import HiveClient, HiveError

_CLIENT_SRC = pathlib.Path(__file__).resolve().parents[2] / "hive" / "client.py"


def _canned(body: bytes, *, ctype: str = "application/json", status: int = 200):
    """A one-reply HTTP server returning ``body`` verbatim — the canned-server fixture for
    the transport/parse contract."""

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


def _tool_body(payload: dict) -> bytes:
    """A valid JSON-RPC tools/call reply carrying ``payload`` as the tool's JSON text."""
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
    ).encode()


def _intercepted() -> tuple[HiveClient, list[tuple[str, dict]]]:
    """A client whose transport is replaced by a recorder — pins the exact (tool, arguments)
    each verb sends, with no server at all."""
    sent: list[tuple[str, dict]] = []
    c = HiveClient("http://x", "t")
    c._call = lambda tool, arguments: (sent.append((tool, arguments)), {})[1]  # type: ignore[method-assign]
    return c, sent


# ── the v3 argument envelopes: minimal stays minimal, set args ride verbatim ───
def test_write_minimal_sends_text_alone():
    # v3: no approver EXISTS — the minimal write is {"text": ...}, nothing else.
    c, sent = _intercepted()
    c.write("a fact")
    assert sent[-1] == ("hive_write", {"text": "a fact"})


def test_write_full_sends_replaces_labels_and_scope():
    c, sent = _intercepted()
    c.write("use port 9090 for the dev server", replaces=7, polarity="do", kind="env_fact",
            anchors=[{"repo": "hivemind", "anchor": "hive/app/config.py:SyncConfig"}],
            repos=["hivemind", "hive-edge"])
    assert sent[-1] == ("hive_write", {
        "text": "use port 9090 for the dev server", "replaces": 7,
        "polarity": "do", "kind": "env_fact",
        "anchors": [{"repo": "hivemind", "anchor": "hive/app/config.py:SyncConfig"}],
        "repos": ["hivemind", "hive-edge"]})


def test_capture_minimal_and_scoped():
    c, sent = _intercepted()
    c.capture("an insight")
    assert sent[-1] == ("hive_capture", {"text": "an insight"})
    c.capture("a gotcha", polarity="dont", kind="gotcha",
              anchors=[{"repo": "hivemind", "anchor": "hive/domain/recall.py:RecallPipeline"}])
    assert sent[-1][1] == {
        "text": "a gotcha", "polarity": "dont", "kind": "gotcha",
        "anchors": [{"repo": "hivemind", "anchor": "hive/domain/recall.py:RecallPipeline"}]}
    c.capture("a scope-only note", repos=["hivemind"])
    assert sent[-1][1] == {"text": "a scope-only note", "repos": ["hivemind"]}


def test_recall_minimal_is_global_and_scope_rides_only_when_set():
    c, sent = _intercepted()
    c.recall("what broke the sync tick")
    assert sent[-1] == ("hive_recall", {"query": "what broke the sync tick"})
    c.recall("what broke the sync tick", repos=["hivemind", "hive-edge@dev"],
             anchor_prefix="hive/app/")
    assert sent[-1][1] == {"query": "what broke the sync tick",
                           "repos": ["hivemind", "hive-edge@dev"],
                           "anchor_prefix": "hive/app/"}


def test_client_surface_carries_no_approver():
    # the CT-12 posture at the client: no approver field anywhere — neither accepted as an
    # argument nor present in the vendorable source.
    import inspect

    for verb in (HiveClient.write, HiveClient.capture, HiveClient.recall):
        assert "approved_by" not in inspect.signature(verb).parameters
    assert "approved_by" not in _CLIENT_SRC.read_text(encoding="utf-8")


# ── parse: reference_context verbatim; tolerant of a missing key ───────────────
def test_recall_returns_reference_context_verbatim_with_v3_hit_fields():
    hits = [{"episode_id": 3, "text": "the tick is fail-open per repo", "sim": 0.91,
             "trust": "established", "polarity": "do", "kind": "contract",
             "repos": ["hivemind"],
             "anchors": [{"repo": "hivemind", "anchor": "hive/app/sync.py:SyncService"}],
             "drift": {"type": "fresh", "detail": {}}}]
    url, stop = _canned(_tool_body({"reference_context": hits, "abstained": False}))
    try:
        got = HiveClient(url, "t").recall("sync tick failure isolation")
        assert got == hits                       # VERBATIM — no client-side reshaping
    finally:
        stop()


def test_recall_yields_empty_list_on_abstain_or_missing_key():
    url, stop = _canned(_tool_body({"reference_context": [], "abstained": True}))
    try:
        assert HiveClient(url, "t").recall("anything") == []
    finally:
        stop()
    c, _ = _intercepted()                        # {} payload: key absent entirely
    assert c.recall("anything") == []


# ── never-partial on any failure layer ─────────────────────────────────────────
def test_client_never_partial_on_transport_error():
    # (a) connection refused — port 9 (discard) has no listener
    dead = HiveClient("http://127.0.0.1:9/", "t", timeout_s=0.5)
    with pytest.raises(HiveError):
        dead.health()
    # (b) non-JSON 200 body
    url2, stop2 = _canned(b"<html>proxy says hi</html>", ctype="text/html")
    try:
        with pytest.raises(HiveError) as ei:
            HiveClient(url2, "t").health()
        assert ei.value.http_status == 200
    finally:
        stop2()
    # (c) JSON-RPC protocol error envelope ⇒ HiveError carrying rpc_error
    url3, stop3 = _canned(json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "error": {"code": -32601, "message": "nope"}}).encode())
    try:
        with pytest.raises(HiveError) as ei:
            HiveClient(url3, "t").recall("x")
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
            HiveClient(url4, "t").capture("y")
    finally:
        stop4()
    # (e) malformed result shape (no content[0].text)
    url5, stop5 = _canned(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"weird": True}}).encode())
    try:
        with pytest.raises(HiveError):
            HiveClient(url5, "t").recall("aa")
    finally:
        stop5()
    # (f) HTTP-level auth failure carries the status
    url6, stop6 = _canned(b"unauthorized", ctype="text/plain", status=401)
    try:
        with pytest.raises(HiveError) as ei:
            HiveClient(url6, "bad-token").health()
        assert ei.value.http_status == 401
    finally:
        stop6()


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
    shutil.copy(_CLIENT_SRC, tmp_path / "client.py")
    out = subprocess.run(
        [sys.executable, "-E", "-s", "-c",
         "import client; c = client.HiveClient('http://x', 't'); "
         "print(type(c).__name__)"],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "HiveClient"
