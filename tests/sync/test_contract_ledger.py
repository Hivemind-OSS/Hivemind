"""CT-3 (+ the CT-6 overlap leg) — the ledger feed contract (intents 3 and 6).

Tip movement on the tracked branch lands as ONE post_merge receipt over
``watermark..new-tip`` (phase post_merge, verdict pass, signal none — the
post-merge hook's exact parity), ingested in-proc against the real store. A
merge push is just a push; coalesced pushes are one contiguous range; a
force-push is a logged discontinuity absorbed by a defensive receipt over
``merge-base..tip`` with the loop alive after; a zero-match receipt is zero rows
and NO error; a non-tracked branch moves nothing. First connect baselines at the
remote tip with NO historical receipt. Overlapping legacy ranges land alongside
the sync range (exact-key dedupe only — transition noise is tolerated).

Every test drives a REAL tmp origin (bare + pushes) through ``SyncService.tick()``
with the real sqlite store and REAL subprocess census builds.
"""
from __future__ import annotations

import logging

from hive.domain.change_evidence import ChangeEvidenceService

from tests.sync.conftest import (
    ANCHOR, evidence_rows, git, make_service, meta, payloads, seed_episode,
    build_receipt,
)

META_LAST_TIP = "sync:last_tip"
META_LAST_ERROR = "sync:last_error"

_V2 = 'def greet(name):\n    return "hello " + name\n'
_V3 = 'def greet(name):\n    return "hej " + name\n\ndef part(x):\n    return x\n'
_V4 = 'def greet(name):\n    return "ola " + name\n'


def _baseline(origin, store, tmp_path, **kw):
    """First connect: tick once — baseline = remote tip, NO historical receipt."""
    svc = make_service(origin, store, tmp_path, **kw)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert evidence_rows(store) == []            # ← a historical receipt here is the mutation
    assert meta(store, META_LAST_TIP) == tip
    return svc, tip


def test_push_lands_rows(origin, store, tmp_path):
    seed_episode(store, "greet growls on empty names", ANCHOR)
    svc, base = _baseline(origin, store, tmp_path)

    origin.commit("app.py", _V2, "tweak greet")
    origin.push()
    tip = origin.origin_sha("refs/heads/main")
    svc.tick()

    rows = evidence_rows(store)
    assert len(rows) == 1                        # one matched episode, one outcome row
    body = payloads(store)[0]
    assert body["base_sha"] == base and body["head_sha"] == tip
    assert body["phase"] == "post_merge" and body["verdict"] == "pass"
    assert body["tag"] == "unverified-judgment"  # signal none — hook parity
    assert body["ref"] == "main"                 # the mirror checkout's measured line
    repo_key = body["repo"]
    assert repo_key and "origin" in repo_key and not repo_key.endswith(".git")
    assert meta(store, META_LAST_TIP) == tip     # the watermark advanced
    assert store.already_ingested_range(repo_key, base, tip, "post_merge")
    assert meta(store, META_LAST_ERROR) is None


def test_merge_is_push(origin, store, tmp_path):
    """A --no-ff merge moving the tracked tip is indistinguishable from a push: ONE
    receipt over watermark..merge-commit."""
    seed_episode(store, "greet growls on empty names", ANCHOR)
    svc, base = _baseline(origin, store, tmp_path)

    git(origin.work, "checkout", "-q", "-b", "feature")
    origin.commit("app.py", _V2, "feature work")
    git(origin.work, "checkout", "-q", "main")
    git(origin.work, "merge", "-q", "--no-ff", "-m", "land feature", "feature")
    origin.push()
    merge_sha = origin.origin_sha("refs/heads/main")

    svc.tick()
    bodies = payloads(store)
    assert len(bodies) == 1
    assert bodies[0]["base_sha"] == base and bodies[0]["head_sha"] == merge_sha
    assert meta(store, META_LAST_TIP) == merge_sha


def test_coalesced_contiguous(origin, store, tmp_path):
    """Two pushes between ticks coalesce into ONE contiguous watermark..tip receipt —
    never an intermediate-range receipt."""
    seed_episode(store, "greet growls on empty names", ANCHOR)
    svc, base = _baseline(origin, store, tmp_path)

    origin.commit("app.py", _V2, "push one")
    origin.push()
    mid = origin.origin_sha("refs/heads/main")
    origin.commit("app.py", _V3, "push two")
    origin.push()
    tip = origin.origin_sha("refs/heads/main")
    assert mid != tip

    svc.tick()
    spans = {(b["base_sha"], b["head_sha"]) for b in payloads(store)}
    assert spans == {(base, tip)}                # one contiguous range, no mid receipt
    ranges = store.conn.execute("SELECT COUNT(*) FROM ingested_ranges").fetchone()[0]
    assert ranges == 1


def test_force_push_defensive(origin, store, tmp_path, caplog):
    """A rewritten line: the discontinuity is logged, a DEFENSIVE receipt covers
    merge-base..new-tip, the watermark resets to the new tip, and the loop stays
    alive (a further tick still works)."""
    seed_episode(store, "greet growls on empty names", ANCHOR)
    svc, base = _baseline(origin, store, tmp_path)

    origin.commit("app.py", _V2, "doomed")
    origin.push()
    svc.tick()
    doomed = origin.origin_sha("refs/heads/main")
    assert meta(store, META_LAST_TIP) == doomed

    git(origin.work, "reset", "-q", "--hard", base)      # rewrite: drop the doomed commit
    origin.commit("app.py", _V4, "rewritten")
    origin.push(force=True)
    new_tip = origin.origin_sha("refs/heads/main")

    with caplog.at_level(logging.WARNING, logger="hive.sync"):
        svc.tick()
    assert any("discontinuity" in r.getMessage() for r in caplog.records)
    spans = {(b["base_sha"], b["head_sha"]) for b in payloads(store)}
    assert (base, new_tip) in spans              # defensive receipt: merge-base..tip
    assert meta(store, META_LAST_TIP) == new_tip
    svc.tick()                                   # the loop is alive after the reset
    assert meta(store, META_LAST_TIP) == new_tip


def test_zero_match_ok(origin, store, tmp_path):
    """No anchored episode matches: the receipt ingests to ZERO rows with NO error;
    the watermark still advances (seen work, nothing to record)."""
    svc, base = _baseline(origin, store, tmp_path)
    origin.commit("app.py", _V2, "unwatched change")
    origin.push()
    tip = origin.origin_sha("refs/heads/main")
    svc.tick()
    assert evidence_rows(store) == []
    assert meta(store, META_LAST_TIP) == tip
    assert meta(store, META_LAST_ERROR) is None


def test_foreign_branch_silent(origin, store, tmp_path):
    """A push to a NON-tracked branch moves nothing: no receipt, watermark holds."""
    seed_episode(store, "greet growls on empty names", ANCHOR)
    svc, base = _baseline(origin, store, tmp_path)

    git(origin.work, "checkout", "-q", "-b", "sidecar")
    origin.commit("app.py", _V2, "sidecar work")
    origin.push("sidecar")

    svc.tick()
    assert evidence_rows(store) == []
    assert meta(store, META_LAST_TIP) == base
    assert meta(store, META_LAST_ERROR) is None


def test_legacy_overlap_lands(origin, store, tmp_path):
    """CT-6 (overlap leg): legacy A..B + B..C receipts (repo "") alongside the sync
    A..C receipt (real repo key) ALL land — exact-key range dedupe tolerates the
    transition overlap, and content-keyed idempotency never collides across keys."""
    seed_episode(store, "greet growls on empty names", ANCHOR)
    svc, a = _baseline(origin, store, tmp_path)

    origin.commit("app.py", _V2, "step b")
    origin.push()
    b = origin.origin_sha("refs/heads/main")
    origin.commit("app.py", _V3, "step c")
    origin.push()
    c = origin.origin_sha("refs/heads/main")

    # the legacy path (hook/censusctl shape): receipts WITHOUT a repo identity
    legacy = ChangeEvidenceService(reader=store, appender=store,
                                   now=lambda: 424242, ranges=store)
    for lo, hi in ((a, b), (b, c)):
        report = legacy.ingest(build_receipt(origin.work, lo, hi, tmp_path),
                               phase="post_merge", verdict="pass", signal="none")
        assert report.matched == 1 and not report.range_skipped
    assert len(evidence_rows(store)) == 2

    svc.tick()                                   # sync covers A..C on top of the overlap
    bodies = payloads(store)
    assert len(bodies) == 3                      # both legacy rows AND the sync row
    spans = {(x["base_sha"], x["head_sha"], x.get("repo", "")) for x in bodies}
    assert (a, b, "") in spans and (b, c, "") in spans
    sync_span = next(s for s in spans if s[2])   # the sync row carries the real repo key
    assert (sync_span[0], sync_span[1]) == (a, c)
    assert store.already_ingested_range("", a, b, "post_merge")
    assert store.already_ingested_range("", b, c, "post_merge")
    assert store.already_ingested_range(sync_span[2], a, c, "post_merge")
