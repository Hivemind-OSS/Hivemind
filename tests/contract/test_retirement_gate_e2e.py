"""The machine-gated retirement gate, driven through the real MCP handler.
Nothing is seeded — every qualifying signal is MANUFACTURED BY PRODUCTION CODE:
  - drift:anchor_missing  <- a real push that deletes the symbol + a real sync tick
  - outcome_hurt(other)   <- a real hive_outcome call from a second identity
No fixture is hand-written into evidence_events or anchor_drift.

Git is the ONE staleness oracle: the census verification channel that used to write a
second, ledger-shaped answer is retired, so the only drift signal here is the one the
sync tick materialized.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hive.app.config import ConflictConfig, SyncConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.sync import SyncService
from hive.domain.change_evidence import ChangeEvidenceService
from tests.mcp._helpers import build_real_server
from tests.sync.conftest import Origin

ANCHOR = "app.py::greet"
TEXT = "greet() must stay single-arg; callers pass positionally"


def call(server, name, args, *, agent="agent-a"):
    resp = server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("t", agent),
    )
    return json.loads(resp.result["content"][0]["text"]), bool(
        resp.result.get("isError")
    )


def make_syncer(store, tmp_path: Path):
    cfg = SyncConfig(mirror_dir=str(tmp_path / "mirrors"))
    evidence = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424_242, ranges=store
    )
    return SyncService(cfg, store, evidence, threading.Lock())


@pytest.fixture
def rig(tmp_path):
    origin = Origin(tmp_path / "remote")
    server, clock = build_real_server(t0=1_000_000, conflict=ConflictConfig())
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    return origin, server, clock, make_syncer(server.store, tmp_path)


def _write(server, text=TEXT, anchored=True, agent="agent-a"):
    args = {"text": text}
    if anchored:
        args["anchors"] = [{"repo": "alpha", "anchor": ANCHOR}]
    env, err = call(server, "hive_write", args, agent=agent)
    assert env["status"] == "approved", env
    return env["id"]


def trust(server, eid):
    return server.store.get_episode(eid).trust


def test_healthy_memory_is_a_benign_noop_then_real_drift_qualifies(rig):
    origin, server, clock, syncer = rig
    eid = _write(server)
    syncer.tick()  # baseline + mint + materialize `fresh`

    env, err = call(server, "hive_prune", {"episode_id": eid})
    assert err is False, "an unqualified prune must never be an error"
    assert env["status"] == "noop"
    assert env["reason"] == "no qualifying machine signal — nothing retired"
    assert trust(server, eid) == "provisional"

    # PRODUCTION manufactures the signal: delete the symbol, push, tick
    origin.commit("app.py", "def other():\n    return 1\n", "remove greet")
    origin.push()
    syncer.tick()

    env, err = call(server, "hive_prune", {"episode_id": eid})
    assert err is False
    assert env["status"] == "pruned", env
    assert "drift:anchor_missing" in env["signals"], env
    assert trust(server, eid) == "deprecated"


def test_self_reported_hurt_cannot_self_authorize_but_another_identity_can(rig):
    origin, server, clock, syncer = rig
    eid = _write(server)
    syncer.tick()

    # same identity reports hurt then prunes -> structurally blocked
    env, _ = call(server, "hive_outcome", {"hurt": [eid]}, agent="agent-a")
    assert env["status"] == "recorded" and env["hurt"] == [eid]
    env, err = call(server, "hive_prune", {"episode_id": eid}, agent="agent-a")
    assert err is False
    assert env["status"] == "noop", (
        "a hive_outcome(hurt)+hive_prune two-call self-destruction must be blocked"
    )
    assert trust(server, eid) == "provisional"

    # a DIFFERENT identity's hurt qualifies it
    call(server, "hive_outcome", {"hurt": [eid]}, agent="agent-b")
    env, err = call(server, "hive_prune", {"episode_id": eid}, agent="agent-a")
    assert env["status"] == "pruned", env
    assert "outcome_hurt_other_identity" in env["signals"], env
    assert trust(server, eid) == "deprecated"


def test_flag_is_advisory_and_never_retires(rig):
    origin, server, clock, syncer = rig
    a = _write(server, TEXT)
    b = _write(server, "greet() may take a punctuation arg")
    syncer.tick()

    env, err = call(
        server,
        "hive_flag",
        {"a": a, "b": b, "kind": "conflict", "resolution": "b wins"},
    )
    assert err is False, env
    assert trust(server, a) == "provisional" and trust(server, b) == "provisional"

    env, err = call(server, "hive_prune", {"episode_id": a})
    assert env["status"] == "noop", (
        f"an advisory hive_flag must never qualify the retirement gate: {env}"
    )
    assert trust(server, a) == "provisional"


def test_supersede_on_a_healthy_target_noops_and_write_replaces_rider_reports_it(rig):
    origin, server, clock, syncer = rig
    loser = _write(server)
    winner = _write(server, "greet() takes exactly one positional arg")
    syncer.tick()

    env, err = call(server, "hive_supersede", {"loser": loser, "winner": winner})
    assert err is False
    assert env["status"] == "noop" and env["reason"].startswith(
        "no qualifying machine signal"
    )
    assert trust(server, loser) == "provisional"

    env, err = call(
        server,
        "hive_write",
        {"text": "a replacement lesson about greet", "replaces": loser},
    )
    assert env["status"] == "approved"
    assert env["superseded"] is None
    assert env["supersede_noop"] == "no qualifying machine signal"
    assert trust(server, loser) == "provisional"

    # now make it real
    origin.commit("app.py", "def other():\n    return 1\n", "remove greet")
    origin.push()
    syncer.tick()
    env, err = call(
        server,
        "hive_write",
        {"text": "a second replacement lesson about greet", "replaces": loser},
    )
    assert env["superseded"] == loser, env
    assert "supersede_noop" not in env
    assert trust(server, loser) == "deprecated"


def test_a_branch_scoped_memory_is_judged_at_its_own_line(rig):
    """The gate must resolve the tip per anchor repo as the episode's OWN
    declared ref when it declares one, never the canonical watermark for a
    memory that named a different line — so a memory dead on its declared line
    qualifies for retirement even while the canonical line stays untouched
    (the declared ref must never become caller-asserted immunity)."""
    origin, server, clock, syncer = rig
    env, err = call(
        server,
        "hive_write",
        {
            "text": TEXT,
            "anchors": [{"repo": "alpha", "anchor": ANCHOR}],
            "repos": ["alpha@feature"],
        },
    )
    assert env["status"] == "approved", env
    eid = env["id"]
    syncer.tick()  # canonical baseline + mint + materialize `fresh` — stays healthy

    # demand the declared line so the materializer covers ITS tip, then really
    # break the anchor there while the canonical line is never touched again
    call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@feature"]})
    origin.commit("app.py", "def other():\n    return 1\n", "remove greet on feature")
    origin.push("HEAD:refs/heads/feature")
    syncer.tick()

    env, err = call(server, "hive_prune", {"episode_id": eid})
    assert err is False, "an unqualified prune must never be an error"
    assert env["status"] == "pruned", (
        "a memory dead on its OWN declared line must qualify for retirement even "
        f"while the canonical line is untouched: {env}"
    )
    assert "drift:anchor_missing" in env["signals"], env
    assert trust(server, eid) == "deprecated"


def test_an_anchorless_memory_never_qualifies_on_a_census_row(rig, tmp_path):
    """The census join reads ``anchored_episodes()``, which filters ``anchor != ''``,
    so no production writer can put a census evidence row on an anchor-less memory —
    and with the conflict-pair feed gone, such a memory has NO single-caller
    retirement route at all (residual R4, stated as a test)."""
    origin, server, clock, syncer = rig
    general = _write(server, "prefer small diffs", anchored=False)
    env, _err = call(
        server,
        "hive_write",
        {"text": "alpha ships on fridays", "repos": ["alpha"]},
    )
    scope_only = env["id"]
    syncer.tick()

    origin.commit("app.py", "def other():\n    return 1\n", "remove greet")
    origin.push()
    syncer.tick()  # a real receipt over a real change

    for eid in (general, scope_only):
        rows = [
            r["kind"]
            for r in server.store.conn.execute(
                "SELECT kind FROM evidence_events WHERE episode_id=?", (eid,)
            )
        ]
        assert rows == [], f"an anchor-less memory acquired census evidence: {rows}"
        env, err = call(server, "hive_prune", {"episode_id": eid})
        assert err is False
        assert env["status"] == "noop", env
        assert trust(server, eid) == "provisional"


def test_the_remediation_rider_never_fires_on_a_line_the_memory_never_claimed(rig):
    """BUG-070's rule, re-homed. The ``own_refs`` machinery that used to enforce it
    by hand is gone; the rule now holds STRUCTURALLY because ``attach_drift`` routes
    a stale-tier verdict to the advisory ``branch_scoped`` for an off-line consumer,
    and ``branch_scoped`` is deliberately not in ``QUALIFYING_DRIFT`` — the one set
    the rider reads."""
    origin, server, clock, syncer = rig
    origin.push("HEAD:refs/heads/feature")
    declared = call(
        server,
        "hive_write",
        {
            "text": TEXT,
            "anchors": [{"repo": "alpha", "anchor": ANCHOR}],
            "repos": ["alpha@feature"],
        },
    )[0]["id"]
    control_text = "greet() is the single greeting entry point"
    control = _write(server, control_text)
    syncer.tick()

    origin.commit("app.py", "def other():\n    return 1\n", "remove greet on main")
    origin.push()
    syncer.tick()

    env, _err = call(server, "hive_recall", {"query": control_text})
    hit = next(h for h in env["reference_context"] if h["episode_id"] == control)
    assert hit["drift"]["type"] == "anchor_missing", hit["drift"]
    assert hit.get("remediation"), (
        f"a memory that declared NO line keeps being judged on the canonical line: {hit}"
    )

    env, _err = call(server, "hive_recall", {"query": TEXT, "repos": ["alpha@main"]})
    hit = next(h for h in env["reference_context"] if h["episode_id"] == declared)
    assert hit["drift"]["type"] == "branch_scoped", hit["drift"]
    assert "remediation" not in hit, (
        f"the rider fired on a line the memory never declared: {hit}"
    )
    assert "last_verified" not in hit, hit
