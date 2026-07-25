"""The TTL/decay leg an operator feels, and the census/sync health block after
REAL ticks — driven writer -> reader rather than seeded.
"""

from __future__ import annotations

import json
import threading

from hive.app.config import SyncConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.sync import SyncService
from hive.domain.change_evidence import ChangeEvidenceService
from tests.mcp._helpers import build_real_server
from tests.sync.conftest import Origin

DAY = 86_400
T = "the mirror is a rebuildable cache; the watermark meta is the durable truth"


def call(server, name, args, *, agent="agent-a"):
    resp = server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("t", agent),
    )
    return json.loads(resp.result["content"][0]["text"])


def served_ids(env):
    return [h["episode_id"] for h in env.get("reference_context", [])]


def test_exposure_refreshes_the_provisional_ttl_clock():
    server, clock = build_real_server(t0=1_000_000)
    eid = call(server, "hive_write", {"text": T})["id"]

    clock.advance(40 * DAY)
    assert eid in served_ids(call(server, "hive_recall", {"query": T}))  # refreshes
    clock.advance(40 * DAY)  # 80 days total, but only 40 since exposure
    assert eid in served_ids(call(server, "hive_recall", {"query": T})), (
        "exposure did not refresh the provisional TTL clock"
    )


def test_an_unused_provisional_stops_serving_at_the_ttl():
    server, clock = build_real_server(t0=1_000_000)
    eid = call(server, "hive_write", {"text": T})["id"]
    clock.advance(46 * DAY)  # provisional_ttl_days = 45
    assert eid not in served_ids(call(server, "hive_recall", {"query": T})), (
        "a TTL-lapsed provisional is still being served"
    )
    assert server.store.get_episode(eid).trust == "provisional", (
        "decay is LAZY — unservable now, materialized by the sweep"
    )
    swept = server.recall.lifecycle.sweep(now=clock.now())
    assert swept
    assert server.store.get_episode(eid).trust == "deprecated"


def test_health_block_is_written_by_the_daemon_not_by_a_fixture(tmp_path):
    origin = Origin(tmp_path / "remote")
    server, _c = build_real_server(t0=1_000_000)
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
        SyncConfig(mirror_dir=str(tmp_path / "mirrors")),
        server.store,
        ev,
        threading.Lock(),
        now=lambda: 777_000,
    )

    health = call(server, "hive_health", {"include_census_health": True})
    ch = health["census_health"]
    assert (
        ch["repos"]["alpha"].get("sync") is None or "sync" not in ch["repos"]["alpha"]
    )
    assert ch["fleet"] == {"last_sync_ts": None, "last_error": None}, ch["fleet"]

    # write an anchored memory so the backfill leg has real work
    call(
        server,
        "hive_write",
        {"text": T, "anchors": [{"repo": "alpha", "anchor": "app.py::greet"}]},
    )
    syncer.tick()

    ch = call(server, "hive_health", {"include_census_health": True})["census_health"]
    block = ch["repos"]["alpha"]["sync"]
    assert block["configured"] is True
    assert block["tracked_ref"] == "main", block
    assert block["last_tip"] == origin.origin_sha("refs/heads/main"), block
    assert block["last_sync_ts"] == "777000", block
    assert block["last_error"] is None, block
    assert block["backfilled_total"] == 1, (
        f"the mint leg's counter is not wired to the served field: {block}"
    )
    assert ch["fleet"]["last_sync_ts"] == "777000"
    assert ch["fleet"]["last_error"] is None

    # a dead daemon must be named in `fleet`, not hidden behind passing repo blocks
    def boom():
        raise RuntimeError("disk I/O error")

    class DeadRegistry:
        """Only the registry read faults; every other store surface is the REAL one."""

        def __init__(self, real):
            self._real = real

        def repo_registry(self):
            raise RuntimeError("disk I/O error")

        def __getattr__(self, name):
            return getattr(self._real, name)

    syncer._store = DeadRegistry(server.store)
    syncer.tick()
    syncer._store = server.store
    ch = call(server, "hive_health", {"include_census_health": True})["census_health"]
    assert ch["repos"]["alpha"]["sync"]["last_error"] is None, (
        "the repo block still reads healthy — as designed"
    )
    assert "registry:" in (ch["fleet"]["last_error"] or ""), ch["fleet"]
    assert ch["fleet"]["last_sync_ts"] == "777000", "frozen at the last clean tick"
