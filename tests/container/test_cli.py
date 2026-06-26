"""ADMIN-CLI — the `hive` operator CLI: argv contracts via the injected fake runner.

Every verb shells through the one `run()` seam, so a fake that records argv makes the
compose/exec invocations assertable facts, not prose — a wrong invocation reds a test,
no Docker needed. The helpers (`FakeRun`/`proc`/`seq_in`) are module-level and reusable:
successor verbs (e.g. `credit`) add their argv tests here against the same fake.
"""
from __future__ import annotations

import io
import os
import subprocess

import pytest

from hive.tools import cli

ENV = {"HIVE_TENANT_ID": "acme"}


def proc(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


def seq_in(argv, *words) -> bool:
    """True iff `words` appears as a CONTIGUOUS subsequence of argv (order-exact)."""
    need = list(words)
    return any(list(argv[i:i + len(need)]) == need for i in range(len(argv) + 1))


class FakeRun:
    """Records every argv (+ the call kwargs, e.g. the stdin `input=` pipe); answers
    from `script` — a list of (predicate, CompletedProcess) pairs tried in order —
    else a default rc-0 empty result."""

    def __init__(self, script=()):
        self.calls: list[list[str]] = []
        self.kws: list[dict] = []
        self.script = list(script)

    def __call__(self, argv, env=None, **kw) -> subprocess.CompletedProcess:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.kws.append(dict(kw))
        for pred, result in self.script:
            if pred(argv):
                return result
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


# ── skeleton: the run() seam ─────────────────────────────────────────────────────


def test_down_is_compose_down_never_volume_destroying():
    fake = FakeRun()
    rc = cli.main(["down"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_OK
    assert len(fake.calls) == 1
    assert fake.calls[0][:2] == ["docker", "compose"]
    assert seq_in(fake.calls[0], "down")
    assert "-v" not in fake.calls[0]          # down PRESERVES the volume (reset destroys)


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
    # the long-lived warm daemon comes up via `up -d`, never an ephemeral
    # cold-start `run --rm` per invocation (which re-warms the embedder each time).
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


# ── MODE-COLLAPSE: --tunnel no longer refuses any auth posture (the door is gated) ──
def test_up_tunnel_proceeds_regardless_of_stale_auth_env():
    # the --tunnel-refuses-open rail is GONE: the tunnel door is structurally token-gated, so
    # even a leftover HIVE_AUTH__MODE in the env does not block --tunnel (secrets present).
    fake = FakeRun(script=list(_HEALTHY))
    env = dict(ENV, HIVE_AUTH__MODE="open", NGROK_AUTHTOKEN="t", NGROK_DOMAIN="brain.ngrok.app")
    rc = cli.main(["up", "--tunnel"], run=fake, out=io.StringIO(), env=env)
    assert rc == cli.EX_OK
    assert seq_in(fake.calls[0], "--profile", "tunnel")


# ── .env folded UNDER the shell env: ONE source for the CLI gate and compose ─────
def test_load_dotenv_adds_keys_absent_from_shell(tmp_path):
    p = tmp_path / ".env"
    p.write_text("NGROK_AUTHTOKEN=tok\nNGROK_DOMAIN=you.ngrok.app\n")
    merged = cli._load_dotenv(str(p), {"HIVE_TENANT_ID": "acme"})
    assert merged["NGROK_AUTHTOKEN"] == "tok"
    assert merged["NGROK_DOMAIN"] == "you.ngrok.app"
    assert merged["HIVE_TENANT_ID"] == "acme"            # base preserved


def test_load_dotenv_shell_env_wins(tmp_path):
    # compose precedence: a var already set in the shell is NEVER overridden by .env
    p = tmp_path / ".env"
    p.write_text("NGROK_DOMAIN=from-dotenv\n")
    merged = cli._load_dotenv(str(p), {"NGROK_DOMAIN": "from-shell"})
    assert merged["NGROK_DOMAIN"] == "from-shell"


def test_load_dotenv_skips_comments_blanks_and_malformed(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# a comment\n\n   \nNOEQUALS\nNGROK_AUTHTOKEN=tok\n  # indented\n")
    assert cli._load_dotenv(str(p), {}) == {"NGROK_AUTHTOKEN": "tok"}


def test_load_dotenv_missing_file_returns_base_copy():
    base = {"HIVE_TENANT_ID": "acme"}
    merged = cli._load_dotenv("/no/such/.env", base)
    assert merged == base and merged is not base         # equal, but a fresh dict


def test_main_folds_dotenv_into_tunnel_check(tmp_path, monkeypatch):
    """The fix, end-to-end: with the NGROK secrets ONLY in the repo-root .env (absent from
    the shell env), `up --tunnel` no longer fail-fasts — main() folds .env in first."""
    (tmp_path / ".env").write_text("NGROK_AUTHTOKEN=tok\nNGROK_DOMAIN=you.ngrok.app\n")
    monkeypatch.chdir(tmp_path)                           # _DOTENV is cwd-relative, like compose
    monkeypatch.setattr(cli.os, "environ", {"HIVE_TENANT_ID": "acme"})   # clean shell: no NGROK
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(["up", "--tunnel"], run=fake, out=io.StringIO())       # env=None → prod path
    assert rc == cli.EX_OK
    assert seq_in(fake.calls[0], "--profile", "tunnel")  # .env alone satisfied the secret gate


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


# ── reset: snapshot OUT of the volume FIRST, then destroy + recreate (recoverable) ──

# the snapshot runs backupctl in a throwaway container; locate that call by its module args
# (NOT a contiguous "python -m ..." — python is the --entrypoint value, the module is the cmd).
def _is_snapshot(c):
    return c[:2] == ["docker", "run"] and seq_in(c, "-m", "hive.tools.backupctl")


def test_reset_requires_confirmation():
    fake = FakeRun()
    rc = cli.main(["reset"], run=fake, out=io.StringIO(), env=ENV, ask=lambda _p: "no")
    assert rc == cli.EX_USAGE
    assert fake.calls == []                             # confirmation gates BEFORE any work


def test_reset_snapshots_before_destroying():
    # the safety invariant: the store is snapshotted OUT of the volume BEFORE `down -v`,
    # and the daemon is recreated after — one verb does the whole clean-start.
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(["reset"], run=fake, out=io.StringIO(), env=ENV, ask=lambda _p: "reset")
    assert rc == cli.EX_OK
    snap_idx = next(i for i, c in enumerate(fake.calls) if _is_snapshot(c))
    downv_idx = next(i for i, c in enumerate(fake.calls) if seq_in(c, "down", "-v"))
    snap = fake.calls[snap_idx]
    assert seq_in(snap, "--user", "0:0")                   # root: reads the uid-owned store + writes host
    assert seq_in(snap, "--entrypoint", "python")          # override the image's daemon ENTRYPOINT
    assert snap_idx < downv_idx                            # snapshot BEFORE destroy
    assert any(seq_in(c, "up", "-d", "--build", "hive-server") for c in fake.calls)


def test_reset_aborts_without_destroying_if_snapshot_fails():
    # if the pre-reset snapshot fails, the volume is NEVER destroyed (fail-safe toward
    # preservation) — an accidental or re-failing reset costs nothing.
    fake = FakeRun(script=[(_is_snapshot, proc(rc=1, stderr="backup boom"))])
    rc = cli.main(["reset"], run=fake, out=io.StringIO(), env=ENV, ask=lambda _p: "reset")
    assert rc == cli.EX_UNAVAILABLE
    assert any(_is_snapshot(c) for c in fake.calls)
    assert not any(seq_in(c, "down", "-v") for c in fake.calls)         # destroy NOT reached
    assert not any(seq_in(c, "--entrypoint", "chown") for c in fake.calls)


def test_reset_chowns_snapshot_back_to_operator():
    fake = FakeRun(script=list(_HEALTHY))
    cli.main(["reset", "--yes"], run=fake, out=io.StringIO(), env=ENV)
    chown = next(c for c in fake.calls if seq_in(c, "--entrypoint", "chown"))
    assert seq_in(chown, "-R", f"{os.getuid()}:{os.getgid()}", "/out")


def test_reset_yes_skips_confirmation():
    def _no_ask(_p):
        raise AssertionError("--yes must not prompt")
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(["reset", "--yes"], run=fake, out=io.StringIO(), env=ENV, ask=_no_ask)
    assert rc == cli.EX_OK
    assert any(seq_in(c, "down", "-v") for c in fake.calls)


def test_reset_snapshot_targets_host_out_dir(tmp_path, monkeypatch):
    # the snapshot lands in a HOST bind dir (default ./hive-backups, abspath'd so docker reads
    # it as a bind, not a named volume); --out overrides it. Env routes backupctl's dest to /out.
    monkeypatch.chdir(tmp_path)
    fake = FakeRun(script=list(_HEALTHY))
    assert cli.main(["reset", "--yes"], run=fake, out=io.StringIO(), env=ENV) == cli.EX_OK
    snap = next(c for c in fake.calls if _is_snapshot(c))
    assert any(tok.startswith("/") and tok.endswith("/hive-backups:/out") for tok in snap)
    assert seq_in(snap, "-v", "hive-data:/data")
    assert seq_in(snap, "-e", "HIVE_RETENTION__BACKUP_DIR=/out")
    fake2 = FakeRun(script=list(_HEALTHY))
    cli.main(["reset", "--yes", "--out", "/custom/dir"], run=fake2, out=io.StringIO(), env=ENV)
    snap2 = next(c for c in fake2.calls if _is_snapshot(c))
    assert seq_in(snap2, "-v", "/custom/dir:/out")


# ── restore: replace the live store with a host snapshot (inverse of reset) ─────────


def test_restore_rejects_missing_file():
    fake = FakeRun()
    rc = cli.main(["restore", "/no/such/snap.db"], run=fake, out=io.StringIO(), env=ENV,
                  ask=lambda _p: "restore")
    assert rc == cli.EX_USAGE
    assert fake.calls == []                             # never touches the stack for a missing file


def test_restore_requires_confirmation(tmp_path):
    snap = tmp_path / "snap.db"
    snap.write_bytes(b"sqlite")
    fake = FakeRun()
    rc = cli.main(["restore", str(snap)], run=fake, out=io.StringIO(), env=ENV, ask=lambda _p: "no")
    assert rc == cli.EX_USAGE
    assert fake.calls == []                             # nothing stopped, nothing copied


def test_restore_copies_into_volume_between_down_and_up(tmp_path):
    snap = tmp_path / "snap.db"
    snap.write_bytes(b"sqlite")
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(["restore", str(snap)], run=fake, out=io.StringIO(), env=ENV, ask=lambda _p: "restore")
    assert rc == cli.EX_OK
    down_idx = next(i for i, c in enumerate(fake.calls) if seq_in(c, "down") and "-v" not in c)
    cp_idx = next(i for i, c in enumerate(fake.calls)
                  if c[:2] == ["docker", "run"] and seq_in(c, "--entrypoint", "sh"))
    up_idx = next(i for i, c in enumerate(fake.calls) if seq_in(c, "up", "-d", "--build"))
    assert down_idx < cp_idx < up_idx                  # stop → overwrite → restart
    cp = fake.calls[cp_idx]
    assert seq_in(cp, "-v", "hive-data:/data")
    assert any("cp /in/snap.db /data/shared.db" in tok for tok in cp)


def test_restore_yes_skips_confirmation(tmp_path):
    snap = tmp_path / "snap.db"
    snap.write_bytes(b"sqlite")

    def _no_ask(_p):
        raise AssertionError("--yes must not prompt")
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(["restore", str(snap), "--yes"], run=fake, out=io.StringIO(), env=ENV, ask=_no_ask)
    assert rc == cli.EX_OK


def test_restore_aborts_if_copy_fails(tmp_path):
    snap = tmp_path / "snap.db"
    snap.write_bytes(b"sqlite")
    fake = FakeRun(script=[(lambda a: a[:2] == ["docker", "run"] and seq_in(a, "--entrypoint", "sh"),
                            proc(rc=1, stderr="cp boom"))])
    rc = cli.main(["restore", str(snap), "--yes"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_UNAVAILABLE
    assert not any(seq_in(c, "up", "-d") for c in fake.calls)   # no restart after a failed copy


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


# ── provisioning: token / revoke / tokens shell to the in-container authctl ─────


def test_token_builds_authctl_create(capsys):
    fake = FakeRun(script=[(lambda a: seq_in(a, "create"), proc(stdout="hive_abc123\n"))])
    out = io.StringIO()
    rc = cli.main(["token", "alice-laptop"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_OK
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.authctl", "create", "alice-laptop") for c in fake.calls)
    assert out.getvalue() == "hive_abc123\n"          # the credential: child stdout ONLY
    err = capsys.readouterr().err                      # the AC7 seat-contract handoff hint
    assert "one token per seat" in err and "never share across agents" in err
    assert "hive_abc123" not in err                    # the token is never echoed elsewhere


def test_token_child_failure_forwards_sysexits(capsys):
    fake = FakeRun(script=[(lambda a: seq_in(a, "create"),
                            proc(rc=70, stderr="authctl: a token already exists\n"))])
    out = io.StringIO()
    rc = cli.main(["token", "dup"], run=fake, out=out, env=ENV)
    assert rc == 70                                    # authctl already speaks sysexits
    assert out.getvalue() == ""                        # no token line on failure
    assert "already exists" in capsys.readouterr().err


def test_revoke_builds_authctl_revoke():
    fake = FakeRun()
    rc = cli.main(["revoke", "alice-laptop"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_OK
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.authctl", "revoke", "alice-laptop") for c in fake.calls)


def test_tokens_builds_authctl_list():
    fake = FakeRun(script=[(lambda a: seq_in(a, "list"), proc(stdout="alice\nbob\n"))])
    out = io.StringIO()
    rc = cli.main(["tokens"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_OK
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.authctl", "list") for c in fake.calls)
    assert out.getvalue() == "alice\nbob\n"            # labels forwarded verbatim


# ── connect: transport registration line only — never the handshake ────────────


def test_connect_renders_mcp_add_line(capsys):
    fake = FakeRun()
    out = io.StringIO()
    rc = cli.main(["connect"], run=fake, out=out,
                  env={"NGROK_DOMAIN": "brain.ngrok.app"})    # no tenant needed: local verb
    assert rc == cli.EX_OK
    assert fake.calls == []                            # purely local — nothing is run
    text = out.getvalue()
    assert ('claude mcp add --transport http hive https://brain.ngrok.app/mcp '
            '--header "Authorization: Bearer ${HIVE_TOKEN}"') in text
    err = capsys.readouterr().err
    assert "hive token <seat>" in err                  # AC7: the inline seat hint
    assert "hive_init" not in text + err               # M11/M12: no handshake here


def test_connect_without_domain_prints_tokenless_loopback_line(capsys):
    out = io.StringIO()
    rc = cli.main(["connect"], run=FakeRun(), out=out, env={})
    assert rc == cli.EX_OK
    text = out.getvalue()
    assert "http://localhost:8765/mcp" in text
    assert "Authorization: Bearer" not in text         # the loopback door is tokenless
    assert "X-Hive-Agent-Id" not in text               # no baked id on the registration line
    err = capsys.readouterr().err
    assert "NGROK_DOMAIN" in err                        # says why it fell back to loopback
    assert "X-Hive-Agent-Id" in err                     # the per-client explicit-id NOTE is shown


def test_default_run_forwards_stdin_input():
    # default_run forwards stdin to the child (the keyword-only `input=` seam)
    p = cli.default_run(["cat"], None, input="ndjson-line\n")
    assert p.returncode == 0 and p.stdout == "ndjson-line\n"


# ── backup: one-shot snapshot (exec → backupctl, dest path forwarded) ──────────
def test_backup_forwards_child_stdout():
    path = "/data/backups/hive-20260616-000000.db\n"
    fake = FakeRun(script=[(lambda a: seq_in(a, "python", "-m", "hive.tools.backupctl"),
                            proc(stdout=path))])
    out = io.StringIO()
    rc = cli.main(["backup"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_OK
    assert out.getvalue() == path                            # snapshot path forwarded verbatim
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.backupctl") for c in fake.calls)   # exec'd in-container


def test_backup_maps_child_failure_to_unavailable():
    fake = FakeRun(script=[(lambda a: seq_in(a, "hive.tools.backupctl"),
                            proc(rc=1, stderr="boom"))])
    rc = cli.main(["backup"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_UNAVAILABLE


# ── health verb removed (KPI trends ride hive_health(include_trends) over MCP) ──
def test_health_verb_is_absent():
    # the host-side `hive health` wrapper was cut; the §8.3 demand-health window now
    # lives ONLY on hive_health(include_trends=true) over MCP. argparse rejects it.
    assert "health" not in cli._HANDLERS
    with pytest.raises(SystemExit):
        cli.main(["health"], run=FakeRun(script=[]), out=io.StringIO(), env=ENV)
