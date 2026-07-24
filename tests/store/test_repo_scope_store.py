"""SqliteEpisodeStore.servable_scopes — the servable-field partition map (v3 §3.6):
every SERVABLE id → (repo scope frozenset, (repo, anchor) pairs), decided by the SAME
``lifecycle.is_servable`` rule as the recall scan (the map and the index can never
disagree about the field). ``provisional_ids`` — the established rung's candidate feed.
The REAL adapter is conformance-tested against the RepoScopeReader port (the
protocol-widening lesson: never just the fake)."""

from __future__ import annotations

import numpy as np

from hive.adapters.index_exhaustive import ExhaustiveCosineIndex
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.lifecycle import (
    DEPRECATED,
    ESTABLISHED,
    PROVISIONAL,
    QUARANTINED,
)
from hive.domain.ports import RepoScopeReader

DIM = 4
NOW = 1_000
P_TTL = 200


def _unit(i: int) -> np.ndarray:
    return np.eye(DIM, dtype=np.float32)[i]


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"), index=ExhaustiveCosineIndex(DIM))


def _materialize(
    s: SqliteEpisodeStore,
    text: str,
    *,
    trust: str,
    vec_i: int = 0,
    ts: int = 10,
    last_active=None,
    anchors=(),
    repos=(),
) -> int:
    eid, _ = s.stage(
        text=text,
        weight=1.0,
        proposed_by="w",
        ts=ts,
        anchors=list(anchors),
        repos=list(repos),
    )
    assert s.complete(
        eid,
        _unit(vec_i),
        expected_version=0,
        trust=trust,
        last_active_ts=ts if last_active is None else last_active,
    )
    return eid


# ── port conformance: the REAL adapter, not just a fake ────────────────────────
def test_real_store_satisfies_repo_scope_reader():
    assert isinstance(SqliteEpisodeStore(connect(":memory:")), RepoScopeReader)


# ── the partition map ──────────────────────────────────────────────────────────
def test_scopes_cover_exactly_the_servable_set():
    s = _store()
    est = _materialize(s, "established", trust=ESTABLISHED, vec_i=0, last_active=0)
    fresh = _materialize(
        s, "fresh provisional", trust=PROVISIONAL, vec_i=1, last_active=NOW - 1
    )
    lapsed = _materialize(
        s, "lapsed provisional", trust=PROVISIONAL, vec_i=2, last_active=NOW - P_TTL - 1
    )
    quar = _materialize(s, "quarantined", trust=QUARANTINED, vec_i=3)
    dep = _materialize(s, "deprecated", trust=ESTABLISHED, vec_i=3)
    s.set_trust(dep, DEPRECATED, now=NOW)
    pend, _ = s.stage(text="pending", weight=1.0, proposed_by="w")
    scopes = s.servable_scopes(now=NOW, provisional_ttl_s=P_TTL)
    assert set(scopes) == {est, fresh}
    for absent in (lapsed, quar, dep, pend):
        assert absent not in scopes  # absence only ever means non-servable


def test_scope_is_the_union_of_anchor_repos_and_declared_scope():
    s = _store()
    eid = _materialize(
        s,
        "scoped fact",
        trust=ESTABLISHED,
        anchors=[("beta", "b.py::g"), ("alpha", "a.py::f")],
        repos=["gamma"],
    )
    scope, pairs = s.servable_scopes(now=NOW, provisional_ttl_s=P_TTL)[eid]
    assert scope == frozenset({"alpha", "beta", "gamma"})
    assert pairs == (("alpha", "a.py::f"), ("beta", "b.py::g"))  # (repo, anchor) order
    assert isinstance(scope, frozenset)


def test_general_memory_reads_explicit_empty_scope():
    s = _store()
    eid = _materialize(s, "a general fact", trust=ESTABLISHED)
    assert s.servable_scopes(now=NOW, provisional_ttl_s=P_TTL)[eid] == (frozenset(), ())


def test_scope_only_membership_has_no_anchor_pairs():
    s = _store()
    eid = _materialize(
        s, "repo-scoped, no code binding", trust=ESTABLISHED, repos=["alpha"]
    )
    scope, pairs = s.servable_scopes(now=NOW, provisional_ttl_s=P_TTL)[eid]
    assert scope == frozenset({"alpha"}) and pairs == ()


def test_empty_store_reads_empty_map():
    assert _store().servable_scopes(now=NOW, provisional_ttl_s=P_TTL) == {}


# ── provisional_ids: the established rung's feed ───────────────────────────────
def test_provisional_ids_returns_materialized_provisional_only():
    s = _store()
    prov_a = _materialize(s, "provisional a", trust=PROVISIONAL, vec_i=0)
    prov_b = _materialize(s, "provisional b", trust=PROVISIONAL, vec_i=1)
    _materialize(s, "established", trust=ESTABLISHED, vec_i=2)
    _materialize(s, "quarantined", trust=QUARANTINED, vec_i=3)
    s.stage(text="pending", weight=1.0, proposed_by="w")
    assert s.provisional_ids() == sorted([prov_a, prov_b])
