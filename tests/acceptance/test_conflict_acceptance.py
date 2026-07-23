"""C10 — conflict surfacing end-to-end with the REAL Qwen3 embedder (gated `embed` tier).

Proves the whole loop on genuine paraphrase similarity (not hash vectors): two near-dup
SERVABLE memories surface as a contradiction (opposing polarity) or a redundancy (+ loser
hint) in BOTH the recall carrier and the hive_health worklist; an advisory hive_flag rides
the worklist; and a human hive_supersede makes the pair vanish from every surface (the loser
deprecates → non-servable → the servable-both filter auto-clears any flag touching it).

Empirically measured real cosines (this model, prior d=768 geometry): container root/non-root
≈ 0.860, pooling paraphrase ≈ 0.858, off-topic ≈ 0.24. At the now-native 1024 dim the absolute
values shift, but the GAP (strong near-dups vs off-topic) stays wide, so conflict.tau=0.80 still
sits comfortably between — detection is deterministic across minor model jitter. (The DEFAULT
tau=0.85 is stricter; its calibration is the benchmark's job.)
A permissive tau_serve=0.01 makes recall serve the co-present near-dups confidently (they
clear the absolute-relevance floor — the gate is proven elsewhere)."""
from __future__ import annotations

import pytest

from hive.app.mcp_server import MCPRequest, ServerIdentity
from tests.acceptance.conftest import build_acc
from tests.fakes._fakes import FakeClock
from tests.mcp._helpers import content

pytestmark = pytest.mark.embed   # loads the real Qwen3 model (gated heavy tier)

_CONF = {"enabled": True, "tau": 0.80}
_PERMISSIVE = {"tau_serve": 0.01}


def _call(server, name, args, agent="agent-A"):
    return content(server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("acc", agent)))


def _write(server, text, **kw):
    return _call(server, "hive_write", {"text": text, **kw})


def _mech(snap):
    return [c for c in snap.get("conflicts", []) if c["source"] == "mechanical"]


def _adv(snap):
    return [c for c in snap.get("conflicts", []) if c["source"] == "advisory"]


# ── contradiction: opposing-polarity near-dups, surfaced then resolved ─────────
def test_e2e_contradiction_surfaced_in_both_surfaces_then_resolved(embedder_v1):
    c = build_acc(embedder_v1, recall=_PERMISSIVE, conflict=_CONF)
    server = c.make_server()
    do = _write(server, "Always run the container as a non-root user.",
                polarity="do")["id"]
    dont = _write(server, "Run the container as root for simplicity.",
                  polarity="dont")["id"]
    c.build_index()

    # (1) the health worklist surfaces the contradiction (gate-independent scan)
    snap = _call(server, "hive_health", {"include_conflicts": True})
    assert any({m["a_id"], m["b_id"]} == {do, dont} and m["relation"] == "contradiction"
               for m in _mech(snap))

    # (2) the recall carrier surfaces it too (both co-served under the permissive gate)
    r = _call(server, "hive_recall", {"query": "should the container run as root or not"})
    assert r["abstained"] is False
    assert any({p["a_id"], p["b_id"]} == {do, dont} and p["relation"] == "contradiction"
               for p in r.get("conflicts", []))

    # (3) resolve it: keep the 'do', retire the 'dont' — the near-dup contradiction
    # is itself the qualifying machine signal (no approver exists in v3)
    out = _call(server, "hive_supersede", {"loser": dont, "winner": do})
    assert out["status"] == "superseded"
    c.build_index()

    # (4) the pair has vanished from EVERY surface (loser deprecated → non-servable)
    snap2 = _call(server, "hive_health", {"include_conflicts": True})
    assert not any({m["a_id"], m["b_id"]} == {do, dont} for m in _mech(snap2))
    r2 = _call(server, "hive_recall", {"query": "should the container run as root or not"})
    served = {h["episode_id"] for h in r2["reference_context"]}
    assert dont not in served and "conflicts" not in r2


# ── redundancy: compatible near-dups → a directional loser hint (older loses) ──
def test_e2e_redundancy_loser_hint_points_at_older(embedder_v1):
    clock = FakeClock(1_000_000)
    c = build_acc(embedder_v1, clock=clock, recall=_PERMISSIVE, conflict=_CONF)
    server = c.make_server()
    older = _write(server, "Use connection pooling for the database; never open a "
                           "connection per request.")["id"]
    clock.advance(10_000)                       # the second write is newer
    newer = _write(server, "Reuse database connections through a pool instead of opening "
                           "one each request.")["id"]
    c.build_index()
    snap = _call(server, "hive_health", {"include_conflicts": True})
    red = [m for m in _mech(snap)
           if {m["a_id"], m["b_id"]} == {older, newer} and m["relation"] == "redundancy"]
    assert red, "expected a redundancy between the two pooling paraphrases"
    # same trust (both established) ⇒ ts breaks the tie ⇒ the OLDER row is the loser
    assert red[0]["loser_hint"] == older


# ── advisory flag rides the worklist, then auto-clears on resolution ───────────
def test_e2e_advisory_flag_surfaces_then_autoclears(embedder_v1):
    c = build_acc(embedder_v1, recall=_PERMISSIVE, conflict=_CONF)
    server = c.make_server()
    # two semantically DISTINCT memories (no mechanical near-dup) an agent judges to conflict
    a = _write(server, "Cache eviction policy should protect queue keys.")["id"]
    b = _write(server, "Deploys must run database migrations in a separate step.")["id"]
    c.build_index()
    flagged = _call(server, "hive_flag",
                    {"a": a, "b": b, "kind": "supersedes", "winner": b,
                     "resolution": "the migration rule is the current one"})
    assert flagged["status"] == "flagged"
    snap = _call(server, "hive_health", {"include_conflicts": True})
    assert any({x["a_id"], x["b_id"]} == {a, b} for x in _adv(snap))
    # resolving (retiring a) auto-clears the advisory via the servable-both filter.
    # The flag itself NEVER qualifies the machine gate — plant real hurt evidence
    # from ANOTHER identity so the supersede qualifies (§3.2 clause 2).
    _call(server, "hive_outcome", {"hurt": [a]}, agent="agent-B")
    _call(server, "hive_supersede", {"loser": a, "winner": b})
    c.build_index()
    snap2 = _call(server, "hive_health", {"include_conflicts": True})
    assert _adv(snap2) == []
