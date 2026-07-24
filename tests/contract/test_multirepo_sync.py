"""CT-9 — N-repo sync feeds per-repo canonical evidence (plan §4 intent 9, §3.6,
§6 step 5).

Two registered repos each get their own mirror (``<mirror_dir>/<name>``),
watermark (``sync:<name>:last_tip``), receipt build and ingest — rows repo-keyed;
one repo's fault never blocks the other; verified riders now land from the
canonical POST_MERGE ingest under a full stamp (the ``phase=='pre_merge'`` gate
is gone); the §6.2.5 canary rule stays intact on tags; the PR-candidate machinery
is GONE; an empty registry is an INERT tick (no git, no engine spawn).
"""

from __future__ import annotations

import json

from tests.contract.conftest import (
    ANCHOR,
    Origin,
    RecordingRun,
    make_syncer,
    meta_value,
    mirror_is_git,
    register_repo,
    seed_scoped_episode,
)


def _two_origins(tmp_path):
    a = Origin(tmp_path / "remote-a")
    b = Origin(tmp_path / "remote-b")
    return a, b


def _evidence(store, kind: str) -> list[dict]:
    return [
        dict(r)
        for r in store.conn.execute(
            "SELECT episode_id, kind, payload FROM evidence_events WHERE kind=? "
            "ORDER BY id",
            (kind,),
        )
    ]


def test_two_repo_tick_isolation_and_watermarks(sync_store, tmp_path):
    a, b = _two_origins(tmp_path)
    register_repo(sync_store, "alpha", a.url, canonical_ref="main")
    register_repo(sync_store, "beta", b.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()

    assert mirror_is_git(syncer.mirror_base, "alpha"), (
        "each registered repo gets its own mirror at <mirror_dir>/<name>"
    )
    assert mirror_is_git(syncer.mirror_base, "beta")
    assert meta_value(sync_store, "sync:alpha:last_tip") == a.origin_sha(
        "refs/heads/main"
    )
    assert meta_value(sync_store, "sync:beta:last_tip") == b.origin_sha(
        "refs/heads/main"
    )
    # FIRST CONNECT baselines at the remote tip — no historical receipt
    assert _evidence(sync_store, "change_outcome") == []


def test_per_repo_watermark_advances_independently(sync_store, tmp_path):
    a, b = _two_origins(tmp_path)
    register_repo(sync_store, "alpha", a.url, canonical_ref="main")
    register_repo(sync_store, "beta", b.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()
    beta_tip0 = meta_value(sync_store, "sync:beta:last_tip")

    a.commit("app.py", 'def greet(name):\n    return "hi " + name + "?"\n', "move a")
    a.push()
    syncer.service.tick()

    assert meta_value(sync_store, "sync:alpha:last_tip") == a.origin_sha(
        "refs/heads/main"
    ), "alpha's watermark advances with alpha's line"
    assert meta_value(sync_store, "sync:beta:last_tip") == beta_tip0, (
        "beta's watermark is untouched by alpha's movement"
    )


def test_one_repo_fault_never_blocks_the_other(sync_store, tmp_path):
    _a, b = _two_origins(tmp_path)
    register_repo(
        sync_store, "alpha", str(tmp_path / "no-such-remote.git"), canonical_ref="main"
    )
    register_repo(sync_store, "beta", b.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()  # must not raise

    assert meta_value(sync_store, "sync:beta:last_tip") == b.origin_sha(
        "refs/heads/main"
    ), "the healthy repo still syncs"
    err = meta_value(sync_store, "sync:alpha:last_error")
    assert err, "the broken repo surfaces its fault under ITS error key"


def test_force_push_discontinuity_is_per_repo(sync_store, tmp_path):
    a, b = _two_origins(tmp_path)
    register_repo(sync_store, "alpha", a.url, canonical_ref="main")
    register_repo(sync_store, "beta", b.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()
    beta_tip0 = meta_value(sync_store, "sync:beta:last_tip")

    a.commit("app.py", "def greet(name):\n    return name\n", "will be rewritten")
    a.push()
    syncer.service.tick()
    from tests.contract.conftest import git

    git(a.work, "commit", "--amend", "-qm", "rewritten line")
    a.push(force=True)
    syncer.service.tick()  # the discontinuity must not crash the tick

    assert meta_value(sync_store, "sync:alpha:last_tip") == a.origin_sha(
        "refs/heads/main"
    ), "after a force-push the watermark resets to the rewritten tip"
    assert meta_value(sync_store, "sync:beta:last_tip") == beta_tip0


def test_riders_land_from_canonical_post_merge_ingest(sync_store, tmp_path):
    a, _b = (Origin(tmp_path / "remote-a"), None)
    register_repo(sync_store, "alpha", a.url, canonical_ref="main")
    eid = seed_scoped_episode(
        sync_store, "greet trims its input", anchors=[("alpha", ANCHOR)]
    )
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()  # baseline (no receipt) + mint

    # a canonical-line change that REMOVES the anchored symbol
    a.commit("app.py", "def farewell(name):\n    return 'bye'\n", "remove greet")
    a.push()
    syncer.service.tick()

    outcomes = _evidence(sync_store, "change_outcome")
    assert outcomes, "the canonical movement lands its change_outcome rows"
    mine = [r for r in outcomes if r["episode_id"] == eid]
    assert mine, f"the alpha-anchored episode joins alpha's receipt: {outcomes}"
    body = json.loads(mine[-1]["payload"])
    assert body.get("phase") == "post_merge"
    assert body.get("repo"), "the payload is repo-keyed"
    # nothing decided in this scratch repo ⇒ landed-line parity verdict
    assert body.get("verdict") == "pass"
    # §6.2.5 canary rule preserved: no canary/randomized signal ⇒ never machine-checked
    assert body.get("tag") != "machine-checked"

    stale = [r for r in _evidence(sync_store, "verify_stale") if r["episode_id"] == eid]
    assert stale, (
        "the verify rider now lands from the canonical POST_MERGE ingest "
        "(the phase=='pre_merge' rider gate is the named mutation)"
    )
    rider = json.loads(stale[-1]["payload"])
    assert (rider.get("stamp") or {}).get("head_sha"), (
        f"riders land only under a FULL version stamp: {rider}"
    )


def test_cross_repo_join_is_exact(sync_store, tmp_path):
    a, b = _two_origins(tmp_path)
    register_repo(sync_store, "alpha", a.url, canonical_ref="main")
    register_repo(sync_store, "beta", b.url, canonical_ref="main")
    # the SAME path::symbol anchor, but bound to beta — alpha's change must not join it
    foreign = seed_scoped_episode(
        sync_store, "beta's greet lesson", anchors=[("beta", ANCHOR)]
    )
    local = seed_scoped_episode(
        sync_store, "alpha's greet lesson", anchors=[("alpha", ANCHOR)]
    )
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()
    a.commit("app.py", "def farewell(name):\n    return 'bye'\n", "remove greet")
    a.push()
    syncer.service.tick()

    outcome_ids = {r["episode_id"] for r in _evidence(sync_store, "change_outcome")}
    assert local in outcome_ids, "the same-repo anchor joins"
    assert foreign not in outcome_ids, (
        "an identical anchor in ANOTHER repo must never join (dropping the repo "
        "filter is the named mutation)"
    )


def test_pr_candidate_machinery_is_gone(sync_store, tmp_path):
    a, _b = (Origin(tmp_path / "remote-a"), None)
    a.set_pr_ref(7)  # a live PR head on the remote
    register_repo(sync_store, "alpha", a.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()

    assert meta_value(sync_store, "sync:pr_heads") is None, (
        "the PR-candidate baseline key must never be written (the leg is DELETED)"
    )
    ranges = [
        dict(r)
        for r in sync_store.conn.execute(
            "SELECT phase FROM ingested_ranges WHERE phase='pre_merge'"
        )
    ]
    assert ranges == [], "no pre_merge candidate ingest may ever run"


def test_empty_registry_is_inert(sync_store, tmp_path):
    run = RecordingRun()
    syncer = make_syncer(sync_store, tmp_path, run=run)
    syncer.service.tick()
    assert run.calls == [], (
        f"an EMPTY registry tick is inert — no git, no clone, no engine spawn: "
        f"{run.calls}"
    )
    assert not (syncer.mirror_base).exists() or not any(syncer.mirror_base.iterdir()), (
        "no mirror may be created by an inert tick"
    )


# ── the repo fan-out (sync.workers): same verdict, less wall-clock ────────────
def test_default_workers_never_reaches_the_fanout(sync_store, tmp_path):
    """workers=1 is the shipped path and stays the SERIAL loop byte-for-byte
    (§9 #9): a fan-out that detonates on entry cannot be reached at the default,
    so no default deployment silently gains threads."""
    a, b = _two_origins(tmp_path)
    register_repo(sync_store, "alpha", a.url, canonical_ref="main")
    register_repo(sync_store, "beta", b.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path)
    assert syncer.service._cfg.workers == 1, "the shipped default is serial"

    def _detonate(rows):
        raise AssertionError("the fan-out must not run at workers=1")

    syncer.service._repo_fanout = _detonate  # type: ignore[method-assign]
    syncer.service.tick()

    assert meta_value(sync_store, "sync:alpha:last_tip") == a.origin_sha(
        "refs/heads/main"
    )
    assert meta_value(sync_store, "sync:beta:last_tip") == b.origin_sha(
        "refs/heads/main"
    )


def test_repo_fanout_reaches_the_same_state_as_the_serial_loop(sync_store, tmp_path):
    """Concurrency is a throughput knob, never a semantic one: every repo still
    gets its own mirror and its own advanced watermark, and a clean fan-out tick
    still stamps the fleet-wide last_sync_ts."""
    a, b = _two_origins(tmp_path)
    register_repo(sync_store, "alpha", a.url, canonical_ref="main")
    register_repo(sync_store, "beta", b.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path, workers=4)
    syncer.service.tick()

    assert mirror_is_git(syncer.mirror_base, "alpha")
    assert mirror_is_git(syncer.mirror_base, "beta")
    assert meta_value(sync_store, "sync:alpha:last_tip") == a.origin_sha(
        "refs/heads/main"
    )
    assert meta_value(sync_store, "sync:beta:last_tip") == b.origin_sha(
        "refs/heads/main"
    )
    assert meta_value(sync_store, "sync:last_sync_ts"), (
        "a fault-free concurrent tick stamps the clean-sync marker like the serial one"
    )


def test_one_repo_fault_never_blocks_the_other_under_fanout(sync_store, tmp_path):
    """CT-9's isolation promise holds when the repos run concurrently: the broken
    remote surfaces on ITS key, the healthy one still syncs."""
    _a, b = _two_origins(tmp_path)
    register_repo(
        sync_store, "alpha", str(tmp_path / "no-such-remote.git"), canonical_ref="main"
    )
    register_repo(sync_store, "beta", b.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path, workers=4)
    syncer.service.tick()  # must not raise

    assert meta_value(sync_store, "sync:beta:last_tip") == b.origin_sha(
        "refs/heads/main"
    )
    assert meta_value(sync_store, "sync:alpha:last_error")
    assert meta_value(sync_store, "sync:last_sync_ts") is None, (
        "a faulted repo means the tick did not complete — the marker must not lie"
    )


def test_worker_fault_is_isolated_and_never_raises(sync_store, tmp_path):
    """The fan-out's OWN guard (the named mutation marker in `_repo_fanout`): a
    worker raising PAST `_repo_tick`'s per-leg guards is surfaced on that repo's
    error key and counted unclean — it never reaches the tick shell and never
    strands its siblings. Deleting that except-block reds this test."""
    a, b = _two_origins(tmp_path)
    register_repo(sync_store, "alpha", a.url, canonical_ref="main")
    register_repo(sync_store, "beta", b.url, canonical_ref="main")
    syncer = make_syncer(sync_store, tmp_path, workers=4)
    real_tick = syncer.service._repo_tick

    def _explode(row):
        if row.name == "alpha":
            raise RuntimeError("worker detonated")
        return real_tick(row)

    syncer.service._repo_tick = _explode  # type: ignore[method-assign]
    syncer.service.tick()  # must not raise

    err = meta_value(sync_store, "sync:alpha:last_error") or ""
    assert "worker detonated" in err and err.startswith("tick:"), (
        f"the escaped fault lands on the repo's own key, tagged as a tick fault: {err!r}"
    )
    assert meta_value(sync_store, "sync:beta:last_tip") == b.origin_sha(
        "refs/heads/main"
    ), "the sibling repo completed despite the other worker dying"
    assert meta_value(sync_store, "sync:last_sync_ts") is None
