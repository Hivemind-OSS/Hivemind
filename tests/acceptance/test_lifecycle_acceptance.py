"""Trust-lifecycle acceptance — the REAL container end-to-end (build_container with
an injected deterministic embedder; no model download, runs everywhere):

  1. demand promotion e2e — misses from two identities, then a capture of the
     demanded answer, promotes mechanically and the next recall serves it LABELED
     provisional; the writer-only-demand variant never serves (anti-gaming).
  2. disabled byte-stability — autonomy off: capture refused before the store,
     recall serves exactly as the pre-lifecycle build, and the read path writes
     ZERO new rows (no misses, no exposure).
  3. boot order — decay sweep runs BEFORE the index rebuild, so a TTL-lapsed row
     can never be warmed back into serving at boot.

The injected embedder is hash-per-text (identical text ⇒ identical vector), so
"demand matching the capture" is modeled with identical strings — the geometry of
the rule (cosine ≥ demand_tau) is what is under test, not semantic quality (that
is the real-embedder acceptance suite's job)."""
from __future__ import annotations

from hive.app.config import Config
from hive.app.container import build_container
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.domain.lifecycle import PROVISIONAL, QUARANTINED
from tests.fakes._fakes import FakeClock, FakeWarmProvider
from tests.mcp._helpers import content

D = 32
DAY_S = 86_400
T0 = 10_000_000


def _build(*, enabled: bool = True, t0: int = T0):
    cfg = Config.load(db_path=":memory:", env={},
                      autonomy={"enabled": enabled, "demand_m": 3,
                                "demand_window_days": 14, "demand_tau": 0.75,
                                "competitor_tau": 0.85})
    clock = FakeClock(t0)
    c = build_container(cfg, tenant_id="acc", agent_id="acc-agent",
                        embedder=FakeWarmProvider(d=D, loaded=True), clock=clock)
    c.migrate()
    c.build_index()
    c.warm_embedder()
    return c, c.make_server(), clock


def _call(server, agent_id: str, name: str, args: dict):
    resp = server.handle(MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
                         identity=ServerIdentity("acc", agent_id))
    return content(resp)


def _count(c, table: str) -> int:
    return c.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# ── ★ the headline: demand promotes, anti-gaming holds, labels ride the hit ────
def test_acceptance_demand_promotion_e2e():
    c, server, clock = _build()
    q = "how do we rotate the foo signing key"
    # three unanswered recalls from TWO identities (A, B, A) — demand accumulates
    for agent in ("agent-A", "agent-B", "agent-A"):
        r = _call(server, agent, "hive_recall", {"query": q})
        assert r["abstained"] is True and r["reference_context"] == []
    assert _count(c, "recall_misses") == 3
    # the writer (a THIRD identity, C) captures the demanded knowledge
    cap = _call(server, "agent-C", "hive_capture", {"text": q})
    assert cap["status"] == "quarantined"
    # mechanical promotion already happened at the capture moment
    ep = c.store.get_episode(cap["id"])
    assert ep.trust == PROVISIONAL
    audit = c.conn.execute(
        "SELECT * FROM evidence_events WHERE episode_id=? AND kind='promote'",
        (cap["id"],)).fetchall()
    assert len(audit) == 1
    # the next recall SERVES it, labeled with its provenance
    served = _call(server, "agent-B", "hive_recall", {"query": q})
    assert served["abstained"] is False
    hit = served["reference_context"][0]
    assert hit["episode_id"] == cap["id"] and hit["trust"] == "provisional"


def test_acceptance_writer_only_demand_never_serves():
    c, server, clock = _build()
    q = "what is the staging db password rotation cadence"
    # all three misses come from the SAME identity that will write the answer —
    # the writer cannot manufacture both supply and demand
    for _ in range(3):
        r = _call(server, "agent-C", "hive_recall", {"query": q})
        assert r["abstained"] is True
    cap = _call(server, "agent-C", "hive_capture", {"text": q})
    assert cap["status"] == "quarantined"
    assert c.store.get_episode(cap["id"]).trust == QUARANTINED   # NOT promoted
    again = _call(server, "agent-C", "hive_recall", {"query": q})
    assert again["abstained"] is True and again["reference_context"] == []


# ── ★ disabled ⇒ byte-stable with the pre-lifecycle build ──────────────────────
def test_acceptance_disabled_byte_stable():
    c, server, clock = _build(enabled=False)
    # capture refused cleanly, before anything touches the store
    cap = _call(server, "agent-A", "hive_capture", {"text": "anything"})
    assert cap == {"status": "disabled"}
    assert _count(c, "episodes") == 0
    # the human write→recall path behaves exactly as today
    w = _call(server, "agent-A", "hive_write",
              {"text": "the canonical fact", "approved_by": "human"})
    assert w["status"] == "approved"
    hit = _call(server, "agent-B", "hive_recall", {"query": "the canonical fact"})
    assert hit["abstained"] is False
    assert hit["reference_context"][0]["trust"] == "established"  # labels: additive only
    # an unanswered recall leaves NO trace: the read path writes zero new rows
    _call(server, "agent-B", "hive_recall", {"query": "an unanswered question"})
    assert _count(c, "recall_misses") == 0
    assert _count(c, "exposure") == 0
    assert c.store.trust_counts()[QUARANTINED] == 0


# ── boot order: sweep BEFORE index rebuild ─────────────────────────────────────
def test_boot_sweep_runs_before_index_build():
    c, server, clock = _build()
    # seed a provisional row whose TTL has LONG lapsed (promoted in the distant past)
    cap = _call(server, "agent-A", "hive_capture", {"text": "stale promoted insight"})
    past = T0 - (c.cfg.autonomy.provisional_ttl_days + 5) * DAY_S
    assert c.store.set_trust(cap["id"], PROVISIONAL, now=past) is True
    # order proof: the sweep call lands before the rebuild call
    events: list[str] = []
    orig_sweep, orig_rebuild = c.store.sweep_decayed, c.store.rebuild_index_from_store
    c.store.sweep_decayed = lambda **kw: (events.append("sweep"), orig_sweep(**kw))[1]
    c.store.rebuild_index_from_store = \
        lambda **kw: (events.append("rebuild"), orig_rebuild(**kw))[1]
    c.build_index()
    assert events == ["sweep", "rebuild"]
    # and the effect: the lapsed row was materialized dead BEFORE warming, so the
    # boot index can never serve it
    assert c.store.get_episode(cap["id"]).trust == "deprecated"
    r = _call(server, "agent-B", "hive_recall", {"query": "stale promoted insight"})
    assert r["abstained"] is True


def test_acceptance_supersession_through_tools():
    c, server, clock = _build()
    old = _call(server, "agent-A", "hive_write",
                {"text": "deploy with make deploy", "approved_by": "human"})
    new = _call(server, "agent-A", "hive_write",
                {"text": "deploy with ship.sh since the migration",
                 "approved_by": "human", "replaces": old["id"]})
    assert new["superseded"] == old["id"]
    # the retired version is out of serving; the correction serves established
    r = _call(server, "agent-B", "hive_recall",
              {"query": "deploy with ship.sh since the migration"})
    assert r["reference_context"][0]["episode_id"] == new["id"]
    assert r["reference_context"][0]["trust"] == "established"
