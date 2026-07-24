"""CT-5 — the server mints eagerly; the backfill sweep is the path (plan §4
intent 5, §6 step 5).

Given a write with an anchor in repo A, the next sync tick mints the ABSENT fp
keys via the real mint seam (the in-image ``hive-edge mint`` CLI — one mint owner
fleet-wide) against A's canonical tip into ``episode_anchors.fp_meta``:
absent-only (present keys never displaced), capped + carried over, unresolvable
anchors skipped silently, per-repo baselining, provenance stamped. Real tmp git
origins + the real store; the cap scenario scripts ONLY the mint spawn through
the injectable ``Run`` seam.
"""

from __future__ import annotations

import json

# NOTE: the ``sync_store`` fixture is auto-discovered from tests/contract/conftest.py
from tests.contract.conftest import (
    ANCHOR,
    Origin,
    RecordingRun,
    anchor_fp,
    completed,
    is_mint_argv,
    make_syncer,
    register_repo,
    seed_scoped_episode,
)
from tests.sync.test_contract_backfill import direct_mint

FP_KEY = "combdrift/fp"
PROVENANCE_KEY = "hive-sync/minted"


def _origin(tmp_path, name: str = "remote") -> Origin:
    return Origin(tmp_path / name)


def test_fresh_mint_lands_in_anchor_fp_meta(sync_store, tmp_path):
    origin = _origin(tmp_path)
    register_repo(sync_store, "alpha", origin.url, canonical_ref="main")
    eid = seed_scoped_episode(
        sync_store, "greet growls on empty names", anchors=[("alpha", ANCHOR)]
    )
    syncer = make_syncer(sync_store, tmp_path)
    syncer.service.tick()

    fp = anchor_fp(sync_store, eid, "alpha", ANCHOR)
    assert FP_KEY in fp, f"the sweep must mint the absent fp key: {fp}"
    assert fp[FP_KEY].startswith("combdrift-fp/"), fp[FP_KEY]
    # byte-equal to a direct edge mint on the same tree (one mint owner)
    reference = direct_mint(origin.work, ANCHOR, tmp_path / "edge-home-ref")
    assert fp[FP_KEY] == reference[FP_KEY], (
        "server-backfilled keys must be BYTE-EQUAL to an edge mint on the same tree"
    )


def test_provenance_stamp_names_the_canonical_tip(sync_store, tmp_path):
    origin = _origin(tmp_path)
    register_repo(sync_store, "alpha", origin.url, canonical_ref="main")
    eid = seed_scoped_episode(
        sync_store, "greet growls on empty names", anchors=[("alpha", ANCHOR)]
    )
    make_syncer(sync_store, tmp_path).service.tick()
    tip = origin.origin_sha("refs/heads/main")

    fp = anchor_fp(sync_store, eid, "alpha", ANCHOR)
    stamp = fp.get(PROVENANCE_KEY, "")
    assert stamp, f"the minted keys carry a provenance stamp: {fp}"
    assert tip in stamp, (
        f"the stamp names the measured canonical tip {tip[:12]}: {stamp!r}"
    )


def test_absent_only_present_keys_never_displaced(sync_store, tmp_path):
    origin = _origin(tmp_path)
    register_repo(sync_store, "alpha", origin.url, canonical_ref="main")
    pinned = "combdrift-fp/1:human-pinned"
    eid = seed_scoped_episode(
        sync_store, "a tagged memory", anchors=[("alpha", ANCHOR)]
    )
    # pre-pin the fp on the anchor row, then tick — the pin must survive
    sync_store.conn.execute(
        "UPDATE episode_anchors SET fp_meta=? WHERE episode_id=?",
        (json.dumps({FP_KEY: pinned}, separators=(",", ":")), eid),
    )
    make_syncer(sync_store, tmp_path).service.tick()

    fp = anchor_fp(sync_store, eid, "alpha", ANCHOR)
    assert fp.get(FP_KEY) == pinned, (
        "a PRESENT fp key must never be displaced (absent-only merge)"
    )


def test_unresolvable_anchor_skips_silently_loop_alive(sync_store, tmp_path):
    origin = _origin(tmp_path)
    register_repo(sync_store, "alpha", origin.url, canonical_ref="main")
    ghost = seed_scoped_episode(
        sync_store, "anchored to nothing", anchors=[("alpha", "nope.py::gone")]
    )
    real = seed_scoped_episode(
        sync_store, "anchored to greet", anchors=[("alpha", ANCHOR)]
    )
    make_syncer(sync_store, tmp_path).service.tick()

    assert FP_KEY not in anchor_fp(sync_store, ghost, "alpha", "nope.py::gone"), (
        "an unresolvable anchor mints nothing (silent skip)"
    )
    assert FP_KEY in anchor_fp(sync_store, real, "alpha", ANCHOR), (
        "the loop stays alive past the skip — later anchors still mint"
    )


def test_cap_carries_over(sync_store, tmp_path):
    origin = _origin(tmp_path)
    register_repo(sync_store, "alpha", origin.url, canonical_ref="main")
    n = 51
    eids = [
        seed_scoped_episode(
            sync_store,
            f"fact number {i} about greet",
            anchors=[("alpha", ANCHOR)],
        )
        for i in range(n)
    ]
    # script ONLY the mint spawn (git + census run real): constant fake token
    run = RecordingRun(
        script=[
            (
                is_mint_argv,
                completed(rc=0, stdout=json.dumps({FP_KEY: "combdrift-fp/1:fake"})),
            )
        ]
    )
    # the cap is PINNED here, never inherited from the shipped default: this test
    # is about the carry-over behavior, not about what number the default happens
    # to be (raising that default must not silently disarm the assertion below).
    syncer = make_syncer(sync_store, tmp_path, run=run, backfill_per_tick=10)

    def n_filled() -> int:
        return sum(
            1 for e in eids if FP_KEY in anchor_fp(sync_store, e, "alpha", ANCHOR)
        )

    syncer.service.tick()
    first = n_filled()
    assert 0 < first < n, (
        f"one tick must mint a BOUNDED batch, the rest carry over (filled {first}/{n})"
    )
    for _ in range(6):
        if n_filled() == n:
            break
        syncer.service.tick()
    assert n_filled() == n, "carried-over anchors must mint on later ticks"


def test_per_repo_baselining(sync_store, tmp_path):
    origin_a = _origin(tmp_path, "remote-a")
    origin_b = _origin(tmp_path, "remote-b")
    # diverge B's tree at the INTERFACE — greet gains a parameter — so the same
    # anchor genuinely fingerprints differently there. A body-only string edit
    # sits below the mint engine's sensitivity floor (interface- and
    # call-graph-identical trees emit byte-identical tokens — BUG-048), so the
    # divergence must change the anchored function's signature.
    origin_b.commit(
        "app.py",
        'def greet(name, punct="!"):\n    return "hi " + name + punct\n',
        "diverge",
    )
    origin_b.push()
    register_repo(sync_store, "alpha", origin_a.url, canonical_ref="main")
    register_repo(sync_store, "beta", origin_b.url, canonical_ref="main")
    ea = seed_scoped_episode(
        sync_store, "alpha greet lesson", anchors=[("alpha", ANCHOR)]
    )
    eb = seed_scoped_episode(
        sync_store, "beta greet lesson", anchors=[("beta", ANCHOR)]
    )
    make_syncer(sync_store, tmp_path).service.tick()

    fp_a = anchor_fp(sync_store, ea, "alpha", ANCHOR)
    fp_b = anchor_fp(sync_store, eb, "beta", ANCHOR)
    assert FP_KEY in fp_a and FP_KEY in fp_b, (ea, fp_a, eb, fp_b)
    ref_a = direct_mint(origin_a.work, ANCHOR, tmp_path / "edge-a")
    assert fp_a[FP_KEY] == ref_a[FP_KEY], (
        "alpha's fp must be minted against ALPHA's tree"
    )
    assert fp_a[FP_KEY] != fp_b[FP_KEY], (
        "each repo baselines against its OWN canonical tip"
    )
