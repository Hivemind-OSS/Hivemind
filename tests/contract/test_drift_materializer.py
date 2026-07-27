"""CT-6 — drift is materialized per (repo, tip, base_tip, anchor).

Given a baselined binding and a moved tip, the materializer writes ``anchor_drift``
rows for the canonical tip and any requested refs by asking git; an anchor with no
baseline has nothing to compare against and materializes NOTHING (the honest unknown,
never false-stale); a moved tip invalidates naturally (the tip is in the key); the
cache is a rebuildable Law-5 cache (wipe → repopulated), and so are the baselines
beside it. Real tmp git origins, the real store, real git plumbing.
"""

from __future__ import annotations

from tests.contract.conftest import (
    ANCHOR,
    DRIFT_ANCHOR_CHANGED,
    DRIFT_ANCHOR_MISSING,
    DRIFT_BLAST_RADIUS,
    DRIFT_FRESH,
    DRIFT_UNVERIFIABLE,
    Origin,
    drift_rows,
    git,
    make_syncer,
    register_repo,
    require_method,
    require_table,
    seed_scoped_episode,
)

STALE_VERDICTS = {DRIFT_ANCHOR_CHANGED, DRIFT_ANCHOR_MISSING, DRIFT_BLAST_RADIUS}
# ``branch_scoped`` is deliberately NOT a cache-stored verdict: it is a
# served-time ROUTING of a real materialized verdict through a memory's
# declared line, computed on read, never written by the materializer.
WIRE_VERDICTS = STALE_VERDICTS | {
    DRIFT_FRESH,
    DRIFT_UNVERIFIABLE,
}


def _armed(sync_store, tmp_path, name: str = "remote"):
    origin = Origin(tmp_path / name)
    register_repo(sync_store, "alpha", origin.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path)
    return origin, syncer


def _verdict_for(store, tip: str, anchor: str):
    for r in drift_rows(store, "alpha", tip):
        if r.get("anchor") == anchor:
            return r.get("verdict")
    return None


def test_materializes_fresh_then_stale_verdicts_at_the_tip(sync_store, tmp_path):
    origin, syncer = _armed(sync_store, tmp_path)
    seed_scoped_episode(sync_store, "greet lesson", anchors=[("alpha", ANCHOR)])
    syncer.service.tick()  # mint the fp + materialize at tip0
    tip0 = origin.origin_sha("refs/heads/main")
    v0 = _verdict_for(sync_store, tip0, ANCHOR)
    assert v0 == DRIFT_FRESH, (
        f"an intact anchor at the measured tip materializes fresh, got {v0!r}"
    )
    assert v0 in WIRE_VERDICTS, "the cache stores the §3.4 wire vocabulary"

    # remove the symbol on the canonical line → the next tick judges the new tip
    origin.commit(
        "app.py", "def farewell(name):\n    return 'bye ' + name\n", "rm greet"
    )
    origin.push()
    syncer.service.tick()
    tip1 = origin.origin_sha("refs/heads/main")
    assert tip1 != tip0
    v1 = _verdict_for(sync_store, tip1, ANCHOR)
    assert v1 == DRIFT_ANCHOR_MISSING, (
        f"a removed symbol materializes anchor_missing at the new tip, got {v1!r}"
    )


def test_signature_change_materializes_anchor_changed(sync_store, tmp_path):
    origin, syncer = _armed(sync_store, tmp_path)
    seed_scoped_episode(sync_store, "greet lesson", anchors=[("alpha", ANCHOR)])
    syncer.service.tick()  # fp minted at tip0
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    syncer.service.tick()
    tip1 = origin.origin_sha("refs/heads/main")
    v = _verdict_for(sync_store, tip1, ANCHOR)
    assert v == DRIFT_ANCHOR_CHANGED, (
        f"a changed signature materializes anchor_changed, got {v!r}"
    )


def test_an_anchor_with_no_baseline_never_reads_fresh_or_stale(sync_store, tmp_path):
    """The version gate this replaced kept an INCOMPARABLE stored token silent. The
    same law with a new subject: an anchor the server has no baseline for has nothing
    to compare against, so it materializes NOTHING and reads the honest unknown —
    never a verdict, in either direction."""
    _origin, syncer = _armed(sync_store, tmp_path)
    seed_scoped_episode(sync_store, "greet lesson", anchors=[("alpha", ANCHOR)])
    work = sync_store.anchor_work_list("alpha")
    assert work and all(base == "" for _e, _a, base in work), (
        "precondition: written before the repo had a watermark, so no baseline"
    )
    assert syncer.service._verdicts_at(tmp_path, "tip", "", [ANCHOR], 0, {}) == (
        [],
        None,
    ), "nothing to measure from means nothing measured — not a verdict"


def test_requested_ref_materializes_via_ref_requests(sync_store, tmp_path):
    origin, syncer = _armed(sync_store, tmp_path)
    seed_scoped_episode(sync_store, "greet lesson", anchors=[("alpha", ANCHOR)])
    syncer.service.tick()  # fp minted at the canonical tip
    # a feature branch that breaks the anchor, pushed to the origin
    git(origin.work, "checkout", "-q", "-b", "feature")
    origin.commit("app.py", "def other():\n    return 1\n", "break on feature")
    origin.push("feature")
    git(origin.work, "checkout", "-q", "main")
    feature_tip = origin.origin_sha("refs/heads/feature")

    touch = require_method(sync_store, "touch_ref_request")
    touch("alpha", "feature", 424_242)
    syncer.service.tick()

    v = _verdict_for(sync_store, feature_tip, ANCHOR)
    assert v is not None, (
        "a requested ref must be materialized at ITS tip on the next tick"
    )
    assert v in STALE_VERDICTS, f"the branch broke the anchor: got {v!r}"


def test_tip_move_invalidates_naturally(sync_store, tmp_path):
    origin, syncer = _armed(sync_store, tmp_path)
    seed_scoped_episode(sync_store, "greet lesson", anchors=[("alpha", ANCHOR)])
    syncer.service.tick()
    tip0 = origin.origin_sha("refs/heads/main")
    assert _verdict_for(sync_store, tip0, ANCHOR) is not None
    origin.commit(
        "app.py", 'def greet(name):\n    return "hi " + name + "!"\n', "tweak"
    )
    origin.push()
    syncer.service.tick()
    tip1 = origin.origin_sha("refs/heads/main")
    from tests.contract.conftest import meta_value

    assert meta_value(sync_store, "sync:alpha:last_tip") == tip1, (
        "the per-repo watermark tracks the moved tip (§3.5 sync:<name>:last_tip)"
    )
    assert _verdict_for(sync_store, tip1, ANCHOR) is not None, (
        "verdicts are keyed by (repo, tip) — a moved tip gets its own rows"
    )


def test_cache_is_rebuildable(sync_store, tmp_path):
    origin, syncer = _armed(sync_store, tmp_path)
    seed_scoped_episode(sync_store, "greet lesson", anchors=[("alpha", ANCHOR)])
    syncer.service.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert drift_rows(sync_store, "alpha", tip), "precondition: materialized rows"

    sync_store.conn.execute("DELETE FROM anchor_drift")
    assert not drift_rows(sync_store, "alpha", tip)
    syncer.service.tick()
    assert drift_rows(sync_store, "alpha", tip), (
        "the drift cache is a rebuildable cache (Law 5): wipe → repopulated"
    )


def test_declared_refs_materialize_without_any_recall_demand(sync_store, tmp_path):
    """The materializer's tip list covers the canonical tip, then every ref a
    LIVE episode has DECLARED, then refs recall has DEMANDED — declared
    coverage must not depend on anyone ever having recalled that branch.
    Guarded on the declared-refs store surface: passing a (repo, branch) pair
    into today's pre-declared-refs ``stage`` would silently write a garbage
    repo-name row instead of failing cleanly, so the guard runs first."""
    require_table(sync_store, "episode_refs")
    require_method(sync_store, "declared_refs")
    origin = Origin(tmp_path / "remote")
    register_repo(sync_store, "alpha", origin.url, canonical_ref="main")

    # a real branch the mirror will see on its very first fetch — never demanded
    # by any recall, and never touched by ref_requests in this test.
    git(origin.work, "checkout", "-q", "-b", "feature")
    origin.commit("app.py", "def other():\n    return 1\n", "break on feature")
    origin.push("feature")
    git(origin.work, "checkout", "-q", "main")
    feature_tip = origin.origin_sha("refs/heads/feature")

    seed_scoped_episode(
        sync_store,
        "greet lesson",
        anchors=[("alpha", ANCHOR)],
        repos=[("alpha", "feature")],
    )
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()

    v = _verdict_for(sync_store, feature_tip, ANCHOR)
    assert v is not None, (
        "a DECLARED ref must materialize even with zero recall demand for it"
    )
