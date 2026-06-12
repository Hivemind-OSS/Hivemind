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
