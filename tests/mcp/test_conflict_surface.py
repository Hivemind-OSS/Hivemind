"""C4 — the recall-time conflict carrier: a DISTINCT ``conflicts`` envelope key (ids only,
never reference_context — Law 4), byte-inert when OFF, and fail-OPEN (a detector fault never
breaks the read). Near-dups are modeled with FakeClusterProvider (cid-tagged texts cluster);
the gate is made permissive (h=1.0) so a confident recall returns the co-present near-dups."""
from __future__ import annotations

from hive.app.config import ConflictConfig
from tests.fakes._fakes import FakeClusterProvider
from tests.mcp._helpers import build_real_server, content, tool_call, write_text

D = 64


def _srv(*, enabled=True, h=1.0):
    # h=1.0 ⇒ the entropy gate never suppresses, so two near-dup hits are served confidently
    # (a flat 2-hit distribution would otherwise abstain — that path is tested elsewhere).
    return build_real_server(d=D, h=h, embedder=FakeClusterProvider(d=D),
                             conflict=ConflictConfig(enabled=enabled) if enabled else
                             ConflictConfig())


def _write(server, text, **kw):
    return write_text(server, text, **kw)


# ── ON: opposing polarity near-dups → a contradiction in the carrier ───────────
def test_recall_surfaces_contradiction_for_opposing_near_dups():
    server, _ = _srv(enabled=True)
    a = _write(server, "cid=1 run database migrations during deploy",
               polarity="do", anchor="deploy.py")["id"]
    b = _write(server, "cid=1 never run migrations during a deploy window",
               polarity="dont", anchor="deploy.py")["id"]
    r = content(tool_call(server, "hive_recall", {"query": "cid=1 migrations on deploy"}))
    assert r["abstained"] is False
    assert "conflicts" in r
    pair = r["conflicts"][0]
    assert {pair["a_id"], pair["b_id"]} == {a, b}
    assert pair["relation"] == "contradiction" and pair["loser_hint"] is None


def test_recall_surfaces_redundancy_with_loser_hint():
    server, _ = _srv(enabled=True)
    a = _write(server, "cid=2 use connection pooling for the db", polarity="do")["id"]
    b = _write(server, "cid=2 reuse db connections via a pool", polarity="do")["id"]
    r = content(tool_call(server, "hive_recall", {"query": "cid=2 db connection reuse"}))
    pair = r["conflicts"][0]
    assert pair["relation"] == "redundancy"
    assert pair["loser_hint"] in (a, b)            # a directional retire candidate
    assert "hive_supersede" in pair["note"]         # the human resolution path is named


# ── Law 4: conflicts carries IDS ONLY, never enters reference_context ───────────
def test_conflicts_never_carry_memory_text_nor_enter_reference_context():
    server, _ = _srv(enabled=True)
    _write(server, "cid=3 prefer merge sort for large inputs", polarity="do")
    _write(server, "cid=3 large datasets sort best with merge sort", polarity="do")
    r = content(tool_call(server, "hive_recall", {"query": "cid=3 sorting large data"}))
    assert "conflicts" in r
    # every reference_context entry is a genuine HIT (text+sim+episode_id) — a leaked
    # conflict dict (which has no text/sim) sitting in reference_context REDs this.
    for hit in r["reference_context"]:
        assert {"episode_id", "text", "sim"} <= set(hit)
        assert "relation" not in hit                 # a conflict pair must never appear as a hit
    for pair in r["conflicts"]:
        assert "text" not in pair                   # NO memory content rides the carrier
        assert set(pair) >= {"a_id", "b_id", "relation", "cosine", "loser_hint", "note"}


# ── OFF: byte-inert (no conflicts key at all) ──────────────────────────────────
def test_conflicts_absent_when_disabled():
    server, _ = _srv(enabled=False)
    _write(server, "cid=4 always parameterize sql queries", polarity="do")
    _write(server, "cid=4 never concatenate user input into sql", polarity="do")
    r = content(tool_call(server, "hive_recall", {"query": "cid=4 sql injection"}))
    assert r["abstained"] is False
    assert "conflicts" not in r                      # OFF ⇒ no key (byte-inert)


def test_no_conflicts_key_when_no_near_dup_pair():
    # ON but the returned hits are NOT near-dup (different clusters) ⇒ no key (lean default)
    server, _ = _srv(enabled=True)
    _write(server, "cid=5 a fact about caching", polarity="do")
    r = content(tool_call(server, "hive_recall", {"query": "cid=5 caching"}))
    assert "conflicts" not in r                      # single hit ⇒ no pair to surface


# ── fail-OPEN: a detector fault never breaks the read ──────────────────────────
def test_detector_fault_still_returns_hits():
    server, _ = _srv(enabled=True)
    _write(server, "cid=6 first near dup memory", polarity="do")
    _write(server, "cid=6 second near dup memory", polarity="do")

    def _boom(*a, **k):
        raise RuntimeError("detector exploded")
    import hive.app.mcp_server as mod
    orig = mod.detect_conflicts
    mod.detect_conflicts = _boom
    try:
        r = content(tool_call(server, "hive_recall", {"query": "cid=6 near dup"}))
    finally:
        mod.detect_conflicts = orig
    assert r["abstained"] is False and len(r["reference_context"]) >= 1   # hits survive
    assert "conflicts" not in r                      # fault ⇒ [] ⇒ no key, no crash
