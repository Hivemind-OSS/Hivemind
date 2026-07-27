"""Staleness/drift, writer -> reader, no seam stubbed.

REAL git origin, REAL sync tick (real ``hive-edge mint`` + ``hive-edge verify``
subprocesses), REAL MCP recall handler. Nothing is seeded — every fact here is
manufactured by production code; the ONLY thing asserted is what an agent would
see in the served envelope: ``reference_context[i].drift.type``.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hive.app.config import SyncConfig
from hive.app.sync import SyncService
from hive.domain.change_evidence import ChangeEvidenceService
from tests.mcp._helpers import build_real_server, content, tool_call
from tests.sync.conftest import Origin, meta

ANCHOR = "app.py::greet"
TEXT = "greet() must stay single-arg; callers pass positionally"


def make_syncer(store, tmp_path: Path, **cfg_kw):
    cfg = SyncConfig(mirror_dir=str(tmp_path / "mirrors"), **cfg_kw)
    evidence = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424_242, ranges=store
    )
    return SyncService(cfg, store, evidence, threading.Lock())


def _hit(env, eid):
    for h in env.get("reference_context", []):
        if h.get("episode_id") == eid:
            return h
    raise AssertionError(f"episode {eid} not served: {env}")


@pytest.fixture
def rig(tmp_path):
    origin = Origin(tmp_path / "remote")
    server, clock = build_real_server(t0=1_000_000)
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    syncer = make_syncer(server.store, tmp_path)
    return origin, server, clock, syncer


def test_drift_fresh_then_anchor_changed_reaches_the_agent(rig):
    origin, server, clock, syncer = rig

    env = content(
        tool_call(
            server,
            "hive_write",
            {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
        )
    )
    assert env["status"] == "approved", env
    eid = env["id"]

    # tick 1: clone, advance the watermark, fill the missing baseline, judge drift
    syncer.tick()
    tip1 = meta(server.store, "sync:alpha:last_tip")
    assert tip1 == origin.origin_sha("refs/heads/main"), (
        f"watermark not baselined at the real tip: {tip1}"
    )
    baselines = [
        dict(r)
        for r in server.store.conn.execute(
            "SELECT base_tip FROM anchor_baselines WHERE episode_id=?", (eid,)
        )
    ]
    assert baselines and baselines[0]["base_tip"], (
        "the binding was never given the commit it is measured from — "
        f"drift can never be judged: {baselines}"
    )
    drift_rows = [
        dict(r)
        for r in server.store.conn.execute(
            "SELECT * FROM anchor_drift WHERE repo='alpha'"
        )
    ]
    assert drift_rows, "the drift materializer wrote no anchor_drift row"

    served = content(tool_call(server, "hive_recall", {"query": TEXT}))
    assert _hit(served, eid)["drift"]["type"] == "fresh", served

    # break the anchor's signature on the canonical line
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "signature change",
    )
    origin.push()
    syncer.tick()
    tip2 = meta(server.store, "sync:alpha:last_tip")
    assert tip2 != tip1, "the watermark did not advance over a real push"

    served = content(tool_call(server, "hive_recall", {"query": TEXT}))
    hit = _hit(served, eid)
    assert hit["drift"]["type"] == "anchor_changed", (
        f"a real signature change did not reach the agent as drift: {hit['drift']}"
    )
    assert hit["drift"]["detail"]["per_anchor"][0]["tip_sha"] == tip2


def test_symbol_removal_reads_anchor_missing(rig):
    origin, server, clock, syncer = rig
    env = content(
        tool_call(
            server,
            "hive_write",
            {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
        )
    )
    eid = env["id"]
    syncer.tick()
    origin.commit("app.py", "def other():\n    return 1\n", "remove greet")
    origin.push()
    syncer.tick()
    served = content(tool_call(server, "hive_recall", {"query": TEXT}))
    assert _hit(served, eid)["drift"]["type"] == "anchor_missing", served


def test_branch_route_degrades_and_records_ref_demand(rig):
    """An un-materialized branch route degrades to unverifiable and records the
    demand that will pull its tip onto the materializer's work list — regardless
    of whether that demanded tip later becomes READABLE (that is this module's
    other test)."""
    origin, server, clock, syncer = rig
    env = content(
        tool_call(
            server,
            "hive_write",
            {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
        )
    )
    eid = env["id"]
    syncer.tick()

    served = content(
        tool_call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    )
    hit = _hit(served, eid)
    assert hit["drift"]["type"] == "unverifiable", hit["drift"]
    assert hit["drift"]["detail"]["ref"] == "feature"
    rows = [dict(r) for r in server.store.conn.execute("SELECT * FROM ref_requests")]
    assert [(r["repo"], r["ref"]) for r in rows] == [("alpha", "feature")], rows

    # the materializer must now cover that branch tip
    origin.commit("app.py", 'def greet(n):\n    return "yo " + n\n', "branch work")
    origin.push("HEAD:refs/heads/feature")
    syncer.tick()
    tips = {
        r["tip_sha"]
        for r in server.store.conn.execute(
            "SELECT DISTINCT tip_sha FROM anchor_drift WHERE repo='alpha'"
        )
    }
    feature_tip = origin.origin_sha("refs/heads/feature")
    assert feature_tip in tips, (
        "ref_requests demand did not pull the branch tip into the drift cache: "
        f"{tips} lacks {feature_tip}"
    )


def test_branch_route_records_demand_then_serves_the_verdict(rig):
    """The other half of the branch-route contract: once the daemon has
    materialized the branch tip a prior recall demanded, THE SAME query must
    read the real verdict — not unverifiable forever. A branch-scoped recall's
    demand is supposed to be followed by coverage; this proves coverage
    actually arrives at the reader."""
    origin, server, clock, syncer = rig
    env = content(
        tool_call(
            server,
            "hive_write",
            {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
        )
    )
    eid = env["id"]
    syncer.tick()  # canonical baseline only — nothing about "feature" yet

    served = content(
        tool_call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    )
    assert _hit(served, eid)["drift"]["type"] == "unverifiable"  # not yet materialized

    # a real breaking push on the demanded branch
    origin.commit("app.py", 'def greet(a, b):\n    return "x"\n', "branch break")
    origin.push("HEAD:refs/heads/feature")
    syncer.tick()  # the materializer now covers the demanded branch tip

    served = content(
        tool_call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    )
    hit = _hit(served, eid)
    assert hit["drift"]["type"] == "anchor_changed", (
        "the branch-tip verdict the daemon just materialized must become "
        f"readable by the query that demanded it: {hit['drift']}"
    )
    feature_tip = origin.origin_sha("refs/heads/feature")
    per_anchor = (hit["drift"].get("detail") or {}).get("per_anchor") or [{}]
    assert per_anchor[0].get("tip_sha") == feature_tip, hit["drift"]
