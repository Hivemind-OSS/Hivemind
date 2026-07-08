"""The operator-console router — the pure `handle_request` contract, no socket.

Mirrors the FakeRun/proc/seq_in idiom from test_cli.py (reused verbatim): every route is
exercised through the injected `run` seam, so a wrong docker/authctl argv reds a test with no
Docker and no socket. The six named request guards each carry a test that reds on exactly their
mutation (the break->red->restore->green protocol is run in the build log).
"""
from __future__ import annotations

import json

import pytest

from hive.tools import cli, ui, ui_page
from tests.container.test_cli import FakeRun, proc, seq_in

ENV = {"HIVE_TENANT_ID": "acme"}
HOST = "127.0.0.1:4173"
H = {"Host": HOST}                                         # a valid loopback Host (GET)
HP = {"Host": HOST, "Origin": "http://127.0.0.1:4173"}     # + same-origin Origin (POST)


def body_of(resp):
    return json.loads(resp.body.decode("utf-8"))


# ── the router table: EXACTLY the eight pinned routes ──────────────────────────
def test_router_covers_exactly_the_eight_routes():
    assert set(ui._ROUTES) == {
        ("GET", "/"),
        ("GET", "/api/status"),
        ("GET", "/api/tokens"),
        ("POST", "/api/tokens"),
        ("POST", "/api/tokens/revoke"),
        ("POST", "/api/backup"),
        ("POST", "/api/lifecycle"),
        ("GET", "/api/logs"),
    }


def test_response_is_a_frozen_carrier():
    import dataclasses
    r = ui.Response(200, b"x")
    assert r.content_type == "application/json"            # the default
    assert dataclasses.is_dataclass(r)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.status = 500


# ── f1: GET / serves the self-contained page as text/html ──────────────────────
def test_get_root_serves_the_html_page():
    resp = ui.handle_request("GET", "/", H, b"", run=FakeRun(), env=ENV)
    assert resp.status == 200
    assert resp.content_type == "text/html; charset=utf-8"
    assert resp.body == ui_page.PAGE_HTML.encode("utf-8")


# ── f2: /api/status serializes the shared StatusSnapshot; down short-circuits ──
def test_status_route_serializes_the_snapshot():
    fake = FakeRun(script=[
        (lambda a: seq_in(a, "ps", "ngrok"), proc(stdout="ngrok Up\n")),
        (lambda a: seq_in(a, "ps", "hive-server"), proc(stdout="hive-server Up (healthy)\n")),
        (lambda a: seq_in(a, "list"), proc(stdout="alice\nbob\n")),
    ])
    resp = ui.handle_request("GET", "/api/status", H, b"", run=fake,
                             env=dict(ENV, NGROK_DOMAIN="brain.ngrok.app"))
    assert resp.status == 200 and resp.content_type == "application/json"
    assert body_of(resp) == {"server": "up", "healthy": True, "tunnel_on": True,
                             "tunnel_url": "https://brain.ngrok.app/mcp", "seats": 2}


def test_status_route_down_is_a_successful_read_with_no_exec():
    fake = FakeRun(script=[(lambda a: seq_in(a, "ps", "hive-server"), proc(rc=1))])
    resp = ui.handle_request("GET", "/api/status", H, b"", run=fake, env=ENV)
    assert resp.status == 200                              # a down read is a SUCCESSFUL read
    assert body_of(resp) == {"server": "down", "healthy": None, "tunnel_on": False,
                             "tunnel_url": None, "seats": None}
    assert not any(seq_in(c, "exec") for c in fake.calls)  # zero in-container exec when down


# ── f3: token list / mint (once) / revoke shell to authctl; blank seat 400s pre-child ──
def test_tokens_list_returns_seat_labels():
    fake = FakeRun(script=[(lambda a: seq_in(a, "list"), proc(stdout="alice\nbob\n"))])
    resp = ui.handle_request("GET", "/api/tokens", H, b"", run=fake, env=ENV)
    assert resp.status == 200 and body_of(resp) == {"seats": ["alice", "bob"]}
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.authctl", "list") for c in fake.calls)


def test_tokens_mint_returns_seat_and_plaintext_once():
    fake = FakeRun(script=[(lambda a: seq_in(a, "create"), proc(stdout="hive_secret123\n"))])
    resp = ui.handle_request("POST", "/api/tokens", HP,
                             json.dumps({"seat": "alice-laptop"}).encode(), run=fake, env=ENV)
    assert resp.status == 200
    assert body_of(resp) == {"seat": "alice-laptop", "token": "hive_secret123"}
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.authctl", "create", "alice-laptop") for c in fake.calls)


def test_tokens_mint_blank_seat_is_400_before_any_child():
    fake = FakeRun()
    resp = ui.handle_request("POST", "/api/tokens", HP,
                             json.dumps({"seat": "   "}).encode(), run=fake, env=ENV)
    assert resp.status == 400 and body_of(resp) == {"error": "bad_request"}
    assert fake.calls == []                                # fail-fast: no authctl child spawned


def test_tokens_mint_malformed_json_is_400_before_any_child():
    fake = FakeRun()
    resp = ui.handle_request("POST", "/api/tokens", HP, b"{not json", run=fake, env=ENV)
    assert resp.status == 400 and body_of(resp) == {"error": "bad_request"}
    assert fake.calls == []


def test_tokens_mint_failure_never_leaks_a_token():
    # authctl create fails (duplicate); the error body is generic upstream_failed — no secret.
    fake = FakeRun(script=[(lambda a: seq_in(a, "create"),
                            proc(rc=70, stderr="authctl: a token already exists\n"))])
    resp = ui.handle_request("POST", "/api/tokens", HP,
                             json.dumps({"seat": "dup"}).encode(), run=fake, env=ENV)
    assert resp.status == 502 and body_of(resp) == {"error": "upstream_failed"}
    assert "token" not in resp.body.decode().lower()


def test_tokens_revoke_returns_revoked_true():
    fake = FakeRun()
    resp = ui.handle_request("POST", "/api/tokens/revoke", HP,
                             json.dumps({"seat": "alice-laptop"}).encode(), run=fake, env=ENV)
    assert resp.status == 200 and body_of(resp) == {"seat": "alice-laptop", "revoked": True}
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.authctl", "revoke", "alice-laptop") for c in fake.calls)


# ── f4: safe lifecycle — backup, up (loopback-only), down (volume preserved) ──
def test_backup_route_forwards_the_snapshot_path():
    path = "/data/backups/hive-20260708.db\n"
    fake = FakeRun(script=[(lambda a: seq_in(a, "hive.tools.backupctl"), proc(stdout=path))])
    resp = ui.handle_request("POST", "/api/backup", HP, b"", run=fake, env=ENV)
    assert resp.status == 200 and body_of(resp) == {"path": "/data/backups/hive-20260708.db"}
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.backupctl") for c in fake.calls)


def test_lifecycle_up_is_loopback_only():
    fake = FakeRun()
    resp = ui.handle_request("POST", "/api/lifecycle", HP,
                             json.dumps({"action": "up"}).encode(), run=fake, env=ENV)
    assert resp.status == 200 and body_of(resp) == {"action": "up", "ok": True}
    up = fake.calls[0]
    assert up[:2] == ["docker", "compose"]
    assert "--tunnel" not in up and "tunnel" not in up and "--profile" not in up
    assert seq_in(up, "up", "-d", "--build", "hive-server")


def test_lifecycle_down_preserves_the_volume():
    fake = FakeRun()
    resp = ui.handle_request("POST", "/api/lifecycle", HP,
                             json.dumps({"action": "down"}).encode(), run=fake, env=ENV)
    assert resp.status == 200 and body_of(resp) == {"action": "down", "ok": True}
    down = fake.calls[0]
    assert seq_in(down, "down") and "-v" not in down       # PRESERVES the volume


def test_lifecycle_child_failure_is_upstream_failed():
    fake = FakeRun(script=[(lambda a: seq_in(a, "down"), proc(rc=1))])
    resp = ui.handle_request("POST", "/api/lifecycle", HP,
                             json.dumps({"action": "down"}).encode(), run=fake, env=ENV)
    assert resp.status == 502 and body_of(resp)["ok"] is False


# ── f5: reset / restore are absent BY CONSTRUCTION ──────────────────────────────
def test_lifecycle_unknown_action_is_400_before_any_child():
    for action in ("reset", "restore", "bogus", ""):
        fake = FakeRun()
        resp = ui.handle_request("POST", "/api/lifecycle", HP,
                                 json.dumps({"action": action}).encode(), run=fake, env=ENV)
        assert resp.status == 400 and body_of(resp) == {"error": "bad_request"}
        assert fake.calls == []                            # never a hidden reset/restore child


def test_reset_and_restore_paths_are_404():
    for path in ("/api/reset", "/api/restore"):
        resp = ui.handle_request("POST", path, HP, b"", run=FakeRun(), env=ENV)
        assert resp.status == 404 and body_of(resp) == {"error": "not_found"}


def test_no_reset_restore_surface_in_the_router():
    keys = " ".join(f"{m} {p}" for (m, p) in ui._ROUTES).lower()
    assert "reset" not in keys and "restore" not in keys


# ── logs tail: bounded, non-blocking (NO -f) ──
def test_logs_route_reads_a_bounded_non_blocking_tail():
    fake = FakeRun(script=[(lambda a: seq_in(a, "logs"), proc(stdout="line1\nline2\n"))])
    resp = ui.handle_request("GET", "/api/logs", H, b"", run=fake, env=ENV)
    assert resp.status == 200 and body_of(resp) == {"lines": ["line1", "line2"]}
    call = fake.calls[0]
    assert seq_in(call, "logs", "--tail=200", "hive-server")
    assert "-f" not in call                                # non-blocking: never follows


# ── c3: the six named guards, each pinned by its own test ──────────────────────
def test_forged_host_is_403_forbidden_host():
    resp = ui.handle_request("GET", "/api/status", {"Host": "evil.example"}, b"",
                             run=FakeRun(), env=ENV)
    assert resp.status == 403 and body_of(resp) == {"error": "forbidden_host"}


def test_missing_host_fails_closed_403():
    resp = ui.handle_request("GET", "/api/status", {}, b"", run=FakeRun(), env=ENV)
    assert resp.status == 403 and body_of(resp) == {"error": "forbidden_host"}


def test_cross_origin_post_is_403_forbidden_origin():
    fake = FakeRun()
    resp = ui.handle_request("POST", "/api/backup",
                             {"Host": HOST, "Origin": "http://evil.example"}, b"",
                             run=fake, env=ENV)
    assert resp.status == 403 and body_of(resp) == {"error": "forbidden_origin"}
    assert fake.calls == []                                # rejected before any child


def test_same_origin_post_is_admitted():
    # the inverted doctrine: the same-origin browser IS the legitimate client → admitted.
    fake = FakeRun(script=[(lambda a: seq_in(a, "hive.tools.backupctl"), proc(stdout="/p\n"))])
    resp = ui.handle_request("POST", "/api/backup", HP, b"", run=fake, env=ENV)
    assert resp.status == 200


def test_oversized_declared_length_is_413_before_any_child():
    fake = FakeRun()
    headers = dict(HP, **{"Content-Length": str(ui.MAX_BODY_BYTES + 1)})
    resp = ui.handle_request("POST", "/api/tokens", headers, b"", run=fake, env=ENV)
    assert resp.status == 413 and body_of(resp) == {"error": "payload_too_large"}
    assert fake.calls == []                                # capped before parse/child


def test_unknown_path_is_404():
    resp = ui.handle_request("GET", "/api/nope", H, b"", run=FakeRun(), env=ENV)
    assert resp.status == 404 and body_of(resp) == {"error": "not_found"}


def test_wrong_method_is_405():
    resp = ui.handle_request("DELETE", "/api/tokens", H, b"", run=FakeRun(), env=ENV)
    assert resp.status == 405 and body_of(resp) == {"error": "method_not_allowed"}
    assert ui._methods_for_path("/api/tokens") == ["GET", "POST"]   # the Allow set


def test_handler_fault_never_raises_becomes_500():
    class Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("child blew up")
    resp = ui.handle_request("GET", "/api/status", H, b"", run=Boom(), env=ENV)
    assert resp.status == 500 and body_of(resp) == {"error": "internal_error"}
