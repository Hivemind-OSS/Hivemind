"""The backfill-mint store seam (duck-typed app reads/writes, no port):
``anchors_lacking_fp`` — the sweep's candidate read (empty-carrier anchor bindings of
one repo; presence-only, the store never parses a non-empty carrier's body) — and
``fill_anchor_fp`` — the ONE-tx absent-only merge into ``episode_anchors.fp_meta``.
The fingerprint is a carried label: the merge writes ONLY the fp_meta column of ONLY
the addressed (episode_id, repo, anchor) row; the episode row is untouched.

The absent-only guard is STRUCTURAL: the merge spreads the CURRENT map last, so a
present key cannot be displaced — an overwrite is unconstructable, not merely refused.
``test_fill_anchor_fp_never_overwrites`` is the named test that reds when the spread
order is swapped. Connections come from the prod ``connect()`` factory (the BEGIN
IMMEDIATE discipline the store is written against)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.meta import BadMeta

REPO = "alpha"
ANCHOR = "app.py::greet"
FP_PIN = '{"combdrift/fp":"combdrift-fp/1:pinned"}'


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"))


def _seed(
    s: SqliteEpisodeStore,
    text: str,
    *,
    repo: str = REPO,
    anchor: str = ANCHOR,
    complete: bool = True,
) -> int:
    anchors = [(repo, anchor)] if anchor else []
    eid, _ = s.stage(text=text, weight=1.0, proposed_by="w", ts=10, anchors=anchors)
    if complete:
        assert s.complete(
            eid,
            np.ones(4, dtype=np.float32),
            expected_version=0,
            trust="provisional",
            last_active_ts=10,
        )
    return eid


def _pin(s: SqliteEpisodeStore, eid: int, carrier: str = FP_PIN) -> None:
    s.conn.execute(
        "UPDATE episode_anchors SET fp_meta=? WHERE episode_id=?", (carrier, eid)
    )


def _raw_fp(
    s: SqliteEpisodeStore, eid: int, repo: str = REPO, anchor: str = ANCHOR
) -> str:
    return s.conn.execute(
        "SELECT fp_meta FROM episode_anchors "
        "WHERE episode_id=? AND repo=? AND anchor=?",
        (eid, repo, anchor),
    ).fetchone()["fp_meta"]


# ── anchors_lacking_fp: the sweep's candidate read ────────────────────────────


def test_lacking_read_returns_empty_carrier_bindings_of_the_repo():
    s = _store()
    bare = _seed(s, "bare binding")
    pinned = _seed(s, "pinned binding")
    _pin(s, pinned)  # ANY carrier ⇒ out of the sweep
    _seed(s, "another repo", repo="beta")  # foreign repo ⇒ excluded
    _seed(s, "scope only", anchor="")  # no code binding ⇒ excluded
    _seed(s, "pending", complete=False)  # never materialized ⇒ excluded
    assert s.anchors_lacking_fp(REPO) == [(bare, ANCHOR)]


def test_lacking_read_includes_all_trust_states():
    # the anchored_episodes twin: trust is NOT a filter — the consumer owns any
    # further filtering.
    s = _store()
    eid = _seed(s, "later deprecated")
    s.conn.execute("UPDATE episodes SET trust='deprecated' WHERE id=?", (eid,))
    assert s.anchors_lacking_fp(REPO) == [(eid, ANCHOR)]


def test_lacking_read_lists_each_binding_separately():
    s = _store()
    eid, _ = s.stage(
        text="two bindings",
        weight=1.0,
        proposed_by="w",
        ts=10,
        anchors=[(REPO, "a.py::f"), (REPO, "b.py::g")],
    )
    assert s.complete(
        eid,
        np.ones(4, dtype=np.float32),
        expected_version=0,
        trust="provisional",
        last_active_ts=10,
    )
    assert s.anchors_lacking_fp(REPO) == [(eid, "a.py::f"), (eid, "b.py::g")]


# ── fill_anchor_fp: the ONE-tx absent-only merge ──────────────────────────────


def test_fill_anchor_fp_fills_empty_carrier_canonically():
    s = _store()
    eid = _seed(s, "no carrier yet")
    added = s.fill_anchor_fp(
        eid,
        REPO,
        ANCHOR,
        {
            "combdrift/fp": "combdrift-fp/1:abc",
            "hive-sync/minted": "hive-sync-minted/1:server@x y",
        },
    )
    assert added == 2
    assert _raw_fp(s, eid) == json.dumps(
        {
            "combdrift/fp": "combdrift-fp/1:abc",
            "hive-sync/minted": "hive-sync-minted/1:server@x y",
        },
        sort_keys=True,
        separators=(",", ":"),
    )  # canonical: sorted + tight
    # a filled binding leaves the sweep's candidate set (the loop converges)
    assert s.anchors_lacking_fp(REPO) == []


def test_fill_anchor_fp_never_overwrites():
    # THE structural guard: a mixed additions map lands ONLY its absent keys — the
    # present key's value byte-survives even though a write happens. Swapping the
    # merge's spread order (additions last) is the mutation that reds this test.
    s = _store()
    eid = _seed(s, "already pinned")
    _pin(s, eid)
    added = s.fill_anchor_fp(
        eid,
        REPO,
        ANCHOR,
        {
            "combdrift/fp": "combdrift-fp/1:minted-later",
            "matrix/subgraph_fp": "matrix-subgraph-fp/1:def",
        },
    )
    assert added == 1  # only the absent key counted
    got = json.loads(_raw_fp(s, eid))
    assert got["combdrift/fp"] == "combdrift-fp/1:pinned"  # ← never displaced
    assert got["matrix/subgraph_fp"] == "matrix-subgraph-fp/1:def"
    # all-present additions: 0 = NO-OP — not even a re-serialization touches the row
    before = _raw_fp(s, eid)
    assert (
        s.fill_anchor_fp(eid, REPO, ANCHOR, {"combdrift/fp": "combdrift-fp/1:again"})
        == 0
    )
    assert _raw_fp(s, eid) == before


def test_fill_anchor_fp_empty_additions_is_noop():
    s = _store()
    eid = _seed(s, "pinned")
    _pin(s, eid)
    assert s.fill_anchor_fp(eid, REPO, ANCHOR, {}) == 0
    assert _raw_fp(s, eid) == FP_PIN


def test_fill_anchor_fp_unknown_row_is_noop_zero():
    s = _store()
    eid = _seed(s, "known binding")
    assert s.fill_anchor_fp(999, REPO, ANCHOR, {"a/b": "c"}) == 0  # unknown episode
    assert s.fill_anchor_fp(eid, "beta", ANCHOR, {"a/b": "c"}) == 0  # unknown repo
    assert s.fill_anchor_fp(eid, REPO, "x.py::y", {"a/b": "c"}) == 0  # unknown anchor
    assert _raw_fp(s, eid) == ""


def test_fill_anchor_fp_touches_only_the_addressed_row():
    s = _store()
    eid, _ = s.stage(
        text="two bindings",
        weight=1.0,
        proposed_by="w",
        ts=10,
        anchors=[(REPO, "a.py::f"), (REPO, "b.py::g")],
    )
    assert s.complete(
        eid,
        np.ones(4, dtype=np.float32),
        expected_version=0,
        trust="provisional",
        last_active_ts=10,
    )
    episode_before = s.conn.execute(
        "SELECT text, content_hash, value, version, meta FROM episodes WHERE id=?",
        (eid,),
    ).fetchone()
    assert (
        s.fill_anchor_fp(eid, REPO, "a.py::f", {"combdrift/fp": "combdrift-fp/1:abc"})
        == 1
    )
    assert _raw_fp(s, eid, REPO, "b.py::g") == ""  # the sibling binding untouched
    episode_after = s.conn.execute(
        "SELECT text, content_hash, value, version, meta FROM episodes WHERE id=?",
        (eid,),
    ).fetchone()
    assert tuple(episode_after) == tuple(episode_before)  # the episode row untouched


def test_fill_anchor_fp_refuses_bad_grammar_atomically():
    # normalize_meta stays the ONE grammar gate: a non-conforming addition refuses
    # loudly and the tx rolls back — the stored carrier is byte-untouched.
    s = _store()
    eid = _seed(s, "guarded")
    _pin(s, eid)
    with pytest.raises(BadMeta):
        s.fill_anchor_fp(eid, REPO, ANCHOR, {"NotNamespaced": "x"})
    assert _raw_fp(s, eid) == FP_PIN
