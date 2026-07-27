"""``anchor_baselines`` — the per-binding staleness baseline, and the ONE work list
both the baseline sweep and the drift materializer read.

The invariant under test is narrow and load-bearing: a recorded baseline is NEVER
moved. Re-baselining an anchor at a later tip erases whatever broke between the two,
and the memory then reads ``fresh`` forever about code that changed under it.
"""

from __future__ import annotations

import numpy as np
import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore

DIM = 4
REPO = "alpha"
TIP0 = "0" * 40
TIP1 = "1" * 40


@pytest.fixture
def store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:", check_same_thread=False))


def seed(store: SqliteEpisodeStore, text: str, anchors, *, trust="provisional") -> int:
    eid, _ = store.stage(
        text=text,
        weight=1.0,
        proposed_by="w",
        ts=10,
        polarity="neutral",
        anchors=list(anchors),
        repos=[],
    )
    assert store.complete(
        eid,
        np.eye(DIM, dtype=np.float32)[0],
        expected_version=0,
        trust=trust,
        last_active_ts=10,
    )
    return eid


def rows(store: SqliteEpisodeStore) -> list[tuple]:
    return [
        (int(r["episode_id"]), r["repo"], r["anchor"], r["base_tip"])
        for r in store.conn.execute(
            "SELECT * FROM anchor_baselines ORDER BY episode_id, repo, anchor"
        )
    ]


# ── insert-if-absent ──────────────────────────────────────────────────────────


def test_a_recorded_baseline_is_never_moved(store):
    """marker 2's test: an upsert here would record the POST-change tip as the
    reference and freeze ``fresh`` for a break that already landed."""
    eid = seed(store, "note", [(REPO, "a.py::f")])
    assert store.anchor_baseline_put([(eid, REPO, "a.py::f", TIP0, 1)]) == 1
    assert store.anchor_baseline_put([(eid, REPO, "a.py::f", TIP1, 2)]) == 0, (
        "a second write must be a no-op, not an update"
    )
    assert rows(store) == [(eid, REPO, "a.py::f", TIP0)], (
        "the FIRST observation is the baseline, forever"
    )


def test_an_unwatermarked_repo_records_nothing(store):
    """An empty base_tip is not an observation. Storing it would occupy the key the
    real observation must later fill, which is the same freeze by another route."""
    eid = seed(store, "note", [(REPO, "a.py::f")])
    assert store.anchor_baseline_put([(eid, REPO, "a.py::f", "", 1)]) == 0
    assert rows(store) == []
    assert store.anchor_baseline_put([(eid, REPO, "a.py::f", TIP0, 2)]) == 1
    assert rows(store) == [(eid, REPO, "a.py::f", TIP0)]


def test_put_is_batched_and_idempotent(store):
    a = seed(store, "note a", [(REPO, "a.py")])
    b = seed(store, "note b", [(REPO, "b.py")])
    batch = [(a, REPO, "a.py", TIP0, 1), (b, REPO, "b.py", TIP0, 1)]
    assert store.anchor_baseline_put(batch) == 2
    assert store.anchor_baseline_put(batch) == 0
    assert store.anchor_baseline_put([]) == 0


def test_two_episodes_on_one_anchor_keep_separate_baselines(store):
    """The key is (episode, repo, anchor): two memories about the same symbol,
    written at different tips, are measured from different commits."""
    a = seed(store, "note a", [(REPO, "a.py::f")])
    b = seed(store, "note b", [(REPO, "a.py::f")])
    store.anchor_baseline_put([(a, REPO, "a.py::f", TIP0, 1)])
    store.anchor_baseline_put([(b, REPO, "a.py::f", TIP1, 2)])
    assert rows(store) == [(a, REPO, "a.py::f", TIP0), (b, REPO, "a.py::f", TIP1)]


# ── the work list ─────────────────────────────────────────────────────────────


def test_work_list_joins_the_baseline_and_defaults_to_empty(store):
    eid = seed(store, "note", [(REPO, "a.py::f"), (REPO, "b.py")])
    assert store.anchor_work_list(REPO) == [(eid, "a.py::f", ""), (eid, "b.py", "")]
    store.anchor_baseline_put([(eid, REPO, "a.py::f", TIP0, 1)])
    assert store.anchor_work_list(REPO) == [(eid, "a.py::f", TIP0), (eid, "b.py", "")]


def test_work_list_orders_by_episode_then_anchor(store):
    first = seed(store, "note 1", [(REPO, "z.py"), (REPO, "a.py")])
    second = seed(store, "note 2", [(REPO, "m.py")])
    assert [e for e, _a, _b in store.anchor_work_list(REPO)] == [
        first,
        first,
        second,
    ]
    assert [a for _e, a, _b in store.anchor_work_list(REPO)] == [
        "a.py",
        "z.py",
        "m.py",
    ]


def test_work_list_excludes_scope_only_rows_and_other_repos(store):
    eid = seed(store, "note", [(REPO, "a.py"), (REPO, ""), ("beta", "b.py")])
    assert store.anchor_work_list(REPO) == [(eid, "a.py", "")]
    assert store.anchor_work_list("beta") == [(eid, "b.py", "")]


def test_a_retired_memory_leaves_the_work_list(store):
    live = seed(store, "live", [(REPO, "a.py")])
    dead = seed(store, "dead", [(REPO, "b.py")])
    store.conn.execute("UPDATE episodes SET trust='deprecated' WHERE id=?", (dead,))
    assert store.anchor_work_list(REPO) == [(live, "a.py", "")]


def test_a_pending_episode_is_not_yet_work(store):
    eid, _ = store.stage(
        text="pending",
        weight=1.0,
        proposed_by="w",
        ts=10,
        polarity="neutral",
        anchors=[(REPO, "a.py")],
        repos=[],
    )
    assert store.anchor_work_list(REPO) == []
    assert eid


# ── prune + the deregistration sweep ──────────────────────────────────────────


def test_prune_keeps_exactly_the_live_pairs(store):
    a = seed(store, "note a", [(REPO, "a.py")])
    b = seed(store, "note b", [(REPO, "b.py")])
    store.anchor_baseline_put([(a, REPO, "a.py", TIP0, 1), (b, REPO, "b.py", TIP0, 1)])
    assert store.anchor_baselines_prune(REPO, [(a, "a.py")]) == 1
    assert rows(store) == [(a, REPO, "a.py", TIP0)]
    assert store.anchor_baselines_prune(REPO, []) == 1
    assert rows(store) == []


def test_prune_never_reaches_another_repo(store):
    eid = seed(store, "note", [(REPO, "a.py"), ("beta", "a.py")])
    store.anchor_baseline_put(
        [(eid, REPO, "a.py", TIP0, 1), (eid, "beta", "a.py", TIP0, 1)]
    )
    assert store.anchor_baselines_prune(REPO, []) == 1
    assert rows(store) == [(eid, "beta", "a.py", TIP0)]


def test_deregistration_forgets_the_baselines(store):
    eid = seed(store, "note", [(REPO, "a.py")])
    store.repo_add(name=REPO, url="https://x.invalid/a.git", added_ts=0)
    store.anchor_baseline_put([(eid, REPO, "a.py", TIP0, 1)])
    assert store.repo_remove(REPO) is True
    assert rows(store) == [], (
        "a baseline is a server observation of a feed that no longer exists"
    )
    assert store.anchor_work_list(REPO) == [(eid, "a.py", "")], (
        "the WRITER's binding survives — only the observation left"
    )


# ── the symbol resolution that rides the baseline ─────────────────────────────


def test_a_new_baseline_starts_unprobed(store):
    """Unprobed is the only honest starting state: nothing has asked git yet, and the
    ladder must read that as the unknown rather than as either answer."""
    eid = seed(store, "note", [(REPO, "a.py::f")])
    store.anchor_baseline_put([(eid, REPO, "a.py::f", TIP0, 1)])
    assert store.anchor_symbol_probes(REPO) == {(TIP0, "a.py::f"): -1}


def test_a_recorded_symbol_resolution_is_never_moved(store):
    """The write-once marker's test. Whether a symbol existed at a given commit cannot
    change, so a second write can only be a transient failure to resolve (a missing
    funcname driver, an unreadable mirror) overwriting a real observation — which would
    turn a live binding into a false ``anchor_missing``."""
    eid = seed(store, "note", [(REPO, "a.py::f")])
    store.anchor_baseline_put([(eid, REPO, "a.py::f", TIP0, 1)])
    assert store.anchor_symbol_ok_put(REPO, [(TIP0, "a.py::f", 1)]) == 1
    assert store.anchor_symbol_ok_put(REPO, [(TIP0, "a.py::f", 0)]) == 0
    assert store.anchor_symbol_probes(REPO) == {(TIP0, "a.py::f"): 1}


def test_probes_are_keyed_by_baseline_not_by_episode(store):
    """The verdict cache is keyed by ``(base_tip, anchor)``, so the resolution must
    collapse onto the same key — and two episodes sharing a baseline share the answer,
    while the same anchor at a DIFFERENT baseline is a different question."""
    a = seed(store, "note a", [(REPO, "a.py::f")])
    b = seed(store, "note b", [(REPO, "a.py::f")])
    c = seed(store, "note c", [(REPO, "a.py::f")])
    store.anchor_baseline_put(
        [
            (a, REPO, "a.py::f", TIP0, 1),
            (b, REPO, "a.py::f", TIP0, 1),
            (c, REPO, "a.py::f", TIP1, 1),
        ]
    )
    assert store.anchor_symbol_ok_put(REPO, [(TIP0, "a.py::f", 1)]) == 2
    assert store.anchor_symbol_probes(REPO) == {
        (TIP0, "a.py::f"): 1,
        (TIP1, "a.py::f"): -1,
    }


def test_probes_fold_disagreeing_rows_onto_the_weaker_claim(store):
    """Two rows on one key cannot legitimately disagree, so if they ever do the fold
    must pick the answer that claims less — never the one that would grant a verdict."""
    a = seed(store, "note a", [(REPO, "a.py::f")])
    b = seed(store, "note b", [(REPO, "a.py::f")])
    store.anchor_baseline_put(
        [(a, REPO, "a.py::f", TIP0, 1), (b, REPO, "a.py::f", TIP0, 1)]
    )
    store.conn.execute(
        "UPDATE anchor_baselines SET symbol_ok=1 WHERE episode_id=?", (a,)
    )
    assert store.anchor_symbol_probes(REPO) == {(TIP0, "a.py::f"): -1}
    store.conn.execute(
        "UPDATE anchor_baselines SET symbol_ok=0 WHERE episode_id=?", (b,)
    )
    assert store.anchor_symbol_probes(REPO) == {(TIP0, "a.py::f"): 0}


def test_probes_never_reach_another_repo_or_an_unbaselined_row(store):
    eid = seed(store, "note", [(REPO, "a.py::f"), ("beta", "a.py::f")])
    store.anchor_baseline_put(
        [(eid, REPO, "a.py::f", TIP0, 1), (eid, "beta", "a.py::f", TIP1, 1)]
    )
    assert store.anchor_symbol_ok_put("beta", [(TIP0, "a.py::f", 1)]) == 0
    assert store.anchor_symbol_probes(REPO) == {(TIP0, "a.py::f"): -1}
    assert store.anchor_symbol_ok_put(REPO, []) == 0


# ── the recall-side read ──────────────────────────────────────────────────────


def test_baselines_get_is_scoped_to_one_episode(store):
    a = seed(store, "note a", [(REPO, "a.py::f")])
    b = seed(store, "note b", [(REPO, "a.py::f")])
    store.anchor_baseline_put(
        [(a, REPO, "a.py::f", TIP0, 1), (b, REPO, "a.py::f", TIP1, 1)]
    )
    assert store.anchor_baselines_get(a, REPO, ["a.py::f"]) == {"a.py::f": TIP0}
    assert store.anchor_baselines_get(b, REPO, ["a.py::f"]) == {"a.py::f": TIP1}
    assert store.anchor_baselines_get(a, REPO, []) == {}
    assert store.anchor_baselines_get(a, REPO, ["absent.py"]) == {}
