"""The per-repo ledger-feed contract (v3 §3.6, §6 step 5).

Tip movement on a registered repo's canonical branch lands as ONE post_merge
receipt over ``watermark..new-tip`` (``--repo-id`` = the REGISTRY NAME — the
exact-match join key), ingested in-proc against the real store with the DERIVED
verdict (nothing decided in these scratch repos ⇒ the landed-line parity
``pass`` / ``unverified-judgment``). The per-repo watermark
(``sync:<name>:last_tip``) advances in the same critical section, and the
post-ingest promotion sweep (``LifecycleService.promote_established``) runs
right behind a canonical ingest. A merge push is just a push; coalesced pushes
are one contiguous range; a force-push is a logged discontinuity absorbed by a
defensive receipt over ``merge-base..tip``; a zero-match receipt is zero rows
and NO error; a non-canonical branch moves nothing. First connect baselines at
the remote tip with NO historical receipt.

Every test drives a REAL tmp origin (bare + pushes) through ``SyncService.tick()``
with the real sqlite store and REAL subprocess census builds.
"""
from __future__ import annotations

import logging
import subprocess

from hive.app.sync import META_LAST_SYNC_TS, default_run
from hive.domain.change_evidence import ChangeEvidenceService

from tests.sync.conftest import (
    REPO, RecordingLifecycle, build_receipt, evidence_rows, git,
    make_service, meta, payloads, register_repo, seed_episode,
)

META_LAST_TIP = f"sync:{REPO}:last_tip"
META_LAST_ERROR = f"sync:{REPO}:last_error"

_V2 = 'def greet(name):\n    return "hello " + name\n'
_V3 = 'def greet(name):\n    return "hej " + name\n\ndef part(x):\n    return x\n'
_V4 = 'def greet(name):\n    return "ola " + name\n'


def _baseline(origin, store, tmp_path, **kw):
    """First connect: register + tick once — baseline = remote tip, NO
    historical receipt."""
    register_repo(store, REPO, origin.url)
    svc = make_service(store, tmp_path, **kw)
    svc.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert evidence_rows(store) == []            # ← a historical receipt here is the mutation
    assert meta(store, META_LAST_TIP) == tip
    return svc, tip


def test_push_lands_rows(origin, store, tmp_path):
    seed_episode(store, "greet growls on empty names")
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
    # nothing decided in a scratch repo ⇒ the landed-line parity tag, and the
    # §6.2.5 canary rule holds (no canary signal ⇒ never machine-checked)
    assert body["tag"] == "unverified-judgment"
    assert body["ref"] == "main"                 # the explicitly-named measured line
    assert body["repo"] == REPO                  # the REGISTRY NAME keys every row
    assert meta(store, META_LAST_TIP) == tip     # the per-repo watermark advanced
    assert store.already_ingested_range(REPO, base, tip, "post_merge")
    assert meta(store, META_LAST_ERROR) is None


def test_merge_is_push(origin, store, tmp_path):
    """A --no-ff merge moving the canonical tip is indistinguishable from a push:
    ONE receipt over watermark..merge-commit."""
    seed_episode(store, "greet growls on empty names")
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
    """Two pushes between ticks coalesce into ONE contiguous watermark..tip
    receipt — never an intermediate-range receipt."""
    seed_episode(store, "greet growls on empty names")
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
    seed_episode(store, "greet growls on empty names")
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
    """No anchored episode matches: the receipt ingests to ZERO rows with NO
    error; the watermark still advances (seen work, nothing to record)."""
    svc, base = _baseline(origin, store, tmp_path)
    origin.commit("app.py", _V2, "unwatched change")
    origin.push()
    tip = origin.origin_sha("refs/heads/main")
    svc.tick()
    assert evidence_rows(store) == []
    assert meta(store, META_LAST_TIP) == tip
    assert meta(store, META_LAST_ERROR) is None


def test_foreign_branch_silent(origin, store, tmp_path):
    """A push to a NON-canonical branch lands no receipt and holds the watermark —
    the all-branches fetch carries it (drift demand needs the tip) but the ledger
    follows the canonical line only."""
    seed_episode(store, "greet growls on empty names")
    svc, base = _baseline(origin, store, tmp_path)

    git(origin.work, "checkout", "-q", "-b", "sidecar")
    origin.commit("app.py", _V2, "sidecar work")
    origin.push("sidecar")

    svc.tick()
    assert evidence_rows(store) == []
    assert meta(store, META_LAST_TIP) == base
    assert meta(store, META_LAST_ERROR) is None


def test_promote_established_runs_post_ingest(origin, store, tmp_path):
    """The post-ingest promotion sweep (CT-8's caller): promote_established runs
    exactly once per canonical ingest — never on a baseline or no-movement tick
    (no ingest, no sweep), once after a movement tick's ingest."""
    seed_episode(store, "greet growls on empty names")
    lifecycle = RecordingLifecycle()
    register_repo(store, REPO, origin.url)
    svc = make_service(store, tmp_path, lifecycle=lifecycle)
    svc.tick()                                   # first connect: baseline, no ingest
    assert lifecycle.calls == 0

    origin.commit("app.py", _V2, "move the tip")
    origin.push()
    svc.tick()                                   # one ingest ⇒ one sweep
    assert lifecycle.calls == 1

    svc.tick()                                   # nothing moved ⇒ no ingest, no sweep
    assert lifecycle.calls == 1


def test_ledger_fault_does_not_advance_last_sync_ts(origin, store, tmp_path):
    """A tick whose ledger leg faults (census build broken) holds
    ``sync:last_sync_ts`` at the last CLEAN tick — the stamp means "the last
    fully-successful sync", read against the per-repo ``sync:<name>:last_error``;
    only a fault-free tick advances it, and a repaired build resumes advancing."""
    clock = [1_000]
    broken = [False]

    def breaking_run(argv, env=None, timeout=None):
        if broken[0] and "hive.census.cli" in list(argv):
            return subprocess.CompletedProcess(list(argv), 1, stdout="",
                                               stderr="census build broken")
        return default_run(argv, env=env, timeout=timeout)

    register_repo(store, REPO, origin.url)
    svc = make_service(store, tmp_path, run=breaking_run, now=lambda: clock[0])
    svc.tick()                                   # first connect: baseline only — clean
    assert meta(store, META_LAST_SYNC_TS) == "1000"

    origin.commit("app.py", _V2, "move the tip")
    origin.push()
    broken[0], clock[0] = True, 2_000
    svc.tick()                                   # the build breaks → ledger-leg fault
    assert meta(store, META_LAST_ERROR).startswith("ledger:")
    assert meta(store, META_LAST_SYNC_TS) == "1000"   # ← the faulted tick held the stamp

    broken[0], clock[0] = False, 3_000
    svc.tick()                                   # repaired: the clean tick advances it
    assert meta(store, META_LAST_SYNC_TS) == "3000"


def test_legacy_overlap_never_blocks_the_sync_range(origin, store, tmp_path):
    """Transition noise is tolerated: legacy repo-less A..B + B..C receipts ride
    the SAME ingest door but, under the strict §3.6 exact-match join, a repo ""
    receipt joins only ""-scoped anchor rows — against a v3 registry store it
    matches nothing (zero rows, and a zero-match receipt records no range: the
    ledger marks ingested WORK, not seen receipts). The overlapping sync A..C
    receipt still lands its rows and its range under the REGISTRY-NAME key —
    exact-key dedupe never lets the legacy shapes absorb it."""
    seed_episode(store, "greet growls on empty names")
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
        assert not report.range_skipped          # distinct exact keys — nothing absorbed
        assert report.matched == 0               # ""-keyed: joins nothing in a v3 store
    assert evidence_rows(store) == []            # zero rows ⇒ zero ranges recorded

    svc.tick()                                   # sync covers A..C on top of the overlap
    sync_bodies = [x for x in payloads(store) if x.get("repo") == REPO]
    assert {(x["base_sha"], x["head_sha"]) for x in sync_bodies} == {(a, c)}
    assert store.already_ingested_range(REPO, a, c, "post_merge")
    assert not store.already_ingested_range("", a, b, "post_merge")
