"""CT — the census-webhook nudge door (U1 D6): an HMAC-gated wake-up on the TUNNEL
door only, never a data channel. ONE nudge wakes the loop for ALL registered
repos (the wake Event is loop-global — there is no per-repo nudge).

Every test drives the REAL handler on a real loopback ``ThreadingHTTPServer`` with a
real HMAC-SHA256 signature over the raw request bytes. The contract: with
``webhook_secret`` set, ``POST /census-webhook`` is resolved after body-drain + the
Origin guard and BEFORE the bearer gate — a valid ``X-Hub-Signature-256``
(``sha256=<hex>``) is 204 + ``webhook_nudge()`` (the sync loop's wake Event), an
invalid/absent one is 401. The payload is NEVER parsed and the handler holds no
server/store handle, so the route is structurally incapable of mutation — a forged
webhook buys at worst one extra fetch of true remote state. Unset secret ⇒ the branch
is dead code: the path behaves byte-identically to every other path. ``run_http_dual``
threads the secret to the TUNNEL door alone — the loopback door never serves the route.
Law-7 mutation: inverting/skipping the ``compare_digest`` gate in ``do_POST`` reds
``test_webhook_bad_signature_is_401_and_nudge_never_fires``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from hive.app.http_server import _build_handler
from hive.app.mcp_server import MCPResponse, ServerIdentity

_RPC = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "hive_health", "arguments": {}},
    }
)


class _SpyServer:
    """Minimal HiveMCPServer stand-in: records every (req, identity) handle() sees."""

    def __init__(self) -> None:
        self.identity = ServerIdentity(tenant_id="default", agent_id="server-default")
        self.calls: list = []

    def handle(self, req, *, identity=None):
        self.calls.append((req, identity))
        return MCPResponse(id=req.id, result={"ok": True, "method": req.method})


def _serve(spy, verify, **kw):
    """A real loopback server over the REAL handler; returns (base_url, stop())."""
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), _build_handler(spy, verify, threading.Lock(), **kw)
    )
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()

    def stop():
        httpd.shutdown()
        httpd.server_close()
        th.join(timeout=5)

    return f"http://127.0.0.1:{httpd.server_address[1]}", stop


def _request(url, *, body=None, token=None, headers=None):
    data = (
        (body.encode("utf-8") if isinstance(body, str) else body)
        if body is not None
        else None
    )
    req = urllib.request.Request(url, data=data, method="POST")
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


def _sig(secret: str, body: bytes) -> str:
    return (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )


# ── the gate: valid signature ⇒ 204 + nudge, pre-bearer, payload never parsed ────
def test_valid_signature_is_204_sets_nudge_pre_bearer_and_never_parses_payload():
    verify_calls: list = []

    def verify(tok):  # every token would be REJECTED —
        verify_calls.append(tok)  # the webhook must not need one
        return None

    ev = threading.Event()
    spy = _SpyServer()
    base, stop = _serve(spy, verify, webhook_secret="s3cret", webhook_nudge=ev.set)
    body = b"\x00\x89 raw push-event bytes, deliberately NOT json {{{"
    try:
        status, resp, _ = _request(
            f"{base}/census-webhook",
            body=body,
            headers={"X-Hub-Signature-256": _sig("s3cret", body)},
        )
        assert status == 204 and resp == ""
        assert ev.is_set()  # the wake fired
        assert spy.calls == []  # no server handle touched
        assert verify_calls == []  # resolved BEFORE the bearer gate
    finally:
        stop()


def test_valid_signature_with_no_nudge_wired_is_still_204():
    # secret set but no live sync thread (a start failure fails open): the gate still
    # authenticates and 204s — the wake is simply a no-op.
    spy = _SpyServer()
    base, stop = _serve(spy, lambda tok: None, webhook_secret="s3cret")
    body = b"payload-bytes"
    try:
        status, resp, _ = _request(
            f"{base}/census-webhook",
            body=body,
            headers={"X-Hub-Signature-256": _sig("s3cret", body)},
        )
        assert status == 204 and resp == ""
        assert spy.calls == []
    finally:
        stop()


def test_webhook_bad_signature_is_401_and_nudge_never_fires():
    # THE Law-7 gate test: inverting or skipping the compare_digest gate in do_POST reds this.
    ev = threading.Event()
    spy = _SpyServer()
    base, stop = _serve(
        spy, lambda tok: None, webhook_secret="s3cret", webhook_nudge=ev.set
    )
    body = b"payload-bytes"
    good = _sig("s3cret", body)
    tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
    try:
        for sig in (
            _sig("wrong-secret", body),  # signed with the wrong secret
            tampered,  # one flipped hex digit
            "sha256=not-hex-at-all",
            None,
        ):  # header absent entirely
            hdrs = {"X-Hub-Signature-256": sig} if sig is not None else {}
            status, resp, _ = _request(
                f"{base}/census-webhook", body=body, headers=hdrs
            )
            assert status == 401
            assert json.loads(resp)["error"] == "unauthorized"
        assert not ev.is_set()  # a forged webhook never wakes sync
        assert spy.calls == []
    finally:
        stop()


def test_webhook_with_origin_header_is_403_guard_order_holds():
    # the Origin belt (DNS-rebinding) resolves BEFORE the webhook branch: a browser is
    # never a legitimate forwarder, valid signature or not.
    ev = threading.Event()
    spy = _SpyServer()
    base, stop = _serve(
        spy, lambda tok: None, webhook_secret="s3cret", webhook_nudge=ev.set
    )
    body = b"payload"
    try:
        status, resp, _ = _request(
            f"{base}/census-webhook",
            body=body,
            headers={
                "X-Hub-Signature-256": _sig("s3cret", body),
                "Origin": "http://evil.example",
            },
        )
        assert status == 403 and json.loads(resp)["error"] == "forbidden_origin"
        assert not ev.is_set()
    finally:
        stop()


# ── unset secret ⇒ dead branch: the path is just another path (byte-identical) ──
def test_unset_secret_keeps_census_webhook_path_byte_identical():
    # webhook_secret unset (the default, and the loopback door's permanent state): even
    # with a nudge callable wired, /census-webhook is indistinguishable from /mcp — the
    # SAME request yields byte-identical status/body on both paths, and the nudge never
    # fires. This pins "unset ⇒ the branch is dead code on that door".
    fired: list = []
    spy = _SpyServer()
    base, stop = _serve(
        spy,
        lambda tok: {"good-tok": "alice"}.get(tok),
        webhook_nudge=lambda: fired.append(True),
    )  # secret NOT set
    body = _RPC.encode("utf-8")
    hdrs = {"X-Hub-Signature-256": _sig("would-be-secret", body)}
    try:
        for token, want in ((None, 401), ("good-tok", 200)):
            s_hook, b_hook, h_hook = _request(
                f"{base}/census-webhook", body=body, token=token, headers=hdrs
            )
            s_mcp, b_mcp, h_mcp = _request(
                f"{base}/mcp", body=body, token=token, headers=hdrs
            )
            assert s_hook == want  # pre-chunk semantics preserved
            assert (s_hook, b_hook) == (
                s_mcp,
                b_mcp,
            )  # byte-identical to any other path
            assert (
                h_hook.get("Content-Type")
                == h_mcp.get("Content-Type")
                == "application/json"
            )
        assert fired == []  # the dead branch never nudges
    finally:
        stop()


def test_loopback_door_handler_treats_webhook_path_as_plain_mcp():
    # the loopback handler is built exactly as before (run_http_dual threads no webhook
    # kwargs there): a signed POST to /census-webhook is ordinary tokenless MCP traffic —
    # dispatched to handle(), never a 204.
    spy = _SpyServer()
    base, stop = _serve(spy, lambda tok: None, auth_required=False)
    body = _RPC.encode("utf-8")
    try:
        status, resp, _ = _request(
            f"{base}/census-webhook",
            body=body,
            headers={"X-Hub-Signature-256": _sig("s3cret", body)},
        )
        assert status == 200
        assert json.loads(resp)["result"]["ok"] is True
        assert len(spy.calls) == 1
    finally:
        stop()


# ── the wake semantics: ONE nudge covers ALL registered repos ────────────────────
def test_one_nudge_wakes_the_loop_for_all_repos(tmp_path):
    """The nudge Event is loop-global: with two registered repos and a long poll
    interval, ONE ``sync_nudge.set()`` (what the webhook door fires) yields one
    tick that advances BOTH repos' watermarks — there is no per-repo nudge."""
    import hive.app.sync as sync_mod
    from hive.adapters.sqlite_db import connect
    from hive.adapters.store_sqlite import SqliteEpisodeStore
    from hive.app.config import Config
    from hive.domain.change_evidence import ChangeEvidenceService
    from tests.sync.conftest import Origin, meta, register_repo

    store = SqliteEpisodeStore(connect(":memory:", check_same_thread=False))
    a, b = Origin(tmp_path / "remote-a"), Origin(tmp_path / "remote-b")
    register_repo(store, "alpha", a.url)
    register_repo(store, "beta", b.url)
    cfg = Config.load(
        db_path=":memory:",
        env={},
        sync={"mirror_dir": str(tmp_path / "mirrors"), "interval_s": 3600},
    )
    evidence = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424242, ranges=store
    )
    thread = sync_mod.start_sync(cfg, store, evidence, threading.Lock())
    try:
        deadline = time.time() + 60
        while time.time() < deadline and (
            meta(store, "sync:alpha:last_tip") is None
            or meta(store, "sync:beta:last_tip") is None
        ):
            time.sleep(0.05)  # the immediate first tick lands both
        a.commit("app.py", 'def greet(name):\n    return "yo " + name\n', "move a")
        a.push()
        b.commit("app.py", 'def greet(name):\n    return "ho " + name\n', "move b")
        b.push()
        tip_a = a.origin_sha("refs/heads/main")
        tip_b = b.origin_sha("refs/heads/main")
        thread.sync_nudge.set()  # ← the ONE wake the webhook fires
        deadline = time.time() + 60
        while time.time() < deadline and (
            meta(store, "sync:alpha:last_tip") != tip_a
            or meta(store, "sync:beta:last_tip") != tip_b
        ):
            time.sleep(0.05)
        assert meta(store, "sync:alpha:last_tip") == tip_a, (
            "one nudge must wake the loop for alpha"
        )
        assert meta(store, "sync:beta:last_tip") == tip_b, (
            "…and for beta — the wake is loop-global, never per-repo"
        )
    finally:
        thread.sync_stop.set()
        thread.sync_nudge.set()
        thread.join(timeout=30)


# ── run_http_dual wiring: the route lives on the TUNNEL door alone ───────────────
def test_run_http_dual_serves_webhook_on_tunnel_door_only(monkeypatch):
    import hive.app.http_server as H

    created: list = []

    class _Recording(ThreadingHTTPServer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    monkeypatch.setattr(H, "ThreadingHTTPServer", _Recording)
    spy = _SpyServer()
    ev = threading.Event()
    runner = threading.Thread(
        target=lambda: H.run_http_dual(
            spy,
            loopback_host="127.0.0.1",
            loopback_port=0,
            tunnel_host="127.0.0.1",
            tunnel_port=0,
            verify=lambda tok: {"good-tok": "alice"}.get(tok),
            lock=threading.Lock(),
            webhook_secret="s3cret",
            webhook_nudge=ev.set,
        ),
        daemon=True,
    )
    runner.start()
    deadline = time.time() + 5
    while len(created) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert len(created) == 2, "run_http_dual never bound both doors"
    # identify the doors behaviorally: a tokenless MCP POST 401s on the tunnel, 200s on loopback
    door: dict = {}
    for port in (s.server_address[1] for s in created):
        status, _, _ = _request(f"http://127.0.0.1:{port}/mcp", body=_RPC)
        door["tunnel" if status == 401 else "loopback"] = port
    body = b"push-event-raw-bytes"
    hdrs = {"X-Hub-Signature-256": _sig("s3cret", body)}
    try:
        assert set(door) == {"tunnel", "loopback"}
        status, resp, _ = _request(
            f"http://127.0.0.1:{door['tunnel']}/census-webhook", body=body, headers=hdrs
        )
        assert status == 204 and resp == "" and ev.is_set()
        ev.clear()
        status, resp, _ = _request(
            f"http://127.0.0.1:{door['loopback']}/census-webhook",
            body=body,
            headers=hdrs,
        )
        assert status == 200  # plain MCP traffic (parse error in a 200)
        assert json.loads(resp)["error"]["code"] == -32700
        assert not ev.is_set()  # the nudge never reaches loopback
    finally:
        for s in created:
            s.shutdown()
        for s in created:
            s.server_close()
        runner.join(timeout=5)
