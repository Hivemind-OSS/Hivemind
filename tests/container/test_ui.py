"""The operator-console router — the pure `handle_request` contract, no socket.

Mirrors the FakeRun/proc/seq_in idiom from test_cli.py (reused verbatim): every route is
exercised through the injected `run` seam, so a wrong docker/authctl argv reds a test with no
Docker and no socket. The six named request guards each carry a test that reds on exactly their
mutation (the break->red->restore->green protocol is run in the build log).
"""

from __future__ import annotations

import json
import shlex
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from hive.tools import cli, ui, ui_page
from tests.container.test_cli import FakeRun, proc, seq_in

ENV = {"HIVE_TENANT_ID": "acme"}
HOST = "127.0.0.1:4173"
H = {"Host": HOST}  # a valid loopback Host (GET)
HP = {"Host": HOST, "Origin": "http://127.0.0.1:4173"}  # + same-origin Origin (POST)


def body_of(resp):
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _reset_singleflight():
    """The lifecycle single-flight slot is process state; reset it around every test so a claimed
    slot can never leak between tests (a stranded slot would 409 every later lifecycle test)."""
    ui._op_in_flight = False
    yield
    ui._op_in_flight = False


def _inline_spawn(monkeypatch):
    """Make the detached worker run INLINE (same thread) so a FakeRun's calls are recorded and the
    single-flight slot is released deterministically before the response is asserted."""
    monkeypatch.setattr(ui, "_spawn", lambda target: target())


# ── the router table: EXACTLY the fourteen pinned routes ───────────────────────
def test_router_covers_exactly_the_fourteen_routes():
    assert set(ui._ROUTES) == {
        ("GET", "/"),
        ("GET", "/api/status"),
        ("GET", "/api/doors"),
        ("GET", "/api/tokens"),
        ("POST", "/api/tokens"),
        ("POST", "/api/tokens/revoke"),
        ("GET", "/api/repos"),
        ("POST", "/api/repos"),
        ("POST", "/api/repos/remove"),
        ("POST", "/api/backup"),
        ("GET", "/api/backups"),
        ("POST", "/api/restore"),
        ("POST", "/api/lifecycle"),
        ("GET", "/api/logs"),
    }


def test_response_is_a_frozen_carrier():
    import dataclasses

    r = ui.Response(200, b"x")
    assert r.content_type == "application/json"  # the default
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
    fake = FakeRun(
        script=[
            (lambda a: seq_in(a, "ps", "ngrok"), proc(stdout="ngrok Up\n")),
            (
                lambda a: seq_in(a, "ps", "hive-server"),
                proc(stdout="hive-server Up (healthy)\n"),
            ),
            (lambda a: seq_in(a, "list"), proc(stdout="alice\nbob\n")),
        ]
    )
    resp = ui.handle_request(
        "GET",
        "/api/status",
        H,
        b"",
        run=fake,
        env=dict(ENV, NGROK_DOMAIN="brain.ngrok.app"),
    )
    assert resp.status == 200 and resp.content_type == "application/json"
    assert body_of(resp) == {
        "server": "up",
        "healthy": True,
        "tunnel_on": True,
        "tunnel_url": "https://brain.ngrok.app/mcp",
        "seats": 2,
    }


def test_status_route_down_is_a_successful_read_with_no_exec():
    fake = FakeRun(script=[(lambda a: seq_in(a, "ps", "hive-server"), proc(rc=1))])
    resp = ui.handle_request("GET", "/api/status", H, b"", run=fake, env=ENV)
    assert resp.status == 200  # a down read is a SUCCESSFUL read
    assert body_of(resp) == {
        "server": "down",
        "healthy": None,
        "tunnel_on": False,
        "tunnel_url": None,
        "seats": None,
    }
    assert not any(
        seq_in(c, "exec") for c in fake.calls
    )  # zero in-container exec when down


# ── f3: token list / mint (once) / revoke shell to authctl; blank seat 400s pre-child ──
def test_tokens_list_returns_seat_labels():
    fake = FakeRun(script=[(lambda a: seq_in(a, "list"), proc(stdout="alice\nbob\n"))])
    resp = ui.handle_request("GET", "/api/tokens", H, b"", run=fake, env=ENV)
    assert resp.status == 200 and body_of(resp) == {"seats": ["alice", "bob"]}
    assert any(
        seq_in(
            c, "exec", "-T", "hive-server", "python", "-m", "hive.tools.authctl", "list"
        )
        for c in fake.calls
    )


def test_tokens_mint_returns_seat_and_plaintext_once():
    fake = FakeRun(
        script=[(lambda a: seq_in(a, "create"), proc(stdout="hive_secret123\n"))]
    )
    resp = ui.handle_request(
        "POST",
        "/api/tokens",
        HP,
        json.dumps({"seat": "alice-laptop"}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 200
    assert body_of(resp) == {"seat": "alice-laptop", "token": "hive_secret123"}
    assert any(
        seq_in(
            c,
            "exec",
            "-T",
            "hive-server",
            "python",
            "-m",
            "hive.tools.authctl",
            "create",
            "alice-laptop",
        )
        for c in fake.calls
    )


def test_tokens_mint_blank_seat_is_400_before_any_child():
    fake = FakeRun()
    resp = ui.handle_request(
        "POST",
        "/api/tokens",
        HP,
        json.dumps({"seat": "   "}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 400 and body_of(resp) == {"error": "bad_request"}
    assert fake.calls == []  # fail-fast: no authctl child spawned


def test_tokens_mint_malformed_json_is_400_before_any_child():
    fake = FakeRun()
    resp = ui.handle_request("POST", "/api/tokens", HP, b"{not json", run=fake, env=ENV)
    assert resp.status == 400 and body_of(resp) == {"error": "bad_request"}
    assert fake.calls == []


def test_tokens_mint_failure_never_leaks_a_token():
    # authctl create fails (duplicate); the error body is generic upstream_failed — no secret.
    fake = FakeRun(
        script=[
            (
                lambda a: seq_in(a, "create"),
                proc(rc=70, stderr="authctl: a token already exists\n"),
            )
        ]
    )
    resp = ui.handle_request(
        "POST",
        "/api/tokens",
        HP,
        json.dumps({"seat": "dup"}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 502 and body_of(resp) == {"error": "upstream_failed"}
    assert "token" not in resp.body.decode().lower()


def test_tokens_revoke_returns_revoked_true():
    fake = FakeRun()
    resp = ui.handle_request(
        "POST",
        "/api/tokens/revoke",
        HP,
        json.dumps({"seat": "alice-laptop"}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 200 and body_of(resp) == {
        "seat": "alice-laptop",
        "revoked": True,
    }
    assert any(
        seq_in(
            c,
            "exec",
            "-T",
            "hive-server",
            "python",
            "-m",
            "hive.tools.authctl",
            "revoke",
            "alice-laptop",
        )
        for c in fake.calls
    )


# ── f4: safe lifecycle — backup, up (loopback-only), down (volume preserved) ──
def test_backup_route_forwards_the_snapshot_path():
    path = "/data/backups/hive-20260708.db\n"
    fake = FakeRun(
        script=[(lambda a: seq_in(a, "hive.tools.backupctl"), proc(stdout=path))]
    )
    resp = ui.handle_request("POST", "/api/backup", HP, b"", run=fake, env=ENV)
    assert resp.status == 200 and body_of(resp) == {
        "path": "/data/backups/hive-20260708.db"
    }
    assert any(
        seq_in(c, "exec", "-T", "hive-server", "python", "-m", "hive.tools.backupctl")
        for c in fake.calls
    )


def test_lifecycle_up_is_loopback_only_and_does_not_rebuild(monkeypatch):
    _inline_spawn(monkeypatch)  # run the detached worker inline
    fake = FakeRun()
    resp = ui.handle_request(
        "POST",
        "/api/lifecycle",
        HP,
        json.dumps({"action": "up"}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 202 and body_of(resp) == {
        "action": "up",
        "status": "starting",
    }
    up = fake.calls[0]
    assert up[:2] == ["docker", "compose"]
    assert "--tunnel" not in up and "--profile" not in up
    assert seq_in(up, "up", "-d", "hive-server")
    assert "--build" not in up  # plain Start never rebuilds+bounces a healthy stack


def test_lifecycle_down_preserves_the_volume(monkeypatch):
    _inline_spawn(monkeypatch)
    fake = FakeRun()
    resp = ui.handle_request(
        "POST",
        "/api/lifecycle",
        HP,
        json.dumps({"action": "down"}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 202 and body_of(resp) == {
        "action": "down",
        "status": "stopping",
    }
    down = fake.calls[0]
    assert seq_in(down, "down") and "-v" not in down  # PRESERVES the volume


def test_lifecycle_is_non_blocking_returns_before_the_docker_work():
    # THE BUG FIX: the request returns 202 IMMEDIATELY; the slow docker child runs on a DETACHED
    # worker, so a browser click never hangs on `docker compose`. Uses the real threaded _spawn.
    started, release = threading.Event(), threading.Event()

    def slow_run(argv, env=None, **kw):
        started.set()
        release.wait(2)  # block the worker mid-flight
        return proc()

    resp = ui.handle_request(
        "POST",
        "/api/lifecycle",
        HP,
        json.dumps({"action": "up"}).encode(),
        run=slow_run,
        env=ENV,
    )
    assert resp.status == 202  # returned WITHOUT awaiting slow_run
    assert started.wait(2)  # the worker DID start (another thread)
    release.set()  # let it finish → releases the slot


def test_second_concurrent_lifecycle_is_409_operation_in_progress():
    # single-flight: while one lifecycle/tunnel/restore op holds the slot, a second is refused 409
    # WITHOUT spawning a child. Dropping the non-blocking claim (always proceeding) is the mutation.
    ui._op_in_flight = True
    try:
        fake = FakeRun()
        resp = ui.handle_request(
            "POST",
            "/api/lifecycle",
            HP,
            json.dumps({"action": "up"}).encode(),
            run=fake,
            env=ENV,
        )
        assert resp.status == 409 and body_of(resp) == {
            "error": "operation_in_progress"
        }
        assert fake.calls == []  # no docker child while an op is in flight
    finally:
        ui._op_in_flight = False


# ── f4b: tunnel activate/deactivate — a lifecycle action; up gates NGROK secrets (env ONLY) ──
def test_tunnel_up_requires_ngrok_secrets_before_any_child():
    # mirror cli._up's fail-fast: missing NGROK_AUTHTOKEN/NGROK_DOMAIN → 400 naming them, no child.
    fake = FakeRun()
    resp = ui.handle_request(
        "POST",
        "/api/lifecycle",
        HP,
        json.dumps({"action": "tunnel-up"}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 400
    body = body_of(resp)
    assert body["error"] == "missing_secrets"
    assert set(body["missing"]) == {"NGROK_AUTHTOKEN", "NGROK_DOMAIN"}
    assert fake.calls == []  # fail-fast: no child, slot not claimed


def test_tunnel_up_names_only_the_missing_secret():
    fake = FakeRun()
    env = dict(ENV, NGROK_AUTHTOKEN="tok")  # only the domain is missing
    resp = ui.handle_request(
        "POST",
        "/api/lifecycle",
        HP,
        json.dumps({"action": "tunnel-up"}).encode(),
        run=fake,
        env=env,
    )
    assert resp.status == 400 and body_of(resp)["missing"] == ["NGROK_DOMAIN"]
    assert fake.calls == []


def test_tunnel_up_secrets_come_from_env_not_the_request_body():
    # secrets are read from ENV ONLY — a body carrying NGROK_* must NOT satisfy the gate.
    fake = FakeRun()
    body = json.dumps(
        {"action": "tunnel-up", "NGROK_AUTHTOKEN": "x", "NGROK_DOMAIN": "y"}
    ).encode()
    resp = ui.handle_request("POST", "/api/lifecycle", HP, body, run=fake, env=ENV)
    assert resp.status == 400 and body_of(resp)["error"] == "missing_secrets"
    assert fake.calls == []


def test_tunnel_up_starts_only_the_sidecar_behind_the_profile(monkeypatch):
    _inline_spawn(monkeypatch)
    fake = FakeRun()
    env = dict(ENV, NGROK_AUTHTOKEN="tok", NGROK_DOMAIN="brain.ngrok.app")
    resp = ui.handle_request(
        "POST",
        "/api/lifecycle",
        HP,
        json.dumps({"action": "tunnel-up"}).encode(),
        run=fake,
        env=env,
    )
    assert resp.status == 202 and body_of(resp) == {
        "action": "tunnel-up",
        "status": "starting",
    }
    up = fake.calls[0]
    assert seq_in(up, "--profile", "tunnel") and seq_in(up, "up", "-d", "ngrok")
    assert "hive-server" not in up  # ONLY the sidecar; never the daemon


def test_tunnel_down_stops_only_the_tunnel(monkeypatch):
    _inline_spawn(monkeypatch)
    fake = FakeRun()
    resp = ui.handle_request(
        "POST",
        "/api/lifecycle",
        HP,
        json.dumps({"action": "tunnel-down"}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 202 and body_of(resp) == {
        "action": "tunnel-down",
        "status": "stopping",
    }
    stop = fake.calls[0]
    assert seq_in(stop, "stop", "ngrok")
    assert (
        "down" not in stop and "-v" not in stop and "hive-server" not in stop
    )  # daemon untouched


# ── f5: reset / restore are absent BY CONSTRUCTION ──────────────────────────────
def test_lifecycle_unknown_action_is_400_before_any_child():
    for action in ("reset", "restore", "bogus", ""):
        fake = FakeRun()
        resp = ui.handle_request(
            "POST",
            "/api/lifecycle",
            HP,
            json.dumps({"action": action}).encode(),
            run=fake,
            env=ENV,
        )
        assert resp.status == 400 and body_of(resp) == {"error": "bad_request"}
        assert fake.calls == []  # never a hidden reset/restore child


def test_reset_path_is_404_reset_stays_absent_by_construction():
    # RESET remains absent entirely — no route, no handler (a graded invariant). restore is now a
    # guarded route, but reset is not reachable at all.
    resp = ui.handle_request("POST", "/api/reset", HP, b"", run=FakeRun(), env=ENV)
    assert resp.status == 404 and body_of(resp) == {"error": "not_found"}


def test_no_reset_surface_in_the_router():
    keys = " ".join(f"{m} {p}" for (m, p) in ui._ROUTES).lower()
    assert "reset" not in keys  # reset is absent by construction


# ── f7: /api/backups lists in-volume snapshots; /api/restore is guarded (traversal + whitelist) ──
def _lister(rows):
    """A FakeRun script entry answering the in-container backups lister (`python -c <script>`)."""
    return (lambda a: seq_in(a, "python", "-c"), proc(stdout=json.dumps(rows)))


_ROWS = [
    {"name": "hive-20260708.db", "size": 4096, "mtime": 1720000000},
    {"name": "hive-20260707.db", "size": 2048, "mtime": 1719900000},
]


def test_backups_route_forwards_the_in_volume_listing():
    fake = FakeRun(script=[_lister(_ROWS)])
    resp = ui.handle_request("GET", "/api/backups", H, b"", run=fake, env=ENV)
    assert resp.status == 200 and body_of(resp) == {"backups": _ROWS}
    # read by exec'ing an in-container python lister — never by parsing `ls` output.
    assert any(
        seq_in(c, "exec", "-T", "hive-server", "python", "-c") for c in fake.calls
    )


def test_backups_route_upstream_failure_is_502():
    fake = FakeRun(script=[(lambda a: seq_in(a, "python", "-c"), proc(rc=1))])
    resp = ui.handle_request("GET", "/api/backups", H, b"", run=fake, env=ENV)
    assert resp.status == 502 and body_of(resp) == {"error": "upstream_failed"}


def test_backups_route_malformed_listing_is_502():
    fake = FakeRun(
        script=[(lambda a: seq_in(a, "python", "-c"), proc(stdout="not json"))]
    )
    resp = ui.handle_request("GET", "/api/backups", H, b"", run=fake, env=ENV)
    assert resp.status == 502 and body_of(resp) == {"error": "upstream_failed"}


def test_restore_blank_name_is_400_before_any_child():
    fake = FakeRun()
    resp = ui.handle_request(
        "POST",
        "/api/restore",
        HP,
        json.dumps({"name": "  "}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 400 and body_of(resp) == {"error": "bad_request"}
    assert fake.calls == []  # fail-fast: not even the lister ran


def test_restore_rejects_path_traversal_before_any_child():
    # THE critical new security control: a name that is not a bare basename is refused 400
    # unknown_backup with ZERO children — path traversal cannot reach the shared data volume.
    for bad in (
        "../shared.db",
        "/etc/passwd",
        "a/b.db",
        "..",
        "foo/../bar.db",
        "sub\\x.db",
    ):
        fake = FakeRun(script=[_lister(_ROWS)])
        resp = ui.handle_request(
            "POST",
            "/api/restore",
            HP,
            json.dumps({"name": bad}).encode(),
            run=fake,
            env=ENV,
        )
        assert resp.status == 400 and body_of(resp) == {"error": "unknown_backup"}, bad
        assert fake.calls == []  # refused BEFORE any child (pure guard)


def test_restore_rejects_shell_metacharacter_names_before_any_child():
    # BELT (fix layer 1): a name carrying a shell metacharacter — which would break out of the
    # destructive restore `cp` sh -c — is outside the shell-inert charset, so it is refused 400
    # unknown_backup with ZERO children (before the whitelist, before the lister). The pure guard,
    # not the whitelist, does the rejecting: `fake.calls == []` is what widening the charset reds.
    for bad in ("a;b.db", "a b.db", "$(x).db", "a|b.db", "a&b.db", "`x`.db", "a>b.db"):
        fake = FakeRun(script=[_lister(_ROWS)])
        resp = ui.handle_request(
            "POST",
            "/api/restore",
            HP,
            json.dumps({"name": bad}).encode(),
            run=fake,
            env=ENV,
        )
        assert resp.status == 400 and body_of(resp) == {"error": "unknown_backup"}, bad
        assert fake.calls == [], bad  # refused BEFORE any child (pure guard)


def test_restore_worker_shell_quotes_the_backup_name():
    # SUSPENDERS (fix layer 2, defense-in-depth): even if _restore_worker is ever reached with a
    # metacharacter name, it shlex.quotes the name into the cp so it can never break out of the
    # sh -c. The QUOTED form appears in the built command; the raw unquoted form does not.
    fake = FakeRun()  # down / cp / up all default rc-0
    name = "a;b.db"
    ui._restore_worker(name, fake, ENV)
    cp = next(
        c
        for c in fake.calls
        if c[:2] == ["docker", "run"] and seq_in(c, "--entrypoint", "sh")
    )
    quoted = shlex.quote(name)
    assert quoted != name  # this name genuinely exercises quoting
    assert any(
        f"cp /data/backups/{quoted} /data/shared.db" in tok for tok in cp
    )  # quoted form
    assert not any(
        f"cp /data/backups/{name} /data/shared.db" in tok for tok in cp
    )  # raw form absent


def test_restore_rejects_a_wellformed_name_not_in_the_listing():
    # a bare basename that is NOT a member of the current listing is refused (whitelist); the lister
    # runs (a read), but no destructive child does.
    fake = FakeRun(script=[_lister(_ROWS)])
    resp = ui.handle_request(
        "POST",
        "/api/restore",
        HP,
        json.dumps({"name": "hive-19990101.db"}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 400 and body_of(resp) == {"error": "unknown_backup"}
    assert not any(
        seq_in(c, "hive.tools.backupctl") for c in fake.calls
    )  # no safety snapshot
    assert not any(seq_in(c, "down") for c in fake.calls)  # no destructive child


def test_restore_takes_safety_snapshot_before_overwrite_then_dispatches(monkeypatch):
    _inline_spawn(monkeypatch)
    fake = FakeRun(script=[_lister(_ROWS)])  # backupctl/down/run/up default rc-0
    name = "hive-20260708.db"
    resp = ui.handle_request(
        "POST",
        "/api/restore",
        HP,
        json.dumps({"name": name}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 202 and body_of(resp) == {
        "action": "restore",
        "status": "restoring",
    }
    snap_idx = next(
        i for i, c in enumerate(fake.calls) if seq_in(c, "hive.tools.backupctl")
    )
    down_idx = next(
        i for i, c in enumerate(fake.calls) if seq_in(c, "down") and "-v" not in c
    )
    cp_idx = next(
        i
        for i, c in enumerate(fake.calls)
        if c[:2] == ["docker", "run"] and seq_in(c, "--entrypoint", "sh")
    )
    up_idx = next(
        i for i, c in enumerate(fake.calls) if seq_in(c, "up", "-d", "hive-server")
    )
    assert (
        snap_idx < down_idx < cp_idx < up_idx
    )  # snapshot FIRST, then stop→overwrite→restart
    cp = fake.calls[cp_idx]
    assert seq_in(cp, "-v", "hive-data:/data")  # mounts ONLY the data volume
    assert any(
        f"cp /data/backups/{name} /data/shared.db" in tok for tok in cp
    )  # sources the volume
    assert any("rm -f /data/shared.db-wal" in tok for tok in cp)  # clears stale WAL
    assert not any(
        seq_in(c, "up", "-d", "--build") for c in fake.calls
    )  # restart never rebuilds


def test_restore_aborts_500_if_the_safety_snapshot_fails(monkeypatch):
    _inline_spawn(monkeypatch)
    fake = FakeRun(
        script=[
            _lister(_ROWS),
            (lambda a: seq_in(a, "hive.tools.backupctl"), proc(rc=1)),
        ]
    )
    resp = ui.handle_request(
        "POST",
        "/api/restore",
        HP,
        json.dumps({"name": "hive-20260708.db"}).encode(),
        run=fake,
        env=ENV,
    )
    assert resp.status == 500 and body_of(resp) == {"error": "safety_snapshot_failed"}
    # the store is NOT overwritten when its safety snapshot failed — no down/cp/up ran.
    assert not any(seq_in(c, "down") for c in fake.calls)
    assert not any(c[:2] == ["docker", "run"] for c in fake.calls)


def test_restore_second_concurrent_is_409(monkeypatch):
    _inline_spawn(monkeypatch)
    ui._op_in_flight = True
    try:
        fake = FakeRun(script=[_lister(_ROWS)])
        resp = ui.handle_request(
            "POST",
            "/api/restore",
            HP,
            json.dumps({"name": "hive-20260708.db"}).encode(),
            run=fake,
            env=ENV,
        )
        assert resp.status == 409 and body_of(resp) == {
            "error": "operation_in_progress"
        }
        assert not any(
            seq_in(c, "hive.tools.backupctl") for c in fake.calls
        )  # no snapshot claimed
    finally:
        ui._op_in_flight = False


# ── logs tail: bounded, non-blocking (NO -f) ──
def test_logs_route_reads_a_bounded_non_blocking_tail():
    fake = FakeRun(
        script=[(lambda a: seq_in(a, "logs"), proc(stdout="line1\nline2\n"))]
    )
    resp = ui.handle_request("GET", "/api/logs", H, b"", run=fake, env=ENV)
    assert resp.status == 200 and body_of(resp) == {"lines": ["line1", "line2"]}
    call = fake.calls[0]
    assert seq_in(call, "logs", "--tail=200", "hive-server")
    assert "-f" not in call  # non-blocking: never follows


# ── c3: the six named guards, each pinned by its own test ──────────────────────
def test_forged_host_is_403_forbidden_host():
    resp = ui.handle_request(
        "GET", "/api/status", {"Host": "evil.example"}, b"", run=FakeRun(), env=ENV
    )
    assert resp.status == 403 and body_of(resp) == {"error": "forbidden_host"}


def test_missing_host_fails_closed_403():
    resp = ui.handle_request("GET", "/api/status", {}, b"", run=FakeRun(), env=ENV)
    assert resp.status == 403 and body_of(resp) == {"error": "forbidden_host"}


def test_cross_origin_post_is_403_forbidden_origin():
    fake = FakeRun()
    resp = ui.handle_request(
        "POST",
        "/api/backup",
        {"Host": HOST, "Origin": "http://evil.example"},
        b"",
        run=fake,
        env=ENV,
    )
    assert resp.status == 403 and body_of(resp) == {"error": "forbidden_origin"}
    assert fake.calls == []  # rejected before any child


def test_same_origin_post_is_admitted():
    # the inverted doctrine: the same-origin browser IS the legitimate client → admitted.
    fake = FakeRun(
        script=[(lambda a: seq_in(a, "hive.tools.backupctl"), proc(stdout="/p\n"))]
    )
    resp = ui.handle_request("POST", "/api/backup", HP, b"", run=fake, env=ENV)
    assert resp.status == 200


def test_oversized_declared_length_is_413_before_any_child():
    fake = FakeRun()
    headers = dict(HP, **{"Content-Length": str(ui.MAX_BODY_BYTES + 1)})
    resp = ui.handle_request("POST", "/api/tokens", headers, b"", run=fake, env=ENV)
    assert resp.status == 413 and body_of(resp) == {"error": "payload_too_large"}
    assert fake.calls == []  # capped before parse/child


def test_unknown_path_is_404():
    resp = ui.handle_request("GET", "/api/nope", H, b"", run=FakeRun(), env=ENV)
    assert resp.status == 404 and body_of(resp) == {"error": "not_found"}


def test_wrong_method_is_405():
    resp = ui.handle_request("DELETE", "/api/tokens", H, b"", run=FakeRun(), env=ENV)
    assert resp.status == 405 and body_of(resp) == {"error": "method_not_allowed"}
    assert ui._methods_for_path("/api/tokens") == ["GET", "POST"]  # the Allow set


def test_handler_fault_never_raises_becomes_500():
    class Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("child blew up")

    resp = ui.handle_request("GET", "/api/status", H, b"", run=Boom(), env=ENV)
    assert resp.status == 500 and body_of(resp) == {"error": "internal_error"}


# ── f6 + c2: the loopback socket shell over a real ThreadingHTTPServer on 127.0.0.1:0 ──

_LIVE_SCRIPT = [
    (lambda a: seq_in(a, "ps", "ngrok"), proc(stdout="")),  # tunnel off
    (
        lambda a: seq_in(a, "ps", "hive-server"),
        proc(stdout="hive-server Up (healthy)\n"),
    ),
    (
        lambda a: seq_in(a, "hive.tools.authctl", "create"),
        proc(stdout="hive_tok_xyz\n"),
    ),
    (lambda a: seq_in(a, "hive.tools.authctl", "list"), proc(stdout="alice\nbob\n")),
    (
        lambda a: seq_in(a, "python", "-c"),
        proc(stdout='[{"name": "hive-x.db", "size": 8, "mtime": 100}]'),
    ),  # backups lister
    (lambda a: seq_in(a, "hive.tools.backupctl"), proc(stdout="/data/backups/x.db\n")),
    (lambda a: seq_in(a, "logs"), proc(stdout="log line a\nlog line b\n")),
    (
        lambda a: seq_in(a, "hive.tools.repoctl", "report"),
        proc(
            stdout='{"repos": [], "fleet": {"last_sync_ts": null, "last_error": null}}'
        ),
    ),
]


@pytest.fixture()
def live():
    """A real loopback ThreadingHTTPServer on an ephemeral port, driven by a FakeRun (no Docker).
    Yields (port, fake) and tears the daemon down."""
    fake = FakeRun(script=list(_LIVE_SCRIPT))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ui._build_handler(fake, ENV))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1], fake
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def _post(port, path, obj=None, headers=None):
    data = json.dumps(obj).encode() if obj is not None else b""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header(
        "Origin", f"http://127.0.0.1:{port}"
    )  # same-origin unless overridden below
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _raw(port, request_bytes):
    """Send a fully hand-built request (full control of Host / declared Content-Length) and read
    the raw response bytes."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(request_bytes)
    s.shutdown(socket.SHUT_WR)
    chunks = []
    try:
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
    except socket.timeout:
        pass
    s.close()
    return b"".join(chunks)


def test_live_get_root_is_200_html(live):
    port, _ = live
    status, ctype, body = _get(port, "/")
    assert status == 200 and ctype == "text/html; charset=utf-8"
    assert body == ui_page.PAGE_HTML.encode("utf-8")


def test_live_get_status_is_200_json(live):
    port, _ = live
    status, ctype, body = _get(port, "/api/status")
    assert status == 200 and ctype == "application/json"
    assert json.loads(body)["server"] == "up"


def test_live_all_fourteen_routes_answer(live, monkeypatch):
    monkeypatch.setattr(
        ui, "_spawn", lambda target: target()
    )  # deterministic: no worker leak
    port, _ = live
    assert _get(port, "/")[0] == 200
    assert _get(port, "/api/status")[0] == 200
    assert _get(port, "/api/doors")[0] == 200
    assert _get(port, "/api/tokens")[0] == 200
    assert _get(port, "/api/logs")[0] == 200
    assert _post(port, "/api/tokens", {"seat": "carol"})[0] == 200
    assert _post(port, "/api/tokens/revoke", {"seat": "carol"})[0] == 200
    assert _get(port, "/api/repos")[0] == 200
    assert _post(port, "/api/repos", {"url": "https://example.invalid/a.git"})[0] == 200
    assert _post(port, "/api/repos/remove", {"name": "a"})[0] == 200
    assert _post(port, "/api/backup")[0] == 200
    assert _get(port, "/api/backups")[0] == 200
    assert (
        _post(port, "/api/restore", {"name": "hive-x.db"})[0] == 202
    )  # guarded + async
    assert (
        _post(port, "/api/lifecycle", {"action": "up"})[0] == 202
    )  # async: 202 Accepted


def test_live_cross_origin_post_is_403(live):
    port, _ = live
    code, body = _post(port, "/api/backup", headers={"Origin": "http://evil.example"})
    assert code == 403 and json.loads(body) == {"error": "forbidden_origin"}


def test_live_forged_host_is_403(live):
    port, _ = live
    resp = _raw(
        port,
        (
            "GET /api/status HTTP/1.1\r\nHost: evil.example\r\n"
            "Connection: close\r\n\r\n"
        ).encode(),
    )
    assert resp.split(b"\r\n", 1)[0].split()[1] == b"403"


def test_live_oversized_declared_length_is_413(live):
    port, _ = live
    # forge a huge DECLARED Content-Length with an empty body — the cap rejects before any read.
    req = (
        f"POST /api/backup HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Origin: http://127.0.0.1:{port}\r\nContent-Length: {ui.MAX_BODY_BYTES + 1}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    resp = _raw(port, req)
    assert resp.split(b"\r\n", 1)[0].split()[1] == b"413"
    assert b"connection: close" in resp.lower()


def test_live_daemon_survives_a_bad_request(live):
    port, _ = live
    code, _ = _post(port, "/api/tokens")  # empty body → malformed JSON → 400
    assert code == 400
    assert _get(port, "/")[0] == 200  # the NEXT request still answers


def test_live_transport_fault_becomes_500_and_daemon_survives(live, monkeypatch):
    # force a fault ABOVE the router (in the transport shell) — the _dispatch never-raise turns it
    # into a 500 and the daemon survives so the next request still answers.
    port, _ = live
    real = ui.handle_request

    def boom(*a, **k):
        raise RuntimeError("transport-level boom")

    monkeypatch.setattr(ui, "handle_request", boom)
    status, _ctype, body = _get(port, "/api/status")
    assert status == 500 and json.loads(body) == {"error": "internal_error"}
    monkeypatch.setattr(ui, "handle_request", real)  # restore, then prove survival
    assert _get(port, "/")[0] == 200


# ── serve_ui: loopback-only bind, sysexits mapping, browser side-channel ──


class _FakeServer:
    def __init__(self, address, handler):
        self.server_address = (address[0], address[1] or 4173)
        self.closed = False

    def serve_forever(self):
        raise KeyboardInterrupt  # simulate an immediate Ctrl-C

    def server_close(self):
        self.closed = True


def test_serve_ui_refuses_non_loopback_without_binding():
    def boom(*a, **k):
        raise AssertionError("serve_ui must NOT bind a routable host")

    rc = ui.serve_ui(
        host="0.0.0.0", run=FakeRun(), env=ENV, open_browser=False, server_factory=boom
    )
    assert rc == cli.EX_USAGE  # fail-fast, no bind


def test_serve_ui_bind_oserror_is_unavailable():
    def oserr(*a, **k):
        raise OSError("address already in use")

    rc = ui.serve_ui(run=FakeRun(), env=ENV, open_browser=False, server_factory=oserr)
    assert rc == cli.EX_UNAVAILABLE


def test_serve_ui_keyboardinterrupt_is_ok_and_closes():
    made = []

    def factory(addr, handler):
        s = _FakeServer(addr, handler)
        made.append(s)
        return s

    rc = ui.serve_ui(run=FakeRun(), env=ENV, open_browser=False, server_factory=factory)
    assert rc == cli.EX_OK
    assert made and made[0].closed is True  # clean shutdown closed the socket


def test_serve_ui_opens_browser_when_requested(monkeypatch):
    opened = []
    monkeypatch.setattr(ui.webbrowser, "open", lambda u: opened.append(u))
    rc = ui.serve_ui(
        run=FakeRun(),
        env=ENV,
        open_browser=True,
        server_factory=lambda addr, h: _FakeServer(addr, h),
    )
    assert rc == cli.EX_OK
    assert opened and opened[0].startswith("http://127.0.0.1:")


def test_serve_ui_no_open_does_not_touch_the_browser(monkeypatch):
    def forbidden(_u):
        raise AssertionError("--no-open must not launch a browser")

    monkeypatch.setattr(ui.webbrowser, "open", forbidden)
    rc = ui.serve_ui(
        run=FakeRun(),
        env=ENV,
        open_browser=False,
        server_factory=lambda addr, h: _FakeServer(addr, h),
    )
    assert rc == cli.EX_OK
