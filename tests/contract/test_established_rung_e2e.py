"""provisional -> established by a SHA-bound outcome-verified win on the
canonical line, with NOTHING seeded.

The ``outcome_verified_helped`` row is minted by the REAL census receipt the REAL
sync ledger leg builds in a subprocess over a REAL git push, and the establish
sweep is the daemon's own post-ingest call. The observable outcome asserted is the
trust label an agent reads on the next hive_recall.
"""

from __future__ import annotations

import json
import threading

import pytest

from hive.app.config import SyncConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.sync import SyncService
from hive.domain.change_evidence import ChangeEvidenceService
from tests.mcp._helpers import build_real_server
from tests.sync.conftest import Origin, git

ANCHOR = "app.py::greet"
DONT = "dont add a second positional arg to greet(); every caller breaks"
BASE_APP = 'def greet(name):\n    return "hi " + name\n'
HEAD_APP = 'def greet(name, punct):\n    return "hi " + name + punct\n'
TEST_PY = "from app import greet\n\n\ndef test_greet():\n    assert greet('bob') == 'hi bob'\n"


def call(server, name, args, *, agent="agent-a"):
    resp = server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("t", agent),
    )
    return json.loads(resp.result["content"][0]["text"])


def evidence(store, eid):
    return [
        dict(r)
        for r in store.conn.execute(
            "SELECT kind, actor, ts FROM evidence_events WHERE episode_id=? ORDER BY id",
            (eid,),
        )
    ]


@pytest.fixture
def rig(tmp_path):
    origin = Origin(tmp_path / "remote")
    (origin.work / "pyproject.toml").write_text('[project]\nname="a"\nversion="0"\n')
    (origin.work / "app.py").write_text(BASE_APP)
    (origin.work / "test_app.py").write_text(TEST_PY)
    git(origin.work, "add", "-A")
    git(origin.work, "commit", "-qm", "verifiable seed")
    origin.push()

    server, clock = build_real_server(t0=1_000_000)
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    cfg = SyncConfig(mirror_dir=str(tmp_path / "mirrors"))
    ev = ChangeEvidenceService(
        reader=server.store,
        appender=server.store,
        now=lambda: 424_242,
        ranges=server.store,
    )
    syncer = SyncService(
        cfg,
        server.store,
        ev,
        threading.Lock(),
        lifecycle=server.recall.lifecycle,
    )
    return origin, server, syncer


def test_a_real_canonical_ingest_establishes_a_dont_memory(rig):
    origin, server, syncer = rig
    env = call(
        server,
        "hive_write",
        {
            "text": DONT,
            "polarity": "dont",
            "anchors": [{"repo": "alpha", "anchor": ANCHOR}],
        },
    )
    assert env["status"] == "approved"
    eid = env["id"]
    assert env["trust"] == "provisional"

    syncer.tick()  # baseline only — no historical receipt
    assert evidence(server.store, eid) == [], evidence(server.store, eid)

    # a real breaking push on the canonical line, whose test run really fails
    (origin.work / "app.py").write_text(HEAD_APP)
    git(origin.work, "add", "-A")
    git(origin.work, "commit", "-qm", "breaking change")
    origin.push()

    syncer.tick()

    kinds = [r["kind"] for r in evidence(server.store, eid)]
    assert "outcome_verified_helped" in kinds, (
        f"the real canonical ingest minted no verified win: {kinds}"
    )
    assert "promote" in kinds, f"the post-ingest establish sweep did not run: {kinds}"
    assert server.store.get_episode(eid).trust == "established"

    served = call(server, "hive_recall", {"query": DONT}, agent="agent-z")
    hit = next(h for h in served["reference_context"] if h["episode_id"] == eid)
    assert hit["trust"] == "established", hit
    # and the same ingest recorded the anchor as stale on the ledger
    assert "verify_stale" in kinds, kinds


def test_opting_out_makes_the_rung_unreachable(tmp_path):
    """HIVE_AUTONOMY__VERIFIED_PROMOTION=false ⇒ no reader wired ⇒ never established."""
    from hive.app.config import AutonomyConfig

    origin = Origin(tmp_path / "remote2")
    (origin.work / "pyproject.toml").write_text('[project]\nname="a"\nversion="0"\n')
    (origin.work / "app.py").write_text(BASE_APP)
    (origin.work / "test_app.py").write_text(TEST_PY)
    git(origin.work, "add", "-A")
    git(origin.work, "commit", "-qm", "verifiable seed")
    origin.push()

    server, _clock = build_real_server(
        t0=1_000_000, autonomy=AutonomyConfig(verified_promotion=False)
    )
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    ev = ChangeEvidenceService(
        reader=server.store,
        appender=server.store,
        now=lambda: 424_242,
        ranges=server.store,
    )
    syncer = SyncService(
        SyncConfig(mirror_dir=str(tmp_path / "m2")),
        server.store,
        ev,
        threading.Lock(),
        lifecycle=server.recall.lifecycle,
    )
    env = call(
        server,
        "hive_write",
        {
            "text": DONT,
            "polarity": "dont",
            "anchors": [{"repo": "alpha", "anchor": ANCHOR}],
        },
    )
    eid = env["id"]
    syncer.tick()
    (origin.work / "app.py").write_text(HEAD_APP)
    git(origin.work, "add", "-A")
    git(origin.work, "commit", "-qm", "breaking change")
    origin.push()
    syncer.tick()

    kinds = [r["kind"] for r in evidence(server.store, eid)]
    assert "outcome_verified_helped" in kinds, kinds
    assert server.store.get_episode(eid).trust == "provisional", (
        "the opt-out must leave the rung unreachable"
    )
