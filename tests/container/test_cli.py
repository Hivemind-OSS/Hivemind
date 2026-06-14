"""ADMIN-CLI — the `hive` operator CLI: argv contracts via the injected fake runner.

Every verb shells through the one `run()` seam, so a fake that records argv makes the
compose/exec invocations assertable facts, not prose — a wrong invocation reds a test,
no Docker needed. The helpers (`FakeRun`/`proc`/`seq_in`) are module-level and reusable:
successor verbs (e.g. `credit`) add their argv tests here against the same fake.
"""
from __future__ import annotations

import io
import json
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


def test_connect_without_domain_prints_loopback_line(capsys):
    out = io.StringIO()
    rc = cli.main(["connect"], run=FakeRun(), out=out, env={})
    assert rc == cli.EX_OK
    assert "http://localhost:8765/mcp" in out.getvalue()
    assert "NGROK_DOMAIN" in capsys.readouterr().err   # says why it fell back to loopback


# ── origin: the GitHub-API credit loop (link / sync / ls / rm) ──────────────────

WIN_ROW = {"kind": "win", "commit_sha": "a" * 40, "commit_ts": 100, "repo": "o/r",
           "credits": [{"trace_id": "f" * 32, "episode_ids": [3]}]}
LOSS_ROW = {"kind": "loss", "commit_sha": "b" * 40, "commit_ts": 110, "repo": "o/r"}
SUMMARY = {"win_rows": 1, "loss_rows": 0, "credit_groups": 1,
           "malformed_trailer_lines": 0, "legacy_trailers_seen": 0,
           "deduped_rebase_copies": 0, "prs_scanned": 1}

INGEST_ARGV = ("exec", "-T", "hive-server", "python", "-m",
               "hive.tools.originctl", "ingest", "--ndjson", "-")


def origin_env(tmp_path, **extra):
    return dict(ENV, XDG_CONFIG_HOME=str(tmp_path / "xdg"), **extra)


def _origins_file(tmp_path):
    return tmp_path / "xdg" / "hive" / "origins.json"


def _seed_origins(tmp_path, repo="o/r", token="stored-tok"):
    cfg = _origins_file(tmp_path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"version": 1, "origins": {repo: {"token": token}}}))


def _cron_writes(fake):
    return [kw["input"] for c, kw in zip(fake.calls, fake.kws)
            if c[:2] == ["crontab", "-"]]


@pytest.fixture()
def checkout(tmp_path, monkeypatch):
    """A hive deployment checkout: compose.yaml + .env, cwd'd into (the link verb
    validates both; the cron line cd's here)."""
    work = tmp_path / "checkout"
    work.mkdir()
    (work / "compose.yaml").write_text("services: {}\n")
    (work / ".env").write_text("HIVE_TENANT_ID=acme\n")
    monkeypatch.chdir(work)
    return work


@pytest.fixture()
def quiet_github(monkeypatch):
    """Access probe passes; scan returns no rows (link's first sync is a no-op)."""
    monkeypatch.setattr(cli.originctl, "probe_repo", lambda repo, **kw: None)
    monkeypatch.setattr(cli.originctl, "scan_github",
                        lambda repo, **kw: ([], dict(SUMMARY)))


def test_origin_link_e2e_writes_0600_config_installs_cron_runs_first_sync(
        checkout, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.originctl, "probe_repo", lambda repo, **kw: None)
    captured = {}

    def fake_scan(repo, **kw):
        captured["repo"], captured["kw"] = repo, kw
        return [WIN_ROW], dict(SUMMARY)

    monkeypatch.setattr(cli.originctl, "scan_github", fake_scan)
    fake = FakeRun(script=[
        (lambda a: a[:2] == ["crontab", "-l"], proc(rc=1)),          # no crontab yet
        (lambda a: seq_in(a, "ingest"), proc(stdout='{"wins_ingested": 1}\n')),
    ])
    out = io.StringIO()
    rc = cli.main(["origin", "o/r"], run=fake, out=out,
                  env=origin_env(tmp_path, GITHUB_TOKEN="env-tok"))
    assert rc == cli.EX_OK
    # 0600 config, token inline (the documented env-var-rule deviation: cron has no env)
    cfg = _origins_file(tmp_path)
    assert cfg.exists() and (cfg.stat().st_mode & 0o777) == 0o600
    assert json.loads(cfg.read_text())["origins"]["o/r"]["token"] == "env-tok"
    # ONE marker-tagged @hourly line, cd'ing THIS checkout
    writes = _cron_writes(fake)
    assert len(writes) == 1
    cron_lines = [ln for ln in writes[0].splitlines() if ln.endswith("# hive-origin")]
    assert len(cron_lines) == 1
    assert cron_lines[0].startswith("@hourly ")
    assert f"cd {checkout}" in cron_lines[0]
    assert "origin sync" in cron_lines[0]
    assert ". ./.env" in cron_lines[0]
    # first sync: 90d backfill, resolved token, NDJSON piped into the container ingest
    assert captured["repo"] == "o/r"
    assert captured["kw"]["lookback_days"] == 90
    assert captured["kw"]["token"] == "env-tok"
    ingest_kws = [kw for c, kw in zip(fake.calls, fake.kws) if seq_in(c, *INGEST_ARGV)]
    assert len(ingest_kws) == 1
    assert ("a" * 40) in ingest_kws[0]["input"]
    assert '"wins_ingested": 1' in out.getvalue()


def test_origin_link_strip_then_append_keeps_one_cron_line(
        checkout, tmp_path, quiet_github):
    stale = ("0 1 * * * /usr/bin/backup\n"
             "@hourly cd /old && set -a && . ./.env && python -m hive.tools.cli "
             "origin sync >> /old.log 2>&1 # hive-origin\n")
    fake = FakeRun(script=[(lambda a: a[:2] == ["crontab", "-l"], proc(stdout=stale))])
    rc = cli.main(["origin", "o/r"], run=fake, out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_OK
    payload = _cron_writes(fake)[0]
    assert "/usr/bin/backup" in payload                      # foreign lines preserved
    marker = [ln for ln in payload.splitlines() if ln.endswith("# hive-origin")]
    assert len(marker) == 1                                  # strip-then-append: ONE line
    assert "cd /old " not in marker[0]                       # the stale line was replaced


def test_origin_link_no_cron_flag_skips_crontab(checkout, tmp_path, quiet_github):
    fake = FakeRun()
    rc = cli.main(["origin", "o/r", "--no-cron"], run=fake, out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_OK
    assert not any(c[0] == "crontab" for c in fake.calls)
    assert _origins_file(tmp_path).exists()                  # config still lands


def test_origin_link_token_ladder_falls_back_to_gh(checkout, tmp_path, quiet_github):
    fake = FakeRun(script=[
        (lambda a: a == ["gh", "auth", "token"], proc(stdout="gh-tok\n")),
        (lambda a: a[:2] == ["crontab", "-l"], proc(rc=1)),
    ])
    rc = cli.main(["origin", "o/r"], run=fake, out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_OK
    assert json.loads(_origins_file(tmp_path).read_text()
                      )["origins"]["o/r"]["token"] == "gh-tok"


def test_origin_link_token_stdin_outranks_env(checkout, tmp_path, quiet_github,
                                              monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("stdin-tok\n"))
    fake = FakeRun(script=[(lambda a: a[:2] == ["crontab", "-l"], proc(rc=1))])
    rc = cli.main(["origin", "o/r", "--token-stdin"], run=fake, out=io.StringIO(),
                  env=origin_env(tmp_path, GITHUB_TOKEN="env-tok"))
    assert rc == cli.EX_OK
    assert json.loads(_origins_file(tmp_path).read_text()
                      )["origins"]["o/r"]["token"] == "stdin-tok"


def test_origin_link_without_compose_yaml_is_config_error(tmp_path, monkeypatch):
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    fake = FakeRun()
    rc = cli.main(["origin", "o/r"], run=fake, out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_CONFIG
    assert fake.calls == []                                  # fail BEFORE any child
    assert not _origins_file(tmp_path).exists()


def test_origin_missing_tenant_fails_fast(tmp_path):
    fake = FakeRun()
    rc = cli.main(["origin", "sync"], run=fake, out=io.StringIO(),
                  env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert rc == cli.EX_CONFIG and fake.calls == []


def test_origin_link_private_repo_without_token_hints_pat_scopes(
        checkout, tmp_path, monkeypatch, capsys):
    def deny(repo, **kw):
        raise cli.originctl.GithubApiError(404, "GitHub API 404 on /repos/o/r")

    monkeypatch.setattr(cli.originctl, "probe_repo", deny)
    fake = FakeRun(script=[(lambda a: a == ["gh", "auth", "token"], proc(rc=1))])
    rc = cli.main(["origin", "o/r"], run=fake, out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_CONFIG
    err = capsys.readouterr().err
    assert "repo" in err and "Contents:Read" in err          # the PAT-scope hint
    assert not _origins_file(tmp_path).exists()              # nothing persisted
    assert not any(c[:2] == ["crontab", "-"] for c in fake.calls)   # no cron installed


def test_origin_sync_env_token_overrides_stored(tmp_path, monkeypatch):
    _seed_origins(tmp_path)
    seen = {}

    def fake_scan(repo, **kw):
        seen["token"] = kw["token"]
        return [], dict(SUMMARY)

    monkeypatch.setattr(cli.originctl, "scan_github", fake_scan)
    rc = cli.main(["origin", "sync"], run=FakeRun(), out=io.StringIO(),
                  env=origin_env(tmp_path, GITHUB_TOKEN="env-tok"))
    assert rc == cli.EX_OK and seen["token"] == "env-tok"


def test_origin_sync_stored_token_and_default_lookback(tmp_path, monkeypatch):
    _seed_origins(tmp_path)
    seen = {}

    def fake_scan(repo, **kw):
        seen.update(kw)
        return [], dict(SUMMARY)

    monkeypatch.setattr(cli.originctl, "scan_github", fake_scan)
    rc = cli.main(["origin", "sync"], run=FakeRun(), out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_OK
    assert seen["token"] == "stored-tok"
    assert seen["lookback_days"] == cli.originctl.DEFAULT_LOOKBACK_DAYS


def test_origin_sync_pipes_ndjson_to_container_ingest(tmp_path, monkeypatch, capsys):
    _seed_origins(tmp_path)
    monkeypatch.setattr(cli.originctl, "scan_github",
                        lambda repo, **kw: ([WIN_ROW, LOSS_ROW], dict(SUMMARY)))
    fake = FakeRun(script=[(lambda a: seq_in(a, "ingest"),
                            proc(stdout='{"wins_ingested": 1}\n'))])
    out = io.StringIO()
    rc = cli.main(["origin", "sync"], run=fake, out=out, env=origin_env(tmp_path))
    assert rc == cli.EX_OK
    assert any(seq_in(c, *INGEST_ARGV) for c in fake.calls)
    piped = next(kw["input"] for c, kw in zip(fake.calls, fake.kws)
                 if seq_in(c, *INGEST_ARGV))
    assert ("a" * 40) in piped and '"kind": "loss"' in piped
    assert '"wins_ingested": 1' in out.getvalue()            # report → stdout
    assert "scan" in capsys.readouterr().err                 # summary → stderr


def test_origin_sync_no_rows_skips_exec(tmp_path, quiet_github):
    _seed_origins(tmp_path)
    fake = FakeRun()
    rc = cli.main(["origin", "sync"], run=fake, out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_OK
    assert fake.calls == []                                  # nothing to ingest → no exec


def test_origin_sync_without_origins_is_config_error(tmp_path, capsys):
    rc = cli.main(["origin", "sync"], run=FakeRun(), out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_CONFIG
    assert "hive origin <owner/repo>" in capsys.readouterr().err


def test_origin_rm_last_origin_removes_cron_line(tmp_path):
    _seed_origins(tmp_path)
    stale = ("0 1 * * * /usr/bin/backup\n"
             "@hourly cd /x && set -a && . ./.env && python -m hive.tools.cli "
             "origin sync >> /x.log 2>&1 # hive-origin\n")
    fake = FakeRun(script=[(lambda a: a[:2] == ["crontab", "-l"], proc(stdout=stale))])
    rc = cli.main(["origin", "rm", "o/r"], run=fake, out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_OK
    payload = _cron_writes(fake)[0]
    assert "# hive-origin" not in payload                    # marker line gone
    assert "/usr/bin/backup" in payload                      # foreign lines preserved
    assert json.loads(_origins_file(tmp_path).read_text())["origins"] == {}


def test_origin_rm_non_last_keeps_cron(tmp_path):
    cfg = _origins_file(tmp_path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"version": 1, "origins": {
        "o/r": {"token": None}, "o/q": {"token": None}}}))
    fake = FakeRun()
    rc = cli.main(["origin", "rm", "o/r"], run=fake, out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_OK
    assert not any(c[0] == "crontab" for c in fake.calls)    # cron untouched
    assert list(json.loads(cfg.read_text())["origins"]) == ["o/q"]


def test_origin_rm_unknown_repo_is_usage_error(tmp_path, capsys):
    _seed_origins(tmp_path)
    rc = cli.main(["origin", "rm", "o/ghost"], run=FakeRun(), out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_USAGE


def test_origin_ls_lists_repos_never_tokens(tmp_path):
    _seed_origins(tmp_path, token="super-secret-token")
    out = io.StringIO()
    rc = cli.main(["origin", "ls"], run=FakeRun(), out=out, env=origin_env(tmp_path))
    assert rc == cli.EX_OK
    text = out.getvalue()
    assert "o/r" in text and "super-secret-token" not in text


def test_origin_dispatch_bad_word_is_usage_error(tmp_path, capsys):
    rc = cli.main(["origin", "frobnicate"], run=FakeRun(), out=io.StringIO(),
                  env=origin_env(tmp_path))
    assert rc == cli.EX_USAGE
    assert "sync|ls|rm" in capsys.readouterr().err           # the grammar, stated


def test_default_run_forwards_stdin_input():
    # the keyword-only widening the origin NDJSON pipe rides — additive
    p = cli.default_run(["cat"], None, input="ndjson-line\n")
    assert p.returncode == 0 and p.stdout == "ndjson-line\n"


# ── trends: the convergence-KPI read (exec → trendsctl, JSON forwarded) ──────────
def test_trends_forwards_child_stdout():
    report = '{"current": {"confident_rate": 0.5}, "previous": {}, "deltas": {}}\n'
    fake = FakeRun(script=[(lambda a: seq_in(a, "python", "-m", "hive.tools.trendsctl"),
                            proc(stdout=report))])
    out = io.StringIO()
    rc = cli.main(["trends"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_OK
    assert out.getvalue() == report                          # JSON forwarded verbatim
    assert any(seq_in(c, "exec", "-T", "hive-server", "python", "-m",
                      "hive.tools.trendsctl") for c in fake.calls)   # exec'd in-container


def test_trends_maps_child_failure_to_unavailable():
    fake = FakeRun(script=[(lambda a: seq_in(a, "hive.tools.trendsctl"),
                            proc(rc=1, stderr="boom"))])
    out = io.StringIO()
    rc = cli.main(["trends"], run=fake, out=out, env=ENV)
    assert rc == cli.EX_UNAVAILABLE
    assert out.getvalue() == ""                              # no JSON forwarded on failure
