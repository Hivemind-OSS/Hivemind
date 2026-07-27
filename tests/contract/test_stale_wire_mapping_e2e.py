"""BUG-078 — a deleted FILE is the strongest possible evidence that a memory's anchor
is dead, and it must reach the same actionable verdict as a deleted SYMBOL.

It used to fall through the engine-reason → wire-verdict table's catch-all onto
``unverifiable``, the one verdict that never qualifies retirement, while a deleted
symbol qualified. There is no such table any more: the producer is the pure
``hive.domain.staleness`` ladder, which reaches ``anchor_missing`` from a departed path
whether the binding named a symbol or only the path. The asymmetry is unconstructable
rather than enumerated — this module pins that, and pins the fail-safe that stayed: an
out-of-vocabulary row already in the cache is never re-interpreted at read time.

Drives the REAL surfaces — a real git origin, a REAL sync tick, the REAL MCP recall and
retirement handlers. No mock stands in for any boundary this bug crosses.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hive.app.config import SyncConfig
from hive.app.drift import DRIFT_UNVERIFIABLE, SEVERITY_ORDER, WIRE_VERDICTS
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.sync import SyncService
from hive.domain import staleness
from hive.domain.change_evidence import ChangeEvidenceService
from hive.domain.retirement import QUALIFYING_DRIFT
from tests.contract.conftest import RecordingRun, completed
from tests.mcp._helpers import build_real_server
from tests.sync.conftest import Origin, git, meta

ANCHOR = "pkg/svc.py::handler"


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
    server, _clock = build_real_server(t0=1_000_000)
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    return origin, server, tmp_path


def write(server, text, anchor, **extra):
    env, _err = call(
        server,
        "hive_write",
        {"text": text, "anchors": [{"repo": "alpha", "anchor": anchor}], **extra},
    )
    assert env["status"] in ("approved", "redacted"), env
    return env["id"]


def drift_of(server, eid, query, **recall_args) -> str:
    env, _err = call(server, "hive_recall", {"query": query, **recall_args})
    for hit in env.get("reference_context", []):
        if hit.get("episode_id") == eid:
            return hit["drift"]["type"]
    raise AssertionError(f"episode {eid} not served for {query!r}: {env}")


def delete(origin, rel: str, msg: str) -> None:
    git(origin.work, "rm", "-q", rel)
    git(origin.work, "commit", "-qm", msg)
    origin.push()


# ── a deleted FILE is a dead anchor, exactly like a deleted SYMBOL ────────────

SVC_TEXT = "handler() owns the retry budget — never call it from a loop"
FILE_TEXT = "pkg/svc.py is the only place the retry budget lives"
SYM_TEXT = "greet() must stay single-arg; callers pass positionally"
PROSE_TEXT = "the auth refresh flow re-issues before expiry, never after"
OFFLINE_TEXT = "handler() takes a budget object on the feature line"


def test_a_deleted_file_reads_anchor_missing_and_qualifies_retirement(rig):
    origin, server, tmp_path = rig
    origin.commit("pkg/svc.py", "def handler(x):\n    return x\n", "add svc")
    origin.push()
    syncer = make_syncer(server.store, tmp_path)

    sym_scoped = write(server, SVC_TEXT, ANCHOR)
    file_scoped = write(server, FILE_TEXT, "pkg/svc.py")
    symbol_twin = write(server, SYM_TEXT, "app.py::greet")
    prose = write(server, PROSE_TEXT, "the auth refresh flow")
    off_line = write(server, OFFLINE_TEXT, ANCHOR, repos=["alpha@feature"])

    syncer.tick()
    assert drift_of(server, sym_scoped, SVC_TEXT) == "fresh", "precondition"
    assert drift_of(server, file_scoped, FILE_TEXT) == "fresh", "precondition"

    # PRODUCTION manufactures the evidence: delete the whole file, and (for the
    # twin) delete only the symbol.
    delete(origin, "pkg/svc.py", "remove svc entirely")
    origin.commit("app.py", "def other():\n    return 1\n", "remove greet")
    origin.push()
    syncer.tick()

    assert drift_of(server, sym_scoped, SVC_TEXT) == "anchor_missing", (
        "a deleted FILE is the maximal form of 'the anchor is missing' — the "
        "strongest evidence must not produce the weakest verdict"
    )
    assert drift_of(server, file_scoped, FILE_TEXT) == "anchor_missing", (
        "a FILE-scoped anchor to a deleted file reaches the same actionable verdict"
    )
    assert drift_of(server, symbol_twin, SYM_TEXT) == "anchor_missing", (
        "the symbol tier is unchanged — the two tiers AGREE"
    )
    assert drift_of(server, prose, PROSE_TEXT) == "unverifiable", (
        "a string that was never a path in the tree was never code, and code "
        "cannot have moved past it"
    )
    assert (
        drift_of(server, off_line, OFFLINE_TEXT, repos=["alpha"]) == "branch_scoped"
    ), "an off-line consumer reads the advisory route, not a bare unverifiable"

    # the gate: a CONSCIOUS prune is now permitted, and stamps which signal allowed it
    env, err = call(server, "hive_prune", {"episode_id": sym_scoped})
    assert err is False and env["status"] == "pruned", env
    assert "drift:anchor_missing" in env["signals"], env
    assert server.store.get_episode(sym_scoped).trust == "deprecated"

    env, err = call(server, "hive_prune", {"episode_id": file_scoped})
    assert err is False and env["status"] == "pruned", env
    assert "drift:anchor_missing" in env["signals"], env

    # ... and the prose memory stays exactly as un-retirable as before
    env, err = call(server, "hive_prune", {"episode_id": prose})
    assert err is False and env["status"] == "noop", env
    assert server.store.get_episode(prose).trust == "provisional"


def test_a_path_that_was_never_in_the_tree_is_unverifiable_not_missing(rig):
    """The honest correction the git ladder makes to the old engine: a mis-typed PATH
    used to read ``anchor_missing`` and qualify a retirement, on evidence that amounts
    to "we looked in a tree and did not find it" — which is indistinguishable from
    "this string was never a path at all". Only a path that demonstrably EXISTED at the
    baseline and then departed is provable absence. A memory with NO anchors is
    untouched: drift is ``n/a`` and the gate stays a benign no-op."""
    origin, server, tmp_path = rig
    typo_path = write(server, "the retry budget lives here", "pkg/nosuch.py::handler")
    env, _err = call(server, "hive_write", {"text": "a general fleet-wide lesson"})
    general = env["id"]

    make_syncer(server.store, tmp_path).tick()

    assert drift_of(server, typo_path, "the retry budget lives here") == "unverifiable"
    assert drift_of(server, general, "a general fleet-wide lesson") == "n/a"

    for eid in (typo_path, general):
        env, err = call(server, "hive_prune", {"episode_id": eid})
        assert err is False and env["status"] == "noop", (
            "an anchor nothing can verify must never qualify a retirement"
        )
        assert server.store.get_episode(eid).trust == "provisional"

    # ... and once the path really exists and really departs, it DOES qualify
    origin.commit("pkg/nosuch.py", "def handler(x):\n    return x\n", "add it")
    origin.push()
    make_syncer(server.store, tmp_path).tick()  # the watermark reaches the new path
    later = write(server, "a note written once the path exists", "pkg/nosuch.py")
    make_syncer(server.store, tmp_path).tick()
    assert drift_of(server, later, "a note written once the path exists") == "fresh"
    delete(origin, "pkg/nosuch.py", "and remove it again")
    make_syncer(server.store, tmp_path).tick()
    assert (
        drift_of(server, later, "a note written once the path exists")
        == "anchor_missing"
    )
    env, err = call(server, "hive_prune", {"episode_id": later})
    assert err is False and env["status"] == "pruned", env


def test_a_failed_probe_writes_unverifiable_rather_than_a_false_anchor_missing(rig):
    """What makes ``anchor_missing`` a genuine measurement rather than a broken-read
    artifact: a git fault degrades that baseline group to ``unverifiable``. Without
    that, an unreadable tree would look exactly like an empty one — and since
    ``anchor_missing`` qualifies retirement, that would be mass false eligibility."""
    origin, server, tmp_path = rig
    origin.commit("pkg/svc.py", "def handler(x):\n    return x\n", "add svc")
    origin.push()
    eid = write(server, SVC_TEXT, ANCHOR)
    make_syncer(server.store, tmp_path).tick()  # baseline + a real fresh verdict

    origin.commit("pkg/svc.py", "def handler(x, y):\n    return x\n", "widen handler")
    origin.push()

    def is_ls_tree(argv):
        return "ls-tree" in argv

    broken = make_syncer(
        server.store,
        tmp_path,
        run=RecordingRun(script=[(is_ls_tree, completed(rc=1, stderr="unreadable"))]),
    )
    broken.tick()

    tip = origin.origin_sha("refs/heads/main")
    rows = [
        dict(r)
        for r in server.store.conn.execute(
            "SELECT * FROM anchor_drift WHERE repo='alpha' AND tip_sha=?", (tip,)
        )
    ]
    assert rows == [], (
        f"a faulted probe writes NO row: absence already reads unverifiable, and a "
        f"cached degradation would freeze at this tip until it moved again: {rows}"
    )
    error = meta(server.store, "sync:alpha:last_error")
    assert error is not None and "drift" in error, error

    assert drift_of(server, eid, SVC_TEXT) == "unverifiable"
    env, err = call(server, "hive_prune", {"episode_id": eid})
    assert err is False, env
    assert not any(str(sig).startswith("drift:") for sig in env.get("signals", ())), (
        "a broken probe must contribute NO drift signal to the gate — whatever else "
        f"the ledger legs saw, this leg saw nothing: {env}"
    )


# ── the asymmetry is unconstructable, and the read-time fail-safe stays ───────


def test_the_two_absence_tiers_cannot_disagree(rig):
    """The structural replacement for the old reason table: absence is decided by ONE
    arm of the ladder, so a gone file and a gone symbol cannot be given different
    tiers by anyone forgetting to enumerate one."""
    base = staleness.AnchorFacts(
        base_tip="b" * 40, in_base_tree=True, symbol_resolved_at_base=True
    )
    import dataclasses

    for status in ("D", "R100"):
        for symbol in ("", "handler"):
            verdict, _d = staleness.decide(
                dataclasses.replace(base, status=status), symbol=symbol
            )
            assert verdict == "anchor_missing"
            assert verdict in QUALIFYING_DRIFT
    # a symbol that vanished from a file that stayed reaches the same tier
    gone_symbol = dataclasses.replace(
        base, status="M", symbol_resolved_at_tip=False, symbol_resolved_at_base=True
    )
    assert staleness.decide(gone_symbol, symbol="handler")[0] == "anchor_missing"


def test_the_producer_can_only_emit_advertised_wire_members():
    """The cache stores wire vocabulary verbatim, so the producer's whole range must
    be inside it — otherwise a materialized row would read as out-of-vocabulary at
    serve time and silently degrade."""
    emitted = {
        staleness.FRESH,
        staleness.ANCHOR_CHANGED,
        staleness.ANCHOR_MISSING,
        staleness.UNVERIFIABLE,
    }
    assert emitted <= set(WIRE_VERDICTS)
    assert emitted <= set(SEVERITY_ORDER)


def test_an_out_of_vocabulary_cache_row_still_fails_safe(rig):
    """The read-time fail-safe STAYS: whatever lands in the cache, only advertised
    wire members ride the hit — an engine reason smuggled in is never re-mapped."""
    origin, server, tmp_path = rig
    origin.commit("pkg/svc.py", "def handler(x):\n    return x\n", "add svc")
    origin.push()
    eid = write(server, SVC_TEXT, ANCHOR)
    make_syncer(server.store, tmp_path).tick()
    tip = origin.origin_sha("refs/heads/main")

    server.store.conn.execute(
        "UPDATE anchor_drift SET verdict='file_missing' WHERE repo='alpha' "
        "AND tip_sha=?",
        (tip,),
    )
    assert drift_of(server, eid, SVC_TEXT) == DRIFT_UNVERIFIABLE, (
        "the cache stores WIRE vocabulary; a reason smuggled into it is out of "
        "vocabulary and must never be re-mapped at read time"
    )
    env, err = call(server, "hive_prune", {"episode_id": eid})
    assert err is False and env["status"] == "noop", env
