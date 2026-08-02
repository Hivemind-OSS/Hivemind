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


def proc(
    rc: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


def seq_in(argv, *words) -> bool:
    """True iff `words` appears as a CONTIGUOUS subsequence of argv (order-exact)."""
    need = list(words)
    return any(list(argv[i : i + len(need)]) == need for i in range(len(argv) + 1))


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
    assert "-v" not in fake.calls[0]  # down PRESERVES the volume (reset destroys)


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

_HEALTHY = [
    (lambda a: seq_in(a, "ps", "-q"), proc(stdout="cid123\n")),
    (lambda a: a[:2] == ["docker", "inspect"], proc(stdout="healthy\n")),
]


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
    env = dict(
        ENV, HIVE_AUTH__MODE="open", NGROK_AUTHTOKEN="t", NGROK_DOMAIN="brain.ngrok.app"
    )
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
    assert merged["HIVE_TENANT_ID"] == "acme"  # base preserved


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
    assert merged == base and merged is not base  # equal, but a fresh dict


def test_main_folds_dotenv_into_tunnel_check(tmp_path, monkeypatch):
    """The fix, end-to-end: with the NGROK secrets ONLY in the repo-root .env (absent from
    the shell env), `up --tunnel` no longer fail-fasts — main() folds .env in first."""
    (tmp_path / ".env").write_text("NGROK_AUTHTOKEN=tok\nNGROK_DOMAIN=you.ngrok.app\n")
    monkeypatch.chdir(tmp_path)  # _DOTENV is cwd-relative, like compose
    monkeypatch.setattr(
        cli.os, "environ", {"HIVE_TENANT_ID": "acme"}
    )  # clean shell: no NGROK
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(
        ["up", "--tunnel"], run=fake, out=io.StringIO()
    )  # env=None → prod path
    assert rc == cli.EX_OK
    assert seq_in(
        fake.calls[0], "--profile", "tunnel"
    )  # .env alone satisfied the secret gate


def test_up_health_timeout_dumps_logs_exits_unavailable():
    # a daemon stuck in `starting` → bounded wait → dump logs → EX_UNAVAILABLE
    # (HIVE_HEALTH_TIMEOUT=0 makes the bound immediate — no sleeps in tests).
    fake = FakeRun(
        script=[
            (lambda a: seq_in(a, "ps", "-q"), proc(stdout="cid123\n")),
            (lambda a: a[:2] == ["docker", "inspect"], proc(stdout="starting\n")),
        ]
    )
    rc = cli.main(
        ["up"], run=fake, out=io.StringIO(), env=dict(ENV, HIVE_HEALTH_TIMEOUT="0")
    )
    assert rc == cli.EX_UNAVAILABLE
    assert any(seq_in(c, "logs", "--tail=200", "hive-server") for c in fake.calls)


def test_up_unhealthy_dumps_logs_exits_unavailable():
    fake = FakeRun(
        script=[
            (lambda a: seq_in(a, "ps", "-q"), proc(stdout="cid123\n")),
            (lambda a: a[:2] == ["docker", "inspect"], proc(stdout="unhealthy\n")),
        ]
    )
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
    assert fake.calls == []  # confirmation gates BEFORE any work


def test_reset_snapshots_before_destroying():
    # the safety invariant: the store is snapshotted OUT of the volume BEFORE `down -v`,
    # and the daemon is recreated after — one verb does the whole clean-start.
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(
        ["reset"], run=fake, out=io.StringIO(), env=ENV, ask=lambda _p: "reset"
    )
    assert rc == cli.EX_OK
    snap_idx = next(i for i, c in enumerate(fake.calls) if _is_snapshot(c))
    downv_idx = next(i for i, c in enumerate(fake.calls) if seq_in(c, "down", "-v"))
    snap = fake.calls[snap_idx]
    assert seq_in(
        snap, "--user", "0:0"
    )  # root: reads the uid-owned store + writes host
    assert seq_in(
        snap, "--entrypoint", "python"
    )  # override the image's daemon ENTRYPOINT
    assert snap_idx < downv_idx  # snapshot BEFORE destroy
    assert any(seq_in(c, "up", "-d", "--build", "hive-server") for c in fake.calls)


def test_reset_prints_rollback_line(capsys):
    # O8: a successful reset tells the operator, on STDERR, exactly how to recover — the
    # `hive restore <snapshot dest>` line — so the destructive clean-start is copy-paste
    # reversible. The dest is the host snapshot dir (default ./hive-backups, abspath'd).
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(["reset", "--yes"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_OK
    err = capsys.readouterr().err
    assert "roll back with: hive restore" in err
    assert (
        os.path.abspath(cli._DEFAULT_RESET_OUT) in err
    )  # names the snapshot dest path


def test_reset_aborts_without_destroying_if_snapshot_fails():
    # if the pre-reset snapshot fails, the volume is NEVER destroyed (fail-safe toward
    # preservation) — an accidental or re-failing reset costs nothing.
    fake = FakeRun(script=[(_is_snapshot, proc(rc=1, stderr="backup boom"))])
    rc = cli.main(
        ["reset"], run=fake, out=io.StringIO(), env=ENV, ask=lambda _p: "reset"
    )
    assert rc == cli.EX_UNAVAILABLE
    assert any(_is_snapshot(c) for c in fake.calls)
    assert not any(seq_in(c, "down", "-v") for c in fake.calls)  # destroy NOT reached
    assert not any(seq_in(c, "--entrypoint", "chown") for c in fake.calls)


def test_reset_chowns_snapshot_back_to_operator():
    fake = FakeRun(script=list(_HEALTHY))
    cli.main(["reset", "--yes"], run=fake, out=io.StringIO(), env=ENV)
    chown = next(c for c in fake.calls if seq_in(c, "--entrypoint", "chown"))
    assert seq_in(chown, "-R", f"{os.getuid()}:{os.getgid()}", "/out")


def test_reset_skips_chown_back_on_non_posix(monkeypatch):
    # native Windows Python has no os.getuid/getgid (no POSIX ownership model) and Docker Desktop
    # bind mounts are already host-owned, so the chown-back is both impossible and unnecessary —
    # it must be SKIPPED, not attempted (an unconditional call raises AttributeError there). Simulate
    # that os surface on POSIX CI by deleting the attributes; the snapshot must still succeed.
    monkeypatch.delattr(cli.os, "getuid", raising=False)
    monkeypatch.delattr(cli.os, "getgid", raising=False)
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(["reset", "--yes"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_OK
    assert not any(seq_in(c, "--entrypoint", "chown") for c in fake.calls)


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
    assert (
        cli.main(["reset", "--yes"], run=fake, out=io.StringIO(), env=ENV) == cli.EX_OK
    )
    snap = next(c for c in fake.calls if _is_snapshot(c))
    assert any(
        tok.startswith("/") and tok.endswith("/hive-backups:/out") for tok in snap
    )
    assert seq_in(snap, "-v", "hive-data:/data")
    assert seq_in(snap, "-e", "HIVE_RETENTION__BACKUP_DIR=/out")
    fake2 = FakeRun(script=list(_HEALTHY))
    cli.main(
        ["reset", "--yes", "--out", "/custom/dir"],
        run=fake2,
        out=io.StringIO(),
        env=ENV,
    )
    snap2 = next(c for c in fake2.calls if _is_snapshot(c))
    assert seq_in(snap2, "-v", "/custom/dir:/out")


# ── restore: replace the live store with a host snapshot (inverse of reset) ─────────


def test_restore_rejects_missing_file():
    fake = FakeRun()
    rc = cli.main(
        ["restore", "/no/such/snap.db"],
        run=fake,
        out=io.StringIO(),
        env=ENV,
        ask=lambda _p: "restore",
    )
    assert rc == cli.EX_USAGE
    assert fake.calls == []  # never touches the stack for a missing file


def test_restore_requires_confirmation(tmp_path):
    snap = tmp_path / "snap.db"
    snap.write_bytes(b"sqlite")
    fake = FakeRun()
    rc = cli.main(
        ["restore", str(snap)],
        run=fake,
        out=io.StringIO(),
        env=ENV,
        ask=lambda _p: "no",
    )
    assert rc == cli.EX_USAGE
    assert fake.calls == []  # nothing stopped, nothing copied


def test_restore_copies_into_volume_between_down_and_up(tmp_path):
    snap = tmp_path / "snap.db"
    snap.write_bytes(b"sqlite")
    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(
        ["restore", str(snap)],
        run=fake,
        out=io.StringIO(),
        env=ENV,
        ask=lambda _p: "restore",
    )
    assert rc == cli.EX_OK
    down_idx = next(
        i for i, c in enumerate(fake.calls) if seq_in(c, "down") and "-v" not in c
    )
    cp_idx = next(
        i
        for i, c in enumerate(fake.calls)
        if c[:2] == ["docker", "run"] and seq_in(c, "--entrypoint", "sh")
    )
    up_idx = next(
        i for i, c in enumerate(fake.calls) if seq_in(c, "up", "-d", "--build")
    )
    assert down_idx < cp_idx < up_idx  # stop → overwrite → restart
    cp = fake.calls[cp_idx]
    assert seq_in(cp, "-v", "hive-data:/data")
    assert any("cp /in/snap.db /data/shared.db" in tok for tok in cp)


def test_restore_yes_skips_confirmation(tmp_path):
    snap = tmp_path / "snap.db"
    snap.write_bytes(b"sqlite")

    def _no_ask(_p):
        raise AssertionError("--yes must not prompt")

    fake = FakeRun(script=list(_HEALTHY))
    rc = cli.main(
        ["restore", str(snap), "--yes"],
        run=fake,
        out=io.StringIO(),
        env=ENV,
        ask=_no_ask,
    )
    assert rc == cli.EX_OK


def test_restore_aborts_if_copy_fails(tmp_path):
    snap = tmp_path / "snap.db"
    snap.write_bytes(b"sqlite")
    fake = FakeRun(
        script=[
            (
                lambda a: (
                    a[:2] == ["docker", "run"] and seq_in(a, "--entrypoint", "sh")
                ),
                proc(rc=1, stderr="cp boom"),
            )
        ]
    )
    rc = cli.main(["restore", str(snap), "--yes"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_UNAVAILABLE
    assert not any(
        seq_in(c, "up", "-d") for c in fake.calls
    )  # no restart after a failed copy


# ── upgrade: move the server to a release ref (snapshot-gated + auto-rollback) ──


def _is_checkout(c, ref):
    return c[:1] == ["git"] and seq_in(c, "checkout", ref)


# a full happy-path upgrade: clean tree (default empty `status`) → fetch + ref resolve
# (default rc 0) → HEAD captured → host snapshot ok → rebuild container-healthy → app
# status healthy. Only the calls that must return a payload are scripted; the rest default.
_UPGRADE_OK = [
    (lambda a: seq_in(a, "rev-parse", "HEAD"), proc(stdout="oldsha0000\n")),
    (_is_snapshot, proc(stdout="/out/hive-STAMP.db\n")),
    (lambda a: seq_in(a, "ps", "-q"), proc(stdout="cid123\n")),
    (lambda a: a[:2] == ["docker", "inspect"], proc(stdout="healthy\n")),
    (
        lambda a: seq_in(a, "ps", "hive-server") and "-q" not in a,
        proc(stdout="hive-server Up (healthy)\n"),
    ),
    (lambda a: any("healthcheck" in t for t in a), proc(rc=0)),
]


def test_upgrade_order_status_fetch_snapshot_checkout_up():
    # ordered like reset: check the tree → fetch/verify the ref → snapshot the store OUT →
    # checkout the ref → rebuild. A wrong order reds here.
    fake = FakeRun(script=list(_UPGRADE_OK))
    rc = cli.main(["upgrade", "--yes"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_OK

    def order(pred):
        return next(i for i, c in enumerate(fake.calls) if pred(c))

    i_status = order(lambda c: seq_in(c, "status", "--porcelain"))
    i_fetch = order(lambda c: c[:1] == ["git"] and seq_in(c, "fetch"))
    i_snap = order(_is_snapshot)
    i_checkout = order(lambda c: _is_checkout(c, "release"))
    i_up = order(lambda c: seq_in(c, "up", "-d", "--build", "hive-server"))
    assert i_status < i_fetch < i_snap < i_checkout < i_up


def test_upgrade_aborts_before_checkout_when_snapshot_fails():
    # the safety invariant (mirrors reset): if the pre-upgrade snapshot fails, the ref is
    # NEVER checked out and the server is NEVER rebuilt — the running version is untouched.
    fake = FakeRun(script=[(_is_snapshot, proc(rc=1, stderr="backup boom"))])
    rc = cli.main(["upgrade", "--yes"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_UNAVAILABLE
    assert any(_is_snapshot(c) for c in fake.calls)
    assert not any(
        _is_checkout(c, "release") for c in fake.calls
    )  # checkout NOT reached
    assert not any(seq_in(c, "up", "-d", "--build") for c in fake.calls)  # no rebuild


def test_upgrade_aborts_on_dirty_tree():
    # a dirty tree aborts BEFORE any fetch/snapshot/checkout — no magic stash.
    fake = FakeRun(
        script=[
            (
                lambda a: seq_in(a, "status", "--porcelain"),
                proc(stdout=" M hive/tools/cli.py\n"),
            )
        ]
    )
    rc = cli.main(["upgrade", "--yes"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_USAGE
    assert not any(c[:1] == ["git"] and seq_in(c, "fetch") for c in fake.calls)
    assert not any(_is_snapshot(c) for c in fake.calls)
    assert not any(_is_checkout(c, "release") for c in fake.calls)


def test_upgrade_aborts_when_ref_not_found():
    # a ref that does not resolve after fetch aborts before the snapshot (no work for a bogus ref).
    fake = FakeRun(
        script=[
            (
                lambda a: seq_in(a, "rev-parse", "--verify", "--quiet", "release"),
                proc(rc=1),
            )
        ]
    )
    rc = cli.main(["upgrade", "--yes"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_USAGE
    assert not any(_is_snapshot(c) for c in fake.calls)


def test_upgrade_requires_confirmation():
    fake = FakeRun(script=list(_UPGRADE_OK))
    rc = cli.main(
        ["upgrade"], run=fake, out=io.StringIO(), env=ENV, ask=lambda _p: "no"
    )
    assert rc == cli.EX_USAGE
    assert not any(_is_snapshot(c) for c in fake.calls)  # declined before the snapshot


def test_upgrade_yes_skips_confirmation():
    def _no_ask(_p):
        raise AssertionError("--yes must not prompt")

    fake = FakeRun(script=list(_UPGRADE_OK))
    assert (
        cli.main(
            ["upgrade", "--yes"], run=fake, out=io.StringIO(), env=ENV, ask=_no_ask
        )
        == cli.EX_OK
    )


def _upgrade_then_unhealthy_status():
    # rebuild is container-healthy but the app status gate reports UNHEALTHY → triggers rollback.
    return [
        (lambda a: seq_in(a, "rev-parse", "HEAD"), proc(stdout="oldsha0000\n")),
        (_is_snapshot, proc(stdout="/out/hive-STAMP.db\n")),
        (lambda a: seq_in(a, "ps", "-q"), proc(stdout="cid123\n")),
        (lambda a: a[:2] == ["docker", "inspect"], proc(stdout="healthy\n")),
        (
            lambda a: seq_in(a, "ps", "hive-server") and "-q" not in a,
            proc(stdout="hive-server Up\n"),
        ),
        (
            lambda a: any("healthcheck" in t for t in a),
            proc(rc=1),
        ),  # app-level unhealthy → gate fails
    ]


def test_upgrade_rolls_back_when_health_gate_fails():
    fake = FakeRun(script=_upgrade_then_unhealthy_status())
    rc = cli.main(["upgrade", "--yes"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_UNAVAILABLE  # upgrade failed, but recovered
    assert any(
        _is_checkout(c, "oldsha0000") for c in fake.calls
    )  # code reverted to prev
    cp = next(
        c
        for c in fake.calls
        if c[:2] == ["docker", "run"] and seq_in(c, "--entrypoint", "sh")
    )
    assert seq_in(cp, "-v", "hive-data:/data")
    assert any(
        "cp /in/hive-STAMP.db /data/shared.db" in tok for tok in cp
    )  # store restored
    i_revert = next(
        i for i, c in enumerate(fake.calls) if _is_checkout(c, "oldsha0000")
    )
    i_copy = next(
        i
        for i, c in enumerate(fake.calls)
        if c[:2] == ["docker", "run"] and seq_in(c, "--entrypoint", "sh")
    )
    assert i_revert < i_copy  # revert code THEN restore store


def test_upgrade_prints_manual_hint_when_rollback_itself_fails(capsys):
    # double failure: the health gate fails AND the revert `git checkout <prev>` also fails →
    # print the exact manual recovery (git checkout + hive restore) and exit EX_SOFTWARE.
    script = _upgrade_then_unhealthy_status() + [
        (lambda a: _is_checkout(a, "oldsha0000"), proc(rc=1, stderr="checkout boom")),
    ]
    rc = cli.main(
        ["upgrade", "--yes"], run=FakeRun(script=script), out=io.StringIO(), env=ENV
    )
    assert rc == cli.EX_SOFTWARE
    err = capsys.readouterr().err
    assert "git checkout oldsha0000" in err
    assert "hive restore" in err


def test_connect_prints_no_edge_install_and_no_onboarding_breadcrumb(capsys):
    # v3 thin-agent connect: the MCP registration line only — no edge-install
    # instruction, no per-repo setup fetch. Agents are repo-agnostic MCP clients;
    # census evidence is server-side off the repo registry (`hive repo add`).
    out = io.StringIO()
    cli.main(["connect"], run=FakeRun(), out=out, env=ENV)
    err = capsys.readouterr().err
    both = out.getvalue() + err
    assert "hive-edge" not in both  # no edge-install breadcrumb survives
    assert (
        "HIVE_SYNC__REPO_URL" not in both
    )  # the deleted sync env var never resurfaces
    assert "HIVE-ADMIN.md" not in both  # no doc-fetch pointer rides connect


# ── status: aggregation (ps + in-container healthcheck + tunnel + seat count) ───


def test_status_aggregates_health_tunnel_and_seats():
    fake = FakeRun(
        script=[
            (lambda a: seq_in(a, "ps", "ngrok"), proc(stdout="ngrok Up 2 hours\n")),
            (
                lambda a: seq_in(a, "ps", "hive-server"),
                proc(stdout="hive-server Up (healthy)\n"),
            ),
            (lambda a: seq_in(a, "list"), proc(stdout="alice\nbob\n")),
        ]
    )
    out = io.StringIO()
    rc = cli.main(
        ["status"], run=fake, out=out, env=dict(ENV, NGROK_DOMAIN="brain.ngrok.app")
    )
    assert rc == cli.EX_OK
    assert any(
        seq_in(c, "exec", "-T", "hive-server", "python", "-m", "hive.tools.healthcheck")
        for c in fake.calls
    )
    assert any(
        seq_in(
            c, "exec", "-T", "hive-server", "python", "-m", "hive.tools.authctl", "list"
        )
        for c in fake.calls
    )
    text = out.getvalue()
    assert "healthy" in text
    assert "https://brain.ngrok.app/mcp" in text  # tunnel on + the public URL
    assert "2" in text  # seat count from authctl list


def test_status_down_server_short_circuits():
    fake = FakeRun(script=[(lambda a: seq_in(a, "ps", "hive-server"), proc(rc=1))])
    out = io.StringIO()
    rc = cli.main(["status"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_UNAVAILABLE
    assert "down" in out.getvalue()
    assert not any(
        seq_in(c, "exec") for c in fake.calls
    )  # no exec against a down stack


# ── _probe_status: the single-owner StatusSnapshot the text + JSON paths both consume ──


def test_status_snapshot_is_frozen_slots_with_failsafe_defaults():
    import dataclasses

    # Law 2: a frozen carrier whose defaults UNDER-claim (an unreachable stack is reported as
    # down/None/False, never optimistically up) — so /api/status and _status agree on the safe floor.
    snap = cli.StatusSnapshot()
    assert (snap.server, snap.healthy, snap.tunnel_on, snap.tunnel_url, snap.seats) == (
        "down",
        None,
        False,
        None,
        None,
    )
    assert dataclasses.is_dataclass(snap) and hasattr(cli.StatusSnapshot, "__slots__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.server = "up"  # frozen: the carrier cannot be mutated


def test_probe_status_up_aggregates_health_tunnel_and_seats():
    fake = FakeRun(
        script=[
            (lambda a: seq_in(a, "ps", "ngrok"), proc(stdout="ngrok Up 2 hours\n")),
            (
                lambda a: seq_in(a, "ps", "hive-server"),
                proc(stdout="hive-server Up (healthy)\n"),
            ),
            (lambda a: seq_in(a, "list"), proc(stdout="alice\nbob\n")),
        ]
    )
    snap = cli._probe_status(fake, dict(ENV, NGROK_DOMAIN="brain.ngrok.app"))
    assert snap == cli.StatusSnapshot(
        server="up",
        healthy=True,
        tunnel_on=True,
        tunnel_url="https://brain.ngrok.app/mcp",
        seats=2,
    )
    assert any(
        seq_in(c, "exec", "-T", "hive-server", "python", "-m", "hive.tools.healthcheck")
        for c in fake.calls
    )


def test_probe_status_down_short_circuits_with_no_exec():
    # the down read is a SUCCESSFUL read of the safe floor — and it runs ZERO in-container exec.
    fake = FakeRun(script=[(lambda a: seq_in(a, "ps", "hive-server"), proc(rc=1))])
    snap = cli._probe_status(fake, ENV)
    assert snap == cli.StatusSnapshot()  # server="down" + all fail-safe defaults
    assert not any(
        seq_in(c, "exec") for c in fake.calls
    )  # no healthcheck/authctl exec when down


def test_probe_status_unhealthy_tunnel_off_and_authctl_failure():
    fake = FakeRun(
        script=[
            (lambda a: seq_in(a, "ps", "hive-server"), proc(stdout="hive-server Up\n")),
            (lambda a: seq_in(a, "hive.tools.healthcheck"), proc(rc=1)),  # unhealthy
            (lambda a: seq_in(a, "ps", "ngrok"), proc(rc=1)),  # tunnel off
            (
                lambda a: seq_in(a, "list"),
                proc(rc=1),
            ),  # authctl list failed → seats unknown
        ]
    )
    snap = cli._probe_status(fake, ENV)
    assert snap == cli.StatusSnapshot(
        server="up", healthy=False, tunnel_on=False, tunnel_url=None, seats=None
    )


def test_probe_status_tunnel_on_without_domain_has_no_url():
    fake = FakeRun(
        script=[
            (lambda a: seq_in(a, "ps", "ngrok"), proc(stdout="ngrok Up\n")),
            (lambda a: seq_in(a, "ps", "hive-server"), proc(stdout="hive-server Up\n")),
        ]
    )
    snap = cli._probe_status(fake, ENV)  # ENV carries no NGROK_DOMAIN
    assert snap.tunnel_on is True and snap.tunnel_url is None


def test_exec_backup_shells_the_in_container_backupctl():
    path = "/data/backups/hive-20260708-000000.db\n"
    fake = FakeRun(
        script=[(lambda a: seq_in(a, "hive.tools.backupctl"), proc(stdout=path))]
    )
    child = cli._exec_backup(fake, ENV)
    assert child.stdout == path
    assert seq_in(
        fake.calls[0],
        "exec",
        "-T",
        "hive-server",
        "python",
        "-m",
        "hive.tools.backupctl",
    )


# ── provisioning: token / revoke / tokens shell to the in-container authctl ─────


def test_token_builds_authctl_create(capsys):
    fake = FakeRun(
        script=[(lambda a: seq_in(a, "create"), proc(stdout="hive_abc123\n"))]
    )
    out = io.StringIO()
    rc = cli.main(["token", "alice-laptop"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_OK
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
    assert out.getvalue() == "hive_abc123\n"  # the credential: child stdout ONLY
    err = capsys.readouterr().err  # the AC7 seat-contract handoff hint
    assert "one token per seat" in err and "never share across agents" in err
    assert "hive_abc123" not in err  # the token is never echoed elsewhere


def test_token_child_failure_forwards_sysexits(capsys):
    fake = FakeRun(
        script=[
            (
                lambda a: seq_in(a, "create"),
                proc(rc=70, stderr="authctl: a token already exists\n"),
            )
        ]
    )
    out = io.StringIO()
    rc = cli.main(["token", "dup"], run=fake, out=out, env=ENV)
    assert rc == 70  # authctl already speaks sysexits
    assert out.getvalue() == ""  # no token line on failure
    assert "already exists" in capsys.readouterr().err


def test_revoke_builds_authctl_revoke():
    fake = FakeRun()
    rc = cli.main(["revoke", "alice-laptop"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_OK
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


def test_tokens_builds_authctl_list():
    fake = FakeRun(script=[(lambda a: seq_in(a, "list"), proc(stdout="alice\nbob\n"))])
    out = io.StringIO()
    rc = cli.main(["tokens"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_OK
    assert any(
        seq_in(
            c, "exec", "-T", "hive-server", "python", "-m", "hive.tools.authctl", "list"
        )
        for c in fake.calls
    )
    assert out.getvalue() == "alice\nbob\n"  # labels forwarded verbatim


# ── repo registry: repo add/remove + repos shell to the in-container repoctl ───


def test_repo_add_shells_repoctl_with_all_flags():
    fake = FakeRun()
    rc = cli.main(
        [
            "repo",
            "add",
            "https://example.invalid/alpha.git",
            "--name",
            "alpha",
            "--branch",
            "main",
            "--token-env",
            "MY_TOKEN",
        ],
        run=fake,
        out=io.StringIO(),
        env=ENV,
    )
    assert rc == cli.EX_OK
    call = fake.calls[0]
    assert call[:2] == ["docker", "compose"]  # exec into the RUNNING container
    assert seq_in(
        call,
        "exec",
        "-T",
        "hive-server",
        "python",
        "-m",
        "hive.tools.repoctl",
        "add",
        "https://example.invalid/alpha.git",
    )
    # BUG-020 quoting discipline: every operator value is its OWN argv element,
    # never a joined shell string — no `sh -c` anywhere in the child argv.
    assert seq_in(call, "--name", "alpha")
    assert seq_in(call, "--branch", "main")
    assert seq_in(call, "--token-env", "MY_TOKEN")  # the var NAME rides; never a token
    assert "sh" not in call and "-c" not in call


def test_repo_add_minimal_passes_no_optional_flags():
    fake = FakeRun()
    rc = cli.main(
        ["repo", "add", "https://example.invalid/alpha.git"],
        run=fake,
        out=io.StringIO(),
        env=ENV,
    )
    assert rc == cli.EX_OK
    call = fake.calls[0]
    assert seq_in(
        call, "hive.tools.repoctl", "add", "https://example.invalid/alpha.git"
    )
    assert "--name" not in call and "--branch" not in call and "--token-env" not in call


def test_repo_remove_shells_repoctl_remove():
    fake = FakeRun()
    rc = cli.main(["repo", "remove", "alpha"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_OK
    assert any(
        seq_in(
            c,
            "exec",
            "-T",
            "hive-server",
            "python",
            "-m",
            "hive.tools.repoctl",
            "remove",
            "alpha",
        )
        for c in fake.calls
    )


def test_repos_lists_via_repoctl_and_forwards_stdout():
    listing = "alpha\thttps://example.invalid/alpha.git\tmain\tMY_TOKEN\n"
    fake = FakeRun(
        script=[
            (lambda a: seq_in(a, "hive.tools.repoctl", "list"), proc(stdout=listing))
        ]
    )
    out = io.StringIO()
    rc = cli.main(["repos"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_OK
    assert out.getvalue() == listing  # the registry lines, verbatim
    assert any(
        seq_in(
            c, "exec", "-T", "hive-server", "python", "-m", "hive.tools.repoctl", "list"
        )
        for c in fake.calls
    )


def test_repo_add_child_failure_forwards_sysexits(capsys):
    fake = FakeRun(
        script=[
            (
                lambda a: seq_in(a, "hive.tools.repoctl", "add"),
                # 65 EX_DATAERR is what repoctl really returns for a duplicate name —
                # an operator-fixable refusal, distinct from an internal fault (70).
                proc(rc=65, stderr="repoctl: repo 'alpha' is already registered\n"),
            )
        ]
    )
    rc = cli.main(
        ["repo", "add", "https://example.invalid/alpha.git", "--name", "alpha"],
        run=fake,
        out=io.StringIO(),
        env=ENV,
    )
    assert rc == 65  # repoctl already speaks sysexits; the CLI forwards them
    assert "already registered" in capsys.readouterr().err


def test_repos_child_failure_forwards_sysexits(capsys):
    fake = FakeRun(
        script=[
            (
                lambda a: seq_in(a, "hive.tools.repoctl", "list"),
                proc(rc=70, stderr="repoctl: boom\n"),
            )
        ]
    )
    out = io.StringIO()
    rc = cli.main(["repos"], run=fake, out=out, env=ENV)
    assert rc == 70
    assert out.getvalue() == ""  # no listing on failure
    assert "boom" in capsys.readouterr().err


# ── connect: transport registration line only — never the handshake ────────────


def test_connect_renders_mcp_add_line(capsys):
    fake = FakeRun()
    out = io.StringIO()
    rc = cli.main(
        ["connect"], run=fake, out=out, env={"NGROK_DOMAIN": "brain.ngrok.app"}
    )  # no tenant needed: local verb
    assert rc == cli.EX_OK
    assert fake.calls == []  # purely local — nothing is run
    text = out.getvalue()
    # shell-neutral: a literal placeholder the teammate replaces with the real seat token — the line
    # is copy-pasted on an unknown OS/shell, so ANY expansion syntax (bash ${VAR}, PowerShell
    # $env:VAR, cmd %VAR%) would be wrong on the other two shells.
    assert (
        "claude mcp add --transport http hive https://brain.ngrok.app/mcp "
        '--header "Authorization: Bearer <seat-token>"'
    ) in text
    err = capsys.readouterr().err
    both = text + err
    assert "${HIVE_TOKEN}" not in both  # not bash ${VAR}
    assert "$env:" not in both  # not PowerShell $env:VAR
    assert "%HIVE_TOKEN%" not in both  # not cmd %VAR%
    assert "hive token <seat>" in err  # AC7: the inline seat hint
    assert "hive_init" not in both  # M11/M12: no handshake here


def test_connect_public_url_renders_the_token_gated_line(capsys):
    """HIVE_PUBLIC_URL names whatever fronts the token-required door — a reverse proxy, a
    platform router, a tunnel this CLI did not start. It prints the same token-gated line
    as the ngrok posture because it describes the same door reached another way."""
    fake = FakeRun()
    out = io.StringIO()
    rc = cli.main(
        ["connect"],
        run=fake,
        out=out,
        env={"HIVE_PUBLIC_URL": "https://hive.example.dev"},
    )
    assert rc == cli.EX_OK
    assert fake.calls == []  # purely local — nothing is run
    text = out.getvalue()
    assert (
        "claude mcp add --transport http hive https://hive.example.dev/mcp "
        '--header "Authorization: Bearer <seat-token>"'
    ) in text
    err = capsys.readouterr().err
    assert "hive token <seat>" in err  # the seat hint rides every token-gated posture
    for expansion in ("${HIVE_TOKEN}", "$env:", "%HIVE_TOKEN%"):
        assert expansion not in text + err  # shell-neutral, like every printed line


@pytest.mark.parametrize(
    "given",
    [
        "https://hive.example.dev",
        "https://hive.example.dev/",
        "https://hive.example.dev/mcp",
    ],
)
def test_connect_public_url_appends_the_endpoint_path_exactly_once(given, capsys):
    # `/mcp` is the SERVER's path, not the operator's to choose — appended when absent and
    # never doubled, so a URL copied with or without it registers the same endpoint.
    out = io.StringIO()
    assert cli.main(
        ["connect"], run=FakeRun(), out=out, env={"HIVE_PUBLIC_URL": given}
    ) == (cli.EX_OK)
    assert "https://hive.example.dev/mcp " in out.getvalue()
    assert "/mcp/mcp" not in out.getvalue()
    capsys.readouterr()


def test_connect_public_url_wins_over_the_ngrok_domain(capsys):
    # most explicit posture first: an operator who set both is describing their own front
    # door, and the sidecar's convenience form must not override it.
    out = io.StringIO()
    cli.main(
        ["connect"],
        run=FakeRun(),
        out=out,
        env={
            "HIVE_PUBLIC_URL": "https://hive.example.dev",
            "NGROK_DOMAIN": "brain.ngrok.app",
        },
    )
    text = out.getvalue()
    assert "hive.example.dev/mcp" in text and "ngrok" not in text
    capsys.readouterr()


def test_connect_without_domain_prints_tokenless_loopback_line(capsys):
    out = io.StringIO()
    rc = cli.main(["connect"], run=FakeRun(), out=out, env={})
    assert rc == cli.EX_OK
    text = out.getvalue()
    assert (
        text.strip() == "claude mcp add --transport http hive http://localhost:8765/mcp"
    )
    assert "Authorization: Bearer" not in text  # the loopback door is tokenless
    err = capsys.readouterr().err
    assert "NGROK_DOMAIN" in err  # says why it fell back to loopback
    assert "X-Hive-Agent-Id" not in err  # the identity walkthrough is gone
    # (registration line only — the usage
    # contract is served over MCP itself)


def test_default_run_forwards_stdin_input():
    # default_run forwards stdin to the child (the keyword-only `input=` seam)
    p = cli.default_run(["cat"], None, input="ndjson-line\n")
    assert p.returncode == 0 and p.stdout == "ndjson-line\n"


# ── ingest: feed a census receipt to the in-container censusctl over stdin ─────


def test_ingest_execs_censusctl_with_stdin_pipe(tmp_path):
    receipt = tmp_path / "r.json"
    receipt.write_text('{"payloadType": "x"}')
    fake = FakeRun(
        script=[
            (
                lambda a: seq_in(a, "hive.tools.censusctl"),
                proc(stdout='{"matched":0}\n'),
            )
        ]
    )
    out = io.StringIO()
    rc = cli.main(["ingest", str(receipt)], run=fake, out=out, env=ENV)
    assert rc == cli.EX_OK
    call = fake.calls[0]
    assert call[:2] == ["docker", "compose"]
    assert seq_in(
        call,
        "exec",
        "-T",
        "hive-server",
        "python",
        "-m",
        "hive.tools.censusctl",
        "ingest",
        "-",
    )
    # the receipt rides stdin — a host path does not exist in-container (BUG-012 class)
    assert str(receipt) not in call
    assert fake.kws[0].get("input") == '{"payloadType": "x"}'
    assert out.getvalue() == '{"matched":0}\n'  # the JSON report, forwarded verbatim


def test_ingest_missing_host_file_is_usage_error_without_a_child():
    fake = FakeRun()
    rc = cli.main(
        ["ingest", "/no/such/receipt.json"], run=fake, out=io.StringIO(), env=ENV
    )
    assert rc == cli.EX_USAGE
    assert fake.calls == []  # fail fast: nothing spawned


def test_ingest_post_merge_flags_ride_the_child_argv(tmp_path):
    receipt = tmp_path / "r.json"
    receipt.write_text("{}")
    fake = FakeRun()
    rc = cli.main(
        [
            "ingest",
            str(receipt),
            "--post-merge",
            "--verdict",
            "fail",
            "--signal",
            "canary",
        ],
        run=fake,
        out=io.StringIO(),
        env=ENV,
    )
    assert rc == cli.EX_OK
    assert seq_in(
        fake.calls[0],
        "ingest",
        "-",
        "--post-merge",
        "--verdict",
        "fail",
        "--signal",
        "canary",
    )


def test_ingest_pre_merge_passes_no_outcome_flags(tmp_path):
    # pre-merge verdicts are derived from the receipt in-kernel — the CLI asserts nothing
    receipt = tmp_path / "r.json"
    receipt.write_text("{}")
    fake = FakeRun()
    cli.main(["ingest", str(receipt)], run=fake, out=io.StringIO(), env=ENV)
    call = fake.calls[0]
    assert "--post-merge" not in call
    assert "--verdict" not in call and "--signal" not in call


def test_ingest_post_merge_without_verdict_is_usage_error_without_a_child(tmp_path):
    receipt = tmp_path / "r.json"
    receipt.write_text("{}")
    fake = FakeRun()
    rc = cli.main(
        ["ingest", str(receipt), "--post-merge"], run=fake, out=io.StringIO(), env=ENV
    )
    assert rc == cli.EX_USAGE
    assert fake.calls == []


def test_ingest_child_exit_code_passes_through(tmp_path, capsys):
    receipt = tmp_path / "r.json"
    receipt.write_text("{}")
    fake = FakeRun(
        script=[
            (
                lambda a: seq_in(a, "hive.tools.censusctl"),
                proc(rc=65, stderr="censusctl: receipt refused: bad\n"),
            )
        ]
    )
    out = io.StringIO()
    rc = cli.main(["ingest", str(receipt)], run=fake, out=out, env=ENV)
    assert rc == 65  # censusctl already speaks sysexits
    assert out.getvalue() == ""  # no report line on failure
    assert "refused" in capsys.readouterr().err


# ── backup: one-shot snapshot (exec → backupctl, dest path forwarded) ──────────
def test_backup_forwards_child_stdout():
    path = "/data/backups/hive-20260616-000000.db\n"
    fake = FakeRun(
        script=[
            (
                lambda a: seq_in(a, "python", "-m", "hive.tools.backupctl"),
                proc(stdout=path),
            )
        ]
    )
    out = io.StringIO()
    rc = cli.main(["backup"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_OK
    assert out.getvalue() == path  # snapshot path forwarded verbatim
    assert any(
        seq_in(c, "exec", "-T", "hive-server", "python", "-m", "hive.tools.backupctl")
        for c in fake.calls
    )  # exec'd in-container


def test_backup_maps_child_failure_to_unavailable():
    fake = FakeRun(
        script=[
            (lambda a: seq_in(a, "hive.tools.backupctl"), proc(rc=1, stderr="boom"))
        ]
    )
    rc = cli.main(["backup"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_UNAVAILABLE


# ── health verb removed (KPI trends ride hive_health(include_trends) over MCP) ──
def test_health_verb_is_absent():
    # the host-side `hive health` wrapper was cut; the §8.3 demand-health window now
    # lives ONLY on hive_health(include_trends=true) over MCP. argparse rejects it.
    assert "health" not in cli._HANDLERS
    with pytest.raises(SystemExit):
        cli.main(["health"], run=FakeRun(script=[]), out=io.StringIO(), env=ENV)


# ── ui: one registry entry + a single subparser; the heavy import stays deferred ──


def test_ui_verb_dispatches_to_serve_ui_with_the_seams(monkeypatch):
    from hive.tools import ui

    captured = {}

    def fake_serve(**kw):
        captured.update(kw)
        return cli.EX_OK

    monkeypatch.setattr(ui, "serve_ui", fake_serve)
    fake = FakeRun()
    rc = cli.main(["ui", "--no-open"], run=fake, out=io.StringIO(), env=ENV)
    assert rc == cli.EX_OK
    assert captured["host"] == "127.0.0.1" and captured["port"] == 4173
    assert captured["open_browser"] is False  # --no-open flips it off
    assert (
        captured["run"] is fake and captured["env"] == ENV
    )  # the injected seams pass through


def test_ui_defaults_open_the_browser_on_the_default_loopback_port(monkeypatch):
    from hive.tools import ui

    captured = {}
    monkeypatch.setattr(ui, "serve_ui", lambda **kw: captured.update(kw) or cli.EX_OK)
    cli.main(["ui"], run=FakeRun(), out=io.StringIO(), env=ENV)
    assert captured["host"] == "127.0.0.1" and captured["port"] == 4173
    assert captured["open_browser"] is True  # default: open the native browser


def test_ui_custom_host_and_port(monkeypatch):
    from hive.tools import ui

    captured = {}
    monkeypatch.setattr(ui, "serve_ui", lambda **kw: captured.update(kw) or cli.EX_OK)
    cli.main(
        ["ui", "--host", "localhost", "--port", "5000", "--no-open"],
        run=FakeRun(),
        out=io.StringIO(),
        env=ENV,
    )
    assert captured["host"] == "localhost" and captured["port"] == 5000


def test_ui_is_a_single_registry_entry():
    assert "ui" in cli._HANDLERS and cli._HANDLERS["ui"].__name__ == "_ui"


def test_cli_module_scope_defers_the_heavy_ui_import():
    # c5: cli.py imports no http.server/threading/webbrowser/ui at module scope — the heavy
    # transport import is lazy INSIDE _ui, off every other verb's path.
    lines = open(cli.__file__, encoding="utf-8").read().splitlines()
    module_scope = [ln for ln in lines if ln and not ln[0].isspace()]
    for banned in (
        "import http",
        "from http",
        "import threading",
        "import webbrowser",
        "from hive.tools import ui",
        "import hive.tools.ui",
    ):
        assert not any(ln.startswith(banned) for ln in module_scope), (
            f"{banned!r} must not be a module-scope import in cli.py"
        )
    assert any(
        ln.strip() == "from hive.tools import ui" for ln in lines
    )  # lazy, indented
