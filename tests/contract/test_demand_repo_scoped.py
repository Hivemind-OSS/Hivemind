"""CT-2 — capture quarantines then demand-promotes WITHIN ITS PARTITION (plan §4
intent 2, §3.6).

A capture scoped to repo A promotes to provisional on ``demand_m`` scope-matching
misses from >=1 other identity with no partition competitor; misses scoped to repo
B never count. Scenario matrix: promote in-scope; B-scoped misses don't promote;
global misses count for any scope; general candidate promoted by any miss;
competitor in another repo does NOT veto; competitor in-partition vetoes;
self-demand refused.
"""

from __future__ import annotations

from hive.domain.lifecycle import PROVISIONAL, QUARANTINED
from tests.contract.conftest import (
    capture,
    ident,
    make_rig,
    recall,
    register_repo,
    trust_of,
    write_ok,
)
from tests.fakes._fakes import FakeClusterProvider

WRITER = ident("writer-w")
ASKER = ident("asker-x")
DEMAND_M = 2


def _rig(tmp_path, *, cluster: bool = False):
    kw = {"autonomy": {"demand_m": DEMAND_M}}
    if cluster:
        return make_rig(tmp_path, embedder=FakeClusterProvider(d=32), **kw)
    return make_rig(tmp_path, **kw)


def _register(rig):
    register_repo(rig.store, "alpha", "https://example.invalid/alpha.git")
    register_repo(rig.store, "beta", "https://example.invalid/beta.git")


def _capture_scoped(rig, text: str, repos, identity=WRITER) -> int:
    env = capture(
        rig.server, text, identity=identity, **({"repos": list(repos)} if repos else {})
    )
    assert env.get("status") == "quarantined", f"capture did not quarantine: {env}"
    return env["id"]


def _miss(rig, text: str, repos=None, identity=ASKER) -> None:
    env = recall(rig.server, text, repos=repos, identity=identity)
    assert not env.get("reference_context"), (
        f"this query must MISS (nothing servable should answer it): {env}"
    )


# ── the promote path, scoped ──────────────────────────────────────────────────


def test_in_scope_misses_promote(tmp_path):
    rig = _rig(tmp_path)
    _register(rig)
    text = "alpha's deploy needs the staging bucket warmed first"
    eid = _capture_scoped(rig, text, ["alpha"])
    for _ in range(DEMAND_M):
        _miss(rig, text, repos=["alpha"])
    assert trust_of(rig.store, eid) == PROVISIONAL, (
        "scope-matching demand from another identity must promote"
    )


def test_other_repo_scoped_misses_never_promote(tmp_path):
    rig = _rig(tmp_path)
    _register(rig)
    text = "alpha's deploy needs the staging bucket warmed first"
    eid = _capture_scoped(rig, text, ["alpha"])
    for _ in range(DEMAND_M + 1):
        _miss(rig, text, repos=["beta"])
    assert trust_of(rig.store, eid) == QUARANTINED, (
        "B-scoped misses must NOT count toward an A-scoped candidate (the scope "
        "clause — dropping it is the named mutation)"
    )


def test_global_misses_count_for_any_scope(tmp_path):
    rig = _rig(tmp_path)
    _register(rig)
    text = "alpha's deploy needs the staging bucket warmed first"
    eid = _capture_scoped(rig, text, ["alpha"])
    for _ in range(DEMAND_M):
        _miss(rig, text, repos=None)  # global search — S == []
    assert trust_of(rig.store, eid) == PROVISIONAL, (
        "a global miss (no repo scope) matches every candidate scope"
    )


def test_general_candidate_promoted_by_scoped_misses(tmp_path):
    rig = _rig(tmp_path)
    _register(rig)
    text = "always pin the CI base image digest"
    eid = _capture_scoped(rig, text, repos=())  # general — R == []
    for _ in range(DEMAND_M):
        _miss(rig, text, repos=["beta"])
    assert trust_of(rig.store, eid) == PROVISIONAL, (
        "a general candidate is matched by ANY scoped miss"
    )


# ── the competitor veto, partition-scoped ─────────────────────────────────────


def test_competitor_in_other_repo_does_not_veto(tmp_path):
    rig = _rig(tmp_path, cluster=True)
    _register(rig)
    # a servable near-duplicate, but in repo beta — partition-incompatible
    write_ok(rig.server, "cid=3 the retry backoff doubles per attempt", repos=["beta"])
    text = "cid=3 retry backoff doubling policy for alpha"
    eid = _capture_scoped(rig, text, ["alpha"])
    for _ in range(DEMAND_M):
        _miss(rig, text, repos=["alpha"])
    assert trust_of(rig.store, eid) == PROVISIONAL, (
        "a near-dup competitor in ANOTHER repo must not veto an alpha promotion "
        "(the veto is partition-scoped)"
    )


def test_competitor_in_partition_vetoes(tmp_path):
    rig = _rig(tmp_path, cluster=True)
    _register(rig)
    # a servable near-duplicate IN alpha: scoped demand is already answered
    write_ok(rig.server, "cid=4 the retry backoff doubles per attempt", repos=["alpha"])
    text = "cid=4 retry backoff doubling policy for alpha"
    eid = _capture_scoped(rig, text, ["alpha"])
    for _ in range(DEMAND_M + 1):
        # the scoped query is ANSWERED by the servable twin (or, at minimum,
        # the in-partition competitor vetoes) — either way: no promotion
        recall(rig.server, text, repos=["alpha"], identity=ASKER)
    assert trust_of(rig.store, eid) == QUARANTINED, (
        "an in-partition servable competitor must keep the candidate quarantined"
    )


# ── anti-gaming (unchanged clause, now also partition-aware) ──────────────────


def test_self_demand_never_promotes(tmp_path):
    rig = _rig(tmp_path)
    _register(rig)
    text = "alpha's deploy needs the staging bucket warmed first"
    eid = _capture_scoped(rig, text, ["alpha"], identity=WRITER)
    for _ in range(DEMAND_M + 1):
        _miss(rig, text, repos=["alpha"], identity=WRITER)  # writer's own misses
    assert trust_of(rig.store, eid) == QUARANTINED, (
        "demand consisting solely of the writer's own misses never promotes"
    )
