"""BUG-071 — ``fresh`` at the symbol tier is unconstructable without a real
fingerprint comparison, and the mint-backfill never baselines from a tree in which
the server has just watched that anchor change.

Drives the REAL surfaces: a real git origin, a REAL sync tick (real
``python -m hive.census.cli build`` / ``hive-edge mint`` / ``hive-edge verify``
subprocesses), and the REAL MCP recall + retirement handlers. The ONLY seam used
is the documented scripted ``Run`` door, and only to make a spawn FAIL — the
failure directions the daemon already declares.

Intents covered:
  I3 — an anchor whose call shape was never compared reads ``unverifiable``, never
       ``fresh``; unknown never qualifies a retirement; the new engine reason never
       leaks into the census change path.
  I4 — an anchor the tick's OWN receipt reported changed is not baselined that tick;
       it mints on the first quiet tick; a faulted ledger leg mints nothing; a
       range-skipped ingest still knows what the range contained.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hive.app.config import SyncConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.sync import SyncService
from hive.domain.change_evidence import ChangeEvidenceService
from tests.contract.conftest import RecordingRun, completed, is_mint_argv
from tests.mcp._helpers import build_real_server
from tests.sync.conftest import Origin, build_receipt, meta

ANCHOR = "app.py::greet"
TEXT = "greet() must stay single-arg; callers pass positionally"

FP_KEY = "combdrift/fp"


# ── rig ───────────────────────────────────────────────────────────────────────


def call(server, name, args, *, agent="agent-a"):
    resp = server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("t", agent),
    )
    return json.loads(resp.result["content"][0]["text"]), bool(
        resp.result.get("isError")
    )


def make_syncer(store, tmp_path: Path, run=None, **cfg_kw):
    cfg = SyncConfig(mirror_dir=str(tmp_path / "mirrors"), **cfg_kw)
    evidence = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424_242, ranges=store
    )
    kwargs = {"run": run} if run is not None else {}
    return SyncService(cfg, store, evidence, threading.Lock(), **kwargs)


@pytest.fixture
def rig(tmp_path):
    origin = Origin(tmp_path / "remote")
    server, clock = build_real_server(t0=1_000_000)
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    return origin, server, clock, tmp_path


def write(server, text, anchor):
    env, _err = call(
        server,
        "hive_write",
        {"text": text, "anchors": [{"repo": "alpha", "anchor": anchor}]},
    )
    assert env["status"] in ("approved", "redacted"), env
    return env["id"]


def carrier(server, eid, anchor) -> dict:
    row = server.store.conn.execute(
        "SELECT fp_meta FROM episode_anchors WHERE episode_id=? AND anchor=?",
        (eid, anchor),
    ).fetchone()
    assert row is not None, f"no anchor row for ({eid}, {anchor!r})"
    if not row["fp_meta"]:
        return {}
    parsed = json.loads(row["fp_meta"])
    return parsed if isinstance(parsed, dict) else {}


def served_drift(server, eid, query=TEXT) -> str:
    env, _err = call(server, "hive_recall", {"query": query})
    for hit in env.get("reference_context", []):
        if hit.get("episode_id") == eid:
            return hit["drift"]["type"]
    raise AssertionError(f"episode {eid} not served: {env}")


def is_census_build(argv) -> bool:
    return "hive.census.cli" in argv and "build" in argv


# ── I3: an uncompared symbol never reads fresh ────────────────────────────────


def test_an_uncompared_symbol_never_reads_fresh(rig):
    """An EMPTY fingerprint carrier means the call shape was never compared. The
    served verdict must be the honest unknown, not the positive claim ``fresh`` —
    while the two tiers whose whole claim IS existence stay exactly as they were."""
    origin, server, _clock, tmp_path = rig

    sym = write(server, TEXT, ANCHOR)
    gone = write(server, TEXT + " (ghost)", "app.py::ghost")
    filed = write(server, TEXT + " (file scope)", "app.py")

    # the declared mint failure direction: nonzero exit ⇒ {} ⇒ silent skip,
    # carrier stays EMPTY (the pre-mint window, verbatim)
    broken_mint = make_syncer(
        server.store,
        tmp_path,
        run=RecordingRun(
            script=[(is_mint_argv, completed(rc=1, stderr="engine broke"))]
        ),
    )
    broken_mint.tick()

    assert carrier(server, sym, ANCHOR) == {}, "precondition: no baseline was minted"
    assert served_drift(server, sym) == "unverifiable", (
        "a symbol whose call shape was NEVER compared must not read fresh — that is "
        "a positive claim about a comparison that did not happen"
    )
    assert served_drift(server, gone, TEXT + " (ghost)") == "anchor_missing", (
        "a MISSING symbol is still provable absence, baseline or not"
    )
    assert served_drift(server, filed, TEXT + " (file scope)") == "fresh", (
        "existence IS the whole claim of a file-scoped anchor — unchanged"
    )
    assert meta(server.store, "sync:alpha:last_error") is None

    # now let the mint through on a fresh tip: a real comparison ⇒ a real fresh
    origin.commit("notes.py", "def note():\n    return 1\n", "add notes")
    origin.push()
    healthy = make_syncer(server.store, tmp_path)
    healthy.tick()

    assert carrier(server, sym, ANCHOR).get(FP_KEY, "").startswith("combdrift-fp/")
    assert served_drift(server, sym) == "fresh", "a COMPARED, intact shape is fresh"

    # ... and a real break still reads anchor_changed off that baseline
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    healthy.tick()
    assert served_drift(server, sym) == "anchor_changed"


def test_an_unbaselined_anchor_never_qualifies_retirement(rig):
    """``unverifiable`` ∉ QUALIFYING_DRIFT: an un-baselined anchor is a benign
    no-op for BOTH retirement verbs — unknown never retires."""
    _origin, server, _clock, tmp_path = rig
    eid = write(server, TEXT, ANCHOR)
    winner = write(server, TEXT + " (successor)", ANCHOR + "2")

    make_syncer(
        server.store,
        tmp_path,
        run=RecordingRun(script=[(is_mint_argv, completed(rc=1))]),
    ).tick()
    assert served_drift(server, eid) == "unverifiable"

    env, err = call(server, "hive_prune", {"episode_id": eid})
    assert err is False, "an unqualified prune is a benign no-op, never an error"
    assert env["status"] == "noop", env
    assert "no qualifying machine signal" in env["reason"], env
    assert server.store.get_episode(eid).trust == "provisional"

    env, err = call(server, "hive_supersede", {"loser": eid, "winner": winner})
    assert err is False and env["status"] == "noop", env
    assert server.store.get_episode(eid).trust == "provisional"


def test_the_no_fingerprint_reason_never_reaches_the_census_change_path(tmp_path):
    """``resolve_anchor``'s SECOND consumer is ``combdrift.change.verify_change`` —
    the census path that feeds ``change_outcome`` / ``verify_*`` and therefore
    retirement clause 1b, this plan's own compensating control. It calls
    ``resolve_anchor`` with a fingerprint-less Anchor, so the new reason must be
    unreachable in every branch that READS a reason there."""
    from hive.combdrift.change import verify_change
    from hive.combdrift.types import (
        REASON_FINGERPRINT_VERSION_MISMATCH,
        REASON_OK,
        REASON_SYMBOL_MISSING,
    )

    base = tmp_path / "base"
    head = tmp_path / "head"
    for root in (base, head):
        root.mkdir(parents=True, exist_ok=True)

    # base: an overloaded symbol (no single interface ⇒ fingerprint_anchor None),
    # an indirect (non-callable) binding, and a symbol that is deleted at head.
    (base / "m.py").write_text(
        "def over(a):\n    return a\n\n\ndef over(a, b):\n    return a\n\n\n"
        "DATA = 1\n\n\ndef doomed(x):\n    return x\n",
        encoding="utf-8",
    )
    # head: the overload persists, DATA persists, doomed is gone, added is NEW.
    (head / "m.py").write_text(
        "def over(a):\n    return a\n\n\ndef over(a, b):\n    return a\n\n\n"
        "DATA = 1\n\n\ndef added(x, y):\n    return x\n",
        encoding="utf-8",
    )

    verdict = verify_change(
        str(base),
        str(head),
        (("m.py", "over"), ("m.py", "DATA"), ("m.py", "doomed"), ("m.py", "added")),
        base_sha="b" * 40,
        head_sha="h" * 40,
    )
    by_symbol = {c.symbol: c for c in verdict.symbols}

    assert by_symbol["over"].drift == "indeterminate"
    assert by_symbol["over"].reason == REASON_FINGERPRINT_VERSION_MISMATCH
    assert by_symbol["DATA"].drift == "indeterminate"
    assert by_symbol["doomed"].drift == "removed"
    assert by_symbol["doomed"].reason.startswith(REASON_SYMBOL_MISSING)
    # the M14 surface: a symbol ADDED in the range resolves at head against a None
    # base token — the branch that must NOT read head_res.reason.
    assert by_symbol["added"].drift == "additive"
    assert by_symbol["added"].reason == REASON_OK, (
        "the no-fingerprint reason leaked into the census change path"
    )
    assert not any("no_fingerprint" in c.reason for c in verdict.symbols), (
        f"no change-path reason may carry the new code: {verdict.symbols}"
    )

    # the roll-up reuses verdict._classify, so a leaked reason would move it too:
    # an ADDED symbol alone stays `current` (nothing was broken), a DELETED symbol
    # alone is `stale`, and the abstaining pair dominates the whole batch.
    def roll_up(*touched):
        return verify_change(
            str(base), str(head), touched, base_sha="b" * 40, head_sha="h" * 40
        ).verdict

    assert roll_up(("m.py", "added")) == "current", (
        "an un-fingerprinted ADD must not turn the change verdict unverifiable"
    )
    assert roll_up(("m.py", "doomed")) == "stale"
    assert verdict.verdict == "unverifiable", (
        "unverifiable still dominates: the overloaded/indirect pair abstains"
    )


def test_a_real_receipt_still_lands_its_verify_rows(rig):
    """Clause 1b's feed, end to end: a real census receipt over a real range still
    writes the ledger rows the gate reads — the change path is untouched."""
    origin, server, _clock, tmp_path = rig
    eid = write(server, TEXT, ANCHOR)
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()

    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    syncer.tick()

    kinds = {
        r["kind"]
        for r in server.store.conn.execute(
            "SELECT DISTINCT kind FROM evidence_events WHERE episode_id=?", (eid,)
        )
    }
    assert "change_outcome" in kinds, kinds
    assert kinds & {"verify_current", "verify_stale"}, (
        f"the census verify rows (clause 1b's feed) stopped landing: {kinds}"
    )


# ── I4: never baseline from a tree in which the anchor just changed ───────────


def test_a_just_changed_anchor_is_not_baselined_this_tick(rig):
    """The tick's OWN receipt names ``app.py::greet`` as touched, so minting the
    carrier here would record the POST-break shape as the baseline and freeze
    ``fresh`` forever. The anchor stays un-baselined; an UNTOUCHED anchor in the
    same tick still mints."""
    origin, server, _clock, tmp_path = rig
    origin.commit("util.py", "def helper(x):\n    return x\n", "add util")
    origin.push()

    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()  # baseline at tip0; nothing anchored yet

    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    changed = write(server, TEXT, ANCHOR)
    untouched = write(server, TEXT + " (helper)", "util.py::helper")

    syncer.tick()  # censuses the break AND sweeps the empty carriers

    assert carrier(server, changed, ANCHOR) == {}, (
        "the backfill baselined an anchor the SAME tick's receipt reported changed"
    )
    assert (
        carrier(server, untouched, "util.py::helper")
        .get(FP_KEY, "")
        .startswith("combdrift-fp/")
    ), "an untouched anchor must still mint in that tick — the skip is per anchor"
    assert served_drift(server, changed) == "unverifiable"
    assert served_drift(server, untouched, TEXT + " (helper)") == "fresh"
    assert meta(server.store, "sync:alpha:last_error") is None, (
        "a deferral is normal operation, not a fault"
    )


def test_a_deferred_anchor_baselines_on_the_next_quiet_tick(rig):
    """The deferral is transient by construction: defer → mint on the first quiet
    tick → a later break reads anchor_changed off that honest baseline."""
    origin, server, _clock, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()  # baseline at tip0

    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    eid = write(server, TEXT, ANCHOR)

    syncer.tick()  # the changed anchor is deferred
    assert carrier(server, eid, ANCHOR) == {}
    assert served_drift(server, eid) == "unverifiable"

    syncer.tick()  # quiet tick: no new commits, nothing touched ⇒ mint
    minted = carrier(server, eid, ANCHOR)
    assert minted.get(FP_KEY, "").startswith("combdrift-fp/"), (
        "a deferred anchor must baseline on the first quiet tick — never permanently"
    )
    tip = origin.origin_sha("refs/heads/main")
    assert minted["hive-sync/minted"] == f"hive-sync-minted/1:server@{tip} main", (
        "the baseline is stamped at the tree it was actually measured against"
    )
    # (the verdict cached for THIS tip predates the carrier and is not recomputed
    # until the tip moves — the anchor_drift cache is keyed by (repo, tip, anchor)
    # alone: BUG-081, out of scope here. Its direction is `unverifiable`, the
    # fail-safe unknown, never a false fresh.)

    origin.commit(
        "app.py",
        "def greet(name, punct, sep):\n    return name\n",
        "widen greet again",
    )
    origin.push()
    syncer.tick()
    assert served_drift(server, eid) == "anchor_changed", (
        "the honest baseline must detect the NEXT break — which the frozen "
        "post-break baseline BUG-071 wrote could never do"
    )


def test_a_faulted_ledger_leg_mints_no_baseline(rig):
    """A faulted ledger leg means the server does not know what changed, so it
    must not baseline anything that tick. The drift leg still runs, the fault is
    surfaced, and the next clean tick mints."""
    origin, server, _clock, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()  # baseline at tip0

    eid = write(server, TEXT, ANCHOR)
    origin.commit("util.py", "def helper(x):\n    return x\n", "add util")
    origin.push()

    faulting = make_syncer(
        server.store,
        tmp_path,
        run=RecordingRun(
            script=[(is_census_build, completed(rc=1, stderr="census broke"))]
        ),
    )
    faulting.tick()

    assert carrier(server, eid, ANCHOR) == {}, (
        "a tick that could not census the range must not baseline any anchor"
    )
    error = meta(server.store, "sync:alpha:last_error")
    assert error is not None and "ledger" in error, error
    tip = origin.origin_sha("refs/heads/main")
    drift = [
        dict(r)
        for r in server.store.conn.execute(
            "SELECT * FROM anchor_drift WHERE repo='alpha' AND tip_sha=?", (tip,)
        )
    ]
    assert drift, "the drift leg is independent — it must still run"
    assert served_drift(server, eid) == "unverifiable"

    syncer.tick()  # recovery: the range censuses cleanly and the anchor mints
    assert carrier(server, eid, ANCHOR).get(FP_KEY, "").startswith("combdrift-fp/"), (
        "the next clean tick must mint what the faulted one withheld"
    )


def test_a_range_skipped_ingest_still_defers_the_changed_anchor(rig):
    """The manual ``hive ingest`` door can absorb a range before the daemon reaches
    it, so the daemon's own ingest returns ``range_skipped``. The report must still
    state what the range CONTAINED — otherwise the deferral silently reopens the
    exact window it exists to close."""
    origin, server, _clock, tmp_path = rig
    origin.commit("util.py", "def helper(x):\n    return x\n", "add util")
    origin.push()
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()  # baseline at tip0
    base = origin.origin_sha("refs/heads/main")

    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    head = origin.origin_sha("refs/heads/main")

    changed = write(server, TEXT, ANCHOR)
    untouched = write(server, TEXT + " (helper)", "util.py::helper")

    # the manual door absorbs the range FIRST (what `hive ingest` does)
    manual = ChangeEvidenceService(
        reader=server.store,
        appender=server.store,
        now=lambda: 424_242,
        ranges=server.store,
    )
    envelope = build_receipt(
        origin.work, base, head, tmp_path, repo_id="alpha", ref="main"
    )
    report = manual.ingest(envelope, phase="post_merge", verdict="pass", signal="none")
    assert report.matched, "precondition: the manual ingest joined the anchor"

    syncer.tick()  # the daemon's own ingest now comes back range_skipped

    assert carrier(server, changed, ANCHOR) == {}, (
        "a range-skipped ingest still knows what the range touched — the changed "
        "anchor must stay deferred"
    )
    assert (
        carrier(server, untouched, "util.py::helper")
        .get(FP_KEY, "")
        .startswith("combdrift-fp/")
    ), "the untouched anchor still mints under a range-skipped ingest"


def test_the_ingest_report_carries_the_touched_paths_on_both_return_sites(rig):
    """The unit twin of the test above: BOTH return sites of ``ingest`` populate
    ``touched_paths`` — the normal one and the range-skipped early return."""
    origin, server, _clock, tmp_path = rig
    write(server, TEXT, ANCHOR)
    base = origin.origin_sha("refs/heads/main")
    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    head = origin.origin_sha("refs/heads/main")

    service = ChangeEvidenceService(
        reader=server.store,
        appender=server.store,
        now=lambda: 424_242,
        ranges=server.store,
    )
    envelope = build_receipt(
        origin.work, base, head, tmp_path, repo_id="alpha", ref="main"
    )

    first = service.ingest(envelope, phase="post_merge", verdict="pass", signal="none")
    assert getattr(first, "touched_paths", None) is not None, (
        "IngestReport gained no touched_paths field"
    )
    assert "app.py" in first.touched_paths, first

    second = service.ingest(envelope, phase="post_merge", verdict="pass", signal="none")
    assert second.range_skipped is True, "precondition: the range ledger absorbed it"
    assert "app.py" in second.touched_paths, (
        "a range-skipped report must still state what the range contained"
    )
