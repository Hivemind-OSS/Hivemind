"""The FROZEN contract suite for git-native staleness (I1–I11).

Staleness is answered by git itself: a per-binding BASELINE COMMIT recorded from the
repo watermark, two plumbing reads per distinct baseline per tick (``ls-tree`` at the
baseline + ``diff --name-status`` over ``base..tip``), and — only for a symbol anchor
whose file actually moved — one ``git log -L ':sym:path'``. No fingerprint carrier, no
dependency-subgraph hash, no engine subprocess, no per-anchor work queue.

Every test drives the REAL surfaces end to end: a real tmp git origin, the REAL
``SyncService.tick()``, and the REAL ``hive_recall`` / ``hive_prune`` MCP handlers. No
test seeds a ``drift_put`` row and reads it back; the served envelope is the assertion
surface, because a verdict that never reaches the agent is not a verdict.

RED-FIRST MECHANICS: this file is authored against the UN-BUILT system and must fail as
FAILING ASSERTIONS, never import/collection errors. Every not-yet-existing module,
method, table and column is reached through a ``require_*`` guard, and the guards double
as the pinned surface names the implementation must conform to.

FROZEN: no later chunk may edit this file. A genuine defect in it is an escalation, not
a silent edit.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from hive.app.config import SyncConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.sync import SyncService
from hive.domain.change_evidence import ChangeEvidenceService
from tests.contract.conftest import (
    RecordingRun,
    require_method,
    require_module,
    require_table,
)
from tests.mcp._helpers import build_real_server
from tests.sync.conftest import Origin

REPO = "alpha"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# ── the tracked tree: two top-level functions + a class with an indented method ──
# ``bill`` is the sibling I3 edits to prove unrelated churn is not drift; ``Store``
# exists because git resolves an INDENTED method only through a funcname driver (M4),
# which is exactly what the attributes file supplies.


def app_py(greet: str = 'return "hi " + name', tail: str = "") -> str:
    return (
        f"def greet(name):\n    {greet}\n\n\ndef bill(user):\n    return user\n" + tail
    )


def store_py(body: str = "return True", extra: str = "") -> str:
    return (
        "class Store:\n"
        "    def repo_remove(self, name):\n"
        f"        {body}\n\n"
        "    def m(self):\n"
        "        return 1\n" + extra
    )


TEXT_SYM = "greet() must stay single-arg; callers pass positionally"
TEXT_FILE = "app.py is the whole public surface of the demo service"
TEXT_BARE = "repo_remove deregisters one repo and forgets its derived feed state"
TEXT_QUAL = "Store.repo_remove must stay idempotent-bool for the repoctl exit code"


# ── rig ───────────────────────────────────────────────────────────────────────


def call(server, name, args, *, agent="agent-a"):
    resp = server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("t", agent),
    )
    return json.loads(resp.result["content"][0]["text"])


def make_syncer(store, tmp_path: Path, run=None, **cfg_kw) -> SyncService:
    """A SyncService wired the entrypoint way. ``cfg_kw`` reaches ``SyncConfig``
    verbatim — this suite never passes a per-tick cap, because the git ladder is
    bounded by real churn and owns no budget knob."""
    cfg = SyncConfig(mirror_dir=str(tmp_path / "mirrors"), **cfg_kw)
    evidence = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424_242, ranges=store
    )
    kwargs = {"run": run} if run is not None else {}
    return SyncService(cfg, store, evidence, threading.Lock(), **kwargs)


@pytest.fixture
def rig(tmp_path):
    origin = Origin(tmp_path / "remote")
    origin.commit("app.py", app_py(), "seed app")
    origin.commit("store.py", store_py(), "seed store")
    origin.push()
    server, _clock = build_real_server(t0=1_000_000)
    server.store.repo_add(
        name=REPO, url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    return origin, server, tmp_path


def write(server, text: str, anchor: str, **kw) -> int:
    env = call(
        server,
        "hive_write",
        {"text": text, "anchors": [{"repo": REPO, "anchor": anchor}], **kw},
    )
    assert env.get("status") in ("approved", "redacted"), env
    return env["id"]


def hit_of(server, eid: int, query: str, **recall_kw) -> dict:
    env = call(server, "hive_recall", {"query": query, **recall_kw})
    for hit in env.get("reference_context", []):
        if hit.get("episode_id") == eid:
            return hit
    raise AssertionError(f"episode {eid} not served for {query!r}: {env}")


def drift_of(server, eid: int, query: str, **recall_kw) -> dict:
    return hit_of(server, eid, query, **recall_kw)["drift"]


def verdict_of(server, eid: int, query: str, **recall_kw) -> str:
    return drift_of(server, eid, query, **recall_kw)["type"]


def evidence_of(server, eid: int, query: str, **recall_kw) -> dict:
    per_anchor = (drift_of(server, eid, query, **recall_kw).get("detail") or {}).get(
        "per_anchor"
    ) or [{}]
    return per_anchor[0]


def meta_value(store, key: str):
    row = store.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


# ── surface guards (red = assertion, never a collection error) ────────────────


def require_column(store, table: str, column: str) -> None:
    require_table(store, table)
    cols = {r["name"] for r in store.conn.execute(f"PRAGMA table_info({table})")}
    assert column in cols, (
        f"{table}.{column} is missing — the git-native staleness schema is not built "
        f"(have {sorted(cols)})"
    )


def baseline_rows(store, repo: str = REPO) -> list[dict]:
    require_table(store, "anchor_baselines")
    return [
        dict(r)
        for r in store.conn.execute(
            "SELECT * FROM anchor_baselines WHERE repo=? ORDER BY episode_id, anchor",
            (repo,),
        )
    ]


def baseline_of(store, eid: int, anchor: str, repo: str = REPO) -> str:
    for row in baseline_rows(store, repo):
        if int(row["episode_id"]) == eid and row["anchor"] == anchor:
            return str(row["base_tip"])
    raise AssertionError(
        f"no anchor_baselines row for ({eid}, {repo!r}, {anchor!r}): "
        f"{baseline_rows(store, repo)}"
    )


def drift_rows(store, repo: str = REPO) -> list[dict]:
    require_column(store, "anchor_drift", "base_tip")
    return [
        dict(r)
        for r in store.conn.execute(
            "SELECT * FROM anchor_drift WHERE repo=? ORDER BY tip_sha, base_tip, anchor",
            (repo,),
        )
    ]


# ── argv predicates (the observed spawn surface) ──────────────────────────────


def is_ls_tree(argv) -> bool:
    return "ls-tree" in argv


def is_range_diff(argv) -> bool:
    return "diff" in argv and "--name-status" in argv


def is_symbol_log(argv) -> bool:
    return "log" in argv and "-L" in argv


def is_any_engine(argv) -> bool:
    return any("hive-edge" in a for a in argv) or any("hive.edge" in a for a in argv)


def counts(run: RecordingRun) -> dict[str, int]:
    return {
        "ls_tree": sum(1 for a in run.calls if is_ls_tree(a)),
        "diff": sum(1 for a in run.calls if is_range_diff(a)),
        "symbol_log": sum(1 for a in run.calls if is_symbol_log(a)),
        "engine": sum(1 for a in run.calls if is_any_engine(a)),
    }


# ── I1 ────────────────────────────────────────────────────────────────────────


def test_an_unchanged_anchor_serves_fresh_and_spawns_nothing(rig):
    """An anchor whose file has not moved since its baseline is ``fresh`` — and the
    cheap tier is a SET LOOKUP: once a tip is materialized, a repeat tick at that tip
    spawns no ls-tree, no diff and no per-anchor log at all. Covers: tip unmoved; tip
    moved but the anchored file untouched; three ticks in a row; a second anchor
    sharing one baseline."""
    origin, server, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()  # watermark lands at tip0

    sym = write(server, TEXT_SYM, "app.py::greet")
    filed = write(server, TEXT_FILE, "app.py")
    syncer.tick()

    base = baseline_of(server.store, sym, "app.py::greet")
    assert base == baseline_of(server.store, filed, "app.py"), (
        "two bindings written at the same watermark share ONE baseline, so the "
        "probe runs once for both"
    )
    assert verdict_of(server, sym, TEXT_SYM) == "fresh"
    assert verdict_of(server, filed, TEXT_FILE) == "fresh"

    # tip moves, but nothing under app.py moves with it
    origin.commit("notes.py", "def note():\n    return 1\n", "unrelated file")
    origin.push()
    quiet = RecordingRun()
    make_syncer(server.store, tmp_path, run=quiet).tick()
    assert verdict_of(server, sym, TEXT_SYM) == "fresh"
    assert verdict_of(server, filed, TEXT_FILE) == "fresh"
    assert counts(quiet)["symbol_log"] == 0, (
        "the anchored file is not in the diff, so tier 1 must never run: "
        f"{[a for a in quiet.calls if is_symbol_log(a)]}"
    )
    assert counts(quiet)["ls_tree"] == 1 and counts(quiet)["diff"] == 1, (
        f"one probe per distinct baseline, no more: {counts(quiet)}"
    )

    # steady state: the tip stands still, so the whole leg is a cache hit
    for _ in range(3):
        steady = RecordingRun()
        make_syncer(server.store, tmp_path, run=steady).tick()
        assert counts(steady) == {
            "ls_tree": 0,
            "diff": 0,
            "symbol_log": 0,
            "engine": 0,
        }, (
            "a quiet repo with every verdict already materialized must spawn NOTHING "
            f"for drift: {steady.calls}"
        )
    assert verdict_of(server, sym, TEXT_SYM) == "fresh"


# ── I2 ────────────────────────────────────────────────────────────────────────


def test_a_changed_file_serves_its_commits_as_evidence(rig):
    """A stale-tier hit must carry the evidence an agent can act on: the baseline it
    drifted from (``since``) and the SHAs that touched it (``commits``, newest first,
    capped at 10, with the true total in ``n_commits``). IDS ONLY — a commit subject is
    unscanned repo text and may never ride a served envelope (Law 4)."""
    origin, server, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()

    sym = write(server, TEXT_SYM, "app.py::greet")
    filed = write(server, TEXT_FILE, "app.py")
    syncer.tick()
    base = baseline_of(server.store, sym, "app.py::greet")

    subject = "SUBJECTLEAKCANARY widen greet"
    first = origin.commit("app.py", app_py(greet="return name"), subject)
    origin.push()
    syncer.tick()

    for eid, query in ((sym, TEXT_SYM), (filed, TEXT_FILE)):
        entry = evidence_of(server, eid, query)
        assert entry["verdict"] == "anchor_changed", entry
        assert entry.get("since") == base, (
            f"the served hit must name the baseline it drifted from: {entry}"
        )
        assert entry.get("commits") == [first], (
            f"the one commit that touched it must ride as its evidence: {entry}"
        )
        assert entry.get("n_commits") == 1, entry

    envelope = json.dumps(drift_of(server, sym, TEXT_SYM))
    assert "SUBJECTLEAKCANARY" not in envelope, (
        "a commit SUBJECT is unscanned repo text and must never reach a served "
        f"envelope — SHAs only: {envelope}"
    )

    # order + cap: 11 more commits on the same symbol
    shas = [first]
    for n in range(11):
        shas.append(
            origin.commit("app.py", app_py(greet=f"return name * {n}"), f"c{n}")
        )
    origin.push()
    syncer.tick()

    entry = evidence_of(server, sym, TEXT_SYM)
    assert entry["n_commits"] == 12, f"the total is honest, not the cap: {entry}"
    assert entry["commits"] == list(reversed(shas))[:10], (
        f"newest first, capped at 10: {entry}"
    )
    assert all(SHA_RE.match(c) for c in entry["commits"]), entry


# ── I3 ────────────────────────────────────────────────────────────────────────


def test_churn_elsewhere_in_the_file_is_not_drift(rig):
    """A symbol anchor claims a symbol, not a file. Edits to a sibling function, to the
    import block, or at EOF leave it ``fresh``; the discriminating pair is that the
    SAME anchor reads ``anchor_changed`` the moment its OWN body moves — which is
    exactly the body-rewrite the fingerprint engine reported ``current``."""
    origin, server, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()
    sym = write(server, TEXT_SYM, "app.py::greet")
    filed = write(server, TEXT_FILE, "app.py")
    syncer.tick()
    assert verdict_of(server, sym, TEXT_SYM) == "fresh"

    # a sibling function's body, an import block, and a trailing comment
    origin.commit("app.py", app_py(tail="\n\ndef extra():\n    return 2\n"), "sibling")
    origin.push()
    syncer.tick()
    assert verdict_of(server, sym, TEXT_SYM) == "fresh", (
        "a NEW sibling function is not this symbol's drift"
    )

    origin.commit(
        "app.py",
        "import os\n\n\n" + app_py(tail="\n# a trailing note\n"),
        "imports + eof",
    )
    origin.push()
    syncer.tick()
    assert verdict_of(server, sym, TEXT_SYM) == "fresh", (
        "an import block and a comment at EOF are not this symbol's drift"
    )
    assert verdict_of(server, filed, TEXT_FILE) == "anchor_changed", (
        "the FILE-scoped anchor's claim IS the file, so the same churn moves it"
    )

    # the discriminating half: the symbol's own body, signature untouched
    origin.commit(
        "app.py",
        "import os\n\n\n"
        + app_py(
            greet='return os.environ.get("X", "") + name', tail="\n# a trailing note\n"
        ),
        "rewrite greet body only",
    )
    origin.push()
    syncer.tick()
    assert verdict_of(server, sym, TEXT_SYM) == "anchor_changed", (
        "a rewritten BODY with an unchanged signature and unchanged callees is the "
        "drift the call-shape fingerprint could never see"
    )


# ── I4 ────────────────────────────────────────────────────────────────────────


def test_a_deleted_target_is_missing_but_an_unresolvable_one_is_not(rig):
    """Provable absence qualifies retirement; unprovable absence never does. A deleted
    symbol and a deleted file both read ``anchor_missing`` and let ``hive_prune``
    through; an anchor that never resolved at its own baseline reads ``unverifiable``
    and the gate REFUSES it."""
    origin, server, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()

    sym = write(server, TEXT_SYM, "app.py::greet")
    filed = write(server, TEXT_FILE, "store.py")
    typo = write(server, "gre3t is spelled wrong on purpose", "app.py::gre3t")
    never = write(server, "a runbook line that was never code", "the release runbook")
    syncer.tick()

    assert (
        verdict_of(server, typo, "gre3t is spelled wrong on purpose") == "unverifiable"
    )
    assert (
        verdict_of(server, never, "a runbook line that was never code")
        == "unverifiable"
    )

    origin.commit("app.py", "def bill(user):\n    return user\n", "delete greet")
    origin.push()
    git_rm(origin, "store.py")
    syncer.tick()

    assert verdict_of(server, sym, TEXT_SYM) == "anchor_missing", (
        "a deleted SYMBOL that demonstrably resolved at its baseline is missing"
    )
    assert verdict_of(server, filed, TEXT_FILE) == "anchor_missing", (
        "a deleted FILE is the maximal form of the same claim"
    )
    assert (
        verdict_of(server, typo, "gre3t is spelled wrong on purpose") == "unverifiable"
    ), (
        "an anchor git could never resolve is UNVERIFIABLE — calling it missing would "
        "qualify a retirement the evidence does not support"
    )

    for eid in (sym, filed):
        env = call(server, "hive_prune", {"episode_id": eid})
        assert env["status"] == "pruned", env
        assert server.store.get_episode(eid).trust == "deprecated"

    env = call(server, "hive_prune", {"episode_id": typo})
    assert env["status"] == "noop", env
    assert "no qualifying machine signal" in env["reason"], env
    assert server.store.get_episode(typo).trust == "provisional"

    # a RENAME is a departure from the named path, judged the same way. The memory is
    # written AFTER the file lands and is baselined at a tip that contains it — a
    # binding whose path never existed at its own baseline is `unverifiable` by I5's
    # own rule, and no departure can be measured from a path that was never there.
    origin.commit("notes.py", "def note():\n    return 1\n", "add notes")
    origin.push()
    syncer.tick()
    renamed = write(server, "notes.py holds the renamed helper", "notes.py")
    syncer.tick()
    assert verdict_of(server, renamed, "notes.py holds the renamed helper") == "fresh"
    git_mv(origin, "notes.py", "notes2.py")
    syncer.tick()
    assert (
        verdict_of(server, renamed, "notes.py holds the renamed helper")
        == "anchor_missing"
    ), "the path this memory named is gone from the tree"


def git_rm(origin: Origin, rel: str) -> None:
    from tests.sync.conftest import git

    git(origin.work, "rm", "-q", rel)
    git(origin.work, "commit", "-qm", f"remove {rel}")
    origin.push()


def git_mv(origin: Origin, src: str, dst: str) -> None:
    from tests.sync.conftest import git

    git(origin.work, "mv", src, dst)
    git(origin.work, "commit", "-qm", f"rename {src}")
    origin.push()


# ── I5 ────────────────────────────────────────────────────────────────────────


def test_every_unknown_reads_unverifiable(rig):
    """Every unknown under-claims. No baseline recorded, an unset repo watermark, a
    prose anchor, a path absent at the baseline, an injected git fault, and an
    un-materialized cache row all read ``unverifiable`` — never ``fresh``, never
    stale."""
    origin, server, tmp_path = rig
    staleness = require_module("hive.domain.staleness")
    require_method(staleness, "decide")
    require_table(server.store, "anchor_baselines")

    # (a) no watermark yet ⇒ nothing baselined, nothing claimed
    early = write(server, TEXT_SYM, "app.py::greet")
    assert baseline_rows(server.store) == [], (
        "a repo with no watermark records no baseline — a server observation it "
        "has not made yet cannot be stored"
    )
    assert verdict_of(server, early, TEXT_SYM) == "unverifiable", (
        "un-materialized cache row ⇒ the honest unknown"
    )

    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()  # the sweep fills the missing baseline and materializes

    # (b) prose + (c) a path that never existed at the baseline
    prose = write(server, "always brief the on-call before a migration", "runbook")
    absent = write(server, "later/added.py is not in the tree yet", "later/added.py")
    syncer.tick()
    assert verdict_of(server, prose, "always brief the on-call before a migration") == (
        "unverifiable"
    )
    assert verdict_of(server, absent, "later/added.py is not in the tree yet") == (
        "unverifiable"
    ), "silence about a path we never saw is not evidence that it is current"

    # (d) an injected git fault on the baseline probe
    origin.commit("app.py", app_py(greet="return name"), "move greet")
    origin.push()
    faulty = RecordingRun(
        script=[(is_ls_tree, lambda argv: _rc1(argv, "ls-tree exploded"))]
    )
    make_syncer(server.store, tmp_path, run=faulty).tick()
    assert verdict_of(server, early, TEXT_SYM) == "unverifiable", (
        "a probe fault degrades that baseline's whole group to the unknown, never "
        "to a verdict"
    )
    error = meta_value(server.store, f"sync:{REPO}:last_error")
    assert error is not None and "drift" in error, (
        f"a faulted probe must surface on the repo's own error key: {error!r}"
    )

    # ...and the next clean tick recovers the real verdict
    make_syncer(server.store, tmp_path).tick()
    assert verdict_of(server, early, TEXT_SYM) == "anchor_changed"

    # (e) the funcname attributes file is unreachable ⇒ an indented method degrades
    # to unverifiable, never to a verdict
    bare = write(server, TEXT_BARE, "store.py::repo_remove")
    make_syncer(server.store, tmp_path).tick()
    origin.commit("store.py", store_py(body="return False"), "move repo_remove")
    origin.push()
    blind = make_syncer(server.store, tmp_path)
    require_method(blind, "_attrs_file")
    blind._attrs_file = str(tmp_path / "no-such-attributes-file")
    blind.tick()
    assert verdict_of(server, bare, TEXT_BARE) == "unverifiable", (
        "without git's funcname driver an indented method cannot be resolved at all "
        "— the result is the unknown, never a verdict"
    )


def _rc1(argv, stderr: str):
    import subprocess

    return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr=stderr)


# ── I6 ────────────────────────────────────────────────────────────────────────


def test_a_new_baseline_at_a_standing_tip_is_not_read_from_the_old_row(rig):
    """The verdict is a total function of ``(repo, tip, base_tip, anchor)``. A baseline
    that lands or changes while a tip STANDS STILL must therefore move the served
    verdict — a key without ``base_tip`` cannot represent that, which is the whole of
    BUG-081."""
    origin, server, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    require_column(server.store, "anchor_drift", "base_tip")

    # written before the repo has a watermark: no baseline ⇒ unverifiable
    eid = write(server, TEXT_SYM, "app.py::greet")
    assert verdict_of(server, eid, TEXT_SYM) == "unverifiable"

    # the tip does not move again for the rest of this test
    syncer.tick()
    tip = origin.origin_sha("refs/heads/main")
    assert meta_value(server.store, f"sync:{REPO}:last_tip") == tip
    assert verdict_of(server, eid, TEXT_SYM) == "fresh", (
        "the baseline landed at the standing tip and the verdict moved with it — "
        "it must not be read off the pre-baseline row"
    )
    assert baseline_of(server.store, eid, "app.py::greet") == tip

    # two episodes on ONE anchor with DIFFERENT baselines, judged at the same tip
    origin.commit("app.py", app_py(greet="return name"), "move greet")
    origin.push()
    syncer.tick()
    tip2 = origin.origin_sha("refs/heads/main")
    later = write(
        server, "greet was rewritten and this note came after", "app.py::greet"
    )
    syncer.tick()

    assert baseline_of(server.store, eid, "app.py::greet") == tip, (
        "a recorded baseline is never moved — re-baselining would erase a break "
        "that already landed"
    )
    assert baseline_of(server.store, later, "app.py::greet") == tip2
    assert verdict_of(server, eid, TEXT_SYM) == "anchor_changed"
    assert (
        verdict_of(server, later, "greet was rewritten and this note came after")
        == "fresh"
    ), (
        "same repo, same tip, same anchor, different baseline ⇒ different verdict; "
        "one cache row cannot answer both"
    )
    keys = {
        (r["tip_sha"], r["base_tip"], r["anchor"]) for r in drift_rows(server.store)
    }
    assert (tip2, tip, "app.py::greet") in keys, keys
    assert (tip2, tip2, "app.py::greet") in keys, keys


# ── I7 ────────────────────────────────────────────────────────────────────────


def test_both_method_spellings_resolve(rig):
    """The two resolvers demand OPPOSITE spellings: git's funcname matches the
    declaration line, so a bare method name resolves and a class-qualified one does
    not. Both must work — the live store already holds qualified anchors, and turning
    those into ``anchor_missing`` would be a false stale, the one direction this system
    must never fail."""
    origin, server, tmp_path = rig
    staleness = require_module("hive.domain.staleness")
    lookups = require_method(staleness, "symbol_lookups")
    assert lookups("Store.repo_remove") == ("Store.repo_remove", "repo_remove"), (
        "the fallback is the last dotted segment, tried only after the spelling "
        "as written"
    )
    assert lookups("repo_remove") == ("repo_remove",)

    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()
    bare = write(server, TEXT_BARE, "store.py::repo_remove")
    qual = write(server, TEXT_QUAL, "store.py::Store.repo_remove")
    nested = write(
        server, "the nested spelling binds the same method", "store.py::Ns::Store.m"
    )
    module_fn = write(server, TEXT_SYM, "app.py::greet")
    syncer.tick()

    for eid, query in (
        (bare, TEXT_BARE),
        (qual, TEXT_QUAL),
        (nested, "the nested spelling binds the same method"),
        (module_fn, TEXT_SYM),
    ):
        assert verdict_of(server, eid, query) == "fresh", (
            f"every spelling must resolve before it can drift: {eid}"
        )

    origin.commit("store.py", store_py(body="return False"), "change repo_remove")
    origin.push()
    syncer.tick()

    assert verdict_of(server, bare, TEXT_BARE) == "anchor_changed"
    assert verdict_of(server, qual, TEXT_QUAL) == "anchor_changed", (
        "the class-qualified spelling git itself rejects must still reach a real "
        "verdict through the segment fallback"
    )
    assert verdict_of(server, nested, "the nested spelling binds the same method") == (
        "fresh"
    ), "Ns::Store.m falls back to `m`, whose own lines did not move"
    assert verdict_of(server, module_fn, TEXT_SYM) == "fresh"

    # residual: a bare name matching two classes resolves to the FIRST match. The
    # direction is a wrong-SPAN read inside the right file, never a false absence.
    origin.commit(
        "dup.py",
        "class A:\n    def dup(self):\n        return 1\n\n\n"
        "class B:\n    def dup(self):\n        return 2\n",
        "two dups",
    )
    origin.push()
    syncer.tick()
    ambiguous = write(server, "dup exists twice on purpose", "dup.py::dup")
    syncer.tick()
    assert verdict_of(server, ambiguous, "dup exists twice on purpose") in (
        "fresh",
        "anchor_changed",
    ), "an ambiguous bare name still reaches a real verdict, never a false missing"


# ── I8 ────────────────────────────────────────────────────────────────────────


def test_branch_semantics_are_unchanged(rig):
    """The two-tier truth is untouched by the new producer: verdicts are computed at
    the canonical tip plus every DECLARED and DEMANDED ref, an off-line stale verdict
    routes to the advisory ``branch_scoped``, an on-line one rides verbatim, an
    unresolved ref degrades to ``unverifiable`` while recording demand, and a demanded
    ref appears in ``ref_tips`` on the next tick."""
    origin, server, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()

    declared = write(server, TEXT_SYM, "app.py::greet", repos=[f"{REPO}@feature"])
    plain = write(server, TEXT_FILE, "app.py")
    syncer.tick()

    # a queried branch that was never materialized degrades AND records demand
    assert verdict_of(server, plain, TEXT_FILE, repos=[f"{REPO}@ghost"]) == (
        "unverifiable"
    )
    requested = {
        r["ref"]
        for r in server.store.conn.execute(
            "SELECT ref FROM ref_requests WHERE repo=?", (REPO,)
        )
    }
    assert "ghost" in requested, f"the query's demand must be recorded: {requested}"

    # the canonical line breaks; the memory declared 'feature', so it routes advisory
    origin.commit("app.py", app_py(greet="return name"), "canonical break")
    origin.push()
    syncer.tick()
    assert verdict_of(server, declared, TEXT_SYM) == "branch_scoped", (
        "a stale-tier verdict read off a line the memory never named degrades to "
        "the advisory branch_scoped"
    )
    assert verdict_of(server, plain, TEXT_FILE) == "anchor_changed", (
        "a memory that declares no line rides its verdict verbatim"
    )

    # the declared ref is on the work list even with no recall demand at all
    origin.push("HEAD:refs/heads/feature")
    syncer.tick()
    tips = {
        r["ref"]: r["tip_sha"]
        for r in server.store.conn.execute(
            "SELECT ref, tip_sha FROM ref_tips WHERE repo=?", (REPO,)
        )
    }
    assert tips.get("feature") == origin.origin_sha("refs/heads/feature"), (
        f"a DECLARED ref earns coverage whether or not anyone recalled it: {tips}"
    )
    assert verdict_of(server, declared, TEXT_SYM, repos=[f"{REPO}@feature"]) == (
        "anchor_changed"
    ), "read on its OWN declared line the verdict rides verbatim, never softened"

    # a deleted branch loses its watermark and degrades — never a stale tip
    from tests.sync.conftest import git

    git(origin.bare, "update-ref", "-d", "refs/heads/feature")
    syncer.tick()
    assert verdict_of(server, declared, TEXT_SYM, repos=[f"{REPO}@feature"]) == (
        "unverifiable"
    ), "a deleted branch resolves to no tip at all — the honest unknown"


# ── I9 ────────────────────────────────────────────────────────────────────────


def test_the_tick_spawns_only_the_git_calls_the_ladder_needs(rig):
    """The cost model IS the contract: two plumbing reads per DISTINCT baseline, one
    ``log -L`` per symbol anchor whose file actually moved, and nothing else. No engine
    subprocess appears in any argv, ever."""
    origin, server, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()

    # one baseline, five anchors, nothing drifted
    anchors = [
        "app.py::greet",
        "app.py::bill",
        "app.py",
        "store.py::Store.m",
        "store.py",
    ]
    ids = [write(server, f"note {i} about {a}", a) for i, a in enumerate(anchors)]
    first = RecordingRun()
    make_syncer(server.store, tmp_path, run=first).tick()
    assert counts(first)["ls_tree"] == 1, counts(first)
    assert counts(first)["diff"] == 1, counts(first)
    assert counts(first)["symbol_log"] == 0, (
        "nothing moved, so no anchor reaches tier 1: "
        f"{[a for a in first.calls if is_symbol_log(a)]}"
    )
    assert counts(first)["engine"] == 0
    assert all(
        verdict_of(server, eid, f"note {i} about {a}") == "fresh"
        for i, (eid, a) in enumerate(zip(ids, anchors))
    )

    # three DISTINCT baselines, then a commit that moves ONE file with two symbol
    # anchors on it
    origin.commit("notes.py", "def note():\n    return 1\n", "b2")
    origin.push()
    make_syncer(server.store, tmp_path).tick()
    write(server, "the second baseline note", "app.py::greet")
    origin.commit("notes.py", "def note():\n    return 2\n", "b3")
    origin.push()
    make_syncer(server.store, tmp_path).tick()
    write(server, "the third baseline note", "app.py::bill")

    origin.commit("app.py", app_py(greet="return name", tail="\n# t\n"), "move app.py")
    origin.push()
    drifted = RecordingRun()
    make_syncer(server.store, tmp_path, run=drifted).tick()

    bases = {r["base_tip"] for r in drift_rows(server.store) if r["base_tip"]}
    assert len(bases) == 3, f"three distinct baselines are live: {sorted(bases)}"
    assert counts(drifted)["ls_tree"] == 3, counts(drifted)
    assert counts(drifted)["diff"] == 3, counts(drifted)
    assert counts(drifted)["engine"] == 0, (
        f"the drift path spawns no engine at all: {drifted.calls}"
    )
    logs = [a for a in drifted.calls if is_symbol_log(a)]
    assert logs, "the file moved, so its symbol anchors must reach tier 1"
    assert all(any("app.py" in tok for tok in a) for a in logs), (
        f"tier 1 runs only for anchors whose OWN file moved: {logs}"
    )
    assert not any("store.py" in tok for a in logs for tok in a), (
        f"store.py did not move; nothing under it may spawn a log: {logs}"
    )


# ── I10 ───────────────────────────────────────────────────────────────────────


def test_deregistration_forgets_the_baselines_too(rig):
    """A baseline is a SERVER OBSERVATION, so it leaves with the feed — while the
    writer's declarations stay. A re-registered repo re-baselines at the NEW tip
    rather than inheriting a dead incarnation's."""
    origin, server, tmp_path = rig
    syncer = make_syncer(server.store, tmp_path)
    syncer.tick()
    eid = write(server, TEXT_SYM, "app.py::greet", repos=[f"{REPO}@main"])
    syncer.tick()
    old_tip = origin.origin_sha("refs/heads/main")
    assert baseline_of(server.store, eid, "app.py::greet") == old_tip
    assert drift_rows(server.store), "precondition: verdicts were materialized"

    assert server.store.repo_remove(REPO) is True
    assert baseline_rows(server.store) == [], (
        "a baseline is a server observation of a feed that no longer exists — it "
        "must leave with ref_tips, anchor_drift and the sync:* meta"
    )
    for table in ("anchor_drift", "ref_tips", "ref_requests"):
        rows = list(
            server.store.conn.execute(f"SELECT 1 FROM {table} WHERE repo=?", (REPO,))
        )
        assert rows == [], f"{table} still carries the dead incarnation: {rows}"
    assert meta_value(server.store, f"sync:{REPO}:last_tip") is None
    survivors = list(
        server.store.conn.execute(
            "SELECT 1 FROM episode_anchors WHERE repo=? AND anchor!=''", (REPO,)
        )
    )
    assert survivors, "the WRITER's declared scope survives deregistration"
    assert list(
        server.store.conn.execute("SELECT 1 FROM episode_refs WHERE repo=?", (REPO,))
    )

    # re-register the SAME url at a NEW tip: the baseline is the new observation
    origin.commit("app.py", app_py(greet="return name"), "post-deregistration break")
    origin.push()
    new_tip = origin.origin_sha("refs/heads/main")
    server.store.repo_add(
        name=REPO, url=origin.url, canonical_ref="main", token_env="", added_ts=1
    )
    make_syncer(server.store, tmp_path).tick()
    assert baseline_of(server.store, eid, "app.py::greet") == new_tip, (
        "a re-registered repo re-baselines from scratch at the tip it can actually "
        "observe — never at a dead incarnation's"
    )
    assert verdict_of(server, eid, TEXT_SYM) == "fresh", (
        "nothing has moved since the NEW baseline, and the break that predates it "
        "is not this incarnation's evidence"
    )


# ── I11 ───────────────────────────────────────────────────────────────────────


def test_the_drift_legs_prior_guarantees_survive(rig):
    """Nothing the old producer guaranteed regresses: a retired memory's anchor leaves
    the work list and its cached rows are dropped even at a live tip, a faulted repo is
    isolated from its healthy sibling, and every advertised ``hive_health`` sync field
    still has a writer."""
    origin, server, tmp_path = rig
    store = server.store
    work_list = require_method(store, "anchor_work_list")

    syncer = make_syncer(store, tmp_path)
    syncer.tick()
    live = write(server, TEXT_SYM, "app.py::greet")
    doomed = write(server, TEXT_FILE, "store.py")
    syncer.tick()
    assert {a for _e, a, _b in work_list(REPO)} == {"app.py::greet", "store.py"}

    # a real machine signal retires `doomed`, and it leaves BOTH the work list and
    # the cache at a tip that is still canonical
    git_rm(origin, "store.py")
    syncer.tick()
    assert verdict_of(server, doomed, TEXT_FILE) == "anchor_missing"
    assert call(server, "hive_prune", {"episode_id": doomed})["status"] == "pruned"
    syncer.tick()
    assert {a for _e, a, _b in work_list(REPO)} == {"app.py::greet"}, (
        "a retired memory's anchor must stop consuming the work list forever"
    )
    assert not any(r["anchor"] == "store.py" for r in drift_rows(store)), (
        "and its cached rows must be dropped even at a still-canonical tip"
    )
    assert not any(int(r["episode_id"]) == doomed for r in baseline_rows(store)), (
        "a retired memory's BASELINE leaves with its anchor — the drift_prune twin"
    )
    assert verdict_of(server, live, TEXT_SYM) == "fresh"

    # a second repo faults; the healthy one is untouched
    other = Origin(tmp_path / "remote2")
    store.repo_add(
        name="beta", url=other.url, canonical_ref="main", token_env="", added_ts=0
    )
    from tests.sync.conftest import git

    git(other.bare, "update-ref", "-d", "refs/heads/main")
    make_syncer(store, tmp_path).tick()
    assert meta_value(store, "sync:beta:last_error") is not None
    assert meta_value(store, f"sync:{REPO}:last_error") is None, (
        "one repo's fault never reaches another's error key"
    )
    assert verdict_of(server, live, TEXT_SYM) == "fresh"

    # every advertised health field still has a writer
    from hive.app.sync_keys import FLEET_KEY_BUILDERS, KEY_BUILDERS

    health = call(server, "hive_health", {"include_census_health": True})
    repo_block = (health.get("census_health") or {}).get("repos", {}).get(REPO, {})
    block = repo_block.get("sync") or {}
    # The served-SLOT check is the runtime half of "every advertised field still has a
    # writer"; the WRITER half is static (tests/app/test_sync_keys.py asserts over
    # hive.app.sync's AST that the daemon still calls every builder). Do not re-add a
    # runtime "every key is non-null" loop: `last_error`'s healthy value is ABSENCE, so
    # such a loop demands non-null for the very key line 915 above requires to be null.
    assert set(KEY_BUILDERS) <= set(block), (
        f"an advertised sync field lost its served slot: {sorted(block)}"
    )
    fleet = (health.get("census_health") or {}).get("fleet", {})
    assert set(FLEET_KEY_BUILDERS) <= set(fleet), sorted(fleet)


def test_the_frozen_suite_reaches_a_real_sqlite_store(rig):
    """A guard on the guards: this file must exercise a REAL store, so a
    ``require_*`` that silently stopped guarding anything is visible."""
    _origin, server, _tmp = rig
    assert isinstance(server.store.conn, sqlite3.Connection)
