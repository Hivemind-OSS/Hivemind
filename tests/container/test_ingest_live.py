"""The in-situ D7 gate for the census change-outcome feed (skip-guarded live tier).

The BUGS.md green-suite-inert-real-path antidote (BUG-002/003/005/007/011/012/013 class):
a REAL unsigned receipt, fed through the REAL `hive ingest` CLI (real `docker compose exec`,
real stdin pipe, real censusctl) into a REAL running hive serving MCP over HTTP, lands a
REAL `evidence_events` row in the same WAL store the daemon serves — while it serves (the
two-process write is part of what D7 proves). Then, live: O7 (trust untouched), idempotent
re-ingest, and a byte-inert read path.

Gated exactly like test_container_live.py (`HIVE_RUN_DOCKER_TESTS=1` + a reachable Docker
daemon; skipped honestly otherwise). The stack is a THROWAWAY compose project on an
isolated volume and a non-default host port — never the operator's `hive-data` or :8765
(the compose-named-volume isolation lesson).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_RUN = os.environ.get("HIVE_RUN_DOCKER_TESTS") == "1"

RECEIPT = _ROOT / "tests" / "data" / "receipt.real.json"
# a real subject of the real unsigned S3 receipt — the join target
ANCHOR = "matrix/_extract_monolith.py::LanguageConfig"


def _have_docker() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


live = pytest.mark.skipif(
    not (_RUN and _have_docker()),
    reason="live docker test — set HIVE_RUN_DOCKER_TESTS=1 with a reachable daemon")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cli(env: dict, *argv: str, input_text=None, timeout=900) -> subprocess.CompletedProcess:
    """Run the REAL operator CLI (`python -m hive.tools.cli …`) against the throwaway
    compose project (COMPOSE_PROJECT_NAME/COMPOSE_FILE in env drive the isolation)."""
    return subprocess.run([sys.executable, "-m", "hive.tools.cli", *argv],
                          cwd=str(_ROOT), env=env, capture_output=True, text=True,
                          input=input_text, timeout=timeout)


def _mcp(port: int, name: str, args: dict) -> dict:
    """One tools/call over the live loopback HTTP door; returns the tool payload."""
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        envelope = json.loads(resp.read().decode("utf-8"))
    return json.loads(envelope["result"]["content"][0]["text"])


def _in_container_py(env: dict, code: str) -> str:
    """Run a python one-liner inside the RUNNING hive-server; returns its last stdout line
    (asserts on the exec itself so a wiring failure is loud, never an inert green)."""
    r = subprocess.run(["docker", "compose", "exec", "-T", "hive-server",
                        "python", "-c", code],
                       cwd=str(_ROOT), env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, f"in-container probe failed: {r.stderr[-2000:]}"
    return r.stdout.strip().splitlines()[-1]


def _evidence_rows(env: dict) -> list[dict]:
    out = _in_container_py(env, (
        "import sqlite3,json;"
        "con=sqlite3.connect('/data/shared.db');"
        "rows=[dict(zip(('id','episode_id','kind','actor','payload'),r)) for r in "
        "con.execute(\"SELECT id,episode_id,kind,actor,payload FROM evidence_events "
        "WHERE kind='change_outcome' ORDER BY id\")];"
        "print(json.dumps(rows))"))
    return json.loads(out)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """(port, env): a real throwaway stack brought up by the REAL `hive up` on an
    isolated compose project, an isolated volume, and a free loopback port."""
    tmp = tmp_path_factory.mktemp("ingest-live")
    proj = f"hiveingest{uuid.uuid4().hex[:8]}"
    vol = f"hive-ingest-live-{uuid.uuid4().hex[:10]}"
    assert vol != "hive-data"                      # hard safety: never the operator's volume
    port = _free_port()
    assert port != 8765                            # never the operator's live door
    override = tmp / "override.yaml"
    override.write_text(
        "services:\n"
        "  hive-server:\n"
        "    ports: !override\n"
        f'      - "127.0.0.1:{port}:8765"\n'
        "volumes:\n"
        "  hive-data:\n"
        f"    name: {vol}\n")
    env = {**os.environ,
           "COMPOSE_PROJECT_NAME": proj,
           "COMPOSE_FILE": f"{_ROOT / 'compose.yaml'}:{override}",
           "HIVE_TENANT_ID": "d7-tenant",
           "HIVE_STORE__DB_PATH": "/data/shared.db"}
    up = _cli(env, "up")
    try:
        assert up.returncode == 0, f"hive up failed:\n{up.stderr[-3000:]}"
        yield port, env
    finally:
        subprocess.run(["docker", "compose", "down", "-v"], cwd=str(_ROOT), env=env,
                       capture_output=True, timeout=300)
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)


@live
def test_d7_gate_real_receipt_real_row_o7_idempotent_byte_inert(stack):
    port, env = stack
    envelope = json.loads(RECEIPT.read_text())
    statement = json.loads(base64.b64decode(envelope["payload"]))
    prov = statement["predicate"]["provenance"]

    # 2. one anchored episode written over the LIVE loopback MCP door
    w = _mcp(port, "hive_write", {
        "text": "the tree-sitter LanguageConfig table drives every per-language "
                "extraction pass in the monolith extractor",
        "approved_by": "d7-gate", "kind": "gotcha", "anchor": ANCHOR})
    assert w["status"] == "approved", w
    eid = int(w["id"])

    # pre-ingest captures: the O7 baseline and the byte-inert read-path baseline
    health_before = _mcp(port, "hive_health", {})
    query = "LanguageConfig per-language extraction table in the monolith extractor"
    recall_before = _mcp(port, "hive_recall", {"query": query})
    assert recall_before["abstained"] is False     # the read path actually serves it

    # 3. the REAL `hive ingest` (compose exec -T + stdin pipe) while the daemon serves
    ing = _cli(env, "ingest", str(RECEIPT), timeout=300)
    assert ing.returncode == 0, f"hive ingest failed:\n{ing.stderr[-3000:]}"
    report = json.loads(ing.stdout.strip().splitlines()[-1])
    print(f"D7-EVIDENCE ingest-report: {json.dumps(report, sort_keys=True)}")
    # one matched episode ⇒ two rows: change_outcome + the verify_stale rider
    # (LanguageConfig was removed at head); ZERO verified rows — the receipt's tests
    # line is not_run and its regression reach is empty (the honest abstention).
    assert report["matched"] == 1 and len(report["inserted"]) == 2, report
    assert (report["verified_helped"], report["verified_hurt"]) == (0, 0), report
    assert (report["verify_current"], report["verify_stale"]) == (0, 1), report

    # 4. exactly the two REAL rows, in the SAME store the daemon serves, SHA-bound
    rows = _evidence_rows(env)
    assert len(rows) == 2, rows
    row = rows[0]
    print(f"D7-EVIDENCE evidence-row: {json.dumps(row, sort_keys=True)}")
    assert row["episode_id"] == eid and row["actor"] == "census"
    body = json.loads(row["payload"])
    assert body["base_sha"] == prov["base_sha"]
    assert body["head_sha"] == prov["head_sha"]
    assert body["receipt_sha256"] == statement["subject"][0]["digest"]["sha256"]
    assert body["matched"]["symbol"] == "LanguageConfig"
    verify_row = rows[1]
    print(f"D7-EVIDENCE verify-row: {json.dumps(verify_row, sort_keys=True)}")
    assert verify_row["episode_id"] == eid and verify_row["kind"] == "verify_stale"
    vbody = json.loads(verify_row["payload"])
    assert (vbody["exists_after"], vbody["drift"]) == (False, "removed")
    assert vbody["stamp"]["head_sha"] == prov["head_sha"]

    # 5. O7 live: the feed mutated NO trust
    health_after = _mcp(port, "hive_health", {})
    assert health_after["trust_counts"] == health_before["trust_counts"]
    trust = _in_container_py(env, (
        "import sqlite3;print(sqlite3.connect('/data/shared.db')"
        f".execute('SELECT trust FROM episodes WHERE id={eid}').fetchone()[0])"))
    assert trust == "established"

    # 6. idempotency under the real transport: re-ingest adds nothing
    ing2 = _cli(env, "ingest", str(RECEIPT), timeout=300)
    assert ing2.returncode == 0, ing2.stderr[-2000:]
    report2 = json.loads(ing2.stdout.strip().splitlines()[-1])
    print(f"D7-EVIDENCE reingest-report: {json.dumps(report2, sort_keys=True)}")
    assert report2["already_recorded"] == 2 and report2["inserted"] == []
    assert len(_evidence_rows(env)) == 2           # row count unchanged

    # 7. byte-inert read path: the served envelope is unchanged by the ingest
    #    (trace_id is the per-call correlation id — the one by-design fresh value)
    recall_after = _mcp(port, "hive_recall", {"query": query})
    before = {k: v for k, v in recall_before.items() if k != "trace_id"}
    after = {k: v for k, v in recall_after.items() if k != "trace_id"}
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)

    # refused-malformed, live: a truncated receipt refuses loudly with zero new rows
    bad = json.dumps({"payloadType": "application/vnd.in-toto+json"})
    r_bad = subprocess.run(["docker", "compose", "exec", "-T", "hive-server",
                            "python", "-m", "hive.tools.censusctl", "ingest", "-"],
                           cwd=str(_ROOT), env=env, capture_output=True, text=True,
                           input=bad, timeout=120)
    assert r_bad.returncode == 65, r_bad.stderr[-1000:]
    assert "refused" in r_bad.stderr.lower()
    assert len(_evidence_rows(env)) == 1           # still just the one row
