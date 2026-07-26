"""BUG-078 — the engine-reason → wire-verdict mapping is EXHAUSTIVE by construction.

A deleted FILE is the strongest possible evidence that a memory's anchor is dead;
today it falls through ``wire_verdict``'s catch-all onto ``unverifiable``, the one
verdict that never qualifies retirement, while a deleted SYMBOL qualifies. This
module pins the symmetry AND the mechanism behind it: every reason the engine can
classify ``stale`` carries an explicit arrow, an unrecognized reason still fails
safe, and a reason added to the engine without a decided arrow cannot pass.

Drives the REAL surfaces — a real git origin, a REAL sync tick (real
``hive-edge verify`` subprocess in a real detached worktree), the REAL MCP recall
and retirement handlers — plus the REAL ``combdrift`` resolver for the ratchet's
reason battery. No mock stands in for any boundary this bug crosses.

Intents covered:
  I5 — a source FILE that is gone produces the same actionable verdict as a gone
       SYMBOL (``anchor_missing``) and qualifies the machine gate.
  I6 — the stale→wire table is exhaustive by construction; the fail-safe remains.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hive.app.config import SyncConfig
from hive.app.drift import DRIFT_UNVERIFIABLE, WIRE_VERDICTS, wire_verdict
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.sync import SyncService
from hive.domain.change_evidence import ChangeEvidenceService
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


# ── I5: a deleted FILE is a dead anchor, exactly like a deleted SYMBOL ────────

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
        "a FILE-scoped anchor to a deleted file has no fingerprint to compare and "
        "must still reach the actionable verdict"
    )
    assert drift_of(server, symbol_twin, SYM_TEXT) == "anchor_missing", (
        "the symbol tier is unchanged — the two tiers now AGREE"
    )
    assert drift_of(server, prose, PROSE_TEXT) == "unverifiable", (
        "the prose carve-out runs BEFORE the wire map: a string that was never "
        "code cannot have been moved past by code"
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


def test_the_file_and_symbol_tiers_agree_on_an_absent_anchor(rig):
    """A path that was never in the repo (a typo) and a symbol that was never in
    the file (a typo) are the same claim — 'the thing this memory names is not
    there' — and now read the same verdict. A memory with NO anchors is untouched:
    drift is n/a and the gate stays a benign no-op."""
    origin, server, tmp_path = rig
    typo_path = write(server, "the retry budget lives here", "pkg/nosuch.py::handler")
    typo_symbol = write(server, "greet must stay single-arg", "app.py::nosuch")
    env, _err = call(server, "hive_write", {"text": "a general fleet-wide lesson"})
    general = env["id"]

    make_syncer(server.store, tmp_path).tick()

    assert (
        drift_of(server, typo_path, "the retry budget lives here") == "anchor_missing"
    )
    assert (
        drift_of(server, typo_symbol, "greet must stay single-arg") == "anchor_missing"
    )
    assert drift_of(server, general, "a general fleet-wide lesson") == "n/a"

    for eid in (typo_path, typo_symbol):
        env, err = call(server, "hive_prune", {"episode_id": eid})
        assert err is False and env["status"] == "pruned", env
        assert "drift:anchor_missing" in env["signals"], env

    env, err = call(server, "hive_prune", {"episode_id": general})
    assert err is False and env["status"] == "noop", (
        "an anchor-less memory has nothing to be missing — still a no-op"
    )


def test_a_failed_worktree_writes_no_verdict_rather_than_a_false_anchor_missing(rig):
    """What makes ``file_missing`` a genuine measurement rather than a partial-
    checkout artifact: a non-zero ``git worktree add`` abandons the WHOLE batch,
    so no verdict is written at all. Without that guard every anchor would read
    ``file_missing`` against an empty directory — and now that ``file_missing``
    qualifies retirement, that would be mass false eligibility."""
    origin, server, tmp_path = rig
    origin.commit("pkg/svc.py", "def handler(x):\n    return x\n", "add svc")
    origin.push()
    eid = write(server, SVC_TEXT, ANCHOR)

    def is_worktree_add(argv):
        return "worktree" in argv and "add" in argv

    broken = make_syncer(
        server.store,
        tmp_path,
        run=RecordingRun(
            script=[(is_worktree_add, completed(rc=1, stderr="no worktree"))]
        ),
    )
    broken.tick()

    tip = origin.origin_sha("refs/heads/main")
    rows = [
        dict(r)
        for r in server.store.conn.execute(
            "SELECT * FROM anchor_drift WHERE repo='alpha' AND tip_sha=?", (tip,)
        )
    ]
    assert rows == [], f"a failed checkout must write NO verdict: {rows}"
    error = meta(server.store, "sync:alpha:last_error")
    assert error is not None and "drift" in error, error

    assert drift_of(server, eid, SVC_TEXT) == "unverifiable"
    env, err = call(server, "hive_prune", {"episode_id": eid})
    assert err is False and env["status"] == "noop", (
        "a broken checkout must never make a healthy memory retirable"
    )


# ── I6: the mapping is exhaustive BY CONSTRUCTION ────────────────────────────


def stale_arrows():
    """``hive.app.drift._STALE_ARROWS`` — the explicit stale-reason table."""
    import hive.app.drift as drift_module

    arrows = getattr(drift_module, "_STALE_ARROWS", None)
    assert arrows is not None, (
        "hive/app/drift.py carries no explicit _STALE_ARROWS table — the stale arm "
        "is still a two-arm if-chain over a silent catch-all (plan §3.3(b))"
    )
    return tuple((str(prefix), str(verdict)) for prefix, verdict in arrows)


def _uncovered(reason_class: dict[str, str], arrows) -> set[str]:
    """The ratchet's core: every reason the engine classifies ``stale`` that no
    arrow prefix covers. Extracted so the checker itself can be shown non-vacuous
    (a ratchet that iterates the ARROWS instead of the REASONS asserts nothing)."""
    return {
        reason
        for reason, verdict in reason_class.items()
        if verdict == "stale" and not any(reason.startswith(p) for p, _v in arrows)
    }


def _engine_reason_classes(tmp_path: Path) -> dict[str, str]:
    """Drive the REAL resolver over a battery covering every reason code the
    engine can attach to an ``AnchorResult``, and classify each through the
    engine's own precedence owner. Returns ``{reason_code: verdict}``."""
    from hive.combdrift.fingerprint import FINGERPRINT_VERSION
    from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
    from hive.combdrift.types import Anchor
    from hive.combdrift.verdict import _classify

    narrow = tmp_path / "narrow"
    wide = tmp_path / "wide"
    for root in (narrow, wide):
        (root / "pkg").mkdir(parents=True, exist_ok=True)
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (narrow / "pkg" / "auth.py").write_text(
        "TOKEN_TTL = 3600\n\n\ndef refresh(token):\n    return token\n",
        encoding="utf-8",
    )
    (wide / "pkg" / "auth.py").write_text(
        "def refresh(token, scope):\n    return token\n", encoding="utf-8"
    )
    (narrow / "pkg" / "broken.py").write_text(
        "def oops(:\n    pass\n", encoding="utf-8"
    )
    (narrow / "pkg" / "data.json").write_text('{"k": 1}\n', encoding="utf-8")

    root = str(narrow)
    matching = fingerprint_anchor(root, Anchor("pkg/auth.py", "refresh"))
    assert matching, "precondition: the battery's reference anchor must fingerprint"
    breaking = fingerprint_anchor(str(wide), Anchor("pkg/auth.py", "refresh"))
    assert breaking, "precondition: the wider shape must fingerprint"
    future = matching.replace(
        f"combdrift-fp/{FINGERPRINT_VERSION}:", "combdrift-fp/999:"
    )

    battery = (
        Anchor("pkg/auth.py", "refresh", matching),  # ok
        Anchor("pkg/auth.py", "refresh"),  # no_fingerprint
        Anchor("pkg/auth.py", None),  # no_symbol_requested
        Anchor("pkg/gone.py", "refresh"),  # file_missing
        Anchor("pkg/auth.py", "nosuch"),  # symbol_missing
        Anchor("pkg/auth.py", "TOKEN_TTL"),  # symbol_indirect
        Anchor("pkg/broken.py", "oops"),  # parse_error
        Anchor("pkg/data.json", "anything"),  # unsupported_language
        Anchor("../escape.py", "refresh"),  # path_outside_repo
        Anchor("pkg/auth.py", "refresh", breaking),  # signature_changed
        Anchor("pkg/auth.py", "refresh", future),  # fingerprint_version_mismatch
    )
    observed: dict[str, str] = {}
    for anchor in battery:
        result = resolve_anchor(root, anchor)
        observed[result.reason.split(":", 1)[0]] = _classify((result,))
    return observed


def test_every_stale_reason_the_engine_can_emit_has_an_explicit_wire_arrow(tmp_path):
    """CT-D1, the ratchet. The catch-all is silent by construction: any reason the
    engine classifies ``stale`` that the mapper does not NAME degrades to
    ``unverifiable`` with no test and no log. This derives the engine's own
    stale set from the engine and requires an explicit arrow for every member —
    so adding a stale reason without deciding its wire arrow cannot pass."""
    import hive.combdrift.types as engine_types
    from hive.combdrift.verdict import _classify

    arrows = stale_arrows()
    observed = _engine_reason_classes(tmp_path)

    # 1. the battery must cover the engine's whole vocabulary; a NEW reason lands
    #    here first, before anyone can forget to decide its arrow.
    vocabulary = {
        value
        for name, value in vars(engine_types).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    unexercised = vocabulary - set(observed) - {engine_types.REASON_NO_ANCHORS}
    assert unexercised == set(), (
        f"engine reasons with no scenario in the ratchet battery: {sorted(unexercised)} "
        "— add one and decide its wire arrow"
    )
    # the one documented exception: no_anchors is reachable only through the empty
    # record, never as an AnchorResult reason, so it needs no wire arrow.
    assert _classify(()) == "unverifiable"

    # 2. every stale-classifiable reason has an EXPLICIT arrow ...
    assert _uncovered(observed, arrows) == set(), (
        "a reason the engine classifies stale has no explicit wire arrow — it would "
        "degrade to unverifiable silently, which is exactly BUG-078's mechanism"
    )
    # ... and the arrow is what wire_verdict actually returns
    by_prefix = dict(arrows)
    for reason, verdict in observed.items():
        if verdict != "stale":
            continue
        prefix = next(p for p, _v in arrows if reason.startswith(p))
        assert wire_verdict("stale", reason) == by_prefix[prefix]
        assert wire_verdict(f"stale/{reason}") == by_prefix[prefix]

    # 3. the table is small, complete, and inside the advertised vocabulary
    assert {reason for reason, v in observed.items() if v == "stale"} == {
        engine_types.REASON_FILE_MISSING,
        engine_types.REASON_SYMBOL_MISSING,
        engine_types.REASON_SIGNATURE_CHANGED,
    }
    assert all(target in WIRE_VERDICTS for _p, target in arrows), arrows

    # 4. the checker is NOT vacuous: a hypothetical unmapped stale reason is caught
    assert _uncovered({"a_brand_new_stale_reason": "stale"}, arrows) == {
        "a_brand_new_stale_reason"
    }
    assert _uncovered({"a_brand_new_stale_reason": "unverifiable"}, arrows) == set()


def test_an_unknown_stale_reason_still_fails_safe(rig):
    """The fail-safe arm STAYS: 'unknown' now means unknown rather than merely
    unenumerated, and an unknown reason — or a hostile cache row — is silence."""
    for state, reason in (
        ("stale", "some_future_engine_reason"),
        ("stale", ""),
        ("stale/xyzzy", ""),
        (42, "file_missing"),
        ("stale", 42),
        (["stale"], "file_missing"),
        ("staleness", "file_missing"),
    ):
        assert wire_verdict(state, reason) == DRIFT_UNVERIFIABLE, (state, reason)

    # ... and an out-of-vocabulary row already IN the cache serves unverifiable
    origin, server, tmp_path = rig
    origin.commit("pkg/svc.py", "def handler(x):\n    return x\n", "add svc")
    origin.push()
    eid = write(server, SVC_TEXT, ANCHOR)
    make_syncer(server.store, tmp_path).tick()
    tip = origin.origin_sha("refs/heads/main")

    server.store.conn.execute(
        "UPDATE anchor_drift SET verdict='file_missing' WHERE repo='alpha' AND tip_sha=?",
        (tip,),
    )
    assert drift_of(server, eid, SVC_TEXT) == "unverifiable", (
        "the cache stores WIRE vocabulary; an engine reason smuggled into it is "
        "out of vocabulary and must never be re-mapped at read time"
    )
    env, err = call(server, "hive_prune", {"episode_id": eid})
    assert err is False and env["status"] == "noop", env
