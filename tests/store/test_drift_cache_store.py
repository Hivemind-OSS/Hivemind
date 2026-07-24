"""The materialized drift cache (D5 — rebuildable, Law 5): ``drift_put`` (last-write-wins
on the (repo, tip_sha, anchor) key, §3.5-DDL-ordered row tuples), ``drift_get`` (exact
(repo, tip) read; an un-materialized anchor is ABSENT — the caller reads absence as
``unverifiable``, never false-fresh/false-stale), ``drift_prune`` (bounded growth), and
the ``ref_requests`` demand seam (``touch_ref_request`` / ``requested_refs``). Verdict
strings ride verbatim — the cache never interprets the wire vocabulary."""

from __future__ import annotations

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore

TIP0, TIP1 = "a" * 40, "b" * 40


def _store() -> SqliteEpisodeStore:
    return SqliteEpisodeStore(connect(":memory:"))


def _row(
    repo="alpha", tip=TIP0, anchor="a.py::f", verdict="fresh", detail="{}", ts=100
) -> tuple:
    return (repo, tip, anchor, verdict, detail, ts)


# ── drift_put / drift_get ──────────────────────────────────────────────────────
def test_put_then_get_roundtrips_verdict_and_detail():
    s = _store()
    s.drift_put(
        [
            _row(verdict="anchor_missing", detail='{"reason":"gone"}'),
            _row(anchor="b.py::g", verdict="fresh"),
        ]
    )
    got = s.drift_get("alpha", TIP0, ["a.py::f", "b.py::g"])
    assert got == {
        "a.py::f": ("anchor_missing", '{"reason":"gone"}'),
        "b.py::g": ("fresh", "{}"),
    }


def test_get_omits_unmaterialized_anchors():
    # absence IS the fail-safe signal: the caller reads a missing key as
    # unverifiable — the cache never fabricates a verdict for an unmeasured anchor.
    s = _store()
    s.drift_put([_row()])
    got = s.drift_get("alpha", TIP0, ["a.py::f", "never.py::seen"])
    assert set(got) == {"a.py::f"}


def test_get_is_keyed_by_repo_and_tip_exactly():
    s = _store()
    s.drift_put([_row(tip=TIP0, verdict="fresh")])
    assert s.drift_get("alpha", TIP1, ["a.py::f"]) == {}  # a moved tip reads empty
    assert s.drift_get("beta", TIP0, ["a.py::f"]) == {}  # another repo reads empty


def test_get_empty_anchor_list_reads_empty():
    s = _store()
    s.drift_put([_row()])
    assert s.drift_get("alpha", TIP0, []) == {}


def test_put_same_key_is_last_write_wins():
    # a re-materialization refreshes the cache row (rebuildable state, not a ledger).
    s = _store()
    s.drift_put([_row(verdict="fresh", ts=100)])
    s.drift_put([_row(verdict="anchor_changed", detail='{"v":2}', ts=200)])
    assert s.drift_get("alpha", TIP0, ["a.py::f"]) == {
        "a.py::f": ("anchor_changed", '{"v":2}')
    }
    n = s.conn.execute("SELECT COUNT(*) AS c FROM anchor_drift").fetchone()["c"]
    assert n == 1  # replaced, never doubled


def test_put_empty_batch_is_a_noop():
    s = _store()
    s.drift_put([])
    assert s.conn.execute("SELECT COUNT(*) AS c FROM anchor_drift").fetchone()["c"] == 0


def test_verdict_strings_ride_verbatim():
    # the store persists whatever verdict it is given — the verify→wire mapping
    # lives upstream; the cache never interprets (or gatekeeps) the vocabulary.
    s = _store()
    s.drift_put(
        [_row(verdict="branch_scoped"), _row(anchor="x", verdict="unverifiable")]
    )
    got = s.drift_get("alpha", TIP0, ["a.py::f", "x"])
    assert got["a.py::f"][0] == "branch_scoped" and got["x"][0] == "unverifiable"


# ── drift_prune: bounded growth for a rebuildable cache ────────────────────────
def test_prune_keeps_listed_tips_only():
    s = _store()
    s.drift_put([_row(tip=TIP0), _row(tip=TIP1), _row(repo="beta", tip=TIP0)])
    dropped = s.drift_prune("alpha", keep_tips=[TIP1])
    assert dropped == 1
    assert s.drift_get("alpha", TIP0, ["a.py::f"]) == {}
    assert s.drift_get("alpha", TIP1, ["a.py::f"]) != {}
    assert s.drift_get("beta", TIP0, ["a.py::f"]) != {}  # other repos untouched


def test_prune_empty_keep_wipes_the_repo():
    s = _store()
    s.drift_put([_row(tip=TIP0), _row(tip=TIP1)])
    assert s.drift_prune("alpha", keep_tips=[]) == 2
    assert s.drift_get("alpha", TIP0, ["a.py::f"]) == {}


def test_cache_is_rebuildable_wipe_then_repopulate():
    # the Law-5 posture at the store level: a wiped cache accepts the same rows again.
    s = _store()
    s.drift_put([_row()])
    s.conn.execute("DELETE FROM anchor_drift")
    s.drift_put([_row()])
    assert s.drift_get("alpha", TIP0, ["a.py::f"]) == {"a.py::f": ("fresh", "{}")}


# ── ref_requests: the recall-touched materialization demand ────────────────────
def test_touch_then_requested_refs_roundtrip():
    s = _store()
    s.touch_ref_request("alpha", "feature", 100)
    s.touch_ref_request("alpha", "develop", 150)
    s.touch_ref_request("beta", "feature", 200)  # another repo's demand
    assert s.requested_refs("alpha", since_ts=0) == [
        "develop",
        "feature",
    ]  # ref-ordered
    assert s.requested_refs("beta", since_ts=0) == ["feature"]


def test_requested_refs_window_is_strict():
    s = _store()
    s.touch_ref_request("alpha", "feature", 100)
    assert s.requested_refs("alpha", since_ts=100) == []  # STRICT: ts > since
    assert s.requested_refs("alpha", since_ts=99) == ["feature"]


def test_touch_keeps_the_newest_request_stamp():
    s = _store()
    s.touch_ref_request("alpha", "feature", 100)
    s.touch_ref_request("alpha", "feature", 300)  # re-touch advances
    s.touch_ref_request("alpha", "feature", 200)  # out-of-order never rewinds
    row = s.conn.execute(
        "SELECT last_requested_ts FROM ref_requests "
        "WHERE repo='alpha' AND ref='feature'"
    ).fetchone()
    assert row["last_requested_ts"] == 300
    n = s.conn.execute("SELECT COUNT(*) AS c FROM ref_requests").fetchone()["c"]
    assert n == 1  # upsert, never a second row
