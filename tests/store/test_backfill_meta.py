"""The backfill-mint store seam (duck-typed app reads/writes, no port):
``anchored_episodes_with_meta`` — the sweep's candidate read (the
``anchored_episodes`` shape with the serialized meta carrier riding along) — and
``fill_absent_meta`` — the ONE-tx absent-only merge. Meta is a carried label:
the merge writes ONLY the meta column; text / content_hash / value are untouched.

The absent-only guard is STRUCTURAL: the merge spreads the CURRENT map last, so
a present key cannot be displaced — an overwrite is unconstructable, not merely
refused. ``test_fill_absent_meta_never_overwrites`` is the named test that reds
when the spread order is swapped. Connections come from the prod ``connect()``
factory (the BEGIN IMMEDIATE discipline the store is written against).
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.meta import BadMeta

ANCHOR = "app.py::greet"
FP_PIN = '{"combdrift/fp":"combdrift-fp/1:pinned"}'


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"))


def _seed(s: SqliteEpisodeStore, text: str, *, anchor: str = ANCHOR,
          meta: str = "", approve: bool = True) -> int:
    eid, _ = s.stage(text=text, weight=1.0, tags="", proposed_by="w", ts=10,
                     anchor=anchor, meta=meta)
    if approve:
        assert s.approve(eid, "h", np.ones(4, dtype=np.float32),
                         expected_version=0, approved_ts=10)
    return eid


def _raw_meta(s: SqliteEpisodeStore, eid: int) -> str:
    return s.conn.execute(
        "SELECT meta FROM episodes WHERE id=?", (eid,)).fetchone()["meta"]


# ── anchored_episodes_with_meta: the sweep's candidate read ───────────────────


def test_anchored_read_returns_approved_anchored_rows_with_meta():
    s = _store()
    tagged = _seed(s, "tagged", meta=FP_PIN)
    bare = _seed(s, "bare")
    _seed(s, "unanchored", anchor="")                    # anchor '' ⇒ never a candidate
    _seed(s, "pending", approve=False)                   # pending ⇒ never a candidate
    assert s.anchored_episodes_with_meta() == [
        (tagged, ANCHOR, FP_PIN), (bare, ANCHOR, "")]    # id order; meta rides verbatim


def test_anchored_read_includes_all_trust_states():
    # the anchored_episodes twin: trust is NOT a filter — a deprecated row keeps its
    # labels and stays visible; the consumer owns any further filtering.
    s = _store()
    eid = _seed(s, "later deprecated")
    s.conn.execute("UPDATE episodes SET trust='deprecated' WHERE id=?", (eid,))
    assert s.anchored_episodes_with_meta() == [(eid, ANCHOR, "")]


# ── fill_absent_meta: the ONE-tx absent-only merge ────────────────────────────


def test_fill_absent_meta_fills_empty_carrier_canonically():
    s = _store()
    eid = _seed(s, "no carrier yet")
    added = s.fill_absent_meta(eid, {"combdrift/fp": "combdrift-fp/1:abc",
                                     "hive-sync/minted": "hive-sync-minted/1:server@x y"})
    assert added == 2
    assert _raw_meta(s, eid) == json.dumps(
        {"combdrift/fp": "combdrift-fp/1:abc",
         "hive-sync/minted": "hive-sync-minted/1:server@x y"},
        sort_keys=True, separators=(",", ":"))           # canonical: sorted + tight


def test_fill_absent_meta_never_overwrites():
    # THE structural guard: a mixed additions map lands ONLY its absent keys — the
    # present key's value byte-survives even though a write happens. Swapping the
    # merge's spread order (additions last) is the mutation that reds this test.
    s = _store()
    eid = _seed(s, "already pinned", meta=FP_PIN)
    added = s.fill_absent_meta(eid, {"combdrift/fp": "combdrift-fp/1:minted-later",
                                     "matrix/subgraph_fp": "matrix-subgraph-fp/1:def"})
    assert added == 1                                    # only the absent key counted
    got = json.loads(_raw_meta(s, eid))
    assert got["combdrift/fp"] == "combdrift-fp/1:pinned"        # ← never displaced
    assert got["matrix/subgraph_fp"] == "matrix-subgraph-fp/1:def"
    # all-present additions: 0 = NO-OP — not even a re-serialization touches the row
    before = _raw_meta(s, eid)
    assert s.fill_absent_meta(eid, {"combdrift/fp": "combdrift-fp/1:again"}) == 0
    assert _raw_meta(s, eid) == before


def test_fill_absent_meta_empty_additions_is_noop():
    s = _store()
    eid = _seed(s, "pinned", meta=FP_PIN)
    assert s.fill_absent_meta(eid, {}) == 0
    assert _raw_meta(s, eid) == FP_PIN


def test_fill_absent_meta_unknown_id_is_noop_zero():
    assert _store().fill_absent_meta(999, {"a/b": "c"}) == 0


def test_fill_absent_meta_touches_only_the_meta_column():
    s = _store()
    eid = _seed(s, "identity stays put")
    before = s.conn.execute(
        "SELECT text, content_hash, value, version FROM episodes WHERE id=?",
        (eid,)).fetchone()
    assert s.fill_absent_meta(eid, {"combdrift/fp": "combdrift-fp/1:abc"}) == 1
    after = s.conn.execute(
        "SELECT text, content_hash, value, version FROM episodes WHERE id=?",
        (eid,)).fetchone()
    assert (after["text"], after["content_hash"], after["value"], after["version"]) \
        == (before["text"], before["content_hash"], before["value"], before["version"])


def test_fill_absent_meta_refuses_bad_grammar_atomically():
    # normalize_meta stays the ONE grammar gate: a non-conforming addition refuses
    # loudly and the tx rolls back — the stored carrier is byte-untouched.
    s = _store()
    eid = _seed(s, "guarded", meta=FP_PIN)
    with pytest.raises(BadMeta):
        s.fill_absent_meta(eid, {"NotNamespaced": "x"})
    assert _raw_meta(s, eid) == FP_PIN
