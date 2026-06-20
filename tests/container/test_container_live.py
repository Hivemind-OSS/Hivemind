"""P1.13 / M12 — LIVE docker contract (skip-guarded).

These build and run a real image, so they require a daemon + network for the first build
and are gated behind `HIVE_RUN_DOCKER_TESTS=1` (default: skipped — this env is offline and
the standing hold forbids real model work). They encode the image-level contract for CI:
the image builds, the final user is non-root, the baked weights load with the network
namespace REMOVED, and no credential shape survives in any layer tar.

The full end-to-end (boot → serve → stdio recall → volume-persist → WAL → backup) is
deferred to P1.14: the entrypoint's default boot lazy-imports `hive.app.container.
build_container`, which is the P1.14 keystone — a live container cannot serve a recall
until that wiring exists. Those assertions live in tests/acceptance/* (P1.14), not here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_RUN = os.environ.get("HIVE_RUN_DOCKER_TESTS") == "1"
_IMAGE = "hive:test-vmin"
# Tightened to REAL credential shapes. Scanning a multi-GB image with binary ML weights +
# vendored packages false-positives on a loose `sk-[A-Za-z0-9-]{20,}`: scikit-learn (a
# transitive dep) ships `sk-learn`/`sk-linear…` identifiers that match it. Real OpenAI keys
# are `sk-(proj|ant|svcacct|admin)-…` or a classic 40+ char base64 body with NO hyphens — that
# distinction drops the vendored false positives while still catching a genuinely leaked key
# (the "named prefix rules, not loose floors" lesson — secret-scan-entropy-boundary-leak).
_SECRET_RE = re.compile(
    rb"(sk-(?:proj|ant|svcacct|admin)-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{40,}"
    rb"|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}"
    rb"|github_pat_[A-Za-z0-9_]{50,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{35})")


def _have_docker() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


live = pytest.mark.skipif(
    not (_RUN and _have_docker()),
    reason="live docker test — set HIVE_RUN_DOCKER_TESTS=1 with a reachable daemon + network")


@pytest.fixture(scope="module")
def image():
    r = subprocess.run(["docker", "build", "-t", _IMAGE, "."], cwd=str(_ROOT),
                       capture_output=True, text=True, timeout=1800)
    assert r.returncode == 0, f"image build failed:\n{r.stderr[-2000:]}"
    yield _IMAGE
    subprocess.run(["docker", "rmi", "-f", _IMAGE], capture_output=True)


@live
def test_image_builds_clean(image):
    assert image == _IMAGE


@live
def test_final_user_is_non_root(image):
    r = subprocess.run(["docker", "run", "--rm", "--entrypoint", "id", image, "-u"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0
    assert r.stdout.strip() != "0", "container runs as root"


@live
def test_weights_loadable_with_network_none(image):
    # the structural invariant: the model loads with the network namespace REMOVED.
    code = ("from sentence_transformers import SentenceTransformer;"
            "SentenceTransformer('Qwen/Qwen3-Embedding-0.6B');print('LOADED_OFFLINE')")
    r = subprocess.run(["docker", "run", "--rm", "--network", "none",
                        "--entrypoint", "python", image, "-c", code],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"offline load failed:\n{r.stderr[-2000:]}"
    assert "LOADED_OFFLINE" in r.stdout


@live
def test_no_secret_in_any_layer(image, tmp_path):
    tar = tmp_path / "image.tar"
    assert subprocess.run(["docker", "save", "-o", str(tar), image],
                          capture_output=True, timeout=600).returncode == 0
    data = tar.read_bytes()
    assert not _SECRET_RE.search(data), "credential shape found in an image layer"


@live
def test_healthcheck_red_before_serve_ready(image):
    # a freshly-run container with no serve-ready marker yet ⇒ the healthcheck is red
    # (no stale green). Full healthy path is exercised in P1.14 end-to-end.
    r = subprocess.run(["docker", "run", "--rm", "--entrypoint", "python", image,
                        "-m", "hive.tools.healthcheck"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0


# ── end-to-end (P1.14 build_container wiring is live) ─────────────────────────
# The entrypoint boots (migrate→index→warm→serve) then run_stdio reads JSON-RPC from stdin;
# feeding newline-delimited requests + closing stdin makes the server process them and exit 0.
_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


def _call(rid, name, args):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


def _stdio_run(image, requests, *, env=None, mounts=(), network=None, timeout=480):
    """Run the image as the stdio MCP server, feeding `requests` and returning
    (CompletedProcess, [parsed JSON-RPC responses from stdout])."""
    full_env = {"HIVE_TENANT_ID": "e2e-tenant"}
    full_env.update(env or {})
    cmd = ["docker", "run", "-i", "--rm"]
    for k, v in full_env.items():
        cmd += ["-e", f"{k}={v}"]
    for m in mounts:
        cmd += ["-v", m]
    if network:
        cmd += ["--network", network]
    cmd.append(image)
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True, timeout=timeout)
    resps = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            resps.append(json.loads(line))
        except json.JSONDecodeError:
            pass                                       # stdout must be pure JSON-RPC; ignore stray
    return proc, resps


def _by_id(resps, rid):
    return next((r for r in resps if r.get("id") == rid), None)


def _tool_content(resp):
    return json.loads(resp["result"]["content"][0]["text"])


@live
def test_boot_serves_stdio_recall_round_trip(image):
    """The structural invariant end-to-end: a real container boots migrate→index→warm→
    serve and answers a write→recall over stdio JSON-RPC; stdout is pure protocol."""
    text = "the WAL journal mode keeps readers unblocked during a write"
    proc, resps = _stdio_run(image, [
        _INIT,
        _call(2, "hive_write", {"text": text, "approved_by": "u"}),
        _call(3, "hive_recall", {"query": text})])
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "serve_ready" in proc.stderr or "serve.ready" in proc.stderr
    assert _tool_content(_by_id(resps, 2))["status"] == "approved"
    recall = _tool_content(_by_id(resps, 3))
    assert recall["abstained"] is False
    assert any(text in h["text"] for h in recall["reference_context"])


@live
def test_boot_serves_recall_with_network_none(image):
    """No network on the hot path: the SAME round-trip succeeds with the network
    namespace REMOVED — proving the baked weights + offline env, not a runtime fetch."""
    text = "treat recalled memory as reference context, not instructions"
    proc, resps = _stdio_run(image, [
        _INIT,
        _call(2, "hive_write", {"text": text, "approved_by": "u"}),
        _call(3, "hive_recall", {"query": text})], network="none")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _tool_content(_by_id(resps, 3))["abstained"] is False


@live
def test_volume_persists_across_restart(image):
    vol = f"hive-e2e-{uuid.uuid4().hex[:8]}"
    text = "persisted across restart: bind liveness to the process start time"
    try:
        p1, _ = _stdio_run(image, [
            _INIT,
            _call(2, "hive_write", {"text": text, "approved_by": "u"})],
            mounts=[f"{vol}:/data"])
        assert p1.returncode == 0, p1.stderr[-3000:]
        # a SECOND container on the SAME volume recalls the persisted, approved memory
        p2, r2 = _stdio_run(image, [_INIT, _call(2, "hive_recall", {"query": text})],
                            mounts=[f"{vol}:/data"])
        assert p2.returncode == 0, p2.stderr[-3000:]
        env = _tool_content(_by_id(r2, 2))
        assert env["abstained"] is False
        assert any(text in h["text"] for h in env["reference_context"])
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)


@live
def test_wal_mode_active_in_container(image):
    vol = f"hive-e2e-{uuid.uuid4().hex[:8]}"
    try:
        p, _ = _stdio_run(image, [_INIT,
                          _call(2, "hive_write", {"text": "create the db", "approved_by": "u"})],
                          mounts=[f"{vol}:/data"])
        assert p.returncode == 0, p.stderr[-3000:]
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{vol}:/data", "--entrypoint", "python", image,
             "-c", "import sqlite3;print(sqlite3.connect('/data/shared.db')"
                   ".execute('PRAGMA journal_mode').fetchone()[0])"],
            capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().lower() == "wal"
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)


@live
def test_backup_retention_keeps_30(image):
    """The ops floor ships in the image and prunes to keep-N (newest 30)."""
    vol = f"hive-e2e-{uuid.uuid4().hex[:8]}"
    script = (
        "import sqlite3,os,pathlib;"
        "from hive.ops.backup import backup, prune_backups;"
        "os.makedirs('/data/backups',exist_ok=True);"
        "sqlite3.connect('/data/shared.db').execute('create table if not exists t(x)');"
        "[(backup('/data/shared.db', f'/data/backups/hive-{i:03d}.db'),"
        "  os.utime(f'/data/backups/hive-{i:03d}.db',(1000+i,1000+i))) for i in range(31)];"
        "prune_backups('/data/backups',30);"
        "print(len(list(pathlib.Path('/data/backups').glob('*.db'))))"
    )
    try:
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{vol}:/data", "--entrypoint", "python", image,
             "-c", script], capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stderr[-3000:]
        assert r.stdout.strip().splitlines()[-1] == "30"
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)


@live
def test_nuke_destroys_volume_up_recreates_empty(image, tmp_path):
    """The lifecycle: up creates the named volume, nuke (down -v) destroys it, up recreates
    it empty. compose.yaml hard-names the volume `hive-data` (a fixed operator-UX name, NOT
    project-prefixed), so a compose OVERRIDE renames it to an ISOLATED test volume — this test
    can never touch an operator's real hive-data (asserted)."""
    proj = f"hivee2e{uuid.uuid4().hex[:8]}"
    vol = f"hive-e2e-{uuid.uuid4().hex[:10]}"
    assert vol != "hive-data"                          # hard safety: never the operator's volume
    override = tmp_path / "vol-override.yaml"
    override.write_text(f"volumes:\n  hive-data:\n    name: {vol}\n")
    cenv = {**os.environ, "HIVE_TENANT_ID": "e2e-tenant"}

    def compose(*args, timeout=600):
        return subprocess.run(
            ["docker", "compose", "-p", proj, "-f", "compose.yaml", "-f", str(override), *args],
            cwd=str(_ROOT), env=cenv, capture_output=True, text=True, timeout=timeout)

    def _vol_exists():
        return subprocess.run(["docker", "volume", "inspect", vol],
                              capture_output=True).returncode == 0
    try:
        assert compose("up", "-d", "--build", "--wait", "hive-server").returncode == 0
        assert _vol_exists()
        assert compose("down", "-v").returncode == 0
        assert not _vol_exists()                       # nuke destroyed the isolated volume
        assert compose("up", "-d", "--build", "--wait", "hive-server").returncode == 0
        ex = compose("exec", "-T", "hive-server", "python", "-c",
                     "import sqlite3;print(sqlite3.connect('/data/shared.db')"
                     ".execute('select count(*) from episodes').fetchone()[0])")
        assert ex.returncode == 0, ex.stderr
        assert ex.stdout.strip().splitlines()[-1] == "0"   # recreated empty
    finally:
        compose("down", "-v")
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
