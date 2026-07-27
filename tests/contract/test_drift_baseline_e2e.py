"""BUG-071 — a baseline is NEVER moved once recorded, and ``fresh`` is never a claim
about a comparison that did not happen.

The mechanism changed (a recorded BASELINE COMMIT replaced a minted fingerprint
carrier) but the invariant did not, and it is the same invariant for the same reason:
re-baselining an anchor at a later tip would record the post-change state as the
reference and freeze ``fresh`` for a break that already landed. Under the old design
that was enforced by DEFERRING a mint the ledger leg had just seen change; under this
one it is enforced structurally — the baseline is written once, insert-if-absent, and
nothing can move it.

Drives the REAL surfaces: a real git origin, a REAL sync tick (real
``python -m hive.census.cli build`` subprocess, real git plumbing), and the REAL MCP
recall + retirement handlers. The ONLY seam used is the documented scripted ``Run``
door, and only to make a spawn FAIL — the failure directions the daemon declares.
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
from tests.contract.conftest import RecordingRun, completed
from tests.mcp._helpers import build_real_server
from tests.sync.conftest import Origin, build_receipt, meta

ANCHOR = "app.py::greet"
TEXT = "greet() must stay single-arg; callers pass positionally"


# ── rig ───────────────────────────────────────────────────────────────────────


def call(server, name, args, *, agent="agent-a"):
    resp = server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("t", agent),
    )
    return json.loads(resp.result["content"][0]["text"]), bool(
        resp.result.get("isError")
    )


def make_syncer(store, tmp_path: Path, run=None, **cfg_kw):
    cfg = SyncConfig(mirror_dir=str(tmp_path / "mirrors"), **cfg_kw)
    evidence = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424_242, ranges=store
    )
    kwargs = {"run": run} if run is not None else {}
    return SyncService(cfg, store, evidence, threading.Lock(), **kwargs)


@pytest.fixture
def rig(tmp_path):
    origin = Origin(tmp_path / "remote")
    server, clock = build_real_server(t0=1_000_000)
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    return origin, server, clock, tmp_path


def write(server, text, anchor):
    env, _err = call(
        server,
        "hive_write",
        {"text": text, "anchors": [{"repo": "alpha", "anchor": anchor}]},
    )
    assert env["status"] in ("approved", "redacted"), env
    return env["id"]


def baseline(server, eid, anchor=ANCHOR) -> str:
    row = server.store.conn.execute(
        "SELECT base_tip FROM anchor_baselines WHERE episode_id=? AND anchor=?",
        (eid, anchor),
    ).fetchone()
    return "" if row is None else str(row["base_tip"])


def served_drift(server, eid, query=TEXT) -> str:
    env, _err = call(server, "hive_recall", {"query": query})
    for hit in env.get("reference_context", []):
        if hit.get("episode_id") == eid:
            return hit["drift"]["type"]
    raise AssertionError(f"episode {eid} not served: {env}")


def is_census_build(argv) -> bool:
    return "hive.census.cli" in argv and "build" in argv


# ── an unmeasured anchor never reads fresh ────────────────────────────────────


def test_an_unbaselined_anchor_never_reads_fresh(rig):
    """No baseline means no comparison happened. The served verdict must be the
    honest unknown, not the positive claim ``fresh``."""
    origin, server, _clock, tmp_path = rig

    eid = write(server, TEXT, ANCHOR)
    assert baseline(server, eid) == "", (
        "precondition: the repo has no watermark yet, so nothing was observed"
    )
    assert served_drift(server, eid) == "unverifiable", (
        "an anchor whose baseline was NEVER recorded must not read fresh — that is "
        "a positive claim about a comparison that did not happen"
    )

    # the daemon's own sweep fills it, and a real comparison then yields a real fresh
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()
    tip0 = origin.origin_sha("refs/heads/main")
    assert baseline(server, eid) == tip0
    assert served_drift(server, eid) == "fresh", "a MEASURED, intact anchor is fresh"

    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    syncer.tick()
    assert served_drift(server, eid) == "anchor_changed"


def test_an_unbaselined_anchor_never_qualifies_retirement(rig):
    """``unverifiable`` ∉ QUALIFYING_DRIFT: an un-baselined anchor is a benign no-op
    for BOTH retirement verbs — unknown never retires."""
    _origin, server, _clock, _tmp_path = rig
    eid = write(server, TEXT, ANCHOR)
    winner = write(server, TEXT + " (successor)", ANCHOR + "2")
    assert served_drift(server, eid) == "unverifiable"

    env, err = call(server, "hive_prune", {"episode_id": eid})
    assert err is False, "an unqualified prune is a benign no-op, never an error"
    assert env["status"] == "noop", env
    assert "no qualifying machine signal" in env["reason"], env
    assert server.store.get_episode(eid).trust == "provisional"

    env, err = call(server, "hive_supersede", {"loser": eid, "winner": winner})
    assert err is False and env["status"] == "noop", env
    assert server.store.get_episode(eid).trust == "provisional"


def test_a_real_receipt_still_lands_its_verify_rows(rig):
    """Retirement clause 1b's feed, end to end: a real census receipt over a real
    range still writes the ledger rows the gate reads — the change path is untouched
    by the staleness rewrite."""
    origin, server, _clock, tmp_path = rig
    eid = write(server, TEXT, ANCHOR)
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()

    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    syncer.tick()

    kinds = {
        r["kind"]
        for r in server.store.conn.execute(
            "SELECT DISTINCT kind FROM evidence_events WHERE episode_id=?", (eid,)
        )
    }
    assert "change_outcome" in kinds, kinds
    assert not any(k.startswith("verify_") for k in kinds), (
        f"the census verification channel is retired — git is the ONE staleness "
        f"oracle, so no second one may start writing rows again: {kinds}"
    )


# ── the invariant: a recorded baseline is never moved ─────────────────────────


def test_a_recorded_baseline_is_never_moved(rig):
    """BUG-071's invariant, verbatim, at the level it now lives: whatever else moves,
    the commit a binding is measured FROM does not. Moving it forward to a tip after
    a break would erase that break — the memory would read ``fresh`` forever about
    code that changed under it."""
    origin, server, _clock, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()
    tip0 = origin.origin_sha("refs/heads/main")

    eid = write(server, TEXT, ANCHOR)
    assert baseline(server, eid) == tip0, "written at the watermark it was written at"

    for message in ("widen greet", "widen greet again", "and again"):
        origin.commit(
            "app.py", f"def greet(a, b, c):\n    return {message!r}\n", message
        )
        origin.push()
        syncer.tick()
        assert baseline(server, eid) == tip0, (
            f"the baseline moved after {message!r} — every later break would be "
            "measured from a tree that already contained the earlier ones"
        )
        assert served_drift(server, eid) == "anchor_changed"


def test_a_break_that_lands_before_the_first_tick_is_still_seen(rig):
    """The window BUG-071's deferral existed to close: a memory written while a break
    is already in flight. The baseline is the watermark the server had ALREADY
    observed, not the tip it is about to advance to — so the range that lands next is
    inside the comparison, not before it."""
    origin, server, _clock, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()  # watermark at tip0
    tip0 = origin.origin_sha("refs/heads/main")

    # the break is pushed, then the memory is written, then the tick censuses it
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    eid = write(server, TEXT, ANCHOR)
    assert baseline(server, eid) == tip0, (
        "the baseline is what the server had OBSERVED, never the unfetched remote tip"
    )

    syncer.tick()
    assert served_drift(server, eid) == "anchor_changed", (
        "a break inside the very first judged range must be visible — this is the "
        "false-fresh BUG-071 was about, reached structurally instead of by deferral"
    )


def test_a_second_memory_on_the_same_anchor_gets_its_own_baseline(rig):
    """Baselines are keyed per (episode, repo, anchor), so a memory written AFTER a
    break measures from after it while the older memory keeps measuring from before —
    two honest answers about the same anchor at the same tip."""
    origin, server, _clock, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()
    tip0 = origin.origin_sha("refs/heads/main")
    early = write(server, TEXT, ANCHOR)

    origin.commit("app.py", 'def greet(name, punct):\n    return "x"\n', "widen greet")
    origin.push()
    syncer.tick()
    tip1 = origin.origin_sha("refs/heads/main")
    late = write(server, "greet takes a punct argument now", ANCHOR)
    syncer.tick()

    assert baseline(server, early) == tip0
    assert baseline(server, late) == tip1
    assert served_drift(server, early) == "anchor_changed"
    assert served_drift(server, late, "greet takes a punct argument now") == "fresh"


def test_a_faulted_ledger_leg_still_baselines_and_judges(rig):
    """The two legs are INDEPENDENT now. The ledger leg used to be a precondition of
    baselining, because a mint from a tree whose changes the server could not census
    was the false-fresh risk. A git baseline carries no such risk — it records a
    COMMIT, not a shape — so a census fault costs evidence rows, never coverage."""
    origin, server, _clock, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()
    tip0 = origin.origin_sha("refs/heads/main")

    eid = write(server, TEXT, ANCHOR)
    origin.commit("util.py", "def helper(x):\n    return x\n", "add util")
    origin.push()

    faulting = make_syncer(
        server.store,
        tmp_path,
        run=RecordingRun(
            script=[(is_census_build, completed(rc=1, stderr="census broke"))]
        ),
    )
    faulting.tick()

    assert baseline(server, eid) == tip0, (
        "the baseline is the watermark the server already had — a census fault "
        "cannot corrupt it, and cannot withhold it either"
    )
    error = meta(server.store, "sync:alpha:last_error")
    assert error is not None and "ledger" in error, error
    assert served_drift(server, eid) == "unverifiable", (
        "the WATERMARK is what recall judges at, and a faulted ledger leg does not "
        "advance it — so the verdicts the drift leg materialized at the fetched tip "
        "are not yet readable. Unverifiable, the honest unknown, never a stale tip's "
        "verdict served as if it were current"
    )

    syncer.tick()  # recovery: the range censuses cleanly and the watermark catches up
    assert meta(server.store, "sync:alpha:last_tip") == origin.origin_sha(
        "refs/heads/main"
    )
    assert served_drift(server, eid) == "fresh", (
        "app.py never moved, so the recovered read is the real verdict"
    )


def test_the_ingest_report_carries_the_touched_paths_on_both_return_sites(rig):
    """``touched_paths`` no longer feeds a second leg, but it is still the honest
    answer to "what did this range contain" on BOTH return sites of ``ingest`` — the
    normal one and the range-skipped early return."""
    origin, server, _clock, tmp_path = rig
    write(server, TEXT, ANCHOR)
    base = origin.origin_sha("refs/heads/main")
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    head = origin.origin_sha("refs/heads/main")

    service = ChangeEvidenceService(
        reader=server.store,
        appender=server.store,
        now=lambda: 424_242,
        ranges=server.store,
    )
    envelope = build_receipt(
        origin.work, base, head, tmp_path, repo_id="alpha", ref="main"
    )

    first = service.ingest(envelope, phase="post_merge", verdict="pass", signal="none")
    assert getattr(first, "touched_paths", None) is not None, (
        "IngestReport lost its touched_paths field"
    )
    assert "app.py" in first.touched_paths, first

    second = service.ingest(envelope, phase="post_merge", verdict="pass", signal="none")
    assert second.range_skipped is True, "precondition: the range ledger absorbed it"
    assert "app.py" in second.touched_paths, (
        "a range-skipped report must still state what the range contained"
    )
