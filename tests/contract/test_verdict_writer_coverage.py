"""Which advertised drift verdicts does a PRODUCTION writer actually emit?

``hive/app/tool_defs.py`` advertises the full wire enum to every agent. This drives
real ticks and records the real git argv to establish, per verdict, whether the daemon
can ever produce it — nothing is seeded.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hive.app.config import SyncConfig
from hive.app.sync import SyncService, default_run
from hive.domain.change_evidence import ChangeEvidenceService
from tests.contract.conftest import require_table
from tests.mcp._helpers import build_real_server, content, tool_call
from tests.sync.conftest import Origin

ANCHOR = "app.py::greet"


class Recorder:
    """The real spawn seam, recording every argv (never faking git or the engines)."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, env=None, timeout=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        return default_run(argv, env=env, timeout=timeout)


def syncer_for(store, tmp_path: Path, run=None, **cfg_kw):
    ev = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424_242, ranges=store
    )
    return SyncService(
        SyncConfig(mirror_dir=str(tmp_path / "mirrors"), **cfg_kw),
        store,
        ev,
        threading.Lock(),
        **({"run": run} if run else {}),
    )


def anchor_meta(store, eid):
    row = store.conn.execute(
        "SELECT fp_meta FROM episode_anchors WHERE episode_id=?", (eid,)
    ).fetchone()
    return json.loads(row["fp_meta"]) if row and row["fp_meta"] else {}


def verdicts(store):
    return {
        r["verdict"]
        for r in store.conn.execute("SELECT DISTINCT verdict FROM anchor_drift")
    }


@pytest.fixture
def rig(tmp_path):
    origin = Origin(tmp_path / "remote")
    server, _c = build_real_server(t0=1_000_000)
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    rec = Recorder()
    return origin, server, syncer_for(server.store, tmp_path, run=rec), rec


def _write(server, text, anchor=ANCHOR):
    from hive.app.mcp_server import MCPRequest, ServerIdentity

    resp = server.handle(
        MCPRequest(
            1,
            "tools/call",
            {
                "name": "hive_write",
                "arguments": {
                    "text": text,
                    "anchors": [{"repo": "alpha", "anchor": anchor}],
                },
            },
        ),
        identity=ServerIdentity("t", "agent-a"),
    )
    return json.loads(resp.result["content"][0]["text"])["id"]


def test_the_daemon_baselines_every_binding_it_will_judge(rig):
    """The radius tier used to need a SECOND minted carrier; it now needs the census
    propagation feed instead. What every tier still needs is the baseline — the commit
    a binding is measured from — so this pins that the daemon records one, from the
    watermark it just advanced, with no per-anchor subprocess."""
    origin, server, syncer, rec = rig
    eid = _write(server, "greet lesson")
    syncer.tick()
    rows = [
        dict(r)
        for r in server.store.conn.execute(
            "SELECT * FROM anchor_baselines WHERE episode_id=?", (eid,)
        )
    ]
    assert rows and rows[0]["base_tip"] == origin.origin_sha("refs/heads/main"), rows
    assert not any("hive-edge" in a for c in rec.calls for a in c), (
        f"the staleness path spawns no engine at all: {rec.calls}"
    )


def test_blast_radius_changed_is_actually_reachable(rig):
    """A dependency-neighborhood change with the anchor's own lines intact. The
    producer is the census propagation feed: ``census build --propagate`` renders the
    breaking seed's callers, the ingest writes an advisory ``stale_suspect`` row for
    the memory bound to one, and the materializer promotes that anchor's ``fresh``
    to the advisory radius tier."""
    origin, server, syncer, _rec = rig
    origin.commit(
        "app.py",
        'def helper(x):\n    return x\n\n\ndef greet(name):\n    return "hi " + helper(name)\n',
        "add helper",
    )
    origin.push()
    _write(server, "greet lesson")
    syncer.tick()
    assert "fresh" in verdicts(server.store), verdicts(server.store)

    # change ONLY the helper's signature: greet's own lines are untouched
    origin.commit(
        "app.py",
        'def helper(x, y):\n    return x\n\n\ndef greet(name):\n    return "hi " + helper(name)\n',
        "neighborhood change",
    )
    origin.push()
    syncer.tick()
    suspects = [
        dict(r)
        for r in server.store.conn.execute(
            "SELECT * FROM evidence_events WHERE kind='stale_suspect'"
        )
    ]
    assert suspects, (
        "precondition: the census must propagate the break onto greet — without the "
        "advisory signal the radius tier has no producer at all"
    )
    tip = origin.origin_sha("refs/heads/main")
    rows = {
        r["anchor"]: r["verdict"]
        for r in server.store.conn.execute(
            "SELECT anchor, verdict FROM anchor_drift WHERE tip_sha=?", (tip,)
        )
    }
    assert rows.get(ANCHOR) == "blast_radius_changed", (
        f"the advisory radius tier never reached the served verdict: {rows}"
    )


def _drift_of(server, eid, text, repos=None):
    args = {"query": text}
    if repos is not None:
        args["repos"] = repos
    env = content(tool_call(server, "hive_recall", args))
    hit = next(
        (h for h in env.get("reference_context", []) if h.get("episode_id") == eid),
        None,
    )
    assert hit is not None, f"episode {eid} not served: {env}"
    return hit["drift"]["type"]


def test_every_advertised_drift_verdict_has_a_production_writer(tmp_path):
    """The full ``hive.app.drift.WIRE_VERDICTS`` enum, each driven end to end
    through the real sync/recall stack — no member may be advertised that no
    production writer can ever emit (the same coverage-gap pattern the served
    per-repo sync health fields were already closed against, applied here to
    drift). Six members are materialized cache verdicts; ``branch_scoped`` is
    the one member computed at SERVE time — routed from a real stale verdict
    through the memory's own declared line, never written to the cache
    directly."""
    from hive.app.drift import WIRE_VERDICTS

    produced: set[str] = set()

    def _rig(subdir: str):
        origin = Origin(tmp_path / subdir)
        server, _c = build_real_server(t0=1_000_000)
        server.store.repo_add(
            name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
        )
        syncer = syncer_for(server.store, tmp_path / subdir)
        return origin, server, syncer

    # fresh: an intact anchor, one tick.
    origin, server, syncer = _rig("fresh")
    require_table(server.store, "episode_refs")  # fail fast: branch_scoped needs it
    eid = _write(server, "fresh lesson")
    syncer.tick()
    produced.add(_drift_of(server, eid, "fresh lesson"))

    # anchor_changed: a real signature change on the canonical line.
    origin, server, syncer = _rig("anchor_changed")
    eid = _write(server, "anchor changed lesson")
    syncer.tick()
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen signature",
    )
    origin.push()
    syncer.tick()
    produced.add(_drift_of(server, eid, "anchor changed lesson"))

    # anchor_missing: the symbol removed outright.
    origin, server, syncer = _rig("anchor_missing")
    eid = _write(server, "anchor missing lesson")
    syncer.tick()
    origin.commit("app.py", "def other():\n    return 1\n", "remove greet")
    origin.push()
    syncer.tick()
    produced.add(_drift_of(server, eid, "anchor missing lesson"))

    # blast_radius_changed: a dependency-neighborhood change, greet's own
    # signature untouched.
    origin, server, syncer = _rig("blast_radius")
    origin.commit(
        "app.py",
        'def helper(x):\n    return x\n\n\ndef greet(name):\n    return "hi " + helper(name)\n',
        "add helper",
    )
    origin.push()
    eid = _write(server, "blast radius lesson")
    syncer.tick()
    origin.commit(
        "app.py",
        'def helper(x, y):\n    return x\n\n\ndef greet(name):\n    return "hi " + helper(name)\n',
        "neighborhood change",
    )
    origin.push()
    syncer.tick()
    produced.add(_drift_of(server, eid, "blast radius lesson"))

    # unverifiable: a branch tip nobody has ever touched.
    origin, server, syncer = _rig("unverifiable")
    eid = _write(server, "unverifiable lesson")
    syncer.tick()
    produced.add(
        _drift_of(server, eid, "unverifiable lesson", repos=["alpha@never-touched"])
    )

    # n/a: a general memory with no anchors — drift is never even consulted.
    origin, server, syncer = _rig("na")
    env = content(
        tool_call(server, "hive_write", {"text": "a general lesson, no anchor"})
    )
    produced.add(_drift_of(server, env["id"], "a general lesson, no anchor"))

    # branch_scoped: a declared-line divergence — the memory's own line
    # ("feature") is never demanded or pushed to; the CONSUMER reads the
    # canonical line, where a real stale verdict must route through the
    # advisory branch_scoped rather than riding through raw.
    origin, server, syncer = _rig("branch_scoped")
    env = content(
        tool_call(
            server,
            "hive_write",
            {
                "text": "branch scoped lesson",
                "anchors": [{"repo": "alpha", "anchor": ANCHOR}],
                "repos": ["alpha@feature"],
            },
        )
    )
    eid = env["id"]
    syncer.tick()
    origin.commit("app.py", 'def greet(a, b):\n    return "x"\n', "canonical break")
    origin.push()
    syncer.tick()
    produced.add(_drift_of(server, eid, "branch scoped lesson", repos=["alpha"]))

    missing = set(WIRE_VERDICTS) - produced
    unexpected = produced - set(WIRE_VERDICTS)
    assert not missing and not unexpected, (
        f"every advertised drift verdict must have a production writer: "
        f"missing={missing}, unexpected={unexpected}"
    )
