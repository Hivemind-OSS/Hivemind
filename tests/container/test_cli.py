"""ADMIN-CLI — the `hive` operator CLI: argv contracts via the injected fake runner.

Every verb shells through the one `run()` seam, so a fake that records argv makes the
compose/exec invocations assertable facts, not prose — a wrong invocation reds a test,
no Docker needed. The helpers (`FakeRun`/`proc`/`seq_in`) are module-level and reusable:
successor verbs (e.g. `credit`) add their argv tests here against the same fake.
"""
from __future__ import annotations

import io
import subprocess

from hive.tools import cli

ENV = {"HIVE_TENANT_ID": "acme"}


def proc(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


def seq_in(argv, *words) -> bool:
    """True iff `words` appears as a CONTIGUOUS subsequence of argv (order-exact)."""
    need = list(words)
    return any(list(argv[i:i + len(need)]) == need for i in range(len(argv) + 1))


class FakeRun:
    """Records every argv; answers from `script` — a list of (predicate, CompletedProcess)
    pairs tried in order — else a default rc-0 empty result."""

    def __init__(self, script=()):
        self.calls: list[list[str]] = []
        self.script = list(script)

    def __call__(self, argv, env=None, **kw) -> subprocess.CompletedProcess:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for pred, result in self.script:
            if pred(argv):
                return result
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


# ── skeleton: tenant gate + the run() seam ──────────────────────────────────────


def test_missing_tenant_fails_fast():
    # compose interpolation requires HIVE_TENANT_ID; the CLI owns the fail-fast and
    # must return EX_CONFIG BEFORE any child invocation.
    fake = FakeRun()
    rc = cli.main(["down"], run=fake, out=io.StringIO(), env={})
    assert rc == cli.EX_CONFIG
    assert fake.calls == []


def test_down_is_compose_down_never_volume_destroying():
    fake = FakeRun()
    rc = cli.main(["down"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_OK
    assert len(fake.calls) == 1
    assert fake.calls[0][:2] == ["docker", "compose"]
    assert seq_in(fake.calls[0], "down")
    assert "-v" not in fake.calls[0]          # down PRESERVES the volume (nuke destroys)


def test_logs_follows_optional_service():
    fake = FakeRun()
    assert cli.main(["logs"], run=fake, out=io.StringIO(), env=ENV) == cli.EX_OK
    assert seq_in(fake.calls[0], "logs", "-f")
    fake2 = FakeRun()
    cli.main(["logs", "ngrok"], run=fake2, out=io.StringIO(), env=ENV)
    assert seq_in(fake2.calls[0], "logs", "-f", "ngrok")


def test_compose_child_failure_maps_to_unavailable():
    fake = FakeRun(script=[(lambda a: seq_in(a, "down"), proc(rc=1))])
    rc = cli.main(["down"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_UNAVAILABLE


# ── up: detached build + bounded health-wait; tunnel is opt-in + fail-fast ──────

_HEALTHY = [(lambda a: seq_in(a, "ps", "-q"), proc(stdout="cid123\n")),
            (lambda a: a[:2] == ["docker", "inspect"], proc(stdout="healthy\n"))]


def test_up_uses_up_detached_not_run_rm():
    # migrated hive.sh contract: the long-lived warm daemon comes up via `up -d`,
    # never an ephemeral cold-start `run --rm` per invocation.
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(["up"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_OK
    assert seq_in(fake.calls[0], "up", "-d", "--build", "hive-server")
    assert not any("run" in c and "--rm" in c for c in fake.calls)


def test_up_loopback_by_default():
    fake = FakeRun(script=list(_HEALTHY))
    cli.main(["up"], run=fake, out=io.StringIO(), env=ENV)
    assert not any("--profile" in c or "tunnel" in c for c in fake.calls)


def test_up_tunnel_requires_secrets():
    # missing NGROK_AUTHTOKEN/NGROK_DOMAIN → EX_CONFIG BEFORE any child call —
    # the CLI owns the fail-fast (compose deliberately `:-` defaults these).
    fake = FakeRun()
    rc = cli.main(["up", "--tunnel"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_CONFIG
    assert fake.calls == []


def test_up_tunnel_sets_profile():
    fake = FakeRun(script=list(_HEALTHY))
    env = dict(ENV, NGROK_AUTHTOKEN="t", NGROK_DOMAIN="brain.ngrok.app")
    rc = cli.main(["up", "--tunnel"], run=fake, out=io.StringIO(), env=env)
    assert rc == cli.EX_OK
    assert seq_in(fake.calls[0], "--profile", "tunnel")
    assert seq_in(fake.calls[0], "up", "-d", "--build")


def test_up_health_timeout_dumps_logs_exits_unavailable():
    # a daemon stuck in `starting` → bounded wait → dump logs → EX_UNAVAILABLE
    # (HIVE_HEALTH_TIMEOUT=0 makes the bound immediate — no sleeps in tests).
    fake = FakeRun(script=[(lambda a: seq_in(a, "ps", "-q"), proc(stdout="cid123\n")),
                           (lambda a: a[:2] == ["docker", "inspect"], proc(stdout="starting\n"))])
    rc = cli.main(["up"], run=fake, out=io.StringIO(),
                  env=dict(ENV, HIVE_HEALTH_TIMEOUT="0"))
    assert rc == cli.EX_UNAVAILABLE
    assert any(seq_in(c, "logs", "--tail=200", "hive-server") for c in fake.calls)


def test_up_unhealthy_dumps_logs_exits_unavailable():
    fake = FakeRun(script=[(lambda a: seq_in(a, "ps", "-q"), proc(stdout="cid123\n")),
                           (lambda a: a[:2] == ["docker", "inspect"], proc(stdout="unhealthy\n"))])
    rc = cli.main(["up"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_UNAVAILABLE
    assert any(seq_in(c, "logs", "--tail=200", "hive-server") for c in fake.calls)


def test_up_missing_container_is_unavailable():
    fake = FakeRun(script=[(lambda a: seq_in(a, "ps", "-q"), proc(stdout=""))])
    rc = cli.main(["up"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_UNAVAILABLE


# ── nuke: typed confirmation guards the volume ──────────────────────────────────


def test_nuke_requires_confirmation():
    fake = FakeRun()
    rc = cli.main(["nuke"], run=fake, out=io.StringIO(), env=ENV, ask=lambda _p: "no")
    assert rc == cli.EX_USAGE
    assert not any("-v" in c for c in fake.calls)      # down -v was NOT issued


def test_nuke_confirmed_issues_down_v():
    fake = FakeRun()
    rc = cli.main(["nuke"], run=fake, out=io.StringIO(), env=ENV, ask=lambda _p: "nuke")
    assert rc == cli.EX_OK
    assert any(seq_in(c, "down", "-v") for c in fake.calls)


# ── status: aggregation (ps + in-container healthcheck + tunnel + seat count) ───


def test_status_aggregates_health_tunnel_and_seats():
    fake = FakeRun(script=[
        (lambda a: seq_in(a, "ps", "ngrok"), proc(stdout="ngrok Up 2 hours\n")),
        (lambda a: seq_in(a, "ps", "hive-server"), proc(stdout="hive-server Up (healthy)\n")),
        (lambda a: seq_in(a, "list"), proc(stdout="alice\nbob\n")),
    ])
    out = io.StringIO()
    rc = cli.main(["status"], run=fake, out=out,
                  env=dict(ENV, NGROK_DOMAIN="brain.ngrok.app"))
    assert rc == cli.EX_OK
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.healthcheck") for c in fake.calls)
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.authctl", "list") for c in fake.calls)
    text = out.getvalue()
    assert "healthy" in text
    assert "https://brain.ngrok.app/mcp" in text       # tunnel on + the public URL
    assert "2" in text                                  # seat count from authctl list


def test_status_down_server_short_circuits():
    fake = FakeRun(script=[(lambda a: seq_in(a, "ps", "hive-server"), proc(rc=1))])
    out = io.StringIO()
    rc = cli.main(["status"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_UNAVAILABLE
    assert "down" in out.getvalue()
    assert not any(seq_in(c, "exec") for c in fake.calls)   # no exec against a down stack
