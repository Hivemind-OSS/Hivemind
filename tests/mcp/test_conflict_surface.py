"""C4 — the recall-time conflict carrier: a DISTINCT ``conflicts`` envelope key (ids only,
never reference_context — Law 4), byte-inert when OFF, and fail-OPEN (a detector fault never
breaks the read). Near-dups are modeled with FakeClusterProvider (cid-tagged texts cluster);
a cid-matched query embeds at cosine≈1 to both, clearing the absolute-relevance floor so a
confident recall returns the co-present near-dups. Plus C5: the hive_health worklist, v3 —
mechanical entries bucket by (repo, anchor) BINDING and carry both."""

from __future__ import annotations

from hive.app.config import ConflictConfig
from tests.fakes._fakes import FakeClusterProvider
from tests.mcp._helpers import (
    build_real_server,
    content,
    register_repo,
    tool_call,
    write_text,
)

D = 64


def _srv(*, enabled=True, tau_serve=0.70):
    # a cid-matched query embeds at cosine≈1 to both near-dups, so they clear the
    # absolute-relevance floor and are served confidently (the near-dup pair the conflict
    # carrier/worklist classify); a non-matching query stays absolutely weak and abstains. The
    # carrier detects over the PRE-select resolved field, so it surfaces the pair even when
    # select_served decorrelates the served set (drops one near-dup twin).
    server, clock = build_real_server(
        d=D,
        tau_serve=tau_serve,
        embedder=FakeClusterProvider(d=D),
        conflict=ConflictConfig(enabled=enabled),
    )
    register_repo(server, "alpha")
    register_repo(server, "beta")
    return server, clock


def _write(server, text, **kw):
    return write_text(server, text, **kw)


def _anchored(repo, anchor):
    return [{"repo": repo, "anchor": anchor}]


# ── ON: opposing polarity near-dups → a contradiction in the carrier ───────────
def test_recall_surfaces_contradiction_for_opposing_near_dups():
    server, _ = _srv(enabled=True)
    a = _write(
        server,
        "cid=1 run database migrations during deploy",
        polarity="do",
        anchors=_anchored("alpha", "deploy.py"),
    )["id"]
    b = _write(
        server,
        "cid=1 never run migrations during a deploy window",
        polarity="dont",
        anchors=_anchored("alpha", "deploy.py"),
    )["id"]
    r = content(
        tool_call(server, "hive_recall", {"query": "cid=1 migrations on deploy"})
    )
    assert r["abstained"] is False
    assert "conflicts" in r
    pair = r["conflicts"][0]
    assert {pair["a_id"], pair["b_id"]} == {a, b}
    assert pair["relation"] == "contradiction" and pair["loser_hint"] is None


def test_recall_surfaces_redundancy_with_loser_hint():
    server, _ = _srv(enabled=True)
    a = _write(server, "cid=2 use connection pooling for the db", polarity="do")["id"]
    b = _write(server, "cid=2 reuse db connections via a pool", polarity="do")["id"]
    r = content(
        tool_call(server, "hive_recall", {"query": "cid=2 db connection reuse"})
    )
    pair = r["conflicts"][0]
    assert pair["relation"] == "redundancy"
    assert pair["loser_hint"] in (a, b)  # a directional retire candidate
    assert "hive_supersede" in pair["note"]  # the resolution path is named


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
        assert "relation" not in hit  # a conflict pair must never appear as a hit
    for pair in r["conflicts"]:
        assert "text" not in pair  # NO memory content rides the carrier
        assert set(pair) >= {"a_id", "b_id", "relation", "cosine", "loser_hint", "note"}


# ── OFF: byte-inert (no conflicts key at all) ──────────────────────────────────
def test_conflicts_absent_when_disabled():
    server, _ = _srv(enabled=False)
    _write(server, "cid=4 always parameterize sql queries", polarity="do")
    _write(server, "cid=4 never concatenate user input into sql", polarity="do")
    r = content(tool_call(server, "hive_recall", {"query": "cid=4 sql injection"}))
    assert r["abstained"] is False
    assert "conflicts" not in r  # OFF ⇒ no key (byte-inert)


def test_no_conflicts_key_when_no_near_dup_pair():
    # ON but the returned hits are NOT near-dup (different clusters) ⇒ no key (lean default)
    server, _ = _srv(enabled=True)
    _write(server, "cid=5 a fact about caching", polarity="do")
    r = content(tool_call(server, "hive_recall", {"query": "cid=5 caching"}))
    assert "conflicts" not in r  # single hit ⇒ no pair to surface


# ── a detector fault fails the read CLOSED (select_served owns it; no fail-open carrier) ──
def test_detector_fault_fails_read_closed():
    # select_served and the carrier share detect_conflicts over the SAME resolved field;
    # select_served runs first and fail-closes the whole read on a detector fault (the conflict
    # carrier therefore needs no separate fail-open guard). Pinned at the pipeline level too
    # (test_recall_pipeline::test_detect_conflicts_fault_fails_the_read_closed).
    server, _ = _srv(enabled=True)
    _write(server, "cid=6 first near dup memory", polarity="do")
    _write(server, "cid=6 second near dup memory", polarity="do")

    def _boom(*a, **k):
        raise RuntimeError("detector exploded")

    import hive.domain.recall as mod

    orig = mod.detect_conflicts
    mod.detect_conflicts = _boom
    try:
        r = content(tool_call(server, "hive_recall", {"query": "cid=6 near dup"}))
    finally:
        mod.detect_conflicts = orig
    assert r["abstained"] is True  # fail-closed: no un-vetted set served
    assert r["reference_context"] == [] and "conflicts" not in r


# ── C5: the health conflict worklist (include_conflicts, v3 buckets) ───────────
def test_health_report_surfaces_mechanical_redundancy_with_repo_and_anchor():
    server, _ = _srv(enabled=True)
    a = _write(
        server,
        "cid=7 cache evictions hurt the queue",
        anchors=_anchored("alpha", "redis.conf"),
    )["id"]
    b = _write(
        server,
        "cid=7 queue keys must not be evicted",
        anchors=_anchored("alpha", "redis.conf"),
    )["id"]
    snap = content(tool_call(server, "hive_health", {"include_conflicts": True}))
    assert "conflicts" in snap
    mech = [c for c in snap["conflicts"] if c["source"] == "mechanical"]
    entry = next(c for c in mech if {c["a_id"], c["b_id"]} == {a, b})
    # v3: every mechanical entry carries its (repo, anchor) bucket
    assert entry["repo"] == "alpha" and entry["anchor"] == "redis.conf"


def test_health_report_buckets_by_repo_and_anchor():
    # the offline report compares WITHIN a (repo, anchor) BINDING bucket: the same
    # cluster under DIFFERENT anchors — or the SAME anchor split across two repos —
    # is NOT a mechanical conflict here (it still surfaces at recall time).
    server, _ = _srv(enabled=True)
    _write(server, "cid=8 first about deploys", anchors=_anchored("alpha", "deploy.py"))
    _write(server, "cid=8 second about deploys", anchors=_anchored("alpha", "ci.yml"))
    _write(
        server,
        "cid=22 do vendor the schema file",
        polarity="do",
        anchors=_anchored("alpha", "pkg/schema.py::load"),
    )
    _write(
        server,
        "cid=22 dont vendor the schema file",
        polarity="dont",
        anchors=_anchored("beta", "pkg/schema.py::load"),
    )
    snap = content(tool_call(server, "hive_health", {"include_conflicts": True}))
    mech = [c for c in snap.get("conflicts", []) if c["source"] == "mechanical"]
    assert mech == [], (
        "different anchors / different repos ⇒ different buckets ⇒ no entry"
    )


def test_health_unanchored_near_dups_share_the_general_bucket():
    server, _ = _srv(enabled=True)
    a = _write(server, "cid=16 general near dup one")["id"]
    b = _write(server, "cid=16 general near dup two")["id"]
    snap = content(tool_call(server, "hive_health", {"include_conflicts": True}))
    mech = [c for c in snap["conflicts"] if c["source"] == "mechanical"]
    entry = next(c for c in mech if {c["a_id"], c["b_id"]} == {a, b})
    assert entry["repo"] == "" and entry["anchor"] == ""


def test_health_conflicts_absent_unless_requested():
    server, _ = _srv(enabled=True)
    _write(server, "cid=9 a memory", anchors=_anchored("alpha", "x.py"))
    _write(server, "cid=9 another memory", anchors=_anchored("alpha", "x.py"))
    snap = content(tool_call(server, "hive_health", {}))  # no include_conflicts
    assert "conflicts" not in snap  # ← drop the gate ⇒ this REDs


def test_health_conflicts_absent_when_disabled():
    server, _ = _srv(enabled=False)
    _write(server, "cid=10 a memory", anchors=_anchored("alpha", "x.py"))
    _write(server, "cid=10 another memory", anchors=_anchored("alpha", "x.py"))
    snap = content(tool_call(server, "hive_health", {"include_conflicts": True}))
    assert "conflicts" not in snap  # OFF ⇒ byte-inert


def test_health_report_merges_open_advisory_flag():
    server, _ = _srv(enabled=True)
    # two DISTINCT-cluster memories (no mechanical near-dup) flagged advisory by an agent
    a = _write(
        server, "cid=11 deploy uses make", anchors=_anchored("alpha", "deploy.py")
    )["id"]
    b = _write(
        server, "cid=12 deploy uses ship.sh", anchors=_anchored("alpha", "release.md")
    )["id"]
    lo, hi = sorted((a, b))
    server.store.record_conflict_flag(
        kind="conflict",
        a_id=lo,
        b_id=hi,
        winner_id=None,
        resolution="agent thinks these disagree",
        proposed_by="agent-A",
        ts=1,
    )
    snap = content(tool_call(server, "hive_health", {"include_conflicts": True}))
    adv = [c for c in snap["conflicts"] if c["source"] == "advisory"]
    assert any(
        {c["a_id"], c["b_id"]} == {a, b} and c["kind"] == "conflict" for c in adv
    )


def test_advisory_flag_autoclears_when_episode_retired():
    server, clock = _srv(enabled=True)
    a = _write(server, "cid=13 alpha", anchors=_anchored("alpha", "a.py"))["id"]
    b = _write(server, "cid=14 beta", anchors=_anchored("alpha", "b.py"))["id"]
    lo, hi = sorted((a, b))
    server.store.record_conflict_flag(
        kind="supersedes",
        a_id=lo,
        b_id=hi,
        winner_id=hi,
        resolution="",
        proposed_by="agent-A",
        ts=1,
    )
    # retire one side → the flag must drop off the worklist (read-time servable-both
    # filter). Store-level retire: the read-side auto-clear is what is under test.
    assert server.store.supersede(a, b, actor="test", ts=int(clock.now()))
    snap = content(tool_call(server, "hive_health", {"include_conflicts": True}))
    adv = [c for c in snap.get("conflicts", []) if c["source"] == "advisory"]
    assert adv == []  # auto-cleared (no status flip needed)


def test_health_report_fail_open_returns_empty_conflicts():
    server, _ = _srv(enabled=True)
    _write(server, "cid=15 a", anchors=_anchored("alpha", "z.py"))
    _write(server, "cid=15 b", anchors=_anchored("alpha", "z.py"))

    def _boom(*a, **k):
        raise RuntimeError("scan exploded")

    server.store.scan_servable_labeled = _boom
    snap = content(tool_call(server, "hive_health", {"include_conflicts": True}))
    assert snap["ok"] is True and snap["conflicts"] == []  # degrades, health still ok
