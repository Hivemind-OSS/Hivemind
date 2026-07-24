"""The drift-materializer contract (v3 D5, §6 step 5, CT-6's unit twin).

A tick materializes ``anchor_drift`` rows per (repo, tip, anchor): a detached
worktree at the tip, the REAL ``hive-edge verify`` per anchor with the STORED
fingerprints, verdicts mapped through ``hive.app.drift.wire_verdict`` (the cache
stores §3.4 wire vocabulary verbatim). Tips = the canonical tip plus branch tips
recall demanded via ``ref_requests`` within the 7-day demand window. An
incomparable stored token version yields silence/``unverifiable``, NEVER
false-stale (BUG-037) — including on the missing-symbol path the engine itself
cannot version-gate. A verify subprocess fault reads ``unverifiable``. The cache
is a rebuildable Law-5 cache (wipe → repopulated; old tips pruned); the verify
cap carries over.
"""

from __future__ import annotations

import json
import re

from tests.sync.conftest import (
    ANCHOR,
    REPO,
    RecordingRun,
    completed,
    drift_rows,
    git,
    make_service,
    meta,
    register_repo,
    seed_episode,
)

FP_KEY = "combdrift/fp"
STALE = {"anchor_changed", "anchor_missing", "blast_radius_changed"}


def _armed(origin, store, tmp_path, **kw):
    register_repo(store, REPO, origin.url)
    return make_service(store, tmp_path, **kw)


def _verdict_for(store, tip: str, anchor: str = ANCHOR):
    for r in drift_rows(store, REPO, tip):
        if r["anchor"] == anchor:
            return r["verdict"]
    return None


def _bump_version(token: str, to: int = 999) -> str:
    m = re.match(r"^([A-Za-z0-9_.-]+/)(\d+)(:.*)$", token, re.DOTALL)
    assert m is not None, f"token {token[:40]!r} has no version envelope"
    return f"{m.group(1)}{to}{m.group(3)}"


def test_fresh_then_stale_at_the_moved_tip_and_prune(origin, store, tmp_path):
    """An intact anchor materializes ``fresh`` at the measured tip; a removed
    symbol materializes ``anchor_missing`` at the NEW tip; the old tip's rows are
    pruned (the cache key is the tip — a moved tip invalidates naturally)."""
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()  # mint the fp + materialize at tip0
    tip0 = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip0) == "fresh"
    assert meta(store, f"sync:{REPO}:last_tip") == tip0

    origin.commit(
        "app.py", "def farewell(name):\n    return 'bye ' + name\n", "rm greet"
    )
    origin.push()
    svc.tick()
    tip1 = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip1) == "anchor_missing"
    assert drift_rows(store, REPO, tip0) == [], (
        "drift_prune drops tips no longer live — the cache is bounded"
    )


def test_signature_change_materializes_anchor_changed(origin, store, tmp_path):
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()  # fp minted at tip0
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    svc.tick()
    tip1 = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip1) == "anchor_changed"


def test_incomparable_token_version_never_false_stale(origin, store, tmp_path):
    """BUG-037 on the missing-symbol path: the engine's own version gate fires
    only when the symbol resolves, so the materializer's belt must keep an
    incomparable stored token silent even when the symbol is GONE."""
    eid = seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()  # real fp minted
    row = store.conn.execute(
        "SELECT fp_meta FROM episode_anchors WHERE episode_id=?", (eid,)
    ).fetchone()
    fp = json.loads(row["fp_meta"])
    assert fp.get(FP_KEY), "precondition: a real minted token"
    fp[FP_KEY] = _bump_version(fp[FP_KEY])  # incomparable, not different
    store.conn.execute(
        "UPDATE episode_anchors SET fp_meta=? WHERE episode_id=?",
        (json.dumps(fp, separators=(",", ":")), eid),
    )
    # move the line so a naive comparator WOULD scream stale
    origin.commit("app.py", "def farewell(name):\n    return 'bye'\n", "rm greet")
    origin.push()
    svc.tick()
    tip1 = origin.origin_sha("refs/heads/main")
    v = _verdict_for(store, tip1)
    assert v not in STALE, f"never false-stale off an incomparable token: {v!r}"
    assert v in (None, "unverifiable")


def test_requested_ref_materializes_at_its_tip(origin, store, tmp_path):
    """Recall demand (``ref_requests``) drives non-canonical coverage: a touched
    branch is materialized at ITS tip on the next tick — the CT-6 shape, with the
    demand stamp far behind the service wall clock (the window anchors on the
    demand clock, so a lagging stamp never starves coverage)."""
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()  # fp minted at the canonical tip
    git(origin.work, "checkout", "-q", "-b", "feature")
    origin.commit("app.py", "def other():\n    return 1\n", "break on feature")
    origin.push("feature")
    git(origin.work, "checkout", "-q", "main")
    feature_tip = origin.origin_sha("refs/heads/feature")

    store.touch_ref_request(REPO, "feature", 424_242)
    svc.tick()

    v = _verdict_for(store, feature_tip)
    assert v is not None, "a requested ref must be materialized at ITS tip"
    assert v in STALE, f"the branch broke the anchor: {v!r}"


def test_demand_window_excludes_refs_older_than_7d(origin, store, tmp_path):
    """The exclusion side of the 7d window: a ref whose last demand is > 7d older
    than the newest demand (and the service clock) ages off the work list."""
    seed_episode(store, "greet lesson")
    for name, content in (
        ("old-branch", "def other():\n    return 1\n"),
        ("new-branch", "def other():\n    return 2\n"),
    ):
        git(origin.work, "checkout", "-q", "-b", name)
        origin.commit("app.py", content, f"break on {name}")
        origin.push(name)
        git(origin.work, "checkout", "-q", "main")
    old_tip = origin.origin_sha("refs/heads/old-branch")
    new_tip = origin.origin_sha("refs/heads/new-branch")

    store.touch_ref_request(REPO, "old-branch", 1_000_000)  # 10.4d before newest
    store.touch_ref_request(REPO, "new-branch", 1_900_000)
    svc = _armed(origin, store, tmp_path, now=lambda: 2_000_000)
    svc.tick()

    assert _verdict_for(store, new_tip) is not None, "in-window demand materializes"
    assert _verdict_for(store, old_tip) is None, (
        "demand older than the 7d window must NOT be materialized"
    )


def test_unknown_requested_ref_skips_silently(origin, store, tmp_path):
    """A demanded ref with no remote tip (deleted/never-pushed branch) is skipped
    silently — the canonical tip still materializes, no error is noted."""
    seed_episode(store, "greet lesson")
    store.touch_ref_request(REPO, "no-such-branch", 424_242)
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip) == "fresh"
    assert meta(store, f"sync:{REPO}:last_error") is None


def test_cache_is_rebuildable(origin, store, tmp_path):
    seed_episode(store, "greet lesson")
    svc = _armed(origin, store, tmp_path)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert drift_rows(store, REPO, tip), "precondition: materialized rows"

    store.conn.execute("DELETE FROM anchor_drift")
    assert not drift_rows(store, REPO, tip)
    svc.tick()
    assert drift_rows(store, REPO, tip), (
        "the drift cache is a rebuildable cache (Law 5): wipe → repopulated"
    )


def test_verify_cap_carries_over(origin, store, tmp_path):
    origin.commit("util.py", "def helper(x):\n    return x\n", "add util")
    origin.push()
    seed_episode(store, "greet lesson")
    seed_episode(store, "helper lesson", "util.py::helper")
    svc = _armed(origin, store, tmp_path, drift_per_tick=1)

    svc.tick()  # one verify slot: one anchor lands
    tip = origin.origin_sha("refs/heads/main")
    first = drift_rows(store, REPO, tip)
    assert len(first) == 1, f"bounded batch: {first}"
    svc.tick()  # the remainder carries over
    verdicts = {r["anchor"]: r["verdict"] for r in drift_rows(store, REPO, tip)}
    assert set(verdicts) == {ANCHOR, "util.py::helper"}
    assert verdicts[ANCHOR] == "fresh" and verdicts["util.py::helper"] == "fresh"


def test_verify_subprocess_fault_reads_unverifiable(origin, store, tmp_path):
    """A nonzero ``hive-edge verify`` exit materializes ``unverifiable`` —
    fail-safe, never false-stale/fresh — and the tick stays clean."""
    seed_episode(store, "greet lesson")

    def is_verify(argv):
        return "verify" in argv and any("hive-edge" in a for a in argv[:1])

    run = RecordingRun(script=[(is_verify, completed(rc=1, stderr="engine broke"))])
    svc = _armed(origin, store, tmp_path, run=run)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert _verdict_for(store, tip) == "unverifiable"
    assert meta(store, f"sync:{REPO}:last_error") is None


def test_radius_change_maps_to_blast_radius_verdict(store, tmp_path):
    """The wire shim: a ``current`` anchor whose dependency-neighborhood radius
    CHANGED rides the radius tier into ``blast_radius_changed`` (never a bare
    fresh) — asserted through ``_verify_anchor`` with a scripted engine."""

    def is_verify(argv):
        return "verify" in argv and any("hive-edge" in a for a in argv[:1])

    run = RecordingRun(
        script=[
            (
                is_verify,
                completed(
                    rc=0,
                    stdout=json.dumps(
                        {"verdict": "current", "reason": "ok", "radius": "changed"}
                    ),
                ),
            )
        ]
    )
    svc = make_service(store, tmp_path, run=run)
    verdict, detail = svc._verify_anchor(tmp_path, ANCHOR, "combdrift-fp/1:x", "")
    assert verdict == "blast_radius_changed"
    assert json.loads(detail)["radius"] == "changed"
