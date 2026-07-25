"""The branch-tip half of the drift cache, and the retired-anchor half of the
materializer work list — both driven end to end, nothing seeded.

``attach_drift`` records ref demand and the materializer really does verify a
demanded branch tip and store a wire verdict for it; a query that demanded a
branch tip must be able to read what the daemon materialized for it, a memory
that names its own line must be judged against THAT line (never softened by,
nor immune via, a line it never named), and a retired memory's anchor must
leave both materializer work lists (verify and mint-backfill) so it stops
consuming the capped per-tick budgets forever.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hive.app.config import SyncConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.sync import SyncService
from hive.domain.change_evidence import ChangeEvidenceService
from tests.contract.conftest import require_method
from tests.mcp._helpers import build_real_server
from tests.sync.conftest import Origin

ANCHOR = "app.py::greet"
TEXT = "greet() must stay single-arg; callers pass positionally"


def call(server, name, args, *, agent="agent-a"):
    resp = server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("t", agent),
    )
    return json.loads(resp.result["content"][0]["text"])


def make_syncer(store, tmp_path: Path, **cfg_kw):
    cfg = SyncConfig(mirror_dir=str(tmp_path / "mirrors"), **cfg_kw)
    ev = ChangeEvidenceService(
        reader=store,
        appender=store,
        now=lambda: 424_242,
        ranges=store,
    )
    return SyncService(cfg, store, ev, threading.Lock())


@pytest.fixture
def rig(tmp_path):
    origin = Origin(tmp_path / "remote")
    server, _c = build_real_server(t0=1_000_000)
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    syncer = make_syncer(server.store, tmp_path)
    return origin, server, syncer


def test_branch_tip_verdict_reaches_the_agent_that_demanded_it(rig):
    """The branch tip the daemon materializes because a recall demanded it
    must become readable by that SAME kind of query — coverage that never
    reaches the reader is not coverage."""
    origin, server, syncer = rig
    eid = call(
        server,
        "hive_write",
        {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
    )["id"]
    syncer.tick()

    # an agent scopes recall to a feature branch -> demand recorded, unverifiable
    env = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    assert hit["drift"]["type"] == "unverifiable"

    # the branch really exists and really drifts
    origin.commit("app.py", 'def greet(a, b):\n    return "x"\n', "branch break")
    origin.push("HEAD:refs/heads/feature")
    syncer.tick()

    feature_tip = origin.origin_sha("refs/heads/feature")
    rows = {
        (r["tip_sha"], r["anchor"]): r["verdict"]
        for r in server.store.conn.execute(
            "SELECT tip_sha, anchor, verdict FROM anchor_drift WHERE repo='alpha'"
        )
    }
    assert (feature_tip, ANCHOR) in rows, rows
    assert rows[(feature_tip, ANCHOR)] == "anchor_changed", rows

    # ...and the very query that demanded it must now be able to read it
    env = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    assert hit["drift"]["type"] == "anchor_changed", (
        "the branch-tip verdict the daemon just materialized must reach the "
        f"agent that demanded it: {hit['drift']}"
    )
    per_anchor = (hit["drift"].get("detail") or {}).get("per_anchor") or [{}]
    assert per_anchor[0].get("tip_sha") == feature_tip, hit["drift"]


def test_unmaterialized_branch_tip_is_unverifiable_not_fresh(tmp_path):
    """A resolved (ref, sha) must be recorded before its verdicts are computed,
    so a budget-starved tick — tip known, no cache row yet for it — reads
    unverifiable, never a fresh inherited from an unrelated tip. Guarded on the
    per-ref tip surface: without it, today's blanket 'any branch route reads
    unverifiable' rule would pass this assertion for the wrong reason (it never
    distinguishes a starved-but-known tip from an unknown one)."""
    origin = Origin(tmp_path / "remote")
    server, _c = build_real_server(t0=1_000_000)
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    require_method(server.store, "ref_tip")
    eid = call(
        server,
        "hive_write",
        {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
    )["id"]
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()  # canonical baselined + materialized fresh at tip0

    call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})  # demand
    origin.commit("app.py", 'def greet(a, b):\n    return "x"\n', "branch break")
    origin.push("HEAD:refs/heads/feature")
    # move the canonical tip too, so ITS re-materialization competes for the same
    # single-spawn-per-tick budget and the newly-demanded feature tip starves
    origin.commit(
        "app.py", 'def greet(a):\n    return "hi " + a + "!"\n', "canonical tweak"
    )
    origin.push()

    starved = make_syncer(server.store, tmp_path, drift_per_tick=1)
    starved.tick()  # the sole spawn is spent re-materializing canonical

    env = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    assert hit["drift"]["type"] == "unverifiable", (
        "a branch tip resolved but not yet verified this tick must read "
        f"unverifiable, never fresh: {hit['drift']}"
    )


def test_declared_line_downgrades_off_line_staleness(rig):
    """A memory that declares a line different from the consumer's, whose base
    verdict is stale-tier, must route through the advisory branch_scoped —
    never the raw verdict from a line the memory never named. 'feature' need
    not exist in git at all: the routing is a pure comparison of the declared
    ref against the consumer's own resolved line, applied to whatever verdict
    was ALREADY materialized for that consumer's line."""
    origin, server, syncer = rig
    eid = call(
        server,
        "hive_write",
        {
            "text": TEXT,
            "anchors": [{"repo": "alpha", "anchor": ANCHOR}],
            "repos": ["alpha@feature"],
        },
    )["id"]
    syncer.tick()  # fresh at the canonical tip
    origin.commit("app.py", 'def greet(a, b):\n    return "x"\n', "canonical break")
    origin.push()
    syncer.tick()  # the CANONICAL line (not the declared "feature") is what broke

    env = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha"]})
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    assert hit["drift"]["type"] == "branch_scoped", (
        "a memory declared on 'feature' read from the canonical line must route "
        f"through the advisory branch_scoped, never the raw stale verdict: {hit['drift']}"
    )


def test_same_line_consumer_still_sees_the_stale_verdict(rig):
    """The other half of the routing contract: when the declared line and the
    consumer's line are the SAME, the raw verdict must ride through unrouted.
    A memory read on its OWN declared line must never be softened to the
    advisory branch_scoped — that would turn the declared ref into
    caller-asserted immunity."""
    origin, server, syncer = rig
    eid = call(
        server,
        "hive_write",
        {
            "text": TEXT,
            "anchors": [{"repo": "alpha", "anchor": ANCHOR}],
            "repos": ["alpha@feature"],
        },
    )["id"]
    syncer.tick()
    call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})  # demand
    origin.commit("app.py", 'def greet(a, b):\n    return "x"\n', "feature break")
    origin.push("HEAD:refs/heads/feature")
    syncer.tick()

    env = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    assert hit["drift"]["type"] == "anchor_changed", (
        "a consumer reading the memory's OWN declared line must see the raw "
        f"verdict, never softened to branch_scoped: {hit['drift']}"
    )


def test_a_pruned_anchor_leaves_the_verify_work_list(rig):
    """Retirement flips TRUST, not status — a retired memory's anchor must
    leave the drift materializer's work list, and stop being re-verified at
    every new canonical tip forever."""
    origin, server, syncer = rig
    eid = call(
        server,
        "hive_write",
        {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
    )["id"]
    syncer.tick()
    origin.commit("app.py", "def other():\n    return 1\n", "remove greet")
    origin.push()
    syncer.tick()
    assert call(server, "hive_prune", {"episode_id": eid})["status"] == "pruned"
    assert server.store.get_episode(eid).trust == "deprecated"

    fps = syncer._repo_fps("alpha")
    assert ANCHOR not in fps, (
        f"a retired memory's anchor must leave the materializer work list: "
        f"{sorted(fps)}"
    )

    origin.commit("app.py", "def other():\n    return 2\n", "more churn")
    origin.push()
    syncer.tick()
    tip = origin.origin_sha("refs/heads/main")
    rows = [
        dict(r)
        for r in server.store.conn.execute(
            "SELECT anchor FROM anchor_drift WHERE repo='alpha' AND tip_sha=?", (tip,)
        )
    ]
    assert not any(r["anchor"] == ANCHOR for r in rows), (
        f"the pruned anchor must not be re-verified at the new tip: {rows}"
    )


def test_a_shared_anchor_survives_one_memorys_retirement(rig):
    """An anchor shared with a LIVE memory must stay in the work list even
    after one of the memories bound to it retires."""
    origin, server, syncer = rig
    a = call(
        server,
        "hive_write",
        {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
    )["id"]
    b = call(
        server,
        "hive_write",
        {
            "text": "a second lesson about the same anchor",
            "anchors": [{"repo": "alpha", "anchor": ANCHOR}],
        },
    )["id"]
    syncer.tick()

    call(server, "hive_outcome", {"hurt": [a]}, agent="agent-b")  # another identity
    env = call(server, "hive_prune", {"episode_id": a})
    assert env["status"] == "pruned", env
    assert server.store.get_episode(a).trust == "deprecated"
    assert server.store.get_episode(b).trust == "provisional"

    fps = syncer._repo_fps("alpha")
    assert ANCHOR in fps, (
        "an anchor shared with a LIVE memory must stay in the work list even "
        f"after one of its memories retires: {sorted(fps)}"
    )


def test_retired_anchors_leave_the_mint_backfill_sweep(rig):
    """The mint-backfill twin of the work-list contract above:
    ``anchors_lacking_fp`` must not keep offering a retired memory's anchor to
    the backfill sweep forever either — the same retirement predicate belongs
    on both legs, or it recurs on the one left behind."""
    origin, server, syncer = rig
    eid = call(
        server,
        "hive_write",
        {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
    )["id"]
    # no tick yet: the fp carrier is still empty, so this anchor is a live
    # backfill candidate right now — the precondition the sweep is FOR
    assert ANCHOR in [a for _e, a in server.store.anchors_lacking_fp("alpha")]

    call(server, "hive_outcome", {"hurt": [eid]}, agent="agent-b")
    env = call(server, "hive_prune", {"episode_id": eid})
    assert env["status"] == "pruned", env

    remaining = [a for _e, a in server.store.anchors_lacking_fp("alpha")]
    assert ANCHOR not in remaining, (
        f"a retired memory's anchor must leave the mint-backfill sweep too: {remaining}"
    )


def test_a_faulting_ref_tip_read_degrades_the_hit_not_the_read(rig, monkeypatch):
    """External-interaction inventory: a ref_tips read fault must degrade that
    hit to unverifiable, never break the read. Guarded on the not-yet-built
    read surface — patching a method that does not exist yet would silently do
    nothing, and today's blanket branch-route rule would pass the assertion for
    the wrong reason."""
    origin, server, syncer = rig
    require_method(server.store, "ref_tip")
    eid = call(
        server,
        "hive_write",
        {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
    )["id"]
    syncer.tick()
    call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})  # demand
    origin.commit("app.py", 'def greet(a, b):\n    return "x"\n', "branch break")
    origin.push("HEAD:refs/heads/feature")
    syncer.tick()  # feature's real anchor_changed verdict is now materialized

    def boom(*a, **kw):
        raise RuntimeError("ref_tips read exploded")

    monkeypatch.setattr(server.store, "ref_tip", boom, raising=False)
    env = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    hit = next(
        h for h in env["reference_context"] if h["episode_id"] == eid
    )  # never breaks
    assert hit["drift"]["type"] == "unverifiable", (
        f"a faulting ref_tip read must degrade this hit, never raise into the "
        f"read: {hit['drift']}"
    )


def _cache_rows(store, repo: str) -> dict[str, int]:
    """Every feed-DERIVED cache row count for one repo, read straight from the
    tables the deregistration sweep must reach."""
    return {
        table: store.conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE repo=?", (repo,)
        ).fetchone()["c"]
        for table in ("ref_tips", "anchor_drift", "ref_requests")
    }


def test_a_reregistered_repo_never_serves_a_verdict_from_its_previous_incarnation(
    rig, tmp_path
):
    """J3 / BUG-068. Deregistration forgets the feed AND everything derived from
    it. ``ref_tips`` is the branch twin of ``sync:<repo>:last_tip`` but lives in a
    table the BUG-060 meta sweep cannot reach — so a re-registered name used to
    resolve a DEAD incarnation's tip and read its surviving ``anchor_drift`` rows
    as ``fresh``. The canonical path was already immune; the branch path was not."""
    origin, server, syncer = rig
    eid = call(
        server,
        "hive_write",
        {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
    )["id"]
    origin.push("HEAD:refs/heads/feature")
    call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    syncer.tick()  # really materializes a `fresh` verdict at the feature tip

    env = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    assert hit["drift"]["type"] == "fresh", hit["drift"]
    assert _cache_rows(server.store, "alpha")["ref_tips"] > 0

    # deregister: the feed AND everything derived from it must go, same tx
    assert server.store.repo_remove("alpha") is True
    assert _cache_rows(server.store, "alpha") == {
        "ref_tips": 0,
        "anchor_drift": 0,
        "ref_requests": 0,
    }, "a deregistered repo must hold zero feed-derived cache rows"

    # while deregistered the NAME is unknown, so the scope is refused outright —
    # the recall never reaches the drift rider at all
    env = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    assert env["status"] == "refused", env

    # the memory and its scope survive: they are MEMORY, not feed
    assert server.store.get_episode(eid) is not None
    assert [a.repo for a in server.store.get_episode(eid).anchors] == ["alpha"]

    # re-register the SAME name against a remote with no `feature` line at all,
    # so nothing can overwrite a surviving watermark on the first tick
    fresh_origin = Origin(tmp_path / "remote2")
    server.store.repo_add(
        name="alpha",
        url=fresh_origin.url,
        canonical_ref="main",
        token_env="",
        added_ts=1,
    )
    call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    syncer.tick()

    env = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    assert hit["drift"]["type"] == "unverifiable", (
        "a re-registered name must never serve a verdict measured on the previous "
        f"incarnation's tip: {hit['drift']}"
    )


def test_a_ref_that_leaves_the_work_list_loses_its_watermark_on_the_same_tick(
    rig, tmp_path
):
    """J4 / BUG-067. ``ref_tips`` is a rebuildable cache with the same bound as its
    ``drift_prune`` sibling: a ref that stops being canonical, declared, or demanded
    leaves the work list, so its watermark goes with it — and a recall on it reads
    the fail-safe ``unverifiable``, never a tip that no longer exists."""
    origin, server, syncer = rig
    eid = call(
        server,
        "hive_write",
        {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
    )["id"]
    for branch in ("keep", "drop-a", "drop-b"):
        origin.push(f"HEAD:refs/heads/{branch}")
        call(server, "hive_recall", {"query": TEXT, "repos": [f"alpha@{branch}"]})
    syncer.tick()

    tips = {
        r["ref"]
        for r in server.store.conn.execute(
            "SELECT ref FROM ref_tips WHERE repo='alpha'"
        )
    }
    assert {"keep", "drop-a", "drop-b"} <= tips, tips

    # two branches really disappear from the remote; demand for `keep` is renewed
    for branch in ("drop-a", "drop-b"):
        origin.push(f":refs/heads/{branch}")
    call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@keep"]})
    syncer.tick()

    tips = {
        r["ref"]
        for r in server.store.conn.execute(
            "SELECT ref FROM ref_tips WHERE repo='alpha'"
        )
    }
    assert "keep" in tips
    assert not ({"drop-a", "drop-b"} & tips), (
        f"a ref that left the work list must lose its watermark: {tips}"
    )

    env = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@drop-a"]})
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    assert hit["drift"]["type"] == "unverifiable", hit["drift"]
