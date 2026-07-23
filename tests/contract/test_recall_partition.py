"""CT-3 — recall partitions by repo ∪ general, full-K, gate-preserved (plan §4
intent 3, §3.3).

``hive_recall {query, repos?, anchor_prefix?}``: served set = memories whose
repo-set intersects the scope, ∪ general; omitted repos ⇒ GLOBAL search; unknown
repo name ⇒ clean refusal; ``anchor_prefix`` narrows the anchored population only.
Scenario matrix: scope hit; cross-repo exclusion; general always in; global
default; multi-repo memory served from either scope; abstain unchanged when the
scoped field is weak (τ preserved); anchor_prefix filter; empty partition ⇒
abstain/empty.
"""

from __future__ import annotations

import pytest

from tests.contract.conftest import (
    is_error,
    make_rig,
    payload,
    recall,
    register_repo,
    served_ids,
    write_ok,
)
from tests.fakes._fakes import FakeClusterProvider

Q = "cid=50 how does the shared retry helper behave"


@pytest.fixture
def bare(tmp_path):
    """An unseeded clustered rig (seeding happens IN-test so an unbuilt v3
    surface fails as an assertion, never as a fixture error)."""
    return make_rig(tmp_path, embedder=FakeClusterProvider(d=32))


def seed_partitions(rig):
    """One memory per partition class: alpha-anchored (x2), beta-scoped, general,
    and alpha+beta shared — each in its OWN embedding cluster (genuinely
    distinct content, pairwise cos ≈ 0, safely under the default near-dup
    ``dup_tau`` floor), so the served set is decided by PARTITION MEMBERSHIP,
    never by dedup collapse. The general memory alone shares the query's
    cluster (cid=50): it belongs to every partition, so it clears ``tau_serve``
    and opens the gate under every scope, and the in-partition rest serve
    full-K alongside it (default ``recall_top_n`` exceeds any partition here).
    """
    for name in ("alpha", "beta", "gamma"):
        register_repo(rig.store, name, f"https://example.invalid/{name}.git")
    ids = {
        "a": write_ok(
            rig.server,
            "cid=51 alpha: the retry helper doubles its backoff",
            anchors=[{"repo": "alpha", "anchor": "hive/app/retry.py::backoff"}],
        ),
        "a2": write_ok(
            rig.server,
            "cid=52 alpha: the retry helper caps at five attempts",
            anchors=[{"repo": "alpha", "anchor": "hive/domain/policy.py::cap"}],
        ),
        "b": write_ok(
            rig.server,
            "cid=53 beta: the retry helper jitters its delay",
            repos=["beta"],
        ),
        "g": write_ok(rig.server, "cid=50 general: retries must be idempotent"),
        "ab": write_ok(
            rig.server,
            "cid=54 shared: the retry helper is vendored in both",
            repos=["alpha", "beta"],
        ),
    }
    return ids


def test_scope_hit_and_cross_repo_exclusion(bare):
    rig = bare
    ids = seed_partitions(rig)
    env = recall(rig.server, Q, repos=["alpha"])
    got = set(served_ids(env))
    assert ids["a"] in got, f"an alpha-anchored memory serves in alpha scope: {env}"
    assert ids["b"] not in got, (
        "a beta-only memory must be EXCLUDED from an alpha-scoped recall"
    )


def test_general_always_in_scope(bare):
    rig = bare
    ids = seed_partitions(rig)
    env = recall(rig.server, Q, repos=["alpha"])
    assert ids["g"] in set(served_ids(env)), (
        "a general (repo-less) memory serves under every scope"
    )


def test_omitted_repos_is_global(bare):
    rig = bare
    ids = seed_partitions(rig)
    env = recall(rig.server, Q)
    got = set(served_ids(env))
    assert {ids["a"], ids["b"], ids["g"], ids["ab"]} <= got, (
        f"omitted repos means GLOBAL search — every partition serves: {env}"
    )


def test_multi_repo_memory_serves_from_either_scope(bare):
    rig = bare
    ids = seed_partitions(rig)
    assert ids["ab"] in set(served_ids(recall(rig.server, Q, repos=["alpha"])))
    assert ids["ab"] in set(served_ids(recall(rig.server, Q, repos=["beta"])))


def test_scoped_weak_field_abstains_tau_preserved(bare):
    rig = bare
    seed_partitions(rig)
    # cid=6 exists ONLY as an alpha memory; a beta-scoped recall must gate on the
    # FILTERED sims and abstain (gating on unfiltered sims is the named mutation).
    only_alpha = write_ok(
        rig.server, "cid=6 alpha-only: the migration lock is advisory", repos=["alpha"]
    )
    env = recall(rig.server, "cid=6 what is the migration lock posture", repos=["beta"])
    assert env.get("abstained") is True, (
        f"a strong OUT-OF-SCOPE match must not rescue a weak scoped field: {env}"
    )
    assert only_alpha not in served_ids(env)
    assert served_ids(env) == []


def test_off_distribution_query_abstains_in_scope(bare):
    rig = bare
    seed_partitions(rig)
    env = recall(
        rig.server, "cid=9 completely unrelated cooking question", repos=["alpha"]
    )
    assert env.get("abstained") is True
    assert served_ids(env) == []


def test_unknown_repo_name_is_clean_refusal(bare):
    rig = bare
    seed_partitions(rig)
    from tests.contract.conftest import call

    resp = call(rig.server, "hive_recall", {"query": Q, "repos": ["ghost"]})
    env = payload(resp)
    assert env.get("status") == "refused", (
        f"an unregistered repo name refuses clean: {env}"
    )
    assert not is_error(resp)


def test_anchor_prefix_narrows_anchored_hits_only(bare):
    rig = bare
    ids = seed_partitions(rig)
    env = recall(rig.server, Q, repos=["alpha"], anchor_prefix="hive/app")
    got = set(served_ids(env))
    assert ids["a"] in got, "an anchor under the prefix is kept"
    assert ids["a2"] not in got, (
        "an anchored memory whose every anchor is OUTSIDE the prefix is filtered"
    )
    assert ids["g"] in got, (
        "general memories are KEPT — the prefix narrows the anchored population only"
    )


def test_empty_partition_abstains(bare):
    rig = bare
    seed_partitions(rig)
    # cid=8 exists only in alpha; gamma has nothing, and no cid=8 general exists
    write_ok(rig.server, "cid=8 alpha-only fact about the scheduler", repos=["alpha"])
    env = recall(rig.server, "cid=8 what about the scheduler", repos=["gamma"])
    assert env.get("abstained") is True
    assert served_ids(env) == []
